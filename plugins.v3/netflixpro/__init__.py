"""
Netflix 榜单插件（v3）。

数据源为 Netflix 官方 Tudum Top 10 的静态 TSV 数据文件（免费、无鉴权、
每周二更新一次全量历史）：

- 全球榜：https://www.netflix.com/tudum/top10/data/all-weeks-global.tsv
  字段含周观看小时数、观看次数、片长、在榜周数，四类目
  （Films/TV × English/Non-English）各 10 条；
- 国家/地区榜：https://www.netflix.com/tudum/top10/data/all-weeks-countries.tsv
  约 90 个国家/地区，每区 Films/TV 各 10 条，只有名次与在榜周数，
  无观看量。Netflix 未进入中国大陆，插件提供中国香港、中国台湾等地区。

榜单条目只有英文片名，插件在抓取后统一走宿主识别链补全中文片名、
海报、年份与发行日期，识别结果写入 identity_cache 跨周复用。
"""

import csv
import datetime
import io
import time
from threading import Event
from typing import Tuple, List, Dict, Any

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, Field

from app import schemas
from app.sdk.config import settings
from app.sdk.events import eventmanager
from app.sdk.events import Event as SdkEvent
from app.sdk.logging import logger
from app.sdk.media import MetaInfo, MediaInfo
from app.sdk.media import normalize_media_source, resolve_media_identity
from app.sdk.network import RequestUtils
from app.plugins import _PluginBase
from app.schemas import MediaSource, MediaType
from app.schemas.types import ChainEventType

from .app_promo import promo_card

# 全球榜 TSV（含观看量）与国家/地区榜 TSV（仅名次）
GLOBAL_TSV_URL = ("https://www.netflix.com/tudum/top10/data/"
                  "all-weeks-global.tsv")
COUNTRIES_TSV_URL = ("https://www.netflix.com/tudum/top10/data/"
                     "all-weeks-countries.tsv")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


class NetflixItem(BaseModel):
    """Netflix 榜单单条快照。"""

    rank: int = Field(0, description="名次")
    title: str = Field("", description="榜单原始标题（多为英文原名）")
    title_cn: str = Field("", description="中文片名（识别结果）")
    season_title: str = Field("", description="季标题，N/A 表示非剧集季度")
    hours_viewed: int = Field(0, description="周观看小时数，仅全球榜提供")
    views: int = Field(0, description="周观看次数，仅全球榜近期提供")
    runtime: float = Field(0.0, description="片长（小时），仅全球电影榜提供")
    weeks_in_top10: int = Field(0, description="累计在榜周数")
    media_source: str = Field("", description="主媒体来源，未识别时为空")
    media_id: str = Field("", description="主媒体 ID，未识别时为空")
    poster: str = Field("", description="海报地址，未识别时为空")
    overview: str = Field("", description="剧情简介，未识别时为空")
    year: str = Field("", description="年份，未识别时为空")
    release_date: str = Field("", description="发行日期 YYYY-MM-DD，未识别时为空")


class NetflixBoard(BaseModel):
    """单个（地区 × 类目）榜单的最新快照。"""

    board: str = Field(..., description="榜单名称")
    type: str = Field("", description="地区类型：global 或 region")
    region: str = Field("", description="地区代码，全球榜为空")
    region_name: str = Field("", description="地区中文名")
    kind: str = Field("", description="内容类型：film 或 tv")
    category: str = Field("", description="数据源原始类目名")
    category_name: str = Field("", description="类目中文名")
    week: str = Field("", description="榜单周期（周二所在周）")
    url: str = Field("", description="榜单来源地址")
    count: int = Field(0, description="榜单条目数")
    updated_at: str = Field("", description="快照时间")
    items: List[NetflixItem] = Field(default_factory=list, description="榜单条目")


class NetflixSnapshot(BaseModel):
    """Netflix 榜单快照汇总数据。"""

    updated_at: str = Field("", description="最近一次刷新的快照时间")
    week: str = Field("", description="最近一次抓取的榜单周期")
    total: int = Field(0, description="返回的条目总数")
    boards: List[NetflixBoard] = Field(default_factory=list, description="榜单列表")


class NetflixRefreshResult(BaseModel):
    """手动刷新接口的业务数据。"""

    refreshed: bool = Field(..., description="是否成功完成一次抓取")
    board_count: int = Field(0, description="抓取到的榜单数量")
    item_count: int = Field(0, description="抓取到的条目数量")
    week: str = Field("", description="本次抓取的榜单周期")
    message: str = Field("", description="结果说明")


def _safe_int(value, default: int = 0) -> int:
    """把 TSV 字段转成整数，空串/非法值回退默认值。"""
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _trim_zeros(text: str) -> str:
    """去掉小数末尾无意义的 0（1.50 → 1.5、2.00 → 2）。"""
    if "." not in text:
        return text
    return text.rstrip("0").rstrip(".")


def _fmt_view_count(value: int) -> str:
    """把观看量格式化为中文可读形式（41500000 → 4150 万）。"""
    if value <= 0:
        return ""
    if value >= 100_000_000:
        return f"{_trim_zeros(f'{value / 100_000_000:.2f}')} 亿"
    return f"{value / 10_000:.0f} 万"


def _fmt_hours(value: int) -> str:
    """周观看小时数（41500000 → 4150 万小时）。"""
    if value <= 0:
        return ""
    if value >= 100_000_000:
        return f"{_trim_zeros(f'{value / 100_000_000:.2f}')} 亿小时"
    return f"{value / 10_000:.0f} 万小时"


def _fmt_runtime(hours: float) -> str:
    """片长（小时）→「1 小时 40 分」，不足一小时只显分钟。"""
    if hours <= 0:
        return ""
    total_minutes = int(round(hours * 60))
    h, m = divmod(total_minutes, 60)
    if h and m:
        return f"{h} 小时 {m} 分"
    if h:
        return f"{h} 小时"
    return f"{m} 分钟"


def _safe_board_payload(board: dict, rows: List[Any]) -> Dict[str, Any]:
    """
    把存储中的榜单字典收窄为响应模型的合法字段。

    快照是插件自己写入的历史数据，升级后可能出现多余或缺失字段；这里显式挑字段并
    补默认值，避免旧数据触发响应模型校验失败导致整个接口 500。
    """

    def text(key: str) -> str:
        value = board.get(key)
        return str(value) if value is not None else ""

    items = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        items.append({
            "rank": _safe_int(row.get("rank")),
            "title": str(row.get("title") or ""),
            "title_cn": str(row.get("title_cn") or ""),
            "season_title": str(row.get("season_title") or ""),
            "hours_viewed": _safe_int(row.get("hours_viewed")),
            "views": _safe_int(row.get("views")),
            "runtime": _safe_float(row.get("runtime")),
            "weeks_in_top10": _safe_int(row.get("weeks_in_top10")),
            "media_source": str(row.get("media_source") or ""),
            "media_id": str(row.get("media_id") or ""),
            "poster": str(row.get("poster") or ""),
            "overview": str(row.get("overview") or ""),
            "year": str(row.get("year") or ""),
            "release_date": str(row.get("release_date") or ""),
        })
    return {
        "board": text("board"),
        "type": text("type"),
        "region": text("region"),
        "region_name": text("region_name"),
        "kind": text("kind"),
        "category": text("category"),
        "category_name": text("category_name"),
        "week": text("week"),
        "url": text("url"),
        "count": _safe_int(board.get("count")),
        "updated_at": text("updated_at"),
        "items": items,
    }


class NetflixPro(_PluginBase):
    """
    Netflix 官方 Top 10 榜单抓取与展示。

    数据源是 Netflix Tudum 的静态 TSV 文件（官方发布、每周二更新），
    本插件负责抓取、解析最新一周、走宿主识别链补全中文片名与海报，
    并向 App、宿主推荐页与插件详情页提供榜单数据。

    Netflix 未进入中国大陆，地区榜提供中国香港、中国台湾等选项；
    全球榜另附周观看小时数、观看次数与在榜周数。
    """

    # 插件名称
    plugin_name = "Netflix榜单"
    # 插件描述
    plugin_desc = "Netflix 官方 Top 10 榜单显示。"
    # 插件图标
    plugin_icon = ("https://raw.githubusercontent.com/KingNuoNuo/"
                   "MoviePilot-Plugins/main/icons/netflix.png")
    # 插件版本
    plugin_version = "1.0.2"
    # 插件作者
    plugin_author = "KingNuoNuo"
    # 作者主页
    author_url = "https://github.com/KingNuoNuo"
    # 插件配置项ID前缀
    plugin_config_prefix = "netflixpro_"
    # 加载顺序
    plugin_order = 7
    # 可使用的用户级别：0 = 不做站点用户认证也能加载
    auth_level = 0

    # 退出事件
    _event = Event()
    # 私有属性
    downloadchain = None
    subscribechain = None
    _scheduler = None
    _enabled = False
    _onlyonce = False
    _cron = ""
    _clear = False
    # 抓取的地区：global 为全球榜，其余为对应国家/地区榜
    _regions = ['global', 'hk', 'tw']

    # 对外接口与推荐数据源开关
    _api_enabled = True
    _recommend_enabled = False

    # 地区代码 -> 中文名（与宿主前端地域表述保持一致）
    _REGION_NAMES = {
        'global': '全球',
        'hk': '中国香港',
        'tw': '中国台湾',
        'jp': '日本',
        'kr': '韩国',
        'sg': '新加坡',
    }

    # 内容类型 -> 中文名
    _KIND_NAMES = {
        'film': '电影',
        'tv': '剧集',
    }

    # 全球榜 TSV 类目 -> (内容类型, 类目中文名)
    _GLOBAL_CATEGORIES = {
        'Films (English)': ('film', '电影（英语）'),
        'Films (Non-English)': ('film', '电影（非英语）'),
        'TV (English)': ('tv', '剧集（英语）'),
        'TV (Non-English)': ('tv', '剧集（非英语）'),
    }

    # 国家/地区榜 TSV 类目 -> (内容类型, 类目中文名)
    _COUNTRY_CATEGORIES = {
        'Films': ('film', '电影'),
        'TV': ('tv', '剧集'),
    }

    # 内容类型 -> 推荐页分类，取值与宿主前端 recommend.category* 文案一致
    _RECOMMEND_TYPES = {
        'film': '电影',
        'tv': '电视剧',
    }

    # 详情页每个榜单展示的条目上限（Netflix 单榜本身只有 10 条）
    _PAGE_BOARD_LIMIT = 10

    def init_plugin(self, config: dict = None):
        """加载地区配置并按周期或一次性开关注册抓取任务。"""
        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._clear = config.get("clear")
            self._onlyonce = config.get("onlyonce")
            self._api_enabled = config.get("api_enabled", True)
            self._recommend_enabled = config.get("recommend_enabled", False)
            regions = config.get("regions")
            if isinstance(regions, list) and regions:
                self._regions = [str(r) for r in regions
                                 if str(r) in self._REGION_NAMES]

        # 停止现有任务
        self.stop_service()

        # 清理插件数据
        if self._clear:
            self.del_data(key="snapshot")
            self.del_data(key="identity_cache")
            self._clear = False
            self.__update_config()
            logger.info("Netflix榜单数据清理完成")

        if self._enabled or self._recommend_enabled or self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            # 周期执行（Netflix 每周二发榜，默认周三 09:00 抓取）
            if self._cron:
                logger.info(f"Netflix榜单服务启动，周期：{self._cron}")
                try:
                    self._scheduler.add_job(func=self.__refresh_netflix,
                                            trigger=CronTrigger.from_crontab(self._cron),
                                            name="Netflix榜单")
                except Exception as e:
                    logger.error(f"Netflix榜单服务启动失败，错误信息：{str(e)}")
                    self.systemmessage.put(f"Netflix榜单服务启动失败，错误信息：{str(e)}")
            else:
                self._scheduler.add_job(func=self.__refresh_netflix,
                                        trigger=CronTrigger.from_crontab("0 9 * * 3"),
                                        name="Netflix榜单")
                logger.info("Netflix榜单服务启动，周期：每周三 09:00")

            # 一次性执行
            if self._onlyonce:
                logger.info("Netflix榜单服务启动，立即运行一次")
                self._scheduler.add_job(func=self.__refresh_netflix, trigger='date',
                                        run_date=datetime.datetime.now(
                                            tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                                        )
                # 关闭一次性开关
                self._onlyonce = False
                # 保存配置
                self.__update_config()

            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def get_state(self) -> bool:
        """返回插件启用状态：抓取或推荐页数据源任一开启即视为启用。"""
        return bool(self._enabled or self._recommend_enabled)

    def stop_service(self):
        """停止定时任务。"""
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
                logger.info("Netflix榜单服务停止")
        except Exception as e:
            logger.error(f"Netflix榜单服务停止失败：{e}")

    def __update_config(self):
        """把运行时状态写回插件配置。"""
        self.update_config({
            "enabled": self._enabled,
            "cron": self._cron,
            "clear": self._clear,
            "onlyonce": self._onlyonce,
            "regions": self._regions,
            "api_enabled": self._api_enabled,
            "recommend_enabled": self._recommend_enabled,
        })

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """本插件不注册聊天命令。"""
        pass

    # ==================== 数据抓取 ====================

    def __fetch_tsv(self, url: str) -> str:
        """下载 TSV 数据文件，返回文本内容。

        Netflix 的 Tudum 数据文件是 UTF-8，但响应不带 charset，直接取
        ``response.text`` 会让 requests 靠嗅探猜编码：这种九成内容是 ASCII 的
        大文件经常被猜成 latin-1 系，弯引号、破折号、汉字全变成
        ``â\x80\x99`` 这类乱码（TMDB 识别跟着失败）。这里按字节取回后显式
        按 UTF-8 解码，绕开嗅探。
        """
        response = RequestUtils(
            headers={"User-Agent": _UA},
            proxies=getattr(settings, "PROXY", None),
        ).get_res(url)
        if not response:
            raise RuntimeError(f"Netflix 数据文件请求失败：{url}")
        status = getattr(response, "status_code", None)
        if status and int(status) != 200:
            raise RuntimeError(f"Netflix 数据文件返回 {status}：{url}")
        content = getattr(response, "content", None)
        if content is None:
            return response.text or ""
        if isinstance(content, str):
            return content
        return content.decode("utf-8", errors="replace")

    @staticmethod
    def __parse_tsv(text: str) -> List[dict]:
        """解析 TSV 文本为字典列表，容错空行与缺列。"""
        if not text:
            return []
        reader = csv.DictReader(io.StringIO(text), delimiter="\t")
        rows = []
        for raw in reader:
            row = {}
            for key, value in raw.items():
                if key is None:
                    continue
                row[str(key).strip()] = ("" if value is None
                                         else str(value).strip())
            if any(row.values()):
                rows.append(row)
        return rows

    def __parse_global_board(self, rows: List[dict]) -> Tuple[str, List[dict]]:
        """解析全球榜：返回 (最新周, 榜单字典列表)。"""
        if not rows:
            return "", []
        latest = max(str(row.get("week") or "") for row in rows)
        boards: List[dict] = []
        for row in rows:
            if str(row.get("week") or "") != latest:
                continue
            category = str(row.get("category") or "")
            mapping = self._GLOBAL_CATEGORIES.get(category)
            if not mapping:
                continue
            kind, category_name = mapping
            item = {
                "rank": _safe_int(row.get("weekly_rank")),
                "title": str(row.get("show_title") or ""),
                "title_cn": "",
                "season_title": str(row.get("season_title") or ""),
                "hours_viewed": _safe_int(row.get("weekly_hours_viewed")),
                "views": _safe_int(row.get("weekly_views")),
                "runtime": _safe_float(row.get("runtime")),
                "weeks_in_top10": _safe_int(row.get("cumulative_weeks_in_top_10")),
            }
            board = next((b for b in boards if b["category"] == category), None)
            if board is None:
                board = {
                    "board": f"Netflix 全球 · {category_name}",
                    "type": "global",
                    "region": "",
                    "region_name": "全球",
                    "kind": kind,
                    "category": category,
                    "category_name": category_name,
                    "week": latest,
                    "url": "https://www.netflix.com/tudum/top10",
                    "count": 0,
                    "updated_at": "",
                    "items": [],
                }
                boards.append(board)
            board["items"].append(item)
            board["count"] = len(board["items"])
        for board in boards:
            board["items"].sort(key=lambda i: i["rank"])
        return latest, boards

    def __parse_country_board(self, rows: List[dict],
                              region: str) -> Tuple[str, List[dict]]:
        """解析单个国家/地区榜：返回 (最新周, 榜单字典列表)。"""
        region_name = self._REGION_NAMES.get(region, region)
        if not rows:
            return "", []
        # 国家榜是每周全量刷新，取该地区最新的周次即可
        latest = ""
        boards: List[dict] = []
        for row in rows:
            if str(row.get("country_iso2") or "").upper() != region.upper():
                continue
            week = str(row.get("week") or "")
            if not week:
                continue
            if not latest or week > latest:
                latest = week
                boards = []
            if week != latest:
                continue
            category = str(row.get("category") or "")
            mapping = self._COUNTRY_CATEGORIES.get(category)
            if not mapping:
                continue
            kind, category_name = mapping
            item = {
                "rank": _safe_int(row.get("weekly_rank")),
                "title": str(row.get("show_title") or ""),
                "title_cn": "",
                "season_title": str(row.get("season_title") or ""),
                "hours_viewed": 0,
                "views": 0,
                "runtime": 0.0,
                "weeks_in_top10": _safe_int(row.get("cumulative_weeks_in_top_10")),
            }
            board = next((b for b in boards if b["kind"] == kind), None)
            if board is None:
                board = {
                    "board": f"Netflix {region_name} · {category_name}",
                    "type": "region",
                    "region": region,
                    "region_name": region_name,
                    "kind": kind,
                    "category": category,
                    "category_name": category_name,
                    "week": latest,
                    "url": (f"https://www.netflix.com/tudum/top10/"
                            f"{region.lower()}"),
                    "count": 0,
                    "updated_at": "",
                    "items": [],
                }
                boards.append(board)
            board["items"].append(item)
            board["count"] = len(board["items"])
        for board in boards:
            board["items"].sort(key=lambda i: i["rank"])
        return latest, boards

    def __refresh_netflix(self) -> Dict[str, Any]:
        """抓取并解析 Netflix 榜单，补全媒体身份后落快照。"""
        if not self._regions:
            return {"board_count": 0, "item_count": 0, "week": "",
                    "message": "未配置任何地区"}
        try:
            global_text = self.__fetch_tsv(GLOBAL_TSV_URL)
            countries_text = self.__fetch_tsv(COUNTRIES_TSV_URL)
        except Exception as e:
            logger.error(f"Netflix榜单数据下载失败：{e}")
            return {"board_count": 0, "item_count": 0, "week": "",
                    "message": f"数据下载失败：{e}"}

        global_rows = self.__parse_tsv(global_text)
        country_rows = self.__parse_tsv(countries_text)
        if not global_rows and not country_rows:
            logger.error("Netflix榜单数据文件为空或格式不符")
            return {"board_count": 0, "item_count": 0, "week": "",
                    "message": "数据文件为空或格式不符"}

        boards: List[dict] = []
        week = ""
        if 'global' in self._regions:
            week, global_boards = self.__parse_global_board(global_rows)
            boards += global_boards
        for region in self._regions:
            if region == 'global':
                continue
            region_week, region_boards = self.__parse_country_board(
                country_rows, region)
            boards += region_boards
            if region_week > week:
                week = region_week

        if not boards:
            logger.warning("Netflix榜单未解析到任何条目，请检查数据文件格式")
            return {"board_count": 0, "item_count": 0, "week": week,
                    "message": "未解析到榜单条目"}

        # 识别媒体身份（中文片名、海报、年份、发行日期），失败不阻塞落快照
        try:
            self.__resolve_identities(boards)
        except Exception as e:
            logger.error(f"Netflix榜单媒体识别异常（快照仍会保存）：{e}")

        now = datetime.datetime.now(tz=pytz.timezone(settings.TZ))
        updated_at = now.strftime("%Y-%m-%d %H:%M:%S")
        total = sum(len(board["items"]) for board in boards)
        for board in boards:
            board["updated_at"] = updated_at
        self.save_data("snapshot", {
            "updated_at": updated_at,
            "week": week,
            "total": total,
            "boards": boards,
        })
        logger.info(f"Netflix榜单刷新完成：{len(boards)} 个榜单 {total} 条，周期 {week}")
        return {"board_count": len(boards), "item_count": total, "week": week,
                "message": ""}

    # ==================== 媒体识别 ====================

    def __resolve_identities(self, boards: List[dict]) -> None:
        """
        为榜单条目补全媒体身份与海报。

        同一标题（按内容类型区分）只识别一次，结果持久化到 identity_cache：
        缓存命中直接复用，不再请求识别链。Netflix 单周全量仅百余条，
        顺序识别即可，无需复杂的优先级队列。
        """
        cache = self.get_data("identity_cache")
        if not isinstance(cache, dict):
            cache = {}
        dirty = False
        for board in boards:
            mtype = (MediaType.MOVIE if board.get("kind") == "film"
                     else MediaType.TV)
            for item in board.get("items", []):
                title = item.get("title")
                if not title:
                    continue
                cache_key = f"{board.get('kind')}|{title}"
                cached = cache.get(cache_key)
                if isinstance(cached, dict) and cached.get("media_source"):
                    self.__apply_identity(item, cached)
                    continue
                identity = self.__recognize_item(title, mtype)
                cache[cache_key] = identity
                dirty = True
                self.__apply_identity(item, identity)
        if dirty:
            self.save_data("identity_cache", cache)

    @staticmethod
    def __apply_identity(item: dict, identity: dict) -> None:
        """把识别结果合并进快照条目（不清空已有值）。"""
        for key in ("title_cn", "media_source", "media_id", "poster",
                    "overview", "year", "release_date"):
            value = identity.get(key) or ""
            if value and not item.get(key):
                item[key] = value

    def __recognize_item(self, title: str, mtype: MediaType) -> dict:
        """识别单个榜单条目，返回媒体身份字典（识别不到时字段为空）。"""
        meta = MetaInfo(title)
        try:
            mediainfo: MediaInfo = self.chain.recognize_media(
                meta=meta, mtype=mtype, cache=False)
        except Exception as e:
            logger.error(f"Netflix 条目 {title} 识别异常，继续处理下一条：{e}")
            mediainfo = None
        if not mediainfo:
            logger.warning(f"未识别到媒体信息，标题：{title}")
            return {"media_source": "", "media_id": "", "poster": "",
                    "overview": "", "year": "", "title_cn": "",
                    "release_date": ""}
        source = getattr(mediainfo, "media_source", None)
        source_value = getattr(source, "value", None) or source or ""
        cn_title = (mediainfo.title or "").strip()
        raw_info = getattr(mediainfo, "tmdb_info", None) or {}
        if not isinstance(raw_info, dict):
            raw_info = {}
        rel_date = str(raw_info.get("release_date")
                       or raw_info.get("first_air_date")
                       or getattr(mediainfo, "release_date", None) or "").strip()[:10]
        return {
            "media_source": str(source_value),
            "media_id": str(mediainfo.media_id or ""),
            "poster": mediainfo.get_poster_image() or "",
            "overview": mediainfo.overview or "",
            "year": str(mediainfo.year or ""),
            "title_cn": cn_title if cn_title and cn_title != title else "",
            "release_date": rel_date,
        }

    # ==================== 对外接口 ====================

    def get_api(self) -> List[Dict[str, Any]]:
        """
        注册对外接口。

        - `/rank`：返回最近一次抓取的榜单快照，供 App、脚本或宿主页面直接消费；
        - `/rank-list`：推荐页/浏览页使用的媒体信息列表；
        - `/refresh`：立即触发一次抓取。

        认证用宿主的 `bear` 模式（依赖 `verify_token`）：它同时接受 App 登录态
        的 JWT（`Authorization: Bearer`）、API 令牌（`X-API-KEY` 头或 `apikey` 查询
        参数）与 `token` 查询参数。默认的 `apikey` 模式只认 API 令牌，账号密码登录
        的 App 会 401，因此这里显式声明 `bear`。

        返回值一律用 `schemas.Response` 信封包裹：宿主普通数据客户端（含详情页
        按钮）会校验 success/message/data 三键结构，裸模型会被判「无效响应」。
        """
        return [
            {
                "path": "/rank",
                "endpoint": self.get_rank_snapshot,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "Netflix 榜单快照",
                "description": "返回最近一次抓取的 Netflix Top 10 榜单快照，"
                               "可按地区（board_type）与内容类型（kind）筛选",
                "response_model": schemas.Response[NetflixSnapshot],
            },
            {
                "path": "/rank-list",
                "endpoint": self.rank_list,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "Netflix 榜单列表",
                "description": "供宿主推荐页与浏览页消费的榜单列表，返回统一的媒体信息结构",
                "response_model": schemas.Response[List[schemas.MediaInfo]],
            },
            {
                "path": "/refresh",
                "endpoint": self.refresh_now,
                "methods": ["GET", "POST"],
                "auth": "bear",
                "summary": "立即刷新 Netflix 榜单",
                "description": "立即触发一次 Netflix 榜单抓取与媒体识别",
                "response_model": schemas.Response[NetflixRefreshResult],
            },
        ]

    @staticmethod
    def __region_key(board_type: str) -> str:
        """把对外的地区参数映射为快照里的 region 值（global 对应空串）。"""
        return "" if (board_type or "global") == "global" else str(board_type)

    def __snapshot_boards(self) -> List[dict]:
        """从存储中读出快照榜单列表（容错旧数据）。"""
        snapshot = self.get_data("snapshot") or {}
        if not isinstance(snapshot, dict):
            return []
        boards = snapshot.get("boards")
        return [b for b in boards if isinstance(b, dict)] if isinstance(boards, list) else []

    def get_rank_snapshot(self, board_type: str = "global",
                          kind: str = None) -> schemas.Response[NetflixSnapshot]:
        """
        返回最近一次抓取的榜单快照。

        :param board_type: 地区代码，global 为全球榜，hk/tw/jp/kr/sg 为地区榜
        :param kind: 内容类型筛选，film 仅电影、tv 仅剧集，不传返回全部
        """
        snapshot = self.get_data("snapshot") or {}
        if not isinstance(snapshot, dict):
            snapshot = {}
        boards = self.__snapshot_boards()
        region_key = self.__region_key(board_type)
        selected = []
        for board in boards:
            if str(board.get("region") or "") != region_key:
                continue
            if kind and str(board.get("kind") or "") != kind:
                continue
            selected.append(_safe_board_payload(board, board.get("items") or []))
        if not selected:
            return schemas.Response(
                success=True,
                data=NetflixSnapshot(
                    updated_at=str(snapshot.get("updated_at") or ""),
                    week=str(snapshot.get("week") or ""),
                    total=0,
                    boards=[],
                ),
            )
        total = sum(board["count"] for board in selected)
        return schemas.Response(
            success=True,
            data=NetflixSnapshot(
                updated_at=str(snapshot.get("updated_at") or ""),
                week=str(snapshot.get("week") or ""),
                total=total,
                boards=selected,
            ),
        )

    def rank_list(self, board_type: str = "global", kind: str = None,
                  page: int = 1, limit: int = None
                  ) -> schemas.Response[List[schemas.MediaInfo]]:
        """
        推荐页/浏览页使用的榜单列表接口。

        扁平返回指定地区的全部条目（电影在前、剧集在后，各榜按名次排序），
        供宿主列表视图翻页消费；kind 可缩窄到单一内容类型。
        """
        rows: List[dict] = []
        region_key = self.__region_key(board_type)
        for board in self.__snapshot_boards():
            if str(board.get("region") or "") != region_key:
                continue
            board_kind = str(board.get("kind") or "")
            if kind and board_kind != kind:
                continue
            rows.extend([(board_kind, row)
                         for row in (board.get("items") or [])
                         if isinstance(row, dict)])
        rows.sort(key=lambda pair: (0 if pair[0] == "film" else 1,
                                    _safe_int(pair[1].get("rank"))))

        result: List[schemas.MediaInfo] = []
        for board_kind, row in rows:
            media_source, media_id = resolve_media_identity(
                media_source=row.get("media_source"),
                media_id=row.get("media_id"),
            )
            # 英文榜单条目优先用识别出的中文片名展示
            title = str(row.get("title_cn") or "") or str(row.get("title") or "")
            # type 用宿主推荐页分类词（电影/电视剧），宿主列表按该值过滤
            media = schemas.MediaInfo(
                media_source=media_source,
                media_id=media_id,
                title=title,
                year=str(row.get("year") or "") or None,
                type=self._RECOMMEND_TYPES.get(board_kind, "电影"),
                poster_path=str(row.get("poster") or "") or None,
                overview=str(row.get("overview") or "") or None,
            )
            if media.title and media.year:
                media.title_year = f"{media.title} ({media.year})"
            result.append(media)
            if limit and limit > 0 and len(result) >= limit:
                break
        return schemas.Response(success=True, data=result)

    def refresh_now(self) -> schemas.Response[NetflixRefreshResult]:
        """立即触发一次 Netflix 榜单抓取与媒体识别。"""
        result = self.__refresh_netflix() or {}
        board_count = int(result.get("board_count") or 0)
        item_count = int(result.get("item_count") or 0)
        message = str(result.get("message") or "")
        if board_count and item_count:
            message = (f"已抓取 {board_count} 个榜单 {item_count} 条，"
                       f"周期 {result.get('week') or '未知'}")
        return schemas.Response(
            success=bool(board_count),
            message=message,
            data=NetflixRefreshResult(
                refreshed=bool(board_count),
                board_count=board_count,
                item_count=item_count,
                week=str(result.get("week") or ""),
                message=message,
            ),
        )

    @eventmanager.register(ChainEventType.RecommendSource)
    def recommend_source(self, event: SdkEvent):
        """把 Netflix 榜单注册为宿主推荐页的数据源（按地区 × 内容类型）。"""
        if not (self._recommend_enabled and self._api_enabled):
            return
        event_data: schemas.RecommendSourceEventData = event.event_data
        if not event_data:
            return

        sources = []
        for region in self._regions:
            region_name = self._REGION_NAMES.get(region, region)
            for kind in ('film', 'tv'):
                sources.append(schemas.RecommendMediaSource(
                    name=f"Netflix {region_name}{self._KIND_NAMES[kind]}",
                    api_path=(f"plugin/{self.__class__.__name__}/rank-list?"
                              f"board_type={region}&kind={kind}"),
                    type=self._RECOMMEND_TYPES[kind],
                ))
        if not sources:
            return

        # 叠加而不是替换，避免影响其它插件塞入的数据源
        if not event_data.extra_sources:
            event_data.extra_sources = sources
        else:
            event_data.extra_sources.extend(sources)

    # ==================== 插件配置 ====================

    @staticmethod
    def __form_col(component: str, props: dict, md: int = 6) -> dict:
        """构造宿主表单的一列（栅格：窄屏占满整行，宽屏按 md 分栏）。"""
        return {
            'component': 'VCol',
            'props': {'cols': 12, 'md': md},
            'content': [{'component': component, 'props': props}],
        }

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """提供启用开关、抓取周期、地区选择与接口/推荐开关。"""
        region_items = [
            {'title': name, 'value': code}
            for code, name in self._REGION_NAMES.items()
        ]
        rows = [
            {
                'component': 'VRow',
                'content': [
                    self.__form_col('VSwitch', {
                        'model': 'enabled', 'label': '启用插件',
                        'hint': '开启后按周期自动抓取 Netflix 榜单',
                        'persistent-hint': True,
                    }),
                    self.__form_col('VSwitch', {
                        'model': 'onlyonce', 'label': '立即运行一次',
                        'hint': '保存后立即抓取一次榜单',
                        'persistent-hint': True,
                    }),
                ],
            },
            {
                'component': 'VRow',
                'content': [
                    self.__form_col('VCronField', {
                        'model': 'cron',
                        'label': '执行周期',
                        'placeholder': '0 9 * * 3',
                        'hint': 'Cron 表达式，默认每周三 09:00（Netflix 每周二发榜）',
                        'persistent-hint': True,
                    }, md=12),
                ],
            },
            {
                'component': 'VRow',
                'content': [
                    self.__form_col('VSelect', {
                        'model': 'regions',
                        'label': '抓取地区',
                        'items': region_items,
                        'chips': True,
                        'multiple': True,
                        'hint': '全球榜附观看量与在榜周数；地区榜仅名次'
                                '（Netflix 未进入中国大陆）',
                        'persistent-hint': True,
                    }, md=12),
                ],
            },
            {
                'component': 'VRow',
                'content': [
                    self.__form_col('VSwitch', {
                        'model': 'api_enabled', 'label': '开放榜单接口',
                        'hint': '开启后 /rank、/rank-list 接口可被 App 与脚本调用',
                        'persistent-hint': True,
                    }),
                    self.__form_col('VSwitch', {
                        'model': 'recommend_enabled', 'label': '推荐页数据源',
                        'hint': '在宿主推荐页展示 Netflix 榜单（需同时开放接口）',
                        'persistent-hint': True,
                    }),
                ],
            },
            {
                'component': 'VRow',
                'content': [
                    self.__form_col('VSwitch', {
                        'model': 'clear', 'label': '清理插件数据',
                        'hint': '清空榜单快照与识别缓存（下次抓取重建）',
                        'persistent-hint': True,
                    }),
                ],
            },
        ]
        # App 推荐卡固定在表单项之前（与详情页同位置、同一函数）
        return [promo_card(), {
            'component': 'VForm',
            'content': rows,
        }], {
            'enabled': self._enabled,
            'cron': self._cron,
            'regions': list(self._regions),
            'api_enabled': self._api_enabled,
            'recommend_enabled': self._recommend_enabled,
            'onlyonce': self._onlyonce,
            'clear': self._clear,
        }

    # ==================== 详情页 ====================

    def __rank_card(self, row: dict, board: dict) -> dict:
        """构造榜单条目卡片（海报 + 名次角标 + 标题 + 数值）。"""
        title = str(row.get("title_cn") or "") or str(row.get("title") or "")
        en_title = str(row.get("title") or "")
        rank = _safe_int(row.get("rank"))
        # 前三名用主色角标，其余用中性色
        badge_color = '#8D51F9' if 1 <= rank <= 3 else '#4C4C55'

        meta_lines = []
        if row.get("year"):
            meta_lines.append(str(row.get("year")))
        season = str(row.get("season_title") or "")
        if season and season != 'N/A':
            meta_lines.append(season)

        value_lines = []
        hours = _fmt_hours(_safe_int(row.get("hours_viewed")))
        views = _fmt_view_count(_safe_int(row.get("views")))
        if hours and views:
            value_lines.append(f"{hours} · {views}次观看")
        elif hours:
            value_lines.append(hours)
        elif views:
            value_lines.append(f"{views}次观看")
        runtime = _fmt_runtime(_safe_float(row.get("runtime")))
        if runtime:
            value_lines.append(runtime)
        weeks = _safe_int(row.get("weeks_in_top10"))
        if weeks > 1:
            value_lines.append(f"在榜 {weeks} 周")

        content = [
            {'component': 'div',
             'props': {'class': 'd-flex align-center ga-3'},
             'content': [
                 {'component': 'div',
                  'props': {'class': 'flex-shrink-0',
                            'style': {'width': '64px', 'height': '96px',
                                      'border-radius': '6px',
                                      'overflow': 'hidden',
                                      'position': 'relative',
                                      'background': '#1E1E24'}},
                  'content': [
                      {'component': 'VImg',
                       'props': {'src': str(row.get("poster") or ""),
                                 'cover': True,
                                 'aspect-ratio': 0.667}},
                      {'component': 'div',
                       'props': {'class': 'd-flex align-center '
                                          'justify-center',
                                 'style': {'position': 'absolute',
                                           'left': '0', 'top': '0',
                                           'min-width': '20px',
                                           'height': '20px',
                                           'padding': '0 4px',
                                           'border-radius': '6px 0 6px 0',
                                           'background': badge_color,
                                           'color': 'white',
                                           'font-size': '12px',
                                           'font-weight': '600'}},
                       'text': str(rank)},
                  ]},
                 {'component': 'div',
                  'props': {'class': 'min-w-0 flex-grow-1'},
                  'content': [
                      {'component': 'div',
                       'props': {'class': 'text-subtitle-2 '
                                          'font-weight-medium'},
                       'text': title},
                      {'component': 'div',
                       'props': {'class': 'text-caption '
                                          'text-medium-emphasis'},
                       'text': en_title if (en_title and en_title != title) else ''},
                      {'component': 'div',
                       'props': {'class': 'text-caption '
                                          'text-medium-emphasis'},
                       'text': ' · '.join(meta_lines)},
                      {'component': 'div',
                       'props': {'class': 'text-caption '
                                          'text-medium-emphasis'},
                       'text': ' · '.join(value_lines)},
                  ]},
             ]},
        ]
        return {'component': 'VCard',
                'props': {'variant': 'flat', 'class': 'pa-3'},
                'content': content}

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面：实时榜单。

        榜单区直接读快照（与对外接口同源），按「地区 × 类目」分区展示；
        Netflix 数据每周二更新，页面右上角提供「刷新榜单」按钮手动触发。
        """
        snapshot = self.get_data("snapshot") or {}
        if not isinstance(snapshot, dict):
            snapshot = {}
        boards = self.__snapshot_boards()
        week = str(snapshot.get("week") or "")

        refresh_button = {
            'component': 'VBtn',
            'props': {'color': 'primary', 'variant': 'tonal', 'size': 'small',
                      'prepend-icon': 'mdi-refresh'},
            'text': '刷新榜单',
            'events': {'click': {'api': f"plugin/{self.__class__.__name__}/refresh",
                                 'method': 'GET', 'params': {}}},
        }
        header = {
            'component': 'div',
            'props': {'class': 'd-flex justify-space-between align-center '
                               'flex-wrap ga-2 mb-4'},
            'content': [
                {'component': 'div', 'content': [
                    {'component': 'div',
                     'props': {'class': 'text-h6'}, 'text': '实时榜单'},
                    {'component': 'div',
                     'props': {'class': 'text-caption text-medium-emphasis'},
                     'text': f"榜单周期 {week or '未知'}"
                             f"（Netflix 每周二发榜） · 快照更新于 "
                             f"{snapshot.get('updated_at') or '未知时间'}"},
                ]},
                refresh_button,
            ],
        }

        if not boards:
            return [promo_card(), header, {
                'component': 'VAlert',
                'props': {'type': 'info', 'variant': 'tonal', 'class': 'mt-2'},
                'text': '暂无榜单快照，点击「刷新榜单」或等待定时任务运行',
            }]

        nodes = [header]
        for board in boards:
            items = board.get("items") or []
            if not items:
                continue
            section_title = str(board.get("board") or "")
            children = [
                {'component': 'div',
                 'props': {'class': 'd-flex align-center ga-2 mb-2'},
                 'content': [
                     {'component': 'div',
                      'props': {'class': 'text-subtitle-1 '
                                         'font-weight-bold'},
                      'text': section_title},
                     {'component': 'div',
                      'props': {'class': 'text-caption '
                                         'text-medium-emphasis'},
                      'text': f"{len(items)} 条"},
                 ]},
            ]
            children.append({
                'component': 'div',
                'props': {'class': 'grid gap-3 grid-info-card'},
                'content': [self.__rank_card(row, board)
                            for row in items[:self._PAGE_BOARD_LIMIT]],
            })
            if len(items) > self._PAGE_BOARD_LIMIT:
                children.append({
                    'component': 'div',
                    'props': {'class': 'text-caption text-medium-emphasis mt-2'},
                    'text': f"共 {len(items)} 条，展示前 "
                            f"{self._PAGE_BOARD_LIMIT} 条，完整数据见 /rank 接口",
                })
            nodes.append({'component': 'div', 'props': {'class': 'mb-6'},
                          'content': children})
        # App 推荐卡置于页面最顶部，与设置页位置一致
        return [promo_card()] + nodes

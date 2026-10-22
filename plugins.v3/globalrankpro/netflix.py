"""
Netflix 榜单数据源（v3）。

原 ``NetflixPro`` 插件的抓取、解析与识别逻辑整体平移到本模块，改由
:class:`GlobalRankPro` 主类驱动：宿主生命周期（配置回写、定时任务、
接口注册）全部上移到主类，这里只保留「抓 TSV、解析最新一周、识别媒体、
写快照」。

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
from typing import Tuple, List, Dict, Any

import pytz

from app import schemas
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.media import MetaInfo, MediaInfo
from app.sdk.media import resolve_media_identity
from app.sdk.network import RequestUtils
from app.schemas import MediaType

from .base import (
    RankBoard,
    RankSource,
    SOURCE_NETFLIX,
)
from . import douban

# 豆瓣评分补全的单轮额度（条）：Netflix 条目只有英文片名和名次，评分全靠豆瓣检索。
# 一条 2 个 HTTP，评分按 30 天缓存在 identity_cache 里，首轮慢、之后只补新上榜的。
_RATING_LIMIT = 60


# 全球榜 TSV（含观看量）与国家/地区榜 TSV（仅名次）
GLOBAL_TSV_URL = ("https://www.netflix.com/tudum/top10/data/"
                  "all-weeks-global.tsv")
COUNTRIES_TSV_URL = ("https://www.netflix.com/tudum/top10/data/"
                     "all-weeks-countries.tsv")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


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
        # 媒体类型（movie / tv）：App 拿 media_id 反查宿主详情时必须带对 type_name，
        # 传错宿主不报错、静默返回空 data（简介 / 演员 / 季数全空）。Netflix 的
        # kind 在榜单层（film / tv），条目自己不存，这里按榜单 kind 补。
        kind = str(board.get("kind") or "")
        media_type = str(row.get("media_type") or "") or (
            "movie" if kind == "film" else ("tv" if kind else ""))
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
            "media_type": media_type,
            "poster": str(row.get("poster") or ""),
            "overview": str(row.get("overview") or ""),
            "year": str(row.get("year") or ""),
            "release_date": str(row.get("release_date") or ""),
            "vote_average": _safe_float(row.get("vote_average")),
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


class NetflixSource(RankSource):
    """
    Netflix 官方 Top 10 榜单数据源。

    数据源是 Netflix Tudum 的静态 TSV 文件（官方发布、每周二更新），
    本数据源负责抓取、解析最新一周、走宿主识别链补全中文片名与海报，
    并向主类提供统一榜单结构供 App、推荐页与详情页消费。

    Netflix 未进入中国大陆，地区榜提供中国香港、中国台湾等选项；
    全球榜另附周观看小时数、观看次数与在榜周数。
    """

    source_id = SOURCE_NETFLIX
    source_name = "Netflix榜单"

    # 抓取的地区：global 为全球榜，其余为对应国家/地区榜。
    # 顺序即 App 子榜单的展示顺序（全球 → 美国 → 日本 → 韩国 → 中国香港 → 中国台湾 → 新加坡）
    _regions = ['global', 'us', 'jp', 'kr', 'hk', 'tw', 'sg']

    # 地区代码 -> 中文名（与宿主前端地域表述保持一致）
    _REGION_NAMES = {
        'global': '全球',
        'hk': '中国香港',
        'tw': '中国台湾',
        'us': '美国',
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

    def apply_config(self, config: Dict[str, Any]) -> None:
        """接收主类剥离前缀后的 Netflix 配置。"""
        super().apply_config(config)
        regions = (config or {}).get("regions")
        if isinstance(regions, list) and regions:
            self._regions = [str(r) for r in regions
                             if str(r) in self._REGION_NAMES]

    def fetch(self) -> Dict[str, Any]:
        """执行一次 Netflix 榜单抓取与媒体识别，返回摘要字典。"""
        return self.__refresh_netflix() or {}

    def rank_boards(self, board_type: str = "global", kind: str = None,
                    limit: int = None) -> List[RankBoard]:
        """
        把快照里的 Netflix 榜单转成统一模型。

        :param board_type: 地区代码，global 为全球榜，hk/tw/jp/kr/sg 为地区榜
        :param kind: 内容类型筛选，film 仅电影、tv 仅剧集，不传返回全部
        :param limit: 每个榜单最多返回的条目数
        """
        region_key = self.__region_key(board_type)
        selected: List[RankBoard] = []
        for board in self.boards():
            if str(board.get("region") or "") != region_key:
                continue
            if kind and str(board.get("kind") or "") != kind:
                continue
            rows = board.get("items") or []
            if limit and limit > 0:
                rows = rows[:limit]
            payload = _safe_board_payload(board, rows)
            payload["source"] = self.source_id
            # count 统一表示「本次返回的条目数」（存储里的 count 是抓取时的原始条数，
            # 被 limit 截断后会与 items 长度不符，跨源聚合时容易误读）
            payload["count"] = len(payload["items"])
            # Netflix 的「榜单种类」是地区，统一层再暴露一份 board_type 便于跨源过滤。
            # 快照里全球榜的 region 是空串（历史约定），对外必须还原成 global，
            # 否则调用方拿到 board_type="" 无法回传过滤。
            payload["board_type"] = region_key or "global"
            selected.append(RankBoard(**payload))
        return selected

    def recommend_sources(self) -> list:
        """按当前配置构造要注册到推荐页的 Netflix 数据源（地区 × 内容类型）。"""
        sources = []
        for region in self._regions:
            region_name = self._REGION_NAMES.get(region, region)
            for kind in ('film', 'tv'):
                sources.append(schemas.RecommendMediaSource(
                    name=f"Netflix {region_name}{self._KIND_NAMES[kind]}",
                    api_path=(f"plugin/GlobalRankPro/rank-list?"
                              f"source={self.source_id}&board_type={region}"
                              f"&kind={kind}"),
                    type=self._RECOMMEND_TYPES[kind],
                ))
        return sources

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

        # 豆瓣评分补全：Netflix 官方 TSV 只有名次与观看量，没有评分，评分统一从豆瓣
        # 检索（条目是英文片名，优先拿识别出的中文名去搜，命中率高得多）。
        # 按轮限额 + 30 天缓存分摊，稳态下每轮只为新上榜的几条付请求。
        try:
            pairs = [(key.split("|", 1)[-1], entry)
                     for key, entry in cache.items() if isinstance(entry, dict)]
            if douban.fill_ratings(
                    pairs, budget=_RATING_LIMIT,
                    # 共享 /rating 的 douban_rating 缓存（v1.4.3 优化①），
                    # _host 直读主类键，不能走带前缀的 get_data
                    store_get=lambda: self._host.get_data("douban_rating") or {},
                    store_save=lambda c: self._host.save_data("douban_rating", c),
            ):
                dirty = True
        except Exception as e:
            logger.warning(f"Netflix 豆瓣评分补全失败，本轮跳过：{e}")

        # 评分可能是在 __apply_identity 之后才补上的，这里再合一次（只补这一个字段）
        for board in boards:
            kind = board.get("kind")
            for item in board.get("items", []):
                entry = cache.get(f"{kind}|{item.get('title')}")
                if isinstance(entry, dict) and entry.get("vote_average") \
                        and not item.get("vote_average"):
                    item["vote_average"] = entry["vote_average"]

        if dirty:
            self.save_data("identity_cache", cache)

    @staticmethod
    def __apply_identity(item: dict, identity: dict) -> None:
        """把识别结果合并进快照条目（不清空已有值）。"""
        for key in ("title_cn", "media_source", "media_id", "poster",
                    "overview", "year", "release_date", "vote_average"):
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

    @staticmethod
    def __region_key(board_type: str) -> str:
        """把对外的地区参数映射为快照里的 region 值（global 对应空串）。"""
        return "" if (board_type or "global") == "global" else str(board_type)

    def __snapshot_boards(self) -> List[dict]:
        """从存储中读出快照榜单列表（容错旧数据）。"""
        snapshot = self.snapshot()
        boards = snapshot.get("boards")
        return [b for b in boards if isinstance(b, dict)] if isinstance(boards, list) else []

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


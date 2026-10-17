import datetime
import json
import random
import re
from threading import Event
from typing import Tuple, List, Dict, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, Field

from app import schemas
from app.chain.download import DownloadChain
from app.chain.subscribe import SubscribeChain
from app.chain.tmdb import TmdbChain
from app.sdk.config import settings
from app.sdk.media import MediaInfo
from app.sdk.browser import launch_browser_context
from app.sdk.media import MetaInfo
# 注意：宿主 v3 的 Event 是事件总线事件类（构造必须传 event_type），
# 不能遮蔽 threading.Event，否则类体里的 `_event = Event()` 会在导入期直接报错
from app.sdk.events import eventmanager
from app.sdk.events import Event as SdkEvent
from app.sdk.logging import logger
from app.plugins import _PluginBase
from app.schemas import MediaSource, MediaType
from app.schemas.types import ChainEventType
from app.sdk.network import RequestUtils
from app.sdk.media import normalize_media_source, resolve_media_identity


# ---------------------------------------------------------------------------
# 对外接口的响应模型
#
# 宿主约定：普通 JSON endpoint 必须声明具体 response_model；需要接入宿主普通数据
# 页面（推荐页、浏览页）时显式返回并声明参数化的 schemas.Response[DataModel]。
# ---------------------------------------------------------------------------

class MaoyanProItem(BaseModel):
    """猫眼榜单中的单个条目。"""

    rank: int = Field(..., description="榜单名次，从 1 开始")
    title: str = Field(..., description="媒体标题")
    type: str = Field("", description="榜单类型：movie/web-movie/web-all/zongyi")
    type_name: str = Field("", description="榜单类型中文名")
    platform: str = Field("", description="平台名称，如全网、腾讯视频")
    release_info: str = Field("", description="上线信息，如「上映 11 天」")
    show_date: str = Field("", description="榜单日期，格式 YYYY-MM-DD")
    board: str = Field("", description="榜单名称")
    title_cn: str = Field("", description="中文片名（识别结果，英文榜单条目专供展示）")
    box_office: str = Field("", description="当日票房或热度值等榜单量值")
    total_box_office: str = Field("", description="累计票房（总票房），仅电影票房榜提供")
    heat: str = Field("", description="热度/票房占比等榜单热度描述")
    media_source: str = Field("", description="主媒体来源，未识别时为空")
    media_id: str = Field("", description="主媒体 ID，未识别时为空")
    poster: str = Field("", description="海报地址，未识别时为空")
    overview: str = Field("", description="剧情简介，未识别时为空")
    year: str = Field("", description="年份，未识别时为空")


class MaoyanProBoard(BaseModel):
    """猫眼单个榜单的最新快照。"""

    board: str = Field(..., description="榜单名称")
    type: str = Field("", description="榜单类型标识")
    type_name: str = Field("", description="榜单类型中文名")
    platform: str = Field("", description="平台名称")
    platform_type: str = Field("", description="猫眼 platformType 参数值，全网榜为空")
    show_date: str = Field("", description="榜单日期")
    url: str = Field("", description="榜单来源地址")
    count: int = Field(0, description="榜单条目数")
    updated_at: str = Field("", description="快照时间")
    items: List[MaoyanProItem] = Field(default_factory=list, description="榜单条目")


class MaoyanProSnapshot(BaseModel):
    """猫眼榜单快照汇总数据。"""

    updated_at: str = Field("", description="最近一次刷新的快照时间")
    total: int = Field(0, description="返回的条目总数")
    boards: List[MaoyanProBoard] = Field(default_factory=list, description="榜单列表")


class MaoyanRefreshResult(BaseModel):
    """手动刷新接口的业务数据。"""

    refreshed: bool = Field(..., description="是否成功完成一次抓取")
    board_count: int = Field(0, description="抓取到的榜单数量")
    item_count: int = Field(0, description="抓取到的条目数量")
    subscribed: int = Field(0, description="本次新增订阅数量")
    message: str = Field("", description="结果说明")


def _board_text(row: dict, key: str, board: dict) -> str:
    """条目不携带该字段时回退到所属榜单的同名字段。"""
    value = row.get(key) or board.get(key)
    return str(value) if value is not None else ""


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
            "rank": int(row.get("rank") or 0),
            "title": str(row.get("title") or ""),
            "type": str(row.get("board_type") or ""),
            "type_name": str(row.get("type_name") or ""),
            "platform": str(row.get("platformDesc") or ""),
            "release_info": str(row.get("releaseInfo") or ""),
            "show_date": _board_text(row, "show_date", board),
            "board": str(row.get("board") or board.get("board") or ""),
            "title_cn": str(row.get("title_cn") or ""),
            "box_office": str(row.get("box_office") or ""),
            "total_box_office": str(row.get("total_box_office") or ""),
            "heat": str(row.get("heat") or ""),
            "media_source": str(row.get("media_source") or ""),
            "media_id": str(row.get("media_id") or ""),
            "poster": str(row.get("poster") or ""),
            "overview": str(row.get("overview") or ""),
            "year": str(row.get("year") or ""),
        })
    return {
        "board": text("board"),
        "type": text("type"),
        "type_name": text("type_name"),
        "platform": text("platform"),
        "platform_type": text("platform_type"),
        "show_date": text("show_date"),
        "url": text("url"),
        "count": len(items),
        "updated_at": text("updated_at"),
        "items": items,
    }


class MaoyanPro(_PluginBase):
    """
    获取猫眼的排行榜进行订阅,建议每天一次

    Fork 自 jxxghp/moviepilot-plugins 的 MaoyanRank 插件
    (上游目录 plugins.v3/maoyanrank，上游原插件作者: 逗猫 @baozaodetudou, 上游版本 v4.1.4)；
    本仓库在此基础上新增榜单对外接口、榜单快照与推荐页数据源，
    并以独立身份 MaoyanPro 发行，版本号自本仓库起从 v1.0.0 独立计数。

    电影获取的url: https://piaofang.maoyan.com/dashboard-ajax/movie
    电视剧获取的url: {tv_url}?showDate=20240223&seriesType=0&platformType=0
        参数 showDate: 时间具体到天
        参数 seriesType: 代表类型 0: 电视剧 1: 网络剧 2: 综艺；不传该参数时为电视剧+网络剧综合榜
        参数 platformType: 代表平台 0 全网 3 腾讯视频 2 爱奇艺 1 优酷 7 芒果 5 搜狐 4 乐视 6 PPTV 20 网络电影networkHot=3

    详情链接:
    https://piaofang.maoyan.com/dashboard/movie?movieId=1489349
    https://piaofang.maoyan.com/dashboard/web-heat?movieId=1484643

    """
    # 插件名称
    plugin_name = "猫眼榜单"
    # 插件描述
    plugin_desc = "猫眼榜单显示和订阅功能。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/KingNuoNuo/MoviePilot-Plugins/main/icons/maoyan-v2.png"
    # 插件版本
    plugin_version = "1.2.2"
    # 插件作者
    plugin_author = "KingNuoNuo"
    # 作者主页
    author_url = "https://github.com/KingNuoNuo"
    # 插件配置项ID前缀
    plugin_config_prefix = "maoyanpro_"
    # 加载顺序
    plugin_order = 6
    # 可使用的用户级别：0 = 不做站点用户认证也能加载
    # （MoviePilot 的 PluginAccessPolicy 要求 宿主 auth_level >= auth_level，
    #   未做站点用户认证的环境 auth_level 为 0，声明 1 会让插件在市场列表里被直接过滤掉）
    auth_level = 0

    # 退出事件
    _event = Event()
    # 私有属性
    downloadchain: DownloadChain = None
    subscribechain: SubscribeChain = None
    _scheduler = None
    _enabled = False
    _onlyonce = False
    _cron = ""
    _clear = False
    # 订阅类型细分：movie 电影票房 / web-movie 网络电影 / web-all 剧集综合榜
    # （电视剧+网络剧，走猫眼不传 seriesType 的综合榜）/ zongyi 综艺热度。
    # 电影与剧集分开配置，两边的勾选互不牵连；综艺属于剧集大类（只是热度榜口径
    # 不同），类型选择并入「剧集订阅类型」同一个下拉，综艺条数仍单独配置。
    _type_movie = ['movie', 'web-movie']
    _type_tv = ['web-all', 'zongyi']
    # 综艺类型在选择上归属 _type_tv，此属性仅作旧配置兼容与配置回写保留（派生视图）
    _type_zongyi = ['zongyi']
    # 实际抓取的榜单类型（由上方两组配置合并而来）
    _type = ['movie', 'web-movie', 'web-all', 'zongyi']

    # 各组「订阅条数」与「推荐开关」：订阅条数为 0 表示不订阅该榜（仍抓取供推荐页），
    # 推荐开关关闭表示推荐页与榜单接口不展示该榜；配置加载后派生出
    # _xxx_rec_num（开=满榜 10 条，关=0），抓取与展示逻辑统一读派生值。
    _movie_sub_num = 10
    _movie_rec_enabled = True
    _movie_rec_num = 10
    _tv_sub_num = 10
    _tv_rec_enabled = True
    _tv_rec_num = 10
    _zongyi_sub_num = 10
    _zongyi_rec_enabled = True
    _zongyi_rec_num = 10
    # 平台细分榜只服务自动订阅（不写入榜单快照/推荐页），条数为 0 表示不订阅该平台
    _tx_sub_num = 0
    _iqy_sub_num = 0
    _mg_sub_num = 0
    _yk_sub_num = 0
    # 平台细分榜「推荐开关」：开启时该平台榜写入榜单快照/推荐页
    _tx_rec_enabled = False
    _iqy_rec_enabled = False
    _mg_rec_enabled = False
    _yk_rec_enabled = False
    _tx_rec_num = 0
    _iqy_rec_num = 0
    _mg_rec_num = 0
    _yk_rec_num = 0

    # 对外接口与推荐数据源开关
    _api_enabled = True
    _recommend_enabled = False

    # 榜单类型 -> 中文名；用于对外接口与推荐源的展示名称
    _TYPE_NAMES = {
        'movie': '电影票房',
        'total': '影史总票房',
        'year': '国内年度票房',
        'global': '全球年度票房',
        'web-movie': '网络电影',
        'web-all': '剧集综合',
        'zongyi': '综艺热度',
    }

    # 榜单类型 -> 推荐页分类，取值与宿主前端 recommend.category* 的文案保持一致
    _RECOMMEND_TYPES = {
        'movie': '电影',
        'total': '电影',
        'year': '电影',
        'global': '电影',
        'web-movie': '电影',
        'web-all': '电视剧',
        'zongyi': '电视剧',
    }

    # 榜单类型 -> 识别媒体时使用的类型（电影榜按电影识别，热度榜按剧集识别）
    _BOARD_MEDIA_TYPES = {
        'movie': MediaType.MOVIE,
        'total': MediaType.MOVIE,
        'year': MediaType.MOVIE,
        'global': MediaType.MOVIE,
        'web-movie': MediaType.MOVIE,
        'web-all': MediaType.TV,
        'zongyi': MediaType.TV,
    }

    # 单次刷新最多识别的条目数：平台细分榜会让待识别条目变多，超出的留待下次刷新补齐。
    # 识别结果会持久化（identity_cache），因此限额只影响「新条目」的收敛速度，
    # 老条目跨轮复用缓存，不会每轮从零识别（v3 每次调度都重建插件实例，内存缓存必丢）。
    _RESOLVE_LIMIT = 80
    # 识别缓存 TTL：已识别到媒体且拿到海报的条目长期有效；
    # 只拿到兜底海报（或啥都没有）的条目过期后重试 —— 新节目可能随后上架 TMDB
    _IDENTITY_TTL_FALLBACK = 2 * 86400

    # 猫眼热度接口的 platformType -> 平台名；空值代表全网
    _PLATFORM_NAMES = {
        '0': '全网',
        '1': '优酷',
        '2': '爱奇艺',
        '3': '腾讯视频',
        '5': '搜狐',
        '7': '芒果',
        '20': '网络电影',
    }

    # 猫眼热度接口的 seriesType -> 榜单类型：剧集统一走综合榜（不传 seriesType），
    # 只有综艺（2）是独立榜单，因此这里只登记综艺
    _SERIES_TYPES = {'2': 'zongyi'}

    # 「订阅条数」候选项：0 表示不订阅该榜（仍抓取供推荐页展示）
    _SUB_NUM_ITEMS = [{'title': str(num), 'value': num} for num in (0, 1, 2, 3, 5, 7, 10)]
    # 推荐已改为开关：猫眼单个榜单最多 10 条，开关打开即按满榜抓取与展示
    _REC_BOARD_MAX = 10
    # 影史总票房榜 / 国内年度票房榜为静态总榜，无订阅语义，固定抓取条数
    _TOTAL_BOARD_FETCH_NUM = 30
    # 全球年度票房榜（Box Office Mojo 年度全球榜）为静态总榜，无订阅语义，固定抓取条数
    _GLOBAL_BOARD_FETCH_NUM = 30
    # 平台细分榜配置：(新配置键, 旧的条数键, 旧的开关键)
    _PLATFORM_CONFIG_KEYS = (
        ('tx_sub_num', 'tx_num', 'tx_enabled'),
        ('iqy_sub_num', 'iqy_num', 'iqy_enabled'),
        ('mg_sub_num', 'mg_num', 'mg_enabled'),
        ('yk_sub_num', 'yk_num', 'yk_enabled'),
    )
    # 猫眼 platformType -> (订阅条数字段, 推荐条数字段)；抓取与推荐源注册共用
    _PLATFORM_NUM_KEYS = {
        3: ('tx_sub_num', 'tx_rec_num'),
        2: ('iqy_sub_num', 'iqy_rec_num'),
        7: ('mg_sub_num', 'mg_rec_num'),
        1: ('yk_sub_num', 'yk_rec_num'),
    }

    # 本次抓取到的榜单元数据，供对外接口落快照（不持久化，失败时为上层回退到历史）
    _last_boards: List[dict] = []

    def init_plugin(self, config: dict = None):
        """加载榜单配置并按周期或一次性开关注册刷新任务。"""
        self.downloadchain = DownloadChain()
        self.subscribechain = SubscribeChain()

        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._clear = config.get("clear")
            self._onlyonce = config.get("onlyonce")
            self._api_enabled = config.get("api_enabled", True)
            self._recommend_enabled = config.get("recommend_enabled", False)
            self.__load_type_config(config)

        self.__migrate_history_identity()

        # 停止现有任务
        self.stop_service()

        # 启动服务
        # 清理插件历史
        if self._clear:
            self.del_data(key="history")
            self._clear = False
            self.__update_config()
            logger.info("历史清理完成")

        # 订阅与推荐页数据源相互独立：任一开启都需要定时抓取榜单
        # （推荐页数据来自榜单快照，抓取停了推荐页就没有新数据）
        if self._enabled or self._recommend_enabled or self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            # 周期执行
            if self._cron:
                logger.info(f"猫眼榜单服务启动，周期：{self._cron}")
                try:
                    self._scheduler.add_job(func=self.__refresh_maoyan,
                                            trigger=CronTrigger.from_crontab(self._cron),
                                            name="猫眼榜单")
                except Exception as e:
                    logger.error(f"猫眼榜单服务启动失败，错误信息：{str(e)}")
                    self.systemmessage.put(f"猫眼榜单服务启动失败，错误信息：{str(e)}")
            else:
                self._scheduler.add_job(func=self.__refresh_maoyan, trigger=CronTrigger.from_crontab("0 9 * * *"),
                                        name="猫眼榜单")
                logger.info("猫眼榜单服务启动，周期：每天 09:00")

            # 一次性执行
            if self._onlyonce:
                logger.info("猫眼榜单服务启动，立即运行一次")
                self._scheduler.add_job(func=self.__refresh_maoyan, trigger='date',
                                        run_date=datetime.datetime.now(
                                            tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                                        )
                # 关闭一次性开关
                self._onlyonce = False
                # 保存配置
                self.__update_config()

            if self._scheduler.get_jobs():
                # 启动服务
                self._scheduler.print_jobs()
                self._scheduler.start()

    def get_state(self) -> bool:
        """返回插件启用状态：订阅或推荐页数据源任一开启即视为启用。"""
        return bool(self._enabled or self._recommend_enabled)

    def __migrate_history_identity(self) -> None:
        """将旧猫眼榜单历史的 TMDB ID 原地迁移为统一媒体身份。"""
        history = self.get_data("history")
        if not isinstance(history, list):
            return
        changed = False
        for item in history:
            if not isinstance(item, dict):
                continue
            media_source, media_id = resolve_media_identity(
                media_source=item.get("media_source"),
                media_id=item.get("media_id"),
            )
            if not media_source:
                media_source, media_id = resolve_media_identity(
                    media_source=MediaSource.TMDB,
                    media_id=item.get("tmdbid"),
                )
            if not media_source:
                continue
            migrated = {key: value for key, value in item.items() if key != "tmdbid"}
            migrated["media_source"] = media_source.value
            migrated["media_id"] = media_id
            if migrated != item:
                item.clear()
                item.update(migrated)
                changed = True
        if changed:
            self.save_data("history", history)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """本插件不注册聊天命令。"""
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        """
        注册对外接口。

        - `/rank`：返回最近一次抓取的榜单快照，供 App、脚本或宿主页面直接消费；
          用宿主统一响应包裹，便于接入宿主普通数据客户端。
        - `/refresh`：立即触发一次抓取与订阅，等价于插件的「立即运行一次」。

        认证用宿主的 `bear` 模式（依赖 `verify_token`）：它同时接受 App 登录态
        的 JWT（`Authorization: Bearer`）、API 令牌（`X-API-KEY` 头或 `apikey` 查询
        参数）与 `token` 查询参数。默认的 `apikey` 模式只认 API 令牌，账号密码登录
        的 App 会 401，因此这里显式声明 `bear`。
        """
        return [
            {
                "path": "/rank",
                "endpoint": self.get_rank_snapshot,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "猫眼榜单快照",
                "description": "返回最近一次抓取的猫眼榜单快照，可按榜单类型筛选条目数量，"
                               "也可按 platform（猫眼 platformType）筛选平台细分榜",
                "response_model": schemas.Response[MaoyanProSnapshot],
            },
            {
                "path": "/rank-list",
                "endpoint": self.rank_list,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "猫眼榜单列表",
                "description": "供宿主推荐页与浏览页消费的榜单列表，返回统一的媒体信息结构",
                "response_model": schemas.Response[List[schemas.MediaInfo]],
            },
            {
                "path": "/refresh",
                "endpoint": self.refresh_now,
                "methods": ["GET", "POST"],
                "summary": "立即刷新猫眼榜单",
                "description": "立即触发一次猫眼榜单抓取并按配置添加订阅",
                "response_model": MaoyanRefreshResult,
            },
        ]

    def get_rank_snapshot(self, board_type: str = None, limit: int = None,
                          board: str = None,
                          platform: str = None) -> schemas.Response[MaoyanProSnapshot]:
        """
        返回最近一次抓取的猫眼榜单快照。

        :param board_type: 仅返回指定榜单类型（movie/web-movie/web-all/zongyi）
        :param limit: 每个榜单最多返回的条目数，用于限制响应体大小
        :param board: 仅返回榜单名包含该关键字的榜单（如「腾讯视频」）
        :param platform: 仅返回指定平台（猫眼 platformType，如 3=腾讯视频）的榜单；
                         不传时返回全部榜单（含已开启推荐的平台榜）
        """
        snapshot = self.get_data("snapshot") or {}
        boards = snapshot.get("boards") if isinstance(snapshot, dict) else None

        # 快照尚未生成时（例如升级后未跑过刷新）回退到订阅流水，保证接口可用。
        if not isinstance(boards, list) or not boards:
            fallback = self.__snapshot_from_history()
            return schemas.Response(
                success=True,
                message="榜单快照尚未生成，当前返回订阅流水" if fallback.boards else "",
                data=fallback,
            )

        selected = []
        for item in boards:
            if not isinstance(item, dict):
                continue
            if board_type and item.get("type") != board_type:
                continue
            if board and board not in str(item.get("board", "")):
                continue
            if platform is not None and \
                    str(item.get("platform_type") or "") != str(platform):
                continue
            rows = item.get("items") or []
            if limit and limit > 0:
                rows = rows[:limit]
            selected.append(MaoyanProBoard(**_safe_board_payload(item, rows)))

        return schemas.Response(
            success=True,
            message="",
            data=MaoyanProSnapshot(
                updated_at=str(snapshot.get("updated_at") or ""),
                total=sum(item.count for item in selected),
                boards=selected,
            ),
        )

    def refresh_now(self) -> MaoyanRefreshResult:
        """立即触发一次猫眼榜单抓取，并按当前配置添加订阅。"""
        if not self._type:
            return MaoyanRefreshResult(
                refreshed=False,
                message="未配置任何猫眼榜单类型，请先在插件配置中选择",
            )
        try:
            result = self.__refresh_maoyan() or {}
        except Exception as e:
            logger.error(f"手动刷新猫眼榜单失败：{e}")
            return MaoyanRefreshResult(refreshed=False, message=f"刷新失败：{e}")
        return MaoyanRefreshResult(
            refreshed=True,
            board_count=int(result.get("board_count", 0)),
            item_count=int(result.get("item_count", 0)),
            subscribed=int(result.get("subscribed", 0)),
            message="已按当前配置完成一次猫眼榜单抓取",
        )

    def __snapshot_from_history(self) -> MaoyanProSnapshot:
        """
        用订阅流水兜底构造榜单快照。

        历史记录只有已识别并入订阅的条目，没有名次与量值，因此统一按单榜单返回，
        并标注为兜底数据，便于调用方区分。
        """
        history = self.get_data("history") or []
        if not isinstance(history, list):
            return MaoyanProSnapshot()
        history = sorted(
            [row for row in history if isinstance(row, dict)],
            key=lambda row: row.get("time") or "",
            reverse=True,
        )
        items = [
            MaoyanProItem(
                rank=index,
                title=str(row.get("title") or ""),
                type_name=str(row.get("type") or ""),
                platform=str(row.get("platformDesc") or ""),
                release_info=str(row.get("releaseInfo") or ""),
                media_source=str(row.get("media_source") or ""),
                media_id=str(row.get("media_id") or ""),
                poster=str(row.get("poster") or ""),
                overview=str(row.get("overview") or ""),
                year=str(row.get("year") or ""),
            )
            for index, row in enumerate(history, start=1)
        ]
        if not items:
            return MaoyanProSnapshot()
        return MaoyanProSnapshot(
            updated_at=str(history[0].get("time") or ""),
            total=len(items),
            boards=[
                MaoyanProBoard(
                    board="猫眼订阅流水",
                    type="",
                    type_name="订阅流水",
                    show_date="",
                    count=len(items),
                    updated_at=str(history[0].get("time") or ""),
                    items=items,
                )
            ],
        )

    @eventmanager.register(ChainEventType.RecommendSource)
    def recommend_source(self, event: SdkEvent):
        """把猫眼榜单注册为宿主推荐页的数据源（与订阅功能相互独立）。"""
        if not (self._recommend_enabled and self._api_enabled):
            return
        event_data: schemas.RecommendSourceEventData = event.event_data
        if not event_data:
            return

        sources = []
        if 'movie' in (self._type or []):
            sources.append(schemas.RecommendMediaSource(
                name="猫眼电影票房",
                api_path=f"plugin/{self.__class__.__name__}/rank-list?board_type=movie",
                type=self._RECOMMEND_TYPES['movie'],
            ))
        if 'total' in (self._type or []):
            sources.append(schemas.RecommendMediaSource(
                name="猫眼影史总票房",
                api_path=f"plugin/{self.__class__.__name__}/rank-list?board_type=total",
                type=self._RECOMMEND_TYPES['total'],
            ))
        if 'year' in (self._type or []):
            sources.append(schemas.RecommendMediaSource(
                name="国内年度票房",
                api_path=f"plugin/{self.__class__.__name__}/rank-list?board_type=year",
                type=self._RECOMMEND_TYPES['year'],
            ))
        if 'global' in (self._type or []):
            sources.append(schemas.RecommendMediaSource(
                name="全球年度票房",
                api_path=f"plugin/{self.__class__.__name__}/rank-list?board_type=global",
                type=self._RECOMMEND_TYPES['global'],
            ))
        for board_type, board_label in (
                ('web-all', '猫眼剧集综合热度'),
                ('zongyi', '猫眼综艺热度')):
            if board_type not in (self._type or []):
                continue
            # 只注册全网榜保持推荐页主界面简洁；平台细分榜不单独挂源，
            # 由 rank-list 的分页编组在「更多」页里依次展示（标题带平台后缀）
            sources.append(schemas.RecommendMediaSource(
                name=board_label,
                api_path=(f"plugin/{self.__class__.__name__}/rank-list?"
                          f"board_type={board_type}"),
                type=self._RECOMMEND_TYPES[board_type],
            ))
        if not sources:
            return

        # 叠加而不是替换，避免影响其它插件塞入的数据源
        if not event_data.extra_sources:
            event_data.extra_sources = sources
        else:
            event_data.extra_sources.extend(sources)

    def rank_list(self, board_type: str = "web-all", page: int = 1,
                  limit: int = None,
                  platform: str = None) -> schemas.Response[List[schemas.MediaInfo]]:
        """
        推荐页/浏览页使用的榜单列表接口。

        宿主列表视图按 page 翻页、翻到空页或重复页终止，因此把多张榜编成页序：
        第 1 页 = 全网榜，第 2 页起 = 推荐条数 > 0 且快照里实际有数据的平台细分榜
        （标题带「（平台名）」后缀）。这样推荐页主界面只挂全网源保持简洁，
        点「更多」继续翻页就能依次看到各平台榜单。显式指定 platform 时只返回
        该平台的细分榜（供调用方精确取数，不做分页编组）。
        """
        snapshot = self.get_data("snapshot") or {}
        boards = snapshot.get("boards") if isinstance(snapshot, dict) else None
        rows_by_platform: Dict[str, list] = {}
        if isinstance(boards, list):
            for item in boards:
                if not isinstance(item, dict) or item.get("type") != board_type:
                    continue
                key = str(item.get("platform_type") or "")
                rows_by_platform.setdefault(key, item.get("items") or [])

        if platform is not None:
            page_keys = [str(platform)]
        else:
            # 页序：全网榜在前，其后按固定顺序排推荐条数 > 0 的平台榜
            candidates: List[tuple] = [('', None)]
            candidates += [(str(pt), rec_key) for pt, (_, rec_key)
                           in self._PLATFORM_NUM_KEYS.items()
                           if getattr(self, f"_{rec_key}", 0) > 0]
            # 只保留快照里实际有数据的页，避免中途出现空页提前终止翻页
            page_keys = [key for key, _ in candidates if rows_by_platform.get(key)]

        page_index = (page or 1) - 1
        if page_index < 0 or page_index >= len(page_keys):
            return schemas.Response(success=True, data=[])
        rows = rows_by_platform.get(page_keys[page_index], [])
        platform_name = ('' if page_keys[page_index] == ''
                         else self._PLATFORM_NAMES.get(page_keys[page_index], ''))

        result: List[schemas.MediaInfo] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            media_source, media_id = resolve_media_identity(
                media_source=row.get("media_source"),
                media_id=row.get("media_id"),
            )
            # 英文榜单条目优先用识别出的中文片名展示
            title = str(row.get("title_cn") or "") or str(row.get("title") or "")
            # 平台页的标题带平台后缀，扁平列表里也能一眼区分来自哪个平台；
            # 显式指定 platform 的调用方拿原始标题即可
            if platform is None and platform_name and title:
                title = f"{title}（{platform_name}）"
            media = schemas.MediaInfo(
                media_source=media_source,
                media_id=media_id,
                title=title,
                year=str(row.get("year") or "") or None,
                type=str(row.get("type_name") or self._TYPE_NAMES.get(board_type, board_type)),
                poster_path=str(row.get("poster") or "") or None,
                overview=str(row.get("overview") or "") or None,
            )
            if media.title and media.year:
                media.title_year = f"{media.title} ({media.year})"
            result.append(media)
            if limit and limit > 0 and len(result) >= limit:
                break
        return schemas.Response(success=True, data=result)

    @classmethod
    def __safe_num(cls, value, default: int = 0) -> int:
        """把配置里的条数转成非负整数，非法值回退到默认值。"""
        try:
            num = int(value)
        except (TypeError, ValueError):
            return default
        return num if num >= 0 else default

    def __load_type_config(self, config: dict) -> None:
        """
        读取订阅类型与条数配置。

        电影与剧集（含综艺）两组类型各成一组、互不牵连；同时兼容旧版只有合并 `type`
        字段、独立的 `type_zongyi` 字段、平台开关（tx_enabled 等）的历史配置，老用户
        升级后无需重新配置。
        """
        type_movie = config.get("type_movie")
        type_tv = config.get("type_tv")
        type_zongyi = config.get("type_zongyi")
        legacy_type = config.get("type")
        if type_movie is None and type_tv is None and type_zongyi is None and legacy_type:
            # 旧配置：从合并的 type 列表还原出电影/剧集两组类型
            legacy = [item for item in legacy_type if isinstance(item, str)]
            type_movie = [item for item in legacy if item in ('movie', 'web-movie')]
            type_tv = [item for item in legacy if item in ('web-heat', 'web-tv', 'web-all')]
            type_zongyi = [item for item in legacy if item == 'zongyi']

        # 综艺已并入剧集下拉：旧配置里独立的 type_zongyi 合并进剧集类型
        tv_items = list(type_tv) if type_tv is not None else None
        if type_zongyi:
            extra = [item for item in type_zongyi if item == 'zongyi']
            tv_items = list(tv_items or []) + extra
        if tv_items is None:
            tv_items = list(self._type_tv)
        # 电视剧/网剧单榜已取消，旧配置里的这两个类型统一归并为剧集综合榜；
        # 去重的同时保持配置顺序，避免迁移后出现重复项
        merged_tv = ['web-all' if item in ('web-heat', 'web-tv') else item for item in tv_items]
        self._type_tv = list(dict.fromkeys(
            item for item in merged_tv if item in ('web-all', 'zongyi')))
        self._type_movie = list(dict.fromkeys(
            item for item in (type_movie if type_movie is not None else self._type_movie)
            if item in ('movie', 'web-movie', 'total', 'year', 'global')))
        # 派生视图：仅供旧版字段回写，综艺类型已归属剧集
        self._type_zongyi = [item for item in self._type_tv if item == 'zongyi']
        self._type = [*self._type_movie, *self._type_tv]

        legacy_rec = config.get("recommend_num")
        # 推荐已改为开关（v1.2.2）：开关打开按满榜（10 条）抓取与展示。
        # 兼容旧配置：未出现开关键时按旧的数值字段 > 0 判定。
        for name, legacy_default in (('movie', 20), ('tv', 20), ('zongyi', 20)):
            enabled = config.get(f"{name}_rec_enabled")
            if enabled is None:
                enabled = self.__safe_num(
                    config.get(f"{name}_rec_num", legacy_rec), legacy_default) > 0
            enabled = bool(enabled)
            setattr(self, f"_{name}_rec_enabled", enabled)
            setattr(self, f"_{name}_rec_num",
                    self._REC_BOARD_MAX if enabled else 0)
        self._movie_sub_num = self.__safe_num(config.get("movie_sub_num", config.get("num")), 10)
        self._tv_sub_num = self.__safe_num(config.get("tv_sub_num", config.get("all_num")), 10)
        self._zongyi_sub_num = self.__safe_num(config.get("zongyi_sub_num", config.get("all_num")), 10)
        for new_key, num_key, enabled_key in self._PLATFORM_CONFIG_KEYS:
            if new_key in config:
                num = self.__safe_num(config.get(new_key), 0)
            elif config.get(enabled_key):
                # 旧配置：只有开关打开时才把旧条数搬过来，否则视为不订阅
                num = self.__safe_num(config.get(num_key), 10)
            else:
                num = 0
            setattr(self, f"_{new_key}", num)
        for platform_type, (_, rec_key) in self._PLATFORM_NUM_KEYS.items():
            name = rec_key[:-8]  # 'tx_rec_num' -> 'tx'
            rec_enabled = config.get(f"{name}_rec_enabled")
            if rec_enabled is None:
                rec_enabled = self.__safe_num(config.get(rec_key), 0) > 0
            rec_enabled = bool(rec_enabled)
            setattr(self, f"_{name}_rec_enabled", rec_enabled)
            setattr(self, f"_{rec_key}",
                    self._REC_BOARD_MAX if rec_enabled else 0)

    @staticmethod
    def __form_col(component: str, props: dict, md: int = 6) -> dict:
        """构造宿主表单的一列（栅格：窄屏占满整行，宽屏按 md 分栏）。"""
        return {
            'component': 'VCol',
            'props': {'cols': 12, 'md': md},
            'content': [{'component': component, 'props': props}],
        }

    def __num_col(self, model: str, label: str) -> dict:
        """构造订阅条数选择列：0 表示不订阅该榜（仍抓取供推荐页展示）。"""
        return self.__form_col('VSelect', {
            'chips': True,
            'multiple': False,
            'model': model,
            'label': label,
            'items': self._SUB_NUM_ITEMS,
        })

    def __type_col(self, model: str, label: str, items: List[dict]) -> dict:
        """构造榜单类型多选列。"""
        return self.__form_col('VSelect', {
            'chips': True,
            'multiple': True,
            'model': model,
            'label': label,
            'items': items,
        })

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """提供订阅类型、订阅条数、推荐开关、平台订阅/推荐配置与执行周期。"""
        rows = [
            # 基础开关与执行周期
            {
                'component': 'VRow',
                'content': [
                    self.__form_col('VSwitch', {'model': 'enabled', 'label': '启用插件'}),
                    self.__form_col('VSwitch', {'model': 'onlyonce', 'label': '立即运行一次'}),
                ],
            },
            {
                'component': 'VRow',
                'content': [
                    self.__form_col('VCronField', {
                        'model': 'cron',
                        'label': '执行周期',
                        'placeholder': '5位cron表达式，留空自动',
                    }),
                    self.__form_col('VSwitch', {'model': 'clear', 'label': '清理历史记录'}),
                ],
            },
            # 订阅类型：电影与剧集分开设置，各自决定抓取哪些榜单；
            # 综艺属于剧集大类，类型勾选并入「剧集订阅类型」，条数仍在下方单独配置
            {
                'component': 'VRow',
                'content': [
                    self.__type_col('type_movie', '电影订阅类型', [
                        {'title': '电影票房榜单', 'value': 'movie'},
                        {'title': '网络电影榜单', 'value': 'web-movie'},
                        {'title': '影史总票房榜', 'value': 'total'},
                        {'title': '国内年度票房榜', 'value': 'year'},
                        {'title': '全球年度票房榜', 'value': 'global'},
                    ]),
                    self.__type_col('type_tv', '剧集订阅类型', [
                        {'title': '剧集综合榜单（电视剧+网剧）', 'value': 'web-all'},
                        {'title': '综艺热度榜单', 'value': 'zongyi'},
                    ]),
                ],
            },
            {
                'component': 'VRow',
                'content': [
                    self.__form_col('VAlert', {
                        'type': 'info',
                        'variant': 'tonal',
                        'text': '订阅类型决定抓取与展示哪些榜单；订阅条数为 0 表示不订阅该榜'
                                '（仍会抓取供推荐页使用），推荐开关关闭表示推荐页与榜单接口不展示该榜，'
                                '订阅条数为 0 且推荐开关关闭则该榜完全不抓取。推荐按满榜展示'
                                '（猫眼单个榜单最多 10 条）。',
                    }, md=12),
                ],
            },
            # 各组配置：左列订阅条数（只影响自动订阅），右列推荐开关（只影响推荐页/榜单接口）
            {
                'component': 'VRow',
                'content': [
                    self.__num_col('movie_sub_num', '电影榜单订阅条数'),
                    self.__form_col('VSwitch', {'model': 'movie_rec_enabled',
                                                'label': '电影榜单推荐'}),
                ],
            },
            {
                'component': 'VRow',
                'content': [
                    self.__num_col('tv_sub_num', '剧集榜单订阅条数'),
                    self.__form_col('VSwitch', {'model': 'tv_rec_enabled',
                                                'label': '剧集榜单推荐'}),
                ],
            },
            {
                'component': 'VRow',
                'content': [
                    self.__num_col('zongyi_sub_num', '综艺榜单订阅条数'),
                    self.__form_col('VSwitch', {'model': 'zongyi_rec_enabled',
                                                'label': '综艺榜单推荐'}),
                ],
            },
            # 平台细分榜：每平台一行，左列订阅条数、右列推荐开关
            {
                'component': 'VRow',
                'content': [
                    self.__form_col('VAlert', {
                        'type': 'info',
                        'variant': 'tonal',
                        'text': '下方为各视频平台榜单的订阅条数与推荐开关（左订阅、右推荐）：'
                                '订阅条数只影响自动订阅，推荐开关决定推荐页与榜单接口是否展示该平台榜单；'
                                '订阅条数为 0 且推荐开关关闭则该平台榜不抓取。',
                    }, md=12),
                ],
            },
            {
                'component': 'VRow',
                'content': [
                    self.__num_col('tx_sub_num', '腾讯视频订阅条数'),
                    self.__form_col('VSwitch', {'model': 'tx_rec_enabled',
                                                'label': '腾讯视频推荐'}),
                ],
            },
            {
                'component': 'VRow',
                'content': [
                    self.__num_col('iqy_sub_num', '爱奇艺订阅条数'),
                    self.__form_col('VSwitch', {'model': 'iqy_rec_enabled',
                                                'label': '爱奇艺推荐'}),
                ],
            },
            {
                'component': 'VRow',
                'content': [
                    self.__num_col('mg_sub_num', '芒果TV订阅条数'),
                    self.__form_col('VSwitch', {'model': 'mg_rec_enabled',
                                                'label': '芒果TV推荐'}),
                ],
            },
            {
                'component': 'VRow',
                'content': [
                    self.__num_col('yk_sub_num', '优酷订阅条数'),
                    self.__form_col('VSwitch', {'model': 'yk_rec_enabled',
                                                'label': '优酷推荐'}),
                ],
            },
            # 对外接口与推荐页数据源
            {
                'component': 'VRow',
                'content': [
                    self.__form_col('VSwitch', {'model': 'api_enabled', 'label': '开放榜单接口'}),
                    self.__form_col('VSwitch', {'model': 'recommend_enabled', 'label': '接入推荐页数据源'}),
                ],
            },
        ]
        return [{
            'component': 'VForm',
            'content': rows,
        }], {
            "enabled": False,
            "onlyonce": False,
            "cron": "",
            "clear": False,
            "type_movie": ['movie', 'web-movie'],
            "type_tv": ['web-all', 'zongyi'],
            "movie_sub_num": 10,
            "movie_rec_enabled": True,
            "tv_sub_num": 10,
            "tv_rec_enabled": True,
            "zongyi_sub_num": 10,
            "zongyi_rec_enabled": True,
            "tx_sub_num": 0,
            "iqy_sub_num": 0,
            "mg_sub_num": 0,
            "yk_sub_num": 0,
            "tx_rec_enabled": False,
            "iqy_rec_enabled": False,
            "mg_rec_enabled": False,
            "yk_rec_enabled": False,
            "api_enabled": True,
            "recommend_enabled": False,
        }

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面，需要返回页面配置，同时附带数据
        """
        # 查询历史记录
        historys = self.get_data('history') or []
        if not historys:
            return [
                {
                    'component': 'div',
                    'text': '暂无订阅历史',
                    'props': {
                        'class': 'text-center pa-6 text-medium-emphasis',
                    }
                }
            ]
        if not isinstance(historys, list):
            historys = [historys]

        def safe_text(value, default: str = "—") -> str:
            """
            统一处理历史记录里的空值，避免详情页直接显示 None。
            """
            if value is None:
                return default
            text = str(value).strip()
            if not text or text.lower() == "none":
                return default
            return text

        def tmdb_href(media_type: str, tmdb_id) -> str:
            """
            根据媒体类型生成 TMDB 链接，缺少 ID 时显示占位。
            """
            clean_id = safe_text(tmdb_id, "")
            if not clean_id:
                return ""
            if media_type == MediaType.TV.value:
                return f"https://www.themoviedb.org/tv/{clean_id}"
            return f"https://www.themoviedb.org/movie/{clean_id}"

        # 数据按时间降序排序，并兼容旧历史中 time 为空的情况。
        historys = sorted(historys, key=lambda x: x.get('time') or "", reverse=True)
        items = []
        for index, history in enumerate(historys, start=1):
            media_type = safe_text(history.get("type"), "未知")
            platform = safe_text(history.get("platformDesc"), "未知")
            items.append({
                "index": index,
                "title": safe_text(history.get("title"), "未命名"),
                "release_info": safe_text(history.get("releaseInfo"), "暂无上线信息"),
                "platform": platform,
                "type": media_type,
                "time": safe_text(history.get("time"), "未知时间"),
                "poster": safe_text(history.get("poster"), ""),
                "tmdb_href": tmdb_href(
                    media_type,
                    history.get("media_id")
                    if normalize_media_source(history.get("media_source")) == MediaSource.TMDB
                    else None,
                ),
            })

        movie_count = sum(1 for item in items if item.get("type") == MediaType.MOVIE.value)
        tv_count = sum(1 for item in items if item.get("type") == MediaType.TV.value)
        platform_count = len({item.get("platform") for item in items if item.get("platform") != "未知"})

        def stat_card(title: str, value: str, color: str) -> dict:
            """
            顶部统计卡片，帮助快速掌握历史记录规模。
            """
            return {
                'component': 'VCol',
                'props': {
                    'cols': 12,
                    'md': 3
                },
                'content': [
                    {
                        'component': 'VCard',
                        'props': {
                            'variant': 'tonal',
                            'color': color,
                            'class': 'pa-2'
                        },
                        'content': [
                            {
                                'component': 'VCardSubtitle',
                                'props': {
                                    'class': 'pb-0'
                                },
                                'text': title
                            },
                            {
                                'component': 'VCardTitle',
                                'props': {
                                    'class': 'text-h6 pt-1'
                                },
                                'text': value
                            }
                        ]
                    }
                ]
            }

        def history_card(item: dict) -> dict:
            """
            订阅历史卡片，保留海报和关键信息，同时压缩卡片高度。
            """
            title_node = {
                'component': 'a' if item.get("tmdb_href") else 'span',
                'props': {
                    'href': item.get("tmdb_href"),
                    'target': '_blank',
                    'class': 'text-decoration-none'
                } if item.get("tmdb_href") else {},
                'text': item.get("title")
            }
            poster_node = {
                'component': 'VImg',
                'props': {
                    'src': item.get("poster"),
                    'height': 132,
                    'width': 88,
                    'aspect-ratio': '2/3',
                    'class': 'rounded flex-shrink-0',
                    'cover': True
                }
            } if item.get("poster") else {
                'component': 'div',
                'props': {
                    'class': 'd-flex align-center justify-center rounded bg-grey-lighten-3 text-caption text-medium-emphasis flex-shrink-0',
                    'style': {
                        'width': '88px',
                        'height': '132px'
                    }
                },
                'text': '无海报'
            }

            return {
                'component': 'VCard',
                'props': {
                    'variant': 'outlined',
                    'class': 'h-100'
                },
                'content': [
                    {
                        'component': 'div',
                        'props': {
                            'class': 'd-flex flex-nowrap ga-3 pa-3'
                        },
                        'content': [
                            poster_node,
                            {
                                'component': 'div',
                                'props': {
                                    'class': 'min-w-0 flex-grow-1'
                                },
                                'content': [
                                    {
                                        'component': 'div',
                                        'props': {
                                            'class': 'text-subtitle-2 font-weight-bold text-truncate mb-2'
                                        },
                                        'content': [title_node]
                                    },
                                    {
                                        'component': 'div',
                                        'props': {
                                            'class': 'd-flex flex-wrap ga-1 mb-2'
                                        },
                                        'content': [
                                            {
                                                'component': 'VChip',
                                                'props': {
                                                    'size': 'x-small',
                                                    'variant': 'tonal',
                                                    'color': 'indigo'
                                                },
                                                'text': item.get("type")
                                            },
                                            {
                                                'component': 'VChip',
                                                'props': {
                                                    'size': 'x-small',
                                                    'variant': 'tonal',
                                                    'color': 'teal'
                                                },
                                                'text': item.get("platform")
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'div',
                                        'props': {
                                            'class': 'text-body-2 mb-1'
                                        },
                                        'text': item.get("release_info")
                                    },
                                    {
                                        'component': 'div',
                                        'props': {
                                            'class': 'text-caption text-medium-emphasis'
                                        },
                                        'text': f"订阅时间：{item.get('time')}"
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }

        page_size = 8
        pages = [items[index:index + page_size] for index in range(0, len(items), page_size)]
        page_items = []
        for page_index, page_records in enumerate(pages, start=1):
            page_items.append({
                'component': 'VWindowItem',
                'content': [
                    {
                        'component': 'div',
                        'props': {
                            'class': 'd-flex justify-space-between align-center mb-2 text-caption text-medium-emphasis'
                        },
                        'content': [
                            {
                                'component': 'span',
                                'text': f"第 {page_index} / {len(pages)} 页"
                            },
                            {
                                'component': 'span',
                                'text': f"本页 {len(page_records)} 条"
                            }
                        ]
                    },
                    {
                        'component': 'div',
                        'props': {
                            'class': 'grid gap-3 grid-info-card'
                        },
                        'content': [history_card(item) for item in page_records]
                    }
                ]
            })

        return [
            {
                'component': 'VRow',
                'props': {
                    'class': 'mb-2'
                },
                'content': [
                    stat_card("订阅记录", f"{len(items)} 条", "primary"),
                    stat_card("电影", f"{movie_count} 条", "deep-orange"),
                    stat_card("剧集/综艺", f"{tv_count} 条", "indigo"),
                    stat_card("来源平台", f"{platform_count} 个", "teal"),
                ]
            },
            {
                'component': 'VWindow',
                'props': {
                    'show-arrows': 'hover'
                },
                'content': page_items
            }
        ]

    def stop_service(self):
        """
        停止服务
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            print(str(e))

    def __update_config(self):
        """
        列新配置
        """

        self.update_config({
            "enabled": self._enabled,
            "cron": self._cron,
            "clear": self._clear,
            "onlyonce": self._onlyonce,
            "type_movie": self._type_movie,
            "type_tv": self._type_tv,
            # 仅作兼容回写：综艺类型已并入 type_tv，此字段是它的派生视图
            "type_zongyi": self._type_zongyi,
            "movie_sub_num": self._movie_sub_num,
            "movie_rec_enabled": self._movie_rec_enabled,
            "tv_sub_num": self._tv_sub_num,
            "tv_rec_enabled": self._tv_rec_enabled,
            "zongyi_sub_num": self._zongyi_sub_num,
            "zongyi_rec_enabled": self._zongyi_rec_enabled,
            "tx_sub_num": self._tx_sub_num,
            "iqy_sub_num": self._iqy_sub_num,
            "mg_sub_num": self._mg_sub_num,
            "yk_sub_num": self._yk_sub_num,
            "tx_rec_enabled": self._tx_rec_enabled,
            "iqy_rec_enabled": self._iqy_rec_enabled,
            "mg_rec_enabled": self._mg_rec_enabled,
            "yk_rec_enabled": self._yk_rec_enabled,
            "api_enabled": self._api_enabled,
            "recommend_enabled": self._recommend_enabled,
        })

    def __refresh_maoyan(self):
        """
        刷新猫眼榜单数据
        电影获取的url:
        https://piaofang.maoyan.com/dashboard-ajax/movie
        电视剧获取的url:
        {tv_url}?showDate=20240223&seriesType=0&platformType=0
        参数 showDate: 时间具体到天
        参数 seriesType: 代表类型 0: 电视剧 1: 网络剧 2: 综艺；不传该参数时为电视剧+网络剧综合榜
        参数 platformType: 代表平台 0 全网 3 腾讯视频 2 爱奇艺 1 优酷 7 芒果 5 搜狐 4 乐视 6 PPTV
        """
        logger.info(f"开始刷新猫眼榜单...")
        # 获取当前日期时间
        current_time = datetime.datetime.now()
        # 各组抓取条数取「订阅条数」与「推荐条数」的较大者：
        # 订阅只取前「订阅条数」条，快照/推荐页保留抓取到的全部条目。
        movie_nums = max(self._movie_sub_num, self._movie_rec_num)
        #
        history: List[dict] = self.get_data('history') or []
        #
        movie_url = ''
        web_movie_url = ''
        tv_urls = []
        # 获取当前日期时间格式化为字符串
        show_date = datetime.datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y%m%d')
        maoyan_url = 'https://piaofang.maoyan.com'
        if movie_nums > 0 and 'movie' in self._type:
            movie_url = f'{maoyan_url}/dashboard-ajax/movie'
        if movie_nums > 0 and 'web-movie' in self._type:
            web_movie_url = (f'{maoyan_url}/i/api/encrypt/dashboard/webHeatNetData?'
                             f'showDate={show_date}&platformType=3&dateType=0&rankType=0')
        # 0: 电视剧 1: 网络剧 2: 综艺；不传 seriesType 时为电视剧+网络剧综合榜
        # 参数 platformType: 代表平台 0 全网 3 腾讯视频 2 爱奇艺 1 优酷 7 芒果
        tv_url = f'{maoyan_url}/i/api/encrypt/dashboard/webHeatData'
        # 订阅与推荐页显示相互独立：
        # - 全网榜只要勾选了榜单类型就抓取（供榜单快照/推荐页显示）；
        # - 平台细分榜：订阅条数只服务自动订阅；推荐条数 > 0 时该平台榜
        #   也写入榜单快照/推荐页（与全网榜同模型）；
        # - 每榜抓取条数取 max(订阅条数, 推荐条数)，订阅时只取前订阅条数条；
        #   某榜的订阅条数与推荐条数都为 0 时该榜完全不抓取。
        # tv_urls 元素: [抓取地址, 抓取条数, 订阅条数(None=该榜不参与订阅), 是否写入快照]
        # 剧集只有综合榜（不传 seriesType，猫眼返回电视剧+网络剧综合榜）与综艺榜
        for type_key, series_type, group in (('web-all', None, 'tv'),
                                             ('zongyi', 2, 'zongyi')):
            if type_key not in self._type:
                continue
            if group == 'tv':
                sub_num, rec_num = self._tv_sub_num, self._tv_rec_num
            else:
                sub_num, rec_num = self._zongyi_sub_num, self._zongyi_rec_num
            fetch_num = max(sub_num, rec_num)
            if fetch_num <= 0:
                continue
            series_query = '' if series_type is None else f'seriesType={series_type}&'
            # 全网榜：写入快照，供推荐页/对外接口展示
            tv_urls.append([
                f'{tv_url}?{series_query}showDate={show_date}',
                fetch_num,
                sub_num or None,
                True,
            ])
            # 平台细分（3 腾讯视频 2 爱奇艺 7 芒果 1 优酷）：订阅/推荐条数都为 0 不抓取
            for platform_type, (sub_key, rec_key) in self._PLATFORM_NUM_KEYS.items():
                platform_sub = getattr(self, f"_{sub_key}")
                platform_rec = getattr(self, f"_{rec_key}")
                platform_fetch = max(platform_sub, platform_rec)
                if platform_fetch <= 0:
                    continue
                tv_urls.append([
                    f'{tv_url}?{series_query}platformType={platform_type}&showDate={show_date}',
                    platform_fetch,
                    platform_sub or None,
                    platform_rec > 0,
                ])
        tv_list = []
        movie_list = []
        # 本次运行新识别的媒体身份映射，供榜单快照补全
        self._resolved_identity = {}
        # 本次识别出的媒体对象，订阅流程直接复用，避免同一标题识别两次
        self._resolved_mediainfo = {}
        # 清空上一次运行的榜单元数据，避免抓取失败时沿用旧榜单
        self._last_boards = []
        try:
            movie_list, tv_list, sub_movies, sub_tv = self.__get_url_info(
                movie_url, tv_urls, web_movie_url, movie_nums)
        except Exception as e:
            logger.error(f"获取猫眼榜单失败：{e}")
        # __get_url_info 会在内部重置榜单列表，抓取失败时此处回退为空列表
        last_boards = self._last_boards if isinstance(self._last_boards, list) else []
        # 影史总票房榜是独立 SSR 页面，与 dashboard 接口分开发抓取；
        # 只进快照/推荐页展示，不参与订阅
        if 'total' in self._type:
            total_board = self.__fetch_total_board(show_date, 'total')
            if total_board is not None:
                last_boards.append(total_board)
        # 国内年度票房榜与影史榜共用同一页面（?year= 区分口径）
        if 'year' in self._type:
            year_board = self.__fetch_total_board(show_date, 'year')
            if year_board is not None:
                last_boards.append(year_board)
        # 全球年度票房榜（Box Office Mojo）同样是独立站点，与猫眼 dashboard 分开发抓取；
        # 只进快照/推荐页展示，不参与订阅
        if 'global' in self._type:
            global_board = self.__fetch_global_board()
            if global_board is not None:
                last_boards.append(global_board)
        # 先为榜单快照识别媒体身份与海报：推荐页/对外接口不依赖订阅开关，
        # 订阅流程随后复用这里的识别结果，不再重复请求识别链。
        self.__resolve_board_identities(last_boards)
        subscribed_before = len(history)
        if self._enabled:
            self.set_sub(sub_movies, history, MediaType.MOVIE)
            self.set_sub(sub_tv, history, MediaType.TV)
        else:
            logger.info("订阅未启用，本次仅更新榜单快照（推荐页/对外接口数据）")
        # 保存历史记录
        self.save_data('history', history)
        # 保存榜单快照：接口读取的是一等公民的榜单数据，不再依赖订阅流水
        self.__save_snapshot(last_boards)
        logger.info(f"猫眼订阅刷新完成")
        return {
            "board_count": len(last_boards),
            "item_count": sum(board.get("count", 0) for board in last_boards),
            "subscribed": max(0, len(history) - subscribed_before),
        }

    def __save_snapshot(self, boards: List[dict]) -> None:
        """
        把本次抓取到的榜单落成快照。

        同时写入逐条识别出的媒体身份，让接口返回的条目可直接用于订阅与跳转；
        抓取全失败时保留上一次快照，避免接口在异常时段返回空数据。
        本轮没轮到识别的条目（识别限额截断）从上一份快照回填媒体身份与海报，
        否则整份覆盖会让已补到的图「得而复失」。
        """
        resolved = getattr(self, "_resolved_identity", {}) or {}
        # 上一份快照按标题建索引，用于回填本轮未识别条目
        prev_map: dict = {}
        try:
            prev = self.get_data("snapshot") or {}
            if isinstance(prev, dict):
                for pb in prev.get("boards") or []:
                    for pit in pb.get("items") or []:
                        t = pit.get("title")
                        if isinstance(t, str) and t and (pit.get("poster") or pit.get("media_id")):
                            prev_map[t] = pit
        except Exception as e:
            logger.warning(f"读取上一份榜单快照失败，跳过海报回填：{e}")
        try:
            for board in boards:
                for item in board.get("items", []):
                    title = item.get("title")
                    identity = resolved.get(title) if isinstance(title, str) else None
                    if identity:
                        item["media_source"] = identity.get("media_source", "")
                        item["media_id"] = identity.get("media_id", "")
                        item["poster"] = identity.get("poster", "")
                        item["overview"] = identity.get("overview", "")
                        item["year"] = identity.get("year", "")
                        item["title_cn"] = identity.get("title_cn", "")
                    elif title in prev_map:
                        # 本轮限额截断没轮到识别：沿用上一份快照的身份，海报不丢
                        p = prev_map[title]
                        item["media_source"] = p.get("media_source", "")
                        item["media_id"] = p.get("media_id", "")
                        item["poster"] = p.get("poster", "")
                        item["overview"] = p.get("overview", "")
                        item["year"] = p.get("year", "")
                        item["title_cn"] = p.get("title_cn", "")
                    else:
                        item["poster"] = ""
                        item["media_source"] = ""
                        item["media_id"] = ""
                        item["title_cn"] = ""
                    item["type_name"] = self._TYPE_NAMES.get(item.get("board_type", ""), item.get("board_type", ""))
        except Exception as e:
            logger.error(f"猫眼榜单快照整理失败，仍按原始条目保存：{e}")

        if not boards:
            logger.warning("本次未抓取到任何猫眼榜单，保留上一次快照")
            return

        snapshot = {
            "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total": sum(board.get("count", 0) for board in boards),
            "boards": boards,
        }
        self.save_data("snapshot", snapshot)

    def __resolve_board_identities(self, boards: List[dict]) -> None:
        """
        为榜单快照预识别媒体身份与海报（与订阅开关解耦）。

        推荐页与对外接口在订阅关闭时同样需要海报与媒体身份，因此在抓取后统一识别一次：
        同一标题只识别一次，结果同时供快照补全与订阅流程复用。

        识别结果持久化到 identity_cache（v3 每次调度重建插件实例，内存必丢）：
        - 已识别到媒体且拿到海报的条目跨轮长期复用，不再请求识别链；
        - 只有兜底海报（或全空）的条目 2 天后过期重试，新节目可能随后上架 TMDB；
        - 限额 [_RESOLVE_LIMIT] 只数本轮真正发起识别的新条目，缓存命中的直接放行。
        """
        if not boards:
            return
        if not hasattr(self, "_resolved_identity"):
            self._resolved_identity = {}
        if not hasattr(self, "_resolved_mediainfo"):
            self._resolved_mediainfo = {}
        current_time = datetime.datetime.now()
        # 恢复持久缓存：识别成功的长期有效，兜底/空结果按 TTL 过期后重试
        try:
            cached = self.get_data("identity_cache") or {}
            if isinstance(cached, dict):
                now_ts = current_time.timestamp()
                for t, entry in cached.items():
                    if not isinstance(t, str) or not t or not isinstance(entry, dict):
                        continue
                    if entry.get("media_source") and entry.get("poster"):
                        self._resolved_identity[t] = entry
                    elif now_ts - float(entry.get("ts") or 0) < self._IDENTITY_TTL_FALLBACK:
                        self._resolved_identity[t] = entry
        except Exception as e:
            logger.warning(f"读取识别缓存失败，本轮从零识别：{e}")
        resolved = 0
        truncated = False
        for board in boards:
            mtype = self._BOARD_MEDIA_TYPES.get(board.get("type"), MediaType.TV)
            for item in board.get("items", []):
                title = item.get("title")
                if not isinstance(title, str) or not title.strip():
                    continue
                if title in self._resolved_identity:
                    continue
                if resolved >= self._RESOLVE_LIMIT:
                    truncated = True
                    continue
                identity = self.__recognize_board_item(
                    title, item.get("releaseInfo"), mtype, current_time)
                resolved += 1
                if identity:
                    identity["ts"] = current_time.timestamp()
                    self._resolved_identity[title] = identity
        if truncated:
            logger.warning(f"本次待识别条目超过 {self._RESOLVE_LIMIT} 条，"
                           f"其余条目留待下次刷新补齐（识别结果已持久化，几轮内收敛）")
        # 回写持久缓存（只存身份字典，MediaInfo 对象不序列化）
        try:
            self.save_data("identity_cache", self._resolved_identity)
        except Exception as e:
            logger.warning(f"保存识别缓存失败：{e}")

    def __recognize_board_item(self, title: str, release_info, mtype: MediaType,
                               current_time: datetime.datetime) -> dict:
        """
        识别单个榜单条目，返回可写入快照的媒体身份（识别不到时至少补一张海报）。

        识别会先带年份尝试；失败则去掉年份重试（新剧在数据源里常按开播天数展示，
        推算出的年份反而会搜不到）。仍识别不到就用豆瓣检索补海报，保证推荐页有图。
        """
        year = self.__release_year(release_info, current_time)
        # 第一次带年份（走宿主识别缓存），失败后去掉年份再试一次并强制不用缓存
        for attempt_year in (year, None):
            if attempt_year is None and year is None:
                continue
            meta = MetaInfo(title)
            meta.year = attempt_year
            try:
                mediainfo: MediaInfo = self.chain.recognize_media(
                    meta=meta, mtype=mtype, cache=attempt_year is not None)
            except Exception as e:
                logger.error(f"猫眼条目 {title} 识别异常，继续处理下一条：{e}")
                continue
            if mediainfo:
                if attempt_year is None:
                    logger.info(f"猫眼条目 {title} 去掉年份后识别成功：{mediainfo.title_year}")
                self._resolved_mediainfo[title] = mediainfo
                # 英文榜单（如全球年度票房）需要中文名展示；与原标题一致时留空
                cn_title = (mediainfo.title or "").strip()
                return {
                    "media_source": mediainfo.media_source.value,
                    "media_id": str(mediainfo.media_id),
                    "poster": mediainfo.get_poster_image() or "",
                    "overview": mediainfo.overview or "",
                    "year": str(mediainfo.year or ""),
                    "type": mediainfo.type.value,
                    "title_cn": cn_title if cn_title and cn_title != title else "",
                }
        logger.warning(f"未识别到媒体信息，标题：{title}，年份：{year}")
        poster, cn_title = self.__douban_poster(title, mtype)
        if poster:
            logger.info(f"猫眼条目 {title} 未识别到媒体信息，已用豆瓣检索补齐海报")
        return {
            "media_source": "",
            "media_id": "",
            "poster": poster,
            "overview": "",
            "year": str(year or ""),
            "type": mtype.value,
            "title_cn": cn_title if cn_title and cn_title != title else "",
        }

    def __douban_poster(self, title: str, mtype: MediaType) -> tuple:
        """
        识别失败时用豆瓣检索兜底补一张海报（只补图，不提供媒体身份）。

        豆瓣图片有防盗链（无 Referer 返回 418），App 直连时同样需要补 Referer，
        宿主图片代理则会自行带上，因此这里只负责给出可用的图片地址；
        同时带回豆瓣词条标题（多为中文），供英文榜单条目转中文展示。
        返回 (poster_url, cn_title)，检索失败时两者为空串。
        """
        try:
            candidates = RequestUtils(
                headers={"User-Agent": self.get_random_user_agent()},
                proxies=getattr(settings, "PROXY", None),
                referer="https://movie.douban.com/",
            ).get_json("https://movie.douban.com/j/subject_suggest", params={"q": title})
        except Exception as e:
            logger.debug(f"猫眼条目 {title} 豆瓣补图请求失败：{e}")
            return "", ""
        if not isinstance(candidates, list):
            return "", ""
        # 优先同名词条；剧集优先带集数（豆瓣 suggestion 用 episode 标记剧集）
        same_title = [c for c in candidates if isinstance(c, dict) and c.get("title") == title]
        pool = same_title or [c for c in candidates if isinstance(c, dict)]
        if mtype == MediaType.TV:
            pool.sort(key=lambda c: 0 if c.get("episode") else 1)
        for candidate in pool:
            img = candidate.get("img")
            if isinstance(img, str) and img.startswith("http"):
                # 缩略图换中等尺寸，避免 App 里糊掉
                return (img.replace("/s_ratio_poster/", "/m_ratio_poster/"),
                        str(candidate.get("title") or "").strip())
        return "", ""

    def set_sub(self, addr_list, history, mtype):
        """
        将猫眼榜单条目识别为媒体信息，并添加到 MoviePilot 订阅。
        """
        # 获取当前日期时间
        current_time = datetime.datetime.now()
        # 本次运行中已识别的媒体身份，供榜单快照补全海报与来源
        if not hasattr(self, "_resolved_identity"):
            self._resolved_identity = {}
        for addr in addr_list:
            title = "未知标题"
            try:
                title = addr.get('title')
                if not isinstance(title, str) or not title.strip():
                    raise ValueError("缺少有效标题")
                year = self.__release_year(addr.get('releaseInfo'), current_time)
                # 元数据
                meta = MetaInfo(title)
                meta.year = year
                unique_flag = f"maoyanpro: {mtype}_{title}_{year}"
                # 检查是否已处理过
                if unique_flag in [h.get("unique") for h in history]:
                    continue
                # 匹配媒体信息（优先复用快照预识别的结果，避免重复请求识别链）
                mediainfo: MediaInfo = getattr(self, "_resolved_mediainfo", {}).get(title)
                if not mediainfo:
                    mediainfo = self.chain.recognize_media(meta=meta, mtype=mtype, cache=False)
                if not mediainfo:
                    logger.warning(f'未识别到媒体信息，标题：{title}，年份：{year}')
                    continue
                # 查询缺失的媒体信息
                exist_flag, _ = self.downloadchain.get_no_exists_info(meta=meta, mediainfo=mediainfo)
                if exist_flag:
                    logger.info(f'{mediainfo.title_year} 媒体库中已存在')
                    continue
                # 判断用户是否已经添加订阅
                if self.subscribechain.exists(mediainfo=mediainfo, meta=meta):
                    logger.info(f'{mediainfo.title_year} 订阅已存在')
                    continue
                # 添加订阅
                season = self.__resolve_tv_subscribe_season(title=title, meta=meta, mediainfo=mediainfo) \
                    if mtype == MediaType.TV else None
                self.subscribechain.add(title=mediainfo.title,
                                        year=mediainfo.year,
                                        mtype=mediainfo.type,
                                        media_source=mediainfo.media_source,
                                        media_id=mediainfo.media_id,
                                        season=season,
                                        exist_ok=True,
                                        username="猫眼订阅")
                # 存储历史记录
                history.append({
                    "title": title,
                    "releaseInfo": addr.get('releaseInfo'),
                    "platformDesc": addr.get('platformDesc', '未知'),
                    "type": mediainfo.type.value,
                    "year": mediainfo.year,
                    "poster": mediainfo.get_poster_image(),
                    "overview": mediainfo.overview,
                    "media_source": mediainfo.media_source.value,
                    "media_id": str(mediainfo.media_id),
                    "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "unique": unique_flag
                })
                # 记录本次识别结果，供榜单快照补全媒体身份
                self._resolved_identity[title] = {
                    "media_source": mediainfo.media_source.value,
                    "media_id": str(mediainfo.media_id),
                    "poster": mediainfo.get_poster_image() or "",
                    "overview": mediainfo.overview or "",
                    "year": str(mediainfo.year or ""),
                    "type": mediainfo.type.value,
                }
            except Exception as e:
                logger.error(f"猫眼{mtype.value}条目 {title} 处理失败，继续处理下一条：{e}")

    @staticmethod
    def __release_year(release_info, current_time: datetime.datetime) -> int | None:
        """仅从明确上映日期或已上映天数推算年份；缺失和未知文案保留未知年份。"""
        if not isinstance(release_info, str):
            return None
        release_info = release_info.strip()
        if release_info in ("上映首日", "上线首日", "开播首日", "今日上映", "今日上线", "今日开播"):
            return current_time.year
        date_match = re.match(r"^(\d{4})[-年/.](\d{1,2})[-月/.](\d{1,2})(?!\d)", release_info)
        days_match = re.fullmatch(r"(?:上映|上线|开播)\s*(\d+)\s*天", release_info)
        try:
            if date_match:
                return datetime.date(*(int(part) for part in date_match.groups())).year
            if days_match:
                return (current_time - datetime.timedelta(days=int(days_match.group(1)))).year
        except (ValueError, OverflowError):
            # 上游可能包含非法日期或异常大的天数，不应阻断媒体识别。
            return None
        # 纯年份（全球年度票房榜等来源只提供年份线索）
        if re.fullmatch(r"(?:19|20)\d{2}", release_info):
            return int(release_info)
        return None

    @staticmethod
    def __safe_int(value) -> int | None:
        """
        将季号字段安全转换为整数，转换失败时返回空值。
        """
        try:
            if value is None or value == "":
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def __get_season_number(cls, season_info) -> int | None:
        """
        从 TMDB 季信息对象或字典中提取有效季号。
        """
        if isinstance(season_info, dict):
            return cls.__safe_int(season_info.get("season_number"))
        return cls.__safe_int(getattr(season_info, "season_number", None))

    @classmethod
    def __latest_season_from_info(cls, season_infos) -> int | None:
        """
        从 TMDB 季列表中过滤第 0 季后取最新季号。
        """
        seasons = []
        for season_info in season_infos or []:
            season = cls.__get_season_number(season_info)
            if season is not None and season > 0:
                seasons.append(season)
        return max(seasons) if seasons else None

    @classmethod
    def __latest_season_from_media_seasons(cls, seasons) -> int | None:
        """
        从 MediaInfo.seasons 的季号键中过滤第 0 季后取最新季号。
        """
        season_numbers = []
        for season in (seasons or {}).keys():
            season_number = cls.__safe_int(season)
            if season_number is not None and season_number > 0:
                season_numbers.append(season_number)
        return max(season_numbers) if season_numbers else None

    def __latest_tmdb_season(self, mediainfo: MediaInfo) -> int | None:
        """
        查询 TMDB 或识别结果中的季信息，返回最新的有效季号。
        """
        tmdb_media_id = (
            mediainfo.media_id
            if normalize_media_source(mediainfo.media_source) == MediaSource.TMDB
            else None
        )
        if tmdb_media_id:
            try:
                latest_season = self.__latest_season_from_info(
                    TmdbChain().tmdb_seasons(tmdbid=tmdb_media_id)
                )
                if latest_season:
                    return latest_season
            except Exception as err:
                logger.warning(f"查询 TMDB 季信息失败，标题：{getattr(mediainfo, 'title', '')}，错误：{err}")

        latest_season = self.__latest_season_from_info(getattr(mediainfo, "season_info", None))
        if latest_season:
            return latest_season

        latest_season = self.__latest_season_from_media_seasons(getattr(mediainfo, "seasons", None))
        if latest_season:
            return latest_season

        return self.__safe_int(getattr(mediainfo, "number_of_seasons", None))

    def __resolve_tv_subscribe_season(self, title: str, meta: MetaInfo, mediainfo: MediaInfo) -> int | None:
        """
        决定猫眼剧集订阅季号：标题显式季号优先，否则使用 TMDB 最新季。
        """
        if meta.begin_season:
            return meta.begin_season

        latest_season = self.__latest_tmdb_season(mediainfo=mediainfo)
        if latest_season:
            meta.begin_season = latest_season
            logger.info(f"猫眼标题 {title} 未指定季号，按 TMDB 最新季 S{latest_season:02d} 订阅")
            return latest_season

        return None

    @classmethod
    def __board_type_from_url(cls, url: str) -> str:
        """
        按热度接口的 seriesType 判定榜单类型。

        不传 seriesType 时猫眼返回的是电视剧+网络剧综合榜（实测 seriesType=-1 不被支持），
        剧集统一走该综合榜，只有综艺（seriesType=2）是独立榜单，其余情况一律归为综合榜。
        """
        if not url:
            return 'web-all'
        query = dict(parse_qsl(urlsplit(url).query))
        return cls._SERIES_TYPES.get(query.get('seriesType', ''), 'web-all')

    def __board_meta(self, url: str, board: str, board_type: str, show_date: str) -> Dict[str, str]:
        """
        从榜单地址推导归属信息，供对外接口标识榜单。

        剧集热度接口里剧集是电视剧+网络剧综合榜、综艺用 seriesType 区分，平台用
        platformType 区分；电影票房与网络电影则是固定来源。
        """
        query = dict(parse_qsl(urlsplit(url).query)) if url else {}
        platform = self._PLATFORM_NAMES.get(query.get('platformType', ''), '')
        return {
            "board": board if not platform else f"{board}（{platform}）",
            "board_type": board_type,
            "board_url": url,
            "show_date": show_date,
            "platform": platform,
            # 猫眼 platformType 原值，供快照与推荐源按平台精确定位榜单；全网榜为空
            "platform_type": query.get('platformType', ''),
            "type_name": self._TYPE_NAMES.get(board_type, board_type),
        }

    def __get_url_info(self, movie_url, tv_urls, web_movie_url, num=10):
        """复用一个官网浏览器会话获取榜单，页面校验失败仅影响对应来源。"""
        movies_list = []
        tv_list = []
        boards = []
        # 记录本次抓取到的榜单元数据，供对外接口落快照；不动返回值以保证既有调用方不变。
        self._last_boards = boards
        show_date = datetime.datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d')
        sources = []
        # 每个来源都带抓取条数、订阅条数上限（None = 该榜不参与订阅、仅供推荐页展示）
        # 与是否写入榜单快照（平台细分榜只服务订阅，不落快照）
        if movie_url:
            sources.append({
                'url': movie_url,
                'limit': num,
                'name': '电影票房榜单',
                'data_key': 'movieList',
                'info_key': 'movieInfo',
                'title_key': 'movieName',
                'target': movies_list,
                'sub_limit': self._movie_sub_num or None,
                'snapshot': True,
                'meta': self.__board_meta(movie_url, '电影票房榜单', 'movie', show_date),
            })
        if web_movie_url:
            sources.append({
                'url': web_movie_url,
                'limit': num,
                'name': '网络电影榜单',
                'data_key': 'dataList',
                'info_key': None,
                'title_key': 'movieName',
                'target': movies_list,
                'sub_limit': self._movie_sub_num or None,
                'snapshot': True,
                'meta': self.__board_meta(web_movie_url, '网络电影榜单', 'web-movie', show_date),
            })
        for tv_url, tv_num, tv_sub_limit, tv_snapshot in tv_urls:
            board_type = self.__board_type_from_url(tv_url)
            board_name = self._TYPE_NAMES.get(board_type, '剧集热度榜单')
            sources.append({
                'url': tv_url,
                'limit': tv_num,
                'name': board_name,
                'data_key': 'dataList',
                'info_key': 'seriesInfo',
                'title_key': 'name',
                'target': tv_list,
                'sub_limit': tv_sub_limit,
                'snapshot': tv_snapshot,
                'meta': self.__board_meta(tv_url, board_name, board_type, show_date),
            })
        if not sources:
            return movies_list, tv_list, [], []

        sub_ranges = []
        processed = set()

        def collect(page=None):
            """按来源隔离请求；浏览器收尾异常时避免重复请求已经处理的榜单。"""
            headers = {'User-Agent': self.get_random_user_agent()}
            cookies = {}
            if page is not None:
                headers['User-Agent'] = page.evaluate('navigator.userAgent')
                cookies = {cookie['name']: cookie['value'] for cookie in page.context.cookies()}
            for index, source_info in enumerate(sources):
                if index in processed:
                    continue
                processed.add(index)
                url = source_info['url']
                limit = source_info['limit']
                data_key = source_info['data_key']
                info_key = source_info['info_key']
                title_key = source_info['title_key']
                target = source_info['target']
                meta = source_info['meta']
                sub_limit = source_info['sub_limit']
                source = f"{source_info['name']}（{url}）"
                try:
                    if page is not None and '/i/api/encrypt/dashboard/' in url:
                        payload = self.__request_browser_json(page, url)
                        if url == web_movie_url and isinstance(payload, dict) and payload.get('status') is False:
                            calendar = payload.get('calendarNet')
                            latest = calendar.get('selectMaxDate') if isinstance(calendar, dict) else None
                            if isinstance(latest, str):
                                latest = latest.replace('-', '')
                                if re.fullmatch(r'\d{8}', latest):
                                    datetime.datetime.strptime(latest, '%Y%m%d')
                                    parts = urlsplit(url)
                                    query = dict(parse_qsl(parts.query))
                                    if latest != query.get('showDate'):
                                        query['showDate'] = latest
                                        retry_url = urlunsplit(parts._replace(query=urlencode(query)))
                                        logger.info(f"猫眼网络电影榜单暂未提供当日数据，重试最新可用日期 {latest}")
                                        payload = self.__request_browser_json(page, retry_url)
                    else:
                        response = RequestUtils().get_res(url, cookies=cookies, headers=headers)
                        if response is None:
                            raise ValueError("未收到有效响应")
                        response.raise_for_status()
                        payload = response.json()
                    container = payload.get(data_key) if isinstance(payload, dict) else None
                    data = container.get('list') if isinstance(container, dict) else None
                    if not isinstance(data, list):
                        raise ValueError(f"响应缺少有效的 {data_key}.list 榜单列表")
                    items = self.__parse_rank_items(data, limit, info_key, title_key, source, meta=meta)
                    slice_start = len(target)
                    target.extend(items)
                    # 记录该来源参与订阅的切片（前 sub_limit 条）；快照仍保存全量条目
                    if sub_limit:
                        slice_end = slice_start + min(len(items), sub_limit)
                        if slice_end > slice_start:
                            sub_ranges.append((target, slice_start, slice_end))
                    if items and source_info['snapshot']:
                        boards.append({
                            "board": meta["board"],
                            "type": meta["board_type"],
                            "type_name": meta["type_name"],
                            "platform": meta["platform"],
                            "platform_type": meta.get("platform_type", ""),
                            "show_date": meta["show_date"],
                            "url": meta["board_url"],
                            "count": len(items),
                            "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "items": items,
                        })
                    logger.info(f"猫眼{source}获取到 {len(items)} 条有效记录")
                except Exception as err:
                    logger.error(f"猫眼{source}获取失败，继续处理其他榜单：{err}")

        try:
            self.__with_browser(collect)
        except Exception as err:
            logger.warning(f"猫眼浏览器会话异常：{err}")
        if len(processed) < len(sources):
            logger.warning("猫眼浏览器不可用，将继续无 Cookie 请求未处理榜单，热度接口可能拒绝访问")
            collect()

        # 同一剧集可能出现在多个平台，保留原有按标题去重的行为。
        deduped_tv = list({item['title']: item for item in tv_list}.values())
        # 订阅子集：只取各来源前「订阅条数」条，与快照/推荐页展示条数互不影响
        sub_movies = [item for target, start, end in sub_ranges if target is movies_list
                      for item in target[start:end]]
        sub_tv = list({item['title']: item
                       for target, start, end in sub_ranges if target is tv_list
                       for item in target[start:end]}.values())
        return movies_list, deduped_tv, sub_movies, sub_tv

    @staticmethod
    def __request_browser_json(page, url):
        """在官网页面内请求当前接口，由网站自身脚本维护会话与请求校验信息。"""
        return page.evaluate("""async url => {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 15000);
            try {
                const headers = {Accept: 'application/json, text/plain, */*'};
                for (const [name, key] of [['csrf', 'uid'], ['deviceId', 'uuid'], ['channelId', 'channelId']]) {
                    const value = document.querySelector(`meta[name="${name}"]`)?.content;
                    if (value) headers[key] = value;
                }
                const response = await fetch(url, {
                    headers, credentials: 'same-origin', signal: controller.signal,
                });
                if (!response.ok) throw new Error(`HTTP ${response.status} ${response.statusText}`);
                return await response.json();
            } finally {
                clearTimeout(timer);
            }
        }""", url)

    @staticmethod
    def __split_unit(value) -> str:
        """把猫眼的 {num, unit} 量值结构拼成可读文本，无法识别时返回空串。"""
        if isinstance(value, dict):
            num = value.get('num')
            unit = value.get('unit')
            if num is None:
                return ""
            return f"{num}{unit or ''}".strip()
        if isinstance(value, str):
            return value.strip()
        return ""

    @classmethod
    def __rank_metrics(cls, item: dict, info: dict) -> Dict[str, str]:
        """
        从榜单原始条目中提取量值与热度描述。

        猫眼各类榜单的字段名并不统一：票房榜用 boxSplitUnit/boxRate，热度榜用
        currHeatDesc/barValue/viewCountDesc。这里按已知字段逐个尝试，取不到就返回空串，
        不影响榜单本身的解析。
        """
        box_office = ""
        for key in ('boxSplitUnit', 'boxDesc', 'playCountSplitUnit', 'splitBoxSplitUnit'):
            box_office = cls.__split_unit(item.get(key))
            if box_office:
                break

        # 累计票房（总票房）：票房榜原始条目自带 sumBoxDesc，如「3.30亿」；其余榜单无此字段
        total_box_office = str(item.get('sumBoxDesc') or "").strip()

        heat = ""
        for key in ('currHeatDesc', 'heatDesc', 'boxRate', 'barValue', 'viewCountDesc'):
            raw = item.get(key)
            if isinstance(raw, str) and raw.strip():
                heat = raw.strip()
                break

        return {
            "box_office": box_office,
            "total_box_office": total_box_office,
            "heat": heat,
            "show_count": str(item.get('showCount') or "").strip(),
            "release_info": str(info.get('releaseInfo') or "").strip(),
            "platform": str(info.get('platformDesc') or info.get('platformTxt') or "").strip(),
        }

    @classmethod
    def __parse_rank_items(cls, data, limit, info_key, title_key, source, meta=None) -> List[dict]:
        """仅解析配置排名范围内的条目，保留同榜有效数据并记录坏条目的位置。"""
        meta = meta or {}
        items = []
        for rank, item in enumerate(data[:limit], start=1):
            try:
                if not isinstance(item, dict):
                    raise ValueError("条目不是对象")
                info = item.get(info_key) if info_key else item
                if not isinstance(info, dict):
                    raise ValueError("缺少媒体详情")
                title = info.get(title_key)
                if not isinstance(title, str) or not title.strip():
                    raise ValueError("缺少有效标题")
                metrics = cls.__rank_metrics(item, info)
                items.append({
                    "title": title.strip(),
                    "releaseInfo": info.get('releaseInfo'),
                    "platformDesc": info.get('platformDesc'),
                    # 榜单归属与量值：供对外接口与推荐源直接渲染，订阅逻辑不使用
                    "rank": rank,
                    "board": meta.get("board", source),
                    "board_type": meta.get("board_type", ""),
                    "board_url": meta.get("board_url", ""),
                    "show_date": meta.get("show_date", ""),
                    "box_office": metrics["box_office"],
                    "total_box_office": metrics["total_box_office"],
                    "heat": metrics["heat"],
                    "show_count": metrics["show_count"],
                })
            except (TypeError, ValueError) as err:
                logger.warning(f"猫眼{source}第 {rank} 条数据无效，跳过该条目：{err}")
        return items

    @staticmethod
    def __format_wan_amount(wan_value) -> str:
        """把猫眼票房数值（单位：万元）拼成可读文本，如 1544614 -> 154.46亿。"""
        try:
            wan = int(str(wan_value).strip())
        except (TypeError, ValueError):
            return ""
        if wan < 0:
            return ""
        if wan >= 10000:
            return f"{wan / 10000:.2f}亿"
        return f"{wan}万"

    @classmethod
    def __parse_total_board(cls, html: str, limit: int, source: str, meta: dict) -> List[dict]:
        """
        解析影史总票房榜（SSR 直出页面，无需签名）。

        页面结构为 ul.row 列表：col0 名次、col1 片名+上映日期、col2 票房（万元）、
        col3 平均票价、col4 场均人次。这里只取榜单需要的字段，解析失败跳过该行，
        不影响其余条目。
        """
        meta = meta or {}
        items = []
        for rank, row in enumerate(
                re.findall(r"<ul class=\"row\"[^>]*>(.*?)</ul>", html or "", re.S)[:limit],
                start=1):
            if 'first-line' not in row:
                continue  # 表头行（排名/片名/票房…）没有条目内容，静默跳过
            try:
                cells = [re.sub(r"<[^>]+>", " ", cell).strip()
                         for cell in re.findall(r"<li class=\"col[^\"]*\">(.*?)</li>", row, re.S)]
                if len(cells) < 3:
                    raise ValueError("缺少有效单元格")
                # col1 同时装着片名与上映日期两个 <p>，但标签剥离后已合在一起；
                # 直接从原始行里分别取 first-line / second-line
                title_m = re.search(r"class=\"first-line\">(.*?)</p>", row, re.S)
                release_m = re.search(r"class=\"second-line\">(.*?)</p>", row, re.S)
                title = re.sub(r"\s+", " ", title_m.group(1)).strip() if title_m else ""
                release = re.sub(r"\s+", " ", release_m.group(1)).strip() if release_m else ""
                if not title:
                    raise ValueError("缺少有效标题")
                page_rank = cls.__safe_int(cells[0]) or rank
                items.append({
                    "title": title,
                    "releaseInfo": release,
                    "platformDesc": "",
                    "rank": page_rank,
                    "board": meta.get("board", source),
                    "board_type": meta.get("board_type", ""),
                    "board_url": meta.get("board_url", ""),
                    "show_date": meta.get("show_date", ""),
                    "box_office": cls.__format_wan_amount(cells[2]),
                    "total_box_office": cls.__format_wan_amount(cells[2]),
                    "heat": "",
                    "show_count": "",
                })
            except (TypeError, ValueError) as err:
                logger.warning(f"猫眼{source}第 {rank} 条数据无效，跳过该条目：{err}")
        return items

    def __fetch_total_board(self, show_date: str, board_type: str = "total") -> dict | None:
        """
        抓取国内总票房榜（https://piaofang.maoyan.com/rankings/year）。

        board_type='total' 为影史总票房榜（不带年份参数），'year' 为国内年度票房榜
        （带当前年份参数，猫眼按 ?year=YYYY 返回对应年度榜）。该页面为服务端直出
        HTML，无需浏览器会话与签名校验；失败时返回 None，快照保留其余榜单数据。
        """
        year = datetime.datetime.now().year
        board_name = '影史总票房榜单' if board_type == 'total' else '国内年度票房榜单'
        url = 'https://piaofang.maoyan.com/rankings/year'
        if board_type == 'year':
            url = f'{url}?year={year}'
        source = f"{board_name}（{url}）"
        meta = self.__board_meta(url, board_name, board_type, show_date)
        try:
            response = RequestUtils(ua=self.get_random_user_agent()).get_res(url)
            if response is None:
                raise ValueError("未收到有效响应")
            response.raise_for_status()
            items = self.__parse_total_board(
                response.text, self._TOTAL_BOARD_FETCH_NUM, source, meta)
        except Exception as err:
            logger.error(f"猫眼{source}获取失败，跳过该榜单：{err}")
            return None
        if not items:
            logger.warning(f"猫眼{source}未解析到有效条目，跳过该榜单")
            return None
        logger.info(f"猫眼{source}获取到 {len(items)} 条有效记录")
        return {
            "board": meta["board"],
            "type": meta["board_type"],
            "type_name": meta["type_name"],
            "platform": meta["platform"],
            "platform_type": meta.get("platform_type", ""),
            "show_date": meta["show_date"],
            "url": meta["board_url"],
            "count": len(items),
            "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "items": items,
        }

    @staticmethod
    def __format_usd_amount(raw_value) -> str:
        """把 Box Office Mojo 票房数值（美元）拼成可读文本，如 2513503453 -> 25.14亿美元。"""
        try:
            usd = int(str(raw_value).replace("$", "").replace(",", "").strip())
        except (TypeError, ValueError):
            return ""
        if usd <= 0:
            return ""
        if usd >= 100000000:
            return f"{usd / 100000000:.2f}亿美元"
        if usd >= 10000:
            return f"{usd / 10000:.1f}万美元"
        return f"{usd}美元"

    @classmethod
    def __parse_global_board(cls, html: str, limit: int, source: str, meta: dict) -> List[dict]:
        """
        解析全球年度票房榜（Box Office Mojo /year/world/{year}/，SSR 直出表格）。

        每行 7 列：名次、片名、全球票房、北美票房、北美占比、海外票房、海外占比。
        首行为表头（无 td），静默跳过；解析失败的行跳过，不影响其余条目。
        条目语言为英文原名片名，媒体识别走 TMDB 原生支持，海报可正常补全。
        """
        meta = meta or {}
        items = []
        # 先过滤表头行（无 td）再截断，避免表头占用抓取名额
        rows = [row for row in re.findall(r"<tr>(.*?)</tr>", html or "", re.S)
                if "<td" in row]
        for rank, row in enumerate(rows[:limit], start=1):
            try:
                cells = [re.sub(r"<[^>]+>", " ", cell).strip()
                         for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
                if len(cells) < 3:
                    raise ValueError("缺少有效单元格")
                title = re.sub(r"\s+", " ", cells[1]).strip()
                if not title:
                    raise ValueError("缺少有效标题")
                # releaseInfo 只用于媒体识别的年份线索，放榜单年份即可
                release = str(meta.get("show_year") or "")
                page_rank = cls.__safe_int(cells[0]) or rank
                worldwide = cls.__format_usd_amount(cells[2])
                items.append({
                    "title": title,
                    "releaseInfo": release,
                    "platformDesc": "",
                    "rank": page_rank,
                    "board": meta.get("board", source),
                    "board_type": meta.get("board_type", ""),
                    "board_url": meta.get("board_url", ""),
                    "show_date": meta.get("show_date", ""),
                    "box_office": worldwide,
                    "total_box_office": worldwide,
                    "heat": "",
                    "show_count": "",
                })
            except (TypeError, ValueError) as err:
                logger.warning(f"{source}第 {rank} 行数据无效，跳过该条目：{err}")
        return items

    def __fetch_global_board(self) -> dict | None:
        """
        抓取全球年度票房榜（https://www.boxofficemojo.com/year/world/{年份}/）。

        猫眼网页端无全球票房数据（专业版 App 独有、接口需 H5guard 签名），改用
        IMDb 系 Box Office Mojo 的年度全球榜，SSR 直出无需签名；失败时返回 None，
        快照保留其余榜单数据。
        """
        year = datetime.datetime.now().year
        url = f"https://www.boxofficemojo.com/year/world/{year}/"
        source = f"全球年度票房榜（{url}）"
        meta = self.__board_meta(url, '全球年度票房榜单', 'global',
                                 datetime.datetime.now().strftime("%Y-%m-%d"))
        meta["show_year"] = year
        try:
            response = RequestUtils(
                ua=self.get_random_user_agent(),
                headers={"Accept-Language": "en-US,en;q=0.9"},
            ).get_res(url)
            if response is None:
                raise ValueError("未收到有效响应")
            response.raise_for_status()
            items = self.__parse_global_board(
                response.text, self._GLOBAL_BOARD_FETCH_NUM, source, meta)
        except Exception as err:
            logger.error(f"{source}获取失败，跳过该榜单：{err}")
            return None
        if not items:
            logger.warning(f"{source}未解析到有效条目，跳过该榜单")
            return None
        logger.info(f"{source}获取到 {len(items)} 条有效记录")
        return {
            "board": meta["board"],
            "type": meta["board_type"],
            "type_name": meta["type_name"],
            "platform": meta["platform"],
            "platform_type": meta.get("platform_type", ""),
            "show_date": meta["show_date"],
            "url": meta["board_url"],
            "count": len(items),
            "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "items": items,
        }

    @staticmethod
    def get_random_user_agent():
        """从桌面浏览器标识中选择请求头。"""
        user_agents = [
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        ]
        return random.choice(user_agents)

    @staticmethod
    def __with_browser(callback):
        """先加载官网校验脚本，再在同一上下文内获取各榜单并可靠释放浏览器。"""
        context = launch_browser_context(headless=True)
        page = None
        try:
            page = context.new_page()
            page.goto('https://piaofang.maoyan.com/dashboard/web-heat',
                      wait_until='load', timeout=30000)
            return callback(page)
        finally:
            try:
                if page is not None:
                    page.close()
            finally:
                context.close()

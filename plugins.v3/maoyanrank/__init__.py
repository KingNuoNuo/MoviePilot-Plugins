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
from app.sdk.events import eventmanager, Event
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

class MaoyanRankItem(BaseModel):
    """猫眼榜单中的单个条目。"""

    rank: int = Field(..., description="榜单名次，从 1 开始")
    title: str = Field(..., description="媒体标题")
    type: str = Field("", description="榜单类型：movie/web-movie/web-heat/web-tv/zongyi")
    type_name: str = Field("", description="榜单类型中文名")
    platform: str = Field("", description="平台名称，如全网、腾讯视频")
    release_info: str = Field("", description="上线信息，如「上映 11 天」")
    show_date: str = Field("", description="榜单日期，格式 YYYY-MM-DD")
    board: str = Field("", description="榜单名称")
    box_office: str = Field("", description="当日票房或热度值等榜单量值")
    heat: str = Field("", description="热度/票房占比等榜单热度描述")
    media_source: str = Field("", description="主媒体来源，未识别时为空")
    media_id: str = Field("", description="主媒体 ID，未识别时为空")
    poster: str = Field("", description="海报地址，未识别时为空")
    overview: str = Field("", description="剧情简介，未识别时为空")
    year: str = Field("", description="年份，未识别时为空")


class MaoyanRankBoard(BaseModel):
    """猫眼单个榜单的最新快照。"""

    board: str = Field(..., description="榜单名称")
    type: str = Field("", description="榜单类型标识")
    type_name: str = Field("", description="榜单类型中文名")
    platform: str = Field("", description="平台名称")
    show_date: str = Field("", description="榜单日期")
    url: str = Field("", description="榜单来源地址")
    count: int = Field(0, description="榜单条目数")
    updated_at: str = Field("", description="快照时间")
    items: List[MaoyanRankItem] = Field(default_factory=list, description="榜单条目")


class MaoyanRankSnapshot(BaseModel):
    """猫眼榜单快照汇总数据。"""

    updated_at: str = Field("", description="最近一次刷新的快照时间")
    total: int = Field(0, description="返回的条目总数")
    boards: List[MaoyanRankBoard] = Field(default_factory=list, description="榜单列表")


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
            "box_office": str(row.get("box_office") or ""),
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
        "show_date": text("show_date"),
        "url": text("url"),
        "count": len(items),
        "updated_at": text("updated_at"),
        "items": items,
    }


class MaoyanRank(_PluginBase):
    """
    获取猫眼的排行榜进行订阅,建议每天一次

    Fork 自 jxxghp/moviepilot-plugins 的 plugins.v3/maoyanrank
    (上游原插件作者: 逗猫 @baozaodetudou, 上游版本 v4.1.4)；
    本仓库在此基础上新增榜单对外接口、榜单快照与推荐页数据源，
    版本号自本仓库起从 v1.0.0 独立计数。

    电影获取的url: https://piaofang.maoyan.com/dashboard-ajax/movie
    电视剧获取的url: {tv_url}?showDate=20240223&seriesType=0&platformType=0
        参数 showDate: 时间具体到天
        参数 seriesType: 代表类型 0: 电视剧 1: 网络剧 2: 综艺 不传递-1代表电视剧+网络剧
        参数 platformType: 代表平台 0 全网 3 腾讯视频 2 爱奇艺 1 优酷 7 芒果 5 搜狐 4 乐视 6 PPTV 20 网络电影networkHot=3

    详情链接:
    https://piaofang.maoyan.com/dashboard/movie?movieId=1489349
    https://piaofang.maoyan.com/dashboard/web-heat?movieId=1484643

    """
    # 插件名称
    plugin_name = "猫眼榜单订阅"
    # 插件描述
    plugin_desc = "监控猫眼数据，自动添加订阅。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/KingNuoNuo/MoviePilot-Plugins/main/icons/maoyan.png"
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "KingNuoNuo"
    # 作者主页
    author_url = "https://github.com/KingNuoNuo"
    # 插件配置项ID前缀
    plugin_config_prefix = "maoyanrank_"
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
    # type细分 movie: 电影 web-heat 电视榜单 web-tv 网剧 zongyi 综艺  web-movie 网络电影
    _type = ['movie', 'web-heat', 'web-tv', 'zongyi', 'web-movie']

    _num = 10
    _all_enabled: bool = False
    _all_num = 10
    _tx_enabled: bool = False
    _tx_num = 10
    _iqy_enabled: bool = False
    _iqy_num = 10
    _mg_enabled: bool = False
    _mg_num = 10
    _yk_enabled: bool = False
    _yk_num = 10

    # 对外接口与推荐数据源开关
    _api_enabled = True
    _recommend_enabled = False

    # 榜单类型 -> 中文名；用于对外接口与推荐源的展示名称
    _TYPE_NAMES = {
        'movie': '电影票房',
        'web-movie': '网络电影',
        'web-heat': '剧集热度',
        'web-tv': '网络剧热度',
        'zongyi': '综艺热度',
    }

    # 榜单类型 -> 推荐页分类，取值与宿主前端 recommend.category* 的文案保持一致
    _RECOMMEND_TYPES = {
        'movie': '电影',
        'web-movie': '电影',
        'web-heat': '电视剧',
        'web-tv': '电视剧',
        'zongyi': '电视剧',
    }

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

    # 猫眼热度接口的 seriesType -> 榜单类型
    _SERIES_TYPES = {'0': 'web-heat', '1': 'web-tv', '2': 'zongyi'}

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

            self._type = config.get("type")
            self._num = config.get("num", 10)

            self._all_enabled = config.get("all_enabled", False)
            self._tx_enabled = config.get("tx_enabled", False)
            self._iqy_enabled = config.get("iqy_enabled", False)
            self._mg_enabled = config.get("mg_enabled", False)
            self._yk_enabled = config.get("yk_enabled", False)
            self._all_num = config.get("all_num", 10)
            self._tx_num = config.get("tx_num", 10)
            self._iqy_num = config.get("iqy_num", 10)
            self._mg_num = config.get("mg_num", 10)
            self._yk_num = config.get("yk_num", 10)

            self._api_enabled = config.get("api_enabled", True)
            self._recommend_enabled = config.get("recommend_enabled", False)

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

        if self._enabled or self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            # 周期执行
            if self._cron:
                logger.info(f"猫眼榜单订阅服务启动，周期：{self._cron}")
                try:
                    self._scheduler.add_job(func=self.__refresh_maoyan,
                                            trigger=CronTrigger.from_crontab(self._cron),
                                            name="猫眼榜单订阅")
                except Exception as e:
                    logger.error(f"猫眼榜单订阅服务启动失败，错误信息：{str(e)}")
                    self.systemmessage.put(f"猫眼榜单订阅服务启动失败，错误信息：{str(e)}")
            else:
                self._scheduler.add_job(func=self.__refresh_maoyan, trigger=CronTrigger.from_crontab("0 9 * * *"),
                                        name="猫眼榜单订阅")
                logger.info("猫眼榜单订阅服务启动，周期：每天 09:00")

            # 一次性执行
            if self._onlyonce:
                logger.info("猫眼榜单订阅服务启动，立即运行一次")
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
        """返回插件启用状态。"""
        return self._enabled

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

        认证沿用宿主默认的 API 令牌（`X-API-KEY` 头或 `apikey` 查询参数），
        因此 App 的 API 令牌模式也能读取，不再依赖网页登录态。
        """
        return [
            {
                "path": "/rank",
                "endpoint": self.get_rank_snapshot,
                "methods": ["GET"],
                "summary": "猫眼榜单快照",
                "description": "返回最近一次抓取的猫眼榜单快照，可按榜单类型筛选条目数量",
                "response_model": schemas.Response[MaoyanRankSnapshot],
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
                          board: str = None) -> schemas.Response[MaoyanRankSnapshot]:
        """
        返回最近一次抓取的猫眼榜单快照。

        :param board_type: 仅返回指定榜单类型（movie/web-movie/web-heat/web-tv/zongyi）
        :param limit: 每个榜单最多返回的条目数，用于限制响应体大小
        :param board: 仅返回榜单名包含该关键字的榜单（如「腾讯视频」）
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
            rows = item.get("items") or []
            if limit and limit > 0:
                rows = rows[:limit]
            selected.append(MaoyanRankBoard(**_safe_board_payload(item, rows)))

        return schemas.Response(
            success=True,
            message="",
            data=MaoyanRankSnapshot(
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

    def __snapshot_from_history(self) -> MaoyanRankSnapshot:
        """
        用订阅流水兜底构造榜单快照。

        历史记录只有已识别并入订阅的条目，没有名次与量值，因此统一按单榜单返回，
        并标注为兜底数据，便于调用方区分。
        """
        history = self.get_data("history") or []
        if not isinstance(history, list):
            return MaoyanRankSnapshot()
        history = sorted(
            [row for row in history if isinstance(row, dict)],
            key=lambda row: row.get("time") or "",
            reverse=True,
        )
        items = [
            MaoyanRankItem(
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
            return MaoyanRankSnapshot()
        return MaoyanRankSnapshot(
            updated_at=str(history[0].get("time") or ""),
            total=len(items),
            boards=[
                MaoyanRankBoard(
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
    def recommend_source(self, event: Event):
        """把猫眼榜单注册为宿主推荐页的数据源。"""
        if not (self._enabled and self._recommend_enabled and self._api_enabled):
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
        for board_type, label in (('web-heat', '猫眼电视剧热度'),
                                  ('web-tv', '猫眼网络剧热度'),
                                  ('zongyi', '猫眼综艺热度')):
            if board_type in (self._type or []):
                sources.append(schemas.RecommendMediaSource(
                    name=label,
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

    def rank_list(self, board_type: str = "web-heat", page: int = 1,
                  limit: int = None) -> schemas.Response[List[schemas.MediaInfo]]:
        """
        推荐页/浏览页使用的榜单列表接口。

        宿主列表视图按 page 翻页，并在返回空或与上一页相同时终止，因此这里把单榜单
        视作一页数据返回，翻到第二页即给出空列表。
        """
        if page and page > 1:
            return schemas.Response(success=True, data=[])

        snapshot = self.get_data("snapshot") or {}
        boards = snapshot.get("boards") if isinstance(snapshot, dict) else None
        rows = []
        if isinstance(boards, list):
            for item in boards:
                if isinstance(item, dict) and item.get("type") == board_type:
                    rows = item.get("items") or []
                    break

        result: List[schemas.MediaInfo] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            media_source, media_id = resolve_media_identity(
                media_source=row.get("media_source"),
                media_id=row.get("media_id"),
            )
            media = schemas.MediaInfo(
                media_source=media_source,
                media_id=media_id,
                title=str(row.get("title") or ""),
                year=str(row.get("year") or "") or None,
                type=str(row.get("type_name") or self._TYPE_NAMES.get(board_type, "")),
                poster_path=str(row.get("poster") or "") or None,
                overview=str(row.get("overview") or "") or None,
            )
            if media.title and media.year:
                media.title_year = f"{media.title} ({media.year})"
            result.append(media)
            if limit and limit > 0 and len(result) >= limit:
                break
        return schemas.Response(success=True, data=result)

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """提供榜单类型、平台、数量和执行周期配置。"""
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '5位cron表达式，留空自动'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'clear',
                                            'label': '清理历史记录',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 22,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'chips': True,
                                            'multiple': True,
                                            'model': 'type',
                                            'label': '订阅类型',
                                            'items': [
                                                {'title': '电影票房榜单', 'value': 'movie'},
                                                {'title': '电视剧热度榜单', 'value': 'web-heat'},
                                                {'title': '网剧热度榜单', 'value': 'web-tv'},
                                                {'title': '综艺热度榜单', 'value': 'zongyi'},
                                                {'title': '网络电影榜单', 'value': 'web-movie'},
                                            ]
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 22,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': False,
                                            'chips': True,
                                            'model': 'num',
                                            'label': '电影榜单条数',
                                            'items': [
                                                {'title': '1', 'value': 1},
                                                {'title': '2', 'value': 2},
                                                {'title': '3', 'value': 3},
                                                {'title': '5', 'value': 5},
                                                {'title': '7', 'value': 7},
                                                {'title': '10', 'value': 10}
                                            ]
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '下边针对电视剧，网剧，综艺的细分类进行设置不开启则不订阅电视剧；'
                                                    '控制是并行的都打开会都进行订阅当然重复会进行过滤。'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'all_enabled',
                                            'label': '全网热门订阅',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': False,
                                            'chips': True,
                                            'model': 'all_num',
                                            'label': '榜单条数',
                                            'items': [
                                                {'title': '1', 'value': 1},
                                                {'title': '2', 'value': 2},
                                                {'title': '3', 'value': 3},
                                                {'title': '5', 'value': 5},
                                                {'title': '7', 'value': 7},
                                                {'title': '10', 'value': 10}
                                            ]
                                        }
                                    }
                                ]
                            }

                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'tx_enabled',
                                            'label': '腾讯热门订阅',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': False,
                                            'chips': True,
                                            'model': 'tx_num',
                                            'label': '腾讯榜单条数',
                                            'items': [
                                                {'title': '1', 'value': 1},
                                                {'title': '2', 'value': 2},
                                                {'title': '3', 'value': 3},
                                                {'title': '5', 'value': 5},
                                                {'title': '7', 'value': 7},
                                                {'title': '10', 'value': 10}
                                            ]
                                        }
                                    }
                                ]
                            }

                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'iqy_enabled',
                                            'label': '爱奇艺热门订阅',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': False,
                                            'chips': True,
                                            'model': 'iqy_num',
                                            'label': '爱奇艺榜单条数',
                                            'items': [
                                                {'title': '1', 'value': 1},
                                                {'title': '2', 'value': 2},
                                                {'title': '3', 'value': 3},
                                                {'title': '5', 'value': 5},
                                                {'title': '7', 'value': 7},
                                                {'title': '10', 'value': 10}
                                            ]
                                        }
                                    }
                                ]
                            }

                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'mg_enabled',
                                            'label': '芒果热门订阅',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': False,
                                            'chips': True,
                                            'model': 'mg_num',
                                            'label': '芒果榜单条数',
                                            'items': [
                                                {'title': '1', 'value': 1},
                                                {'title': '2', 'value': 2},
                                                {'title': '3', 'value': 3},
                                                {'title': '5', 'value': 5},
                                                {'title': '7', 'value': 7},
                                                {'title': '10', 'value': 10}
                                            ]
                                        }
                                    }
                                ]
                            }

                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'yk_enabled',
                                            'label': '优酷热门订阅',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': False,
                                            'chips': True,
                                            'model': 'yk_num',
                                            'label': '优酷榜单条数',
                                            'items': [
                                                {'title': '1', 'value': 1},
                                                {'title': '2', 'value': 2},
                                                {'title': '3', 'value': 3},
                                                {'title': '5', 'value': 5},
                                                {'title': '7', 'value': 7},
                                                {'title': '10', 'value': 10}
                                            ]
                                        }
                                    }
                                ]
                            }

                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'api_enabled',
                                            'label': '开放榜单接口',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'recommend_enabled',
                                            'label': '接入推荐页数据源',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "cron": "",
            "clear": False,
            "type": ['movie', 'web-heat', 'web-tv', 'zongyi', 'web-movie'],
            "num": 10,
            "all_enabled": False,
            "tx_enabled": False,
            "iqy_enabled": False,
            "mg_enabled": False,
            "yk_enabled": False,
            "all_num": 10,
            "tx_num": 10,
            "iqy_num": 10,
            "yk_num": 10,
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
            "type": self._type,
            "num": self._num,
            "all_enabled": self._all_enabled,
            "tx_enabled": self._tx_enabled,
            "iqy_enabled": self._iqy_enabled,
            "mg_enabled": self._mg_enabled,
            "yk_enabled": self._yk_enabled,
            "all_num": self._all_num,
            "tx_num": self._tx_num,
            "iqy_num": self._iqy_num,
            "mg_num": self._mg_num,
            "yk_num": self._yk_num,
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
        参数 seriesType: 代表类型 0: 电视剧 1: 网络剧 2: 综艺 不传递-1代表电视剧+网络剧
        参数 platformType: 代表平台 0 全网 3 腾讯视频 2 爱奇艺 1 优酷 7 芒果 5 搜狐 4 乐视 6 PPTV
        """
        logger.info(f"开始刷新猫眼榜单...")
        # 获取当前日期时间
        current_time = datetime.datetime.now()
        nums = self._num
        #
        history: List[dict] = self.get_data('history') or []
        #
        movie_url = ''
        web_movie_url = ''
        tv_urls = []
        # 获取当前日期时间格式化为字符串
        show_date = datetime.datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y%m%d')
        maoyan_url = 'https://piaofang.maoyan.com'
        if 'movie' in self._type:
            movie_url = f'{maoyan_url}/dashboard-ajax/movie'
        if 'web-movie' in self._type:

            web_movie_url = (f'{maoyan_url}/i/api/encrypt/dashboard/webHeatNetData?'
                             f'showDate={show_date}&platformType=3&dateType=0&rankType=0')
        # 0: 电视剧  1: 网络剧 2: 综艺 不传递-1代表电视剧+网络剧
        # 参数 platformType: 代表平台 0 全网 3 腾讯视频 2 爱奇艺 1 优酷 7 芒果
        # 电视剧
        tv_url = f'{maoyan_url}/i/api/encrypt/dashboard/webHeatData'
        if 'web-heat' in self._type:
            # 全网
            if self._all_enabled:
                url = f'{tv_url}?seriesType=0&showDate={show_date}'
                tv_urls.append([url, self._all_num])
            # tx
            if self._tx_enabled:
                url = f'{tv_url}?seriesType=0&platformType=3&showDate={show_date}'
                tv_urls.append([url, self._tx_num])
            # iqy
            if self._iqy_enabled:
                url = f'{tv_url}?seriesType=0&platformType=2&showDate={show_date}'
                tv_urls.append([url, self._iqy_num])
            # mg
            if self._mg_enabled:
                url = f'{tv_url}?seriesType=0&platformType=7&showDate={show_date}'
                tv_urls.append([url, self._mg_num])
            # yk
            if self._yk_enabled:
                url = f'{tv_url}?seriesType=0&platformType=1&showDate={show_date}'
                tv_urls.append([url, self._yk_num])
        # 网剧
        if 'web-tv' in self._type:
            # 全网
            if self._all_enabled:
                url = f'{tv_url}?seriesType=1&showDate={show_date}'
                tv_urls.append([url, self._all_num])
            # tx
            if self._tx_enabled:
                url = f'{tv_url}?seriesType=1&platformType=3&showDate={show_date}'
                tv_urls.append([url, self._tx_num])
            # iqy
            if self._iqy_enabled:
                url = f'{tv_url}?seriesType=1&platformType=2&showDate={show_date}'
                tv_urls.append([url, self._iqy_num])
            # mg
            if self._mg_enabled:
                url = f'{tv_url}?seriesType=1&platformType=7&showDate={show_date}'
                tv_urls.append([url, self._mg_num])
            # yk
            if self._yk_enabled:
                url = f'{tv_url}?seriesType=1&platformType=1&showDate={show_date}'
                tv_urls.append([url, self._yk_num])
        # 综艺
        if 'zongyi' in self._type:
            # 全网
            if self._all_enabled:
                url = f'{tv_url}?seriesType=2&showDate={show_date}'
                tv_urls.append([url, self._all_num])
            # tx
            if self._tx_enabled:
                url = f'{tv_url}?seriesType=2&platformType=3&showDate={show_date}'
                tv_urls.append([url, self._tx_num])
            # iqy
            if self._iqy_enabled:
                url = f'{tv_url}?seriesType=2&platformType=2&showDate={show_date}'
                tv_urls.append([url, self._iqy_num])
            # mg
            if self._mg_enabled:
                url = f'{tv_url}?seriesType=2&platformType=7&showDate={show_date}'
                tv_urls.append([url, self._mg_num])
            # yk
            if self._yk_enabled:
                url = f'{tv_url}?seriesType=2&platformType=1&showDate={show_date}'
                tv_urls.append([url, self._yk_num])
        tv_list = []
        movie_list = []
        # 本次运行新识别的媒体身份映射，供榜单快照补全
        self._resolved_identity = {}
        # 清空上一次运行的榜单元数据，避免抓取失败时沿用旧榜单
        self._last_boards = []
        try:
            movie_list, tv_list = self.__get_url_info(movie_url, tv_urls, web_movie_url, nums)
        except Exception as e:
            logger.error(f"获取猫眼榜单失败：{e}")
        # __get_url_info 会在内部重置榜单列表，抓取失败时此处回退为空列表
        last_boards = self._last_boards if isinstance(self._last_boards, list) else []
        subscribed_before = len(history)
        self.set_sub(movie_list, history, MediaType.MOVIE)
        self.set_sub(tv_list, history, MediaType.TV)
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
        """
        resolved = getattr(self, "_resolved_identity", {}) or {}
        try:
            for board in boards:
                for item in board.get("items", []):
                    identity = resolved.get(item.get("title")) or {}
                    item["media_source"] = identity.get("media_source", "")
                    item["media_id"] = identity.get("media_id", "")
                    item["poster"] = identity.get("poster", "")
                    item["overview"] = identity.get("overview", "")
                    item["year"] = identity.get("year", "")
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
                unique_flag = f"maoyanrank: {mtype}_{title}_{year}"
                # 检查是否已处理过
                if unique_flag in [h.get("unique") for h in history]:
                    continue
                # 匹配媒体信息
                mediainfo: MediaInfo = self.chain.recognize_media(meta=meta, mtype=mtype, cache=False)
                if not mediainfo:
                    logger.warn(f'未识别到媒体信息，标题：{title}，年份：{year}')
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
                logger.warn(f"查询 TMDB 季信息失败，标题：{getattr(mediainfo, 'title', '')}，错误：{err}")

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
        """按热度接口的 seriesType 判定榜单类型，识别不出时归为剧集热度。"""
        query = dict(parse_qsl(urlsplit(url).query)) if url else {}
        return cls._SERIES_TYPES.get(query.get('seriesType', ''), 'web-heat')

    def __board_meta(self, url: str, board: str, board_type: str, show_date: str) -> Dict[str, str]:
        """
        从榜单地址推导归属信息，供对外接口标识榜单。

        剧集热度接口用 seriesType 区分电视剧/网络剧/综艺，用 platformType 区分平台；
        电影票房与网络电影则是固定来源。
        """
        query = dict(parse_qsl(urlsplit(url).query)) if url else {}
        platform = self._PLATFORM_NAMES.get(query.get('platformType', ''), '')
        return {
            "board": board if not platform else f"{board}（{platform}）",
            "board_type": board_type,
            "board_url": url,
            "show_date": show_date,
            "platform": platform,
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
        if movie_url:
            sources.append((
                movie_url, num, '电影票房榜单', 'movieList', 'movieInfo', 'movieName', movies_list,
                self.__board_meta(movie_url, '电影票房榜单', 'movie', show_date),
            ))
        if web_movie_url:
            sources.append((
                web_movie_url, num, '网络电影榜单', 'dataList', None, 'movieName', movies_list,
                self.__board_meta(web_movie_url, '网络电影榜单', 'web-movie', show_date),
            ))
        for tv_url, tv_num in tv_urls:
            board_type = self.__board_type_from_url(tv_url)
            sources.append((
                tv_url, tv_num, '剧集热度榜单', 'dataList', 'seriesInfo', 'name', tv_list,
                self.__board_meta(tv_url, self._TYPE_NAMES.get(board_type, '剧集热度榜单'), board_type, show_date),
            ))
        if not sources:
            return movies_list, tv_list

        processed = set()

        def collect(page=None):
            """按来源隔离请求；浏览器收尾异常时避免重复请求已经处理的榜单。"""
            headers = {'User-Agent': self.get_random_user_agent()}
            cookies = {}
            if page is not None:
                headers['User-Agent'] = page.evaluate('navigator.userAgent')
                cookies = {cookie['name']: cookie['value'] for cookie in page.context.cookies()}
            for index, (url, limit, name, data_key, info_key, title_key, target, meta) in enumerate(sources):
                if index in processed:
                    continue
                processed.add(index)
                source = f"{name}（{url}）"
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
                    target.extend(items)
                    if items:
                        boards.append({
                            "board": meta["board"],
                            "type": meta["board_type"],
                            "type_name": meta["type_name"],
                            "platform": meta["platform"],
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
        return movies_list, list({item['title']: item for item in tv_list}.values())

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

        heat = ""
        for key in ('currHeatDesc', 'heatDesc', 'boxRate', 'barValue', 'viewCountDesc'):
            raw = item.get(key)
            if isinstance(raw, str) and raw.strip():
                heat = raw.strip()
                break

        return {
            "box_office": box_office,
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
                    "heat": metrics["heat"],
                    "show_count": metrics["show_count"],
                })
            except (TypeError, ValueError) as err:
                logger.warning(f"猫眼{source}第 {rank} 条数据无效，跳过该条目：{err}")
        return items

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

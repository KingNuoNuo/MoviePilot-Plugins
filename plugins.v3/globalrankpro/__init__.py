"""全球影视榜单插件（GlobalRankPro）。

把猫眼榜单、Netflix 榜单与红果短剧榜单合并成一个插件，对外只暴露一套接口：

- ``GET /rank?source=maoyan|netflix|hongguo|all``：统一榜单快照（三源字段并集）；
- ``GET /rank-list?source=...``：统一媒体信息列表（宿主 ``MediaInfo``）；
- ``GET /refresh?source=...``：立即抓取指定数据源；
- ``GET /poster?u=...``：红果海报转发（匿名可访问，手机端不直连字节 CDN）。

三个数据源各自封装在 ``maoyan.py`` / ``netflix.py`` / ``hongguo.py`` 里（原
MaoyanPro 与 NetflixPro 的抓取解析逻辑整体平移，红果为新增的 SSR 抓取），
宿主生命周期、配置与接口注册集中在本类。数据键带 ``maoyan_`` / ``netflix_`` /
``hongguo_`` 前缀，三源互不覆盖。
"""
import datetime
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Request

from app import schemas
from app.chain.download import DownloadChain
from app.chain.subscribe import SubscribeChain
from app.plugins import _PluginBase
from app.sdk.config import settings
from app.sdk.events import eventmanager
from app.sdk.events import Event as SdkEvent
from app.sdk.logging import logger
from app.schemas.types import ChainEventType

from .base import (
    RankRefreshResult,
    RankSnapshot,
    RankSourceRefresh,
    SOURCE_ALL,
    SOURCE_CHOICES,
    SOURCE_HONGGUO,
    SOURCE_MAOYAN,
    SOURCE_NETFLIX,
)
from .hongguo import HongguoSource
from .maoyan import MaoyanSource
from .netflix import NetflixSource
from . import page as page_ui


class GlobalRankPro(_PluginBase):
    """全球影视榜单：猫眼 + Netflix + 红果短剧三数据源，统一接口对外提供。"""

    # 插件名称
    plugin_name = "全球影视榜单"
    # 插件描述
    plugin_desc = ("整合猫眼、Netflix 与红果短剧榜单，提供统一的榜单接口与"
                   "推荐页数据源。")
    # 插件图标
    plugin_icon = ("https://raw.githubusercontent.com/KingNuoNuo/"
                   "MoviePilot-Plugins/main/icons/globalrank.png")
    # 插件版本
    plugin_version = "1.2.4"
    # 插件作者
    plugin_author = "KingNuoNuo"
    # 作者主页
    author_url = "https://github.com/KingNuoNuo"
    # 插件配置项ID前缀
    plugin_config_prefix = "globalrankpro_"
    # 加载顺序
    plugin_order = 6
    # 可使用的用户级别：0 = 不做站点用户认证也能加载
    auth_level = 0

    def __init__(self):
        super().__init__()
        # 数据源实例：构造时把宿主能力代理给它，抓取解析逻辑与宿主生命周期解耦
        self.sources = {
            SOURCE_MAOYAN: MaoyanSource(self),
            SOURCE_NETFLIX: NetflixSource(self),
            SOURCE_HONGGUO: HongguoSource(self),
        }
        # 下载/订阅链：猫眼自动订阅用，Netflix / 红果不用，但代理层统一暴露
        self.downloadchain = None
        self.subscribechain = None
        self._scheduler = None
        self._enabled = False
        self._onlyonce = False
        self._cron = ""
        self._clear = False
        self._api_enabled = True
        self._recommend_enabled = False

    # ==================== 生命周期 ====================

    def init_plugin(self, config: dict = None):
        """加载配置并注册抓取任务。"""
        self.downloadchain = DownloadChain()
        self.subscribechain = SubscribeChain()

        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._clear = config.get("clear")
            self._onlyonce = config.get("onlyonce")
            self._api_enabled = config.get("api_enabled", True)
            self._recommend_enabled = config.get("recommend_enabled", False)
            self.__apply_config(config)

        # 停止现有任务
        self.stop_service()

        # 清理插件数据：三个源的快照、识别缓存与订阅历史一起清
        if self._clear:
            for source in self.sources.values():
                for key in ("snapshot", "history", "identity_cache"):
                    source.del_data(key)
            self._clear = False
            self.__update_config()
            logger.info("全球影视榜单数据清理完成")

        enabled_sources = [s for s in self.sources.values() if s.enabled]
        # 推荐页数据源同样依赖定时抓取（推荐页读的是榜单快照）
        if self._enabled or self._recommend_enabled or self._onlyonce:
            if not enabled_sources:
                logger.warning("全球影视榜单未启用任何数据源，跳过定时任务")
                return
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            job_names = "、".join(s.source_name for s in enabled_sources)
            if self._cron:
                logger.info(f"全球影视榜单服务启动，周期：{self._cron}（{job_names}）")
                try:
                    self._scheduler.add_job(
                        func=self.__refresh_all,
                        trigger=CronTrigger.from_crontab(self._cron),
                        name="全球影视榜单")
                except Exception as e:
                    logger.error(f"全球影视榜单服务启动失败，错误信息：{str(e)}")
                    self.systemmessage.put(f"全球影视榜单服务启动失败，错误信息：{str(e)}")
            else:
                self._scheduler.add_job(
                    func=self.__refresh_all,
                    trigger=CronTrigger.from_crontab("0 9 * * *"),
                    name="全球影视榜单")
                logger.info("全球影视榜单服务启动，周期：每天 09:00")

            if self._onlyonce:
                logger.info("全球影视榜单服务启动，立即运行一次")
                self._scheduler.add_job(
                    func=self.__refresh_all, trigger='date',
                    run_date=datetime.datetime.now(
                        tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3))
                self._onlyonce = False
                self.__update_config()

            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def get_state(self) -> bool:
        """至少启用一个数据源且插件开关打开时视为运行中。"""
        return bool(self._enabled or self._recommend_enabled)

    def stop_service(self):
        """停止定时任务。

        ⚠️ 必须 ``wait=False``：抓取单轮要跑几分钟（识别 + 补图 + 订阅），
        ``shutdown()`` 默认会阻塞到正在运行的任务结束，宿主保存配置时就会
        一直转圈；后台线程让它自然跑完即可，下次抓取会覆盖快照。
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    try:
                        self._scheduler.shutdown(wait=False)
                    except TypeError:
                        # 老/新版调度器签名差异时退回无参调用
                        self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"退出定时任务失败：{str(e)}")

    # ==================== 配置 ====================

    def __apply_config(self, config: Dict[str, Any]) -> None:
        """把扁平配置按源前缀拆给各数据源。"""
        for source_id, source in self.sources.items():
            prefix = f"{source_id}_"
            sub = {key[len(prefix):]: value
                   for key, value in config.items() if key.startswith(prefix)}
            source.apply_config(sub)

    def serialize_config(self) -> Dict[str, Any]:
        """把当前运行态还原成扁平配置（设置页回显与配置回写共用）。"""
        config: Dict[str, Any] = {
            'enabled': bool(self._enabled),
            'onlyonce': bool(self._onlyonce),
            'cron': str(self._cron or ""),
            'clear': bool(self._clear),
            'api_enabled': bool(self._api_enabled),
            'recommend_enabled': bool(self._recommend_enabled),
        }
        maoyan = self.sources.get(SOURCE_MAOYAN)
        if maoyan is not None:
            config.update({
                'maoyan_enabled': maoyan.enabled,
                'maoyan_sub_enabled': getattr(maoyan, '_enabled', True),
                'maoyan_type_movie': list(getattr(maoyan, '_type_movie', [])),
                'maoyan_type_tv': list(getattr(maoyan, '_type_tv', [])),
                'maoyan_movie_sub_num': maoyan._movie_sub_num,
                'maoyan_movie_rec_enabled': maoyan._movie_rec_enabled,
                'maoyan_tv_sub_num': maoyan._tv_sub_num,
                'maoyan_tv_rec_enabled': maoyan._tv_rec_enabled,
                'maoyan_zongyi_sub_num': maoyan._zongyi_sub_num,
                'maoyan_zongyi_rec_enabled': maoyan._zongyi_rec_enabled,
                'maoyan_tx_sub_num': maoyan._tx_sub_num,
                'maoyan_iqy_sub_num': maoyan._iqy_sub_num,
                'maoyan_mg_sub_num': maoyan._mg_sub_num,
                'maoyan_yk_sub_num': maoyan._yk_sub_num,
                # 平台细分榜展示开关收敛成一个多选（值 = 平台代码）
                'maoyan_platforms': [name for name in maoyan._PLATFORM_CODES
                                     if getattr(maoyan, f"_{name}_rec_enabled", False)],
            })
        netflix = self.sources.get(SOURCE_NETFLIX)
        if netflix is not None:
            config.update({
                'netflix_enabled': netflix.enabled,
                'netflix_regions': list(getattr(netflix, '_regions', [])),
            })
        hongguo = self.sources.get(SOURCE_HONGGUO)
        if hongguo is not None:
            config.update({
                'hongguo_enabled': hongguo.enabled,
                'hongguo_boards': list(getattr(hongguo, '_boards', [])),
                'hongguo_limit': int(getattr(hongguo, '_limit', 0) or 0),
            })
        return config

    def __update_config(self) -> None:
        """回写配置（一次性开关、清理开关变化后需要落库）。"""
        self.update_config(self.serialize_config())

    # ==================== 抓取 ====================

    def __refresh_source(self, source_id: str) -> RankSourceRefresh:
        """抓取单个数据源，把异常收敛成结果对象。"""
        source = self.sources.get(source_id)
        if source is None:
            return RankSourceRefresh(source=source_id, message="未知数据源")
        if not source.enabled:
            return RankSourceRefresh(
                source=source_id, message="数据源未启用，跳过抓取")
        try:
            result = source.fetch() or {}
        except Exception as e:
            logger.error(f"{source.source_name}刷新失败：{e}")
            return RankSourceRefresh(
                source=source_id, message=f"刷新失败：{e}")
        board_count = int(result.get("board_count") or 0)
        item_count = int(result.get("item_count") or 0)
        message = str(result.get("message") or "")
        if not message:
            # 数据源的 fetch 摘要不一定带 message，这里补一句可读结果，
            # 避免刷新按钮点了之后界面上什么都没有
            message = (f"已抓取 {board_count} 个榜单 {item_count} 条"
                       if board_count else "本次未抓取到榜单（数据源异常或网络不可用）")
        return RankSourceRefresh(
            source=source_id,
            refreshed=bool(board_count),
            board_count=board_count,
            item_count=item_count,
            subscribed=int(result.get("subscribed") or 0),
            week=str(result.get("week") or ""),
            message=message,
        )

    def __refresh_all(self) -> RankRefreshResult:
        """按顺序抓取全部已启用数据源（定时任务入口）。"""
        results = [self.__refresh_source(sid)
                   for sid, s in self.sources.items() if s.enabled]
        return self.__aggregate(results)

    @staticmethod
    def __aggregate(results: List[RankSourceRefresh]) -> RankRefreshResult:
        """把各数据源结果汇总成统一刷新结果。"""
        if not results:
            return RankRefreshResult(refreshed=False,
                                     message="未启用任何数据源")
        refreshed = [r for r in results if r.refreshed]
        messages = [f"{r.source}：{r.message or '完成'}" for r in results]
        return RankRefreshResult(
            refreshed=bool(refreshed),
            board_count=sum(r.board_count for r in results),
            item_count=sum(r.item_count for r in results),
            subscribed=sum(r.subscribed for r in results),
            message="；".join(messages),
            sources=results,
        )

    # ==================== 统一接口 ====================

    def get_api(self) -> List[Dict[str, Any]]:
        """
        注册对外接口（三套接口用 ``source`` 参数区分数据源）。

        认证统一用宿主的 ``bear`` 模式：它同时接受 App 登录态的 JWT
        （``Authorization: Bearer``）、API 令牌（``X-API-KEY`` 头或 ``apikey``
        查询参数）与 ``token`` 查询参数；默认的 ``apikey`` 模式只认 API 令牌。

        返回值一律用 ``schemas.Response`` 信封包裹：宿主普通数据客户端（含详情页
        按钮）会校验 success/message/data 三键结构，裸模型会被判「无效响应」。
        """
        return [
            {
                "path": "/rank",
                "endpoint": self.get_rank_snapshot,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "全球影视榜单快照",
                "description": "返回最近一次抓取的榜单快照：source 选数据源"
                               "（maoyan/netflix/hongguo/all），board_type 按源语义过滤"
                               "（猫眼为榜单类型，Netflix 为地区代码，红果为榜单代码）",
                "response_model": schemas.Response[RankSnapshot],
            },
            {
                "path": "/rank-list",
                "endpoint": self.rank_list,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "全球影视榜单列表",
                "description": "供宿主推荐页与浏览页消费的榜单列表，返回统一的媒体信息结构",
                "response_model": schemas.Response[List[schemas.MediaInfo]],
            },
            {
                "path": "/refresh",
                "endpoint": self.refresh_now,
                "methods": ["GET", "POST"],
                "auth": "bear",
                "summary": "立即刷新榜单",
                "description": "立即触发一次抓取：source 选数据源"
                               "（maoyan/netflix/hongguo/all）",
                "response_model": schemas.Response[RankRefreshResult],
            },
            {
                "path": "/poster",
                "endpoint": self.hongguo_poster,
                "methods": ["GET"],
                "summary": "红果海报转发",
                "description": "把红果海报经宿主转发给客户端，手机端不直连字节 CDN。"
                               "只放行字节图床（byteimg / ecombdimg）下的图片。",
                # <img> 标签带不了 Authorization 头，必须匿名可读；
                # 安全边界由 endpoint 内部的域名/路径白名单保证
                "allow_anonymous": True,
            },
        ]

    @staticmethod
    def __resolve_sources(source: str) -> List[str]:
        """把 source 参数展开成数据源列表（非法值按全部处理）。"""
        key = (source or SOURCE_ALL).strip().lower()
        if key in SOURCE_CHOICES:
            return [key]
        return list(SOURCE_CHOICES)

    @staticmethod
    def __split_board_types(board_type: str) -> List[str]:
        """board_type 支持逗号分隔多值。"""
        if not board_type:
            return []
        return [part.strip() for part in str(board_type).split(",") if part.strip()]

    def get_rank_snapshot(self, source: str = SOURCE_ALL,
                          board_type: str = None, kind: str = None,
                          limit: int = None, board: str = None,
                          platform: str = None
                          ) -> schemas.Response[RankSnapshot]:
        """
        返回统一榜单快照。

        :param source: 数据源，``maoyan`` / ``netflix`` / ``hongguo`` / ``all``（默认 all）
        :param board_type: 榜单类型，逗号分隔多值；猫眼取 type（movie/web-all/total/
                           year/global/web-movie/zongyi），Netflix 取地区代码
                           （global/hk/tw/jp/kr/sg），红果取榜单代码
                           （hot/real/comic/ai），all 下各源各自匹配
        :param kind: 内容类型，``film`` / ``tv``（仅 Netflix 生效）
        :param limit: 每个榜单最多返回的条目数
        :param board: 榜单名包含该关键字（仅猫眼生效）
        :param platform: 猫眼 platformType（如 3=腾讯视频）
        """
        wanted_types = self.__split_board_types(board_type)
        boards: List = []
        sources_used: List[str] = []
        messages: List[str] = []
        week = ""
        updated: List[str] = []

        for source_id in self.__resolve_sources(source):
            src = self.sources.get(source_id)
            if src is None:
                continue
            if not src.enabled:
                messages.append(f"{src.source_name}未启用")
                continue

            if source_id == SOURCE_MAOYAN:
                # 猫眼一次只按一种类型过滤，多值时多次取再合并
                batches = []
                if wanted_types:
                    for one in wanted_types:
                        batches.extend(src.rank_boards(
                            board_type=one, limit=limit,
                            board=board, platform=platform))
                else:
                    batches = src.rank_boards(
                        limit=limit, board=board, platform=platform)
            elif source_id == SOURCE_NETFLIX:
                batches = []
                # Netflix 的 board_type 是地区；多值时逐个地区取
                for one in (wanted_types or ["global"]):
                    batches.extend(src.rank_boards(
                        board_type=one, kind=kind, limit=limit))
                if not wanted_types:
                    # 未指定地区时返回全部已抓取地区，而不是只有全球榜
                    seen = {b.board for b in batches}
                    for other in getattr(src, "_regions", []):
                        if other == "global":
                            continue
                        batches.extend(
                            b for b in src.rank_boards(board_type=other,
                                                       kind=kind, limit=limit)
                            if b.board not in seen)
            else:
                # 红果：board_type 是榜单代码（hot/real/comic/ai），多值逐个取；
                # 不传时 rank_boards 直接返回快照里的全部榜单
                batches = []
                for one in (wanted_types or [None]):
                    batches.extend(src.rank_boards(board_type=one, limit=limit))

            if batches:
                boards.extend(batches)
                sources_used.append(source_id)
            meta = src.snapshot_meta()
            if meta.get("updated_at"):
                updated.append(str(meta["updated_at"]))
            if source_id == SOURCE_NETFLIX and meta.get("week"):
                week = str(meta["week"])

        return schemas.Response(
            success=True,
            message="；".join(messages),
            data=RankSnapshot(
                updated_at=max(updated) if updated else "",
                week=week,
                total=sum(b.count for b in boards),
                sources=sources_used,
                boards=boards,
            ),
        )

    def rank_list(self, source: str = SOURCE_MAOYAN,
                  board_type: str = None, kind: str = None,
                  page: int = 1, limit: int = None,
                  platform: str = None, request: Request = None
                  ) -> schemas.Response[List[schemas.MediaInfo]]:
        """
        推荐页/浏览页使用的榜单列表接口（内部转发到对应数据源）。

        :param source: 数据源，``maoyan``（默认，保持旧接口语义）/ ``netflix`` / ``hongguo``
        :param board_type: 猫眼为榜单类型（默认 web-all），Netflix 为地区（默认 global），
                           红果为榜单代码（默认返回全部已抓取榜单）
        :param kind: 内容类型（仅 Netflix：film/tv）
        :param page: 页码（仅猫眼平台细分榜编组时有效）
        :param limit: 返回条目上限
        :param platform: 猫眼 platformType
        :param request: 宿主注入的请求对象，用于把海报地址拼成客户端真正能连上的地址
        """
        source_id = (source or SOURCE_MAOYAN).strip().lower()
        src = self.sources.get(source_id)
        if src is None:
            return schemas.Response(
                success=False,
                message=f"未知数据源：{source}，可选 maoyan/netflix/hongguo",
                data=[])
        if not src.enabled:
            return schemas.Response(
                success=False, message=f"{src.source_name}未启用", data=[])
        if source_id == SOURCE_MAOYAN:
            return src.rank_list(
                board_type=board_type or "web-all", page=page,
                limit=limit, platform=platform)
        if source_id == SOURCE_NETFLIX:
            return src.rank_list(
                board_type=board_type or "global", kind=kind,
                page=page, limit=limit)
        # 红果：不传 board_type 时返回全部已抓取榜单；海报地址按请求地址拼
        return src.rank_list(board_type=board_type, kind=kind,
                             page=page, limit=limit,
                             base=self.__request_base(request))

    @staticmethod
    def __request_base(request: Optional[Request]) -> str:
        """取请求方实际访问的宿主地址（协议 + 域名/IP + 端口）。

        只在 ``APP_DOMAIN`` 没配时兜底使用：手机用局域网 IP 访问、宿主又没配
        对外域名时，请求里的地址才是客户端能连上的那个。
        """
        if request is None:
            return ""
        try:
            base_url = str(request.base_url or "").strip()
        except Exception:
            return ""
        if not base_url:
            return ""
        # 服务端内部调用（如宿主自己做聚合）拿到的是 127.0.0.1，这种地址对
        # 客户端没意义，直接放弃，回落到 APP_DOMAIN
        host = str(getattr(request.url, "hostname", "") or "")
        if host in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
            return ""
        return base_url.rstrip("/")

    def refresh_now(self, source: str = SOURCE_ALL
                    ) -> schemas.Response[RankRefreshResult]:
        """立即触发一次抓取：source 选数据源（maoyan/netflix/hongguo/all）。"""
        targets = self.__resolve_sources(source)
        results = [self.__refresh_source(sid) for sid in targets]
        result = self.__aggregate(results)
        if not any(r.refreshed for r in results):
            # 只有「每条结果都明确说自己未启用」才提示去开开关，
            # 否则会掩盖真正的抓取失败（网络异常时 message 不含「未启用」）
            disabled_only = bool(results) and all(
                "未启用" in (r.message or "") for r in results)
            message = ("所选数据源均未启用，请先在插件配置中开启"
                       if disabled_only else (result.message or "刷新失败"))
            return schemas.Response(success=False, message=message, data=result)
        return schemas.Response(
            success=True, message=result.message, data=result)

    def hongguo_poster(self, u: str = ""):
        """红果海报转发接口（匿名可访问，供 ``<img>`` 直接引用）。

        手机端直连字节图床经常拿不到图，这里由宿主代取后原样回传字节，
        客户端只需要能访问 MoviePilot 本身。
        """
        source = self.sources.get(SOURCE_HONGGUO)
        if source is None:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="红果数据源不存在")
        return source.poster_response(u)

    # ==================== 推荐页数据源 ====================

    @eventmanager.register(ChainEventType.RecommendSource)
    def recommend_source(self, event: SdkEvent):
        """把三个数据源的榜单注册为宿主推荐页数据源（与订阅功能相互独立）。"""
        if not (self._recommend_enabled and self._api_enabled):
            return
        event_data = event.event_data if event else None
        if not event_data:
            return

        sources: List[Any] = []
        for source in self.sources.values():
            if not source.enabled:
                continue
            try:
                sources.extend(source.recommend_sources())
            except Exception as e:
                logger.error(f"{source.source_name}推荐源注册失败：{e}")
        if not sources:
            return

        # 叠加而不是替换，避免影响其它插件塞入的数据源
        if not event_data.extra_sources:
            event_data.extra_sources = sources
        else:
            event_data.extra_sources.extend(sources)

    # ==================== 表单与详情页 ====================

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """设置表单：App 推荐卡 + 通用开关 + 猫眼 / Netflix / 红果配置。"""
        return page_ui.build_form(self)

    def get_page(self) -> List[dict]:
        """详情页：App 推荐 + 三源抓取状态与刷新 + 猫眼订阅历史。"""
        return page_ui.build_page(self)

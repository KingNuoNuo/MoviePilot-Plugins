"""猫眼榜单数据源。

原 ``MaoyanPro`` 插件的抓取、解析、识别与订阅逻辑整体平移到本模块，
改由 :class:`GlobalRankPro` 主类驱动：宿主生命周期（配置回写、定时任务、
接口注册）全部上移到主类，这里只保留「抓一次、解析成榜单、认媒体、写快照」。

宿主能力（``get_data``/``save_data``/``chain``/``downloadchain`` 等）通过
:class:`RankSource` 的代理拿到主插件实例，因此数据键会自动带 ``maoyan_``
前缀，与 Netflix 数据源互不覆盖。
"""
import datetime
import random
import re
import time
from typing import List, Dict, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytz

from app import schemas
from app.chain.tmdb import TmdbChain
from app.sdk.media import MediaInfo
from app.sdk.browser import launch_browser_context
from app.sdk.config import settings
from app.sdk.media import MetaInfo
from app.sdk.logging import logger
from app.schemas import MediaSource, MediaType
from app.sdk.network import RequestUtils
from app.sdk.media import normalize_media_source, resolve_media_identity

from .base import (
    RankBoard,
    RankSource,
    SOURCE_MAOYAN,
)



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


class MaoyanSource(RankSource):
    """
    猫眼榜单数据源：抓取猫眼实时榜单、识别媒体并写入快照。

    Fork 自 jxxghp/moviepilot-plugins 的 MaoyanRank 插件
    (上游目录 plugins.v3/maoyanrank，上游原插件作者: 逗猫 @baozaodetudou, 上游版本 v4.1.4)；
    本仓库在此基础上新增榜单快照、推荐页数据源与对外接口。

    电影获取的url: https://piaofang.maoyan.com/dashboard-ajax/movie
    电视剧获取的url: {tv_url}?showDate=20240223&seriesType=0&platformType=0
        参数 showDate: 时间具体到天
        参数 seriesType: 代表类型 0: 电视剧 1: 网络剧 2: 综艺；不传该参数时为电视剧+网络剧综合榜
        参数 platformType: 代表平台 0 全网 3 腾讯视频 2 爱奇艺 1 优酷 7 芒果 5 搜狐 4 乐视 6 PPTV 20 网络电影networkHot=3

    详情链接:
    https://piaofang.maoyan.com/dashboard/movie?movieId=1489349
    https://piaofang.maoyan.com/dashboard/web-heat?movieId=1484643

    """

    source_id = SOURCE_MAOYAN
    source_name = "猫眼榜单"

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

    # 详情页榜单区每个分区最多展示的条目数：年度/全球年度动辄 30 条，
    # 全铺开会把订阅历史挤到很下面；完整数据走推荐页与对外接口
    _PAGE_BOARD_LIMIT = 10
    # 详情页榜单分区的展示顺序，快照里存在哪个类型就出现哪个分区
    _PAGE_BOARD_ORDER = ('movie', 'total', 'year', 'global',
                         'web-movie', 'web-all', 'zongyi')

    # 单次刷新最多识别的条目数：平台细分榜会让待识别条目变多，超出的留待下次刷新补齐。
    # 识别结果会持久化（identity_cache），因此限额只影响「新条目」的收敛速度，
    # 老条目跨轮复用缓存，不会每轮从零识别（v3 每次调度都重建插件实例，内存缓存必丢）。
    _RESOLVE_LIMIT = 80
    # 积压时临时抬高的上限：待识别条目超过 _RESOLVE_LIMIT 说明有整批新条目（或缓存
    # 整体过期），按常规限额要好几轮才轮到排在最后的全球年度票房榜 —— 实测那里新进榜
    # 条目会长期空海报（Obsession 就是），所以积压时一轮多识别一些。
    _RESOLVE_LIMIT_BACKLOG = 240
    # 单轮识别的时间预算（秒）：识别一条要请求 TMDB，条数再多也不能把整轮刷新拖死；
    # 超时就收工，没轮到的条目靠「缺图优先」排序在下一轮排到最前面。
    _RESOLVE_TIME_BUDGET = 180.0
    # 缺海报的条目用豆瓣 suggest 补兜底图的单轮额度（单次 HTTP、不走识别链，很便宜）。
    # 覆盖三类：被限额/超时截断还没识别的、识别到身份但 TMDB 没给图的、压根认不出来的。
    _POSTER_FALLBACK_LIMIT = 60
    # 同一条目连续补不到图的上限：豆瓣确实没有这条时不必每轮都再打一次
    _POSTER_FALLBACK_MAX_MISS = 3
    # 豆瓣 suggest 的请求间隔（秒）：连打会被拦（实测连续几次后直接返回空），
    # 一旦某次请求抛异常就认定被限流，本轮立即停止补图（已补上的照常落库）
    _POSTER_FALLBACK_INTERVAL = 0.4
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
    # 平台代码 -> 展示名（设置页 platforms 多选与详情页共用）
    _PLATFORM_CODES = ('tx', 'iqy', 'mg', 'yk')
    _PLATFORM_LABELS = {
        'tx': '腾讯视频', 'iqy': '爱奇艺', 'mg': '芒果TV', 'yk': '优酷',
    }
    # 猫眼 platformType -> (订阅条数字段, 推荐条数字段)；抓取与推荐源注册共用
    _PLATFORM_NUM_KEYS = {
        3: ('tx_sub_num', 'tx_rec_num'),
        2: ('iqy_sub_num', 'iqy_rec_num'),
        7: ('mg_sub_num', 'mg_rec_num'),
        1: ('yk_sub_num', 'yk_rec_num'),
    }

    # 本次抓取到的榜单元数据，供对外接口落快照（不持久化，失败时为上层回退到历史）
    _last_boards: List[dict] = []

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

    def apply_config(self, config: Dict[str, Any]) -> None:
        """接收主类剥离前缀后的猫眼配置，并完成旧配置迁移。"""
        super().apply_config(config)
        # 订阅开关：插件总开关由主类持有，猫眼这里只关心「是否按榜单自动订阅」
        self._enabled = bool((config or {}).get("sub_enabled", True))
        self.__load_type_config(dict(config or {}))
        self.__migrate_history_identity()

    def fetch(self) -> Dict[str, Any]:
        """执行一次猫眼榜单抓取（订阅 + 快照），返回摘要字典。"""
        if not self._type:
            raise RuntimeError("未配置任何猫眼榜单类型，请先在插件配置中选择")
        return self.__refresh_maoyan() or {}

    def rank_boards(self, board_type: str = None, limit: int = None,
                    board: str = None, platform: str = None) -> List[RankBoard]:
        """
        把快照里的猫眼榜单转成统一模型。

        :param board_type: 仅返回指定榜单类型（movie/web-movie/web-all/zongyi/total/year/global）
        :param limit: 每个榜单最多返回的条目数
        :param board: 仅返回榜单名包含该关键字的榜单（如「腾讯视频」）
        :param platform: 仅返回指定平台（猫眼 platformType，如 3=腾讯视频）
        """
        selected: List[RankBoard] = []
        for item in self.boards():
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
            payload = _safe_board_payload(item, rows)
            payload["source"] = self.source_id
            # 猫眼的榜单种类就是 type 字段，统一层再暴露一份 board_type 便于跨源过滤
            payload["board_type"] = payload.get("type") or ""
            selected.append(RankBoard(**payload))
        return selected

    def recommend_sources(self) -> list:
        """按当前配置构造要注册到推荐页的猫眼数据源列表。"""
        sources = []
        if 'movie' in (self._type or []):
            sources.append(schemas.RecommendMediaSource(
                name="猫眼电影票房",
                api_path=(f"plugin/GlobalRankPro/rank-list?"
                          f"source={self.source_id}&board_type=movie"),
                type=self._RECOMMEND_TYPES['movie'],
            ))
        if 'total' in (self._type or []):
            sources.append(schemas.RecommendMediaSource(
                name="猫眼影史总票房",
                api_path=(f"plugin/GlobalRankPro/rank-list?"
                          f"source={self.source_id}&board_type=total"),
                type=self._RECOMMEND_TYPES['total'],
            ))
        if 'year' in (self._type or []):
            sources.append(schemas.RecommendMediaSource(
                name="国内年度票房",
                api_path=(f"plugin/GlobalRankPro/rank-list?"
                          f"source={self.source_id}&board_type=year"),
                type=self._RECOMMEND_TYPES['year'],
            ))
        if 'global' in (self._type or []):
            sources.append(schemas.RecommendMediaSource(
                name="全球年度票房",
                api_path=(f"plugin/GlobalRankPro/rank-list?"
                          f"source={self.source_id}&board_type=global"),
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
                api_path=(f"plugin/GlobalRankPro/rank-list?"
                          f"source={self.source_id}&board_type={board_type}"),
                type=self._RECOMMEND_TYPES[board_type],
            ))
        return sources

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
        # 平台细分榜（v1.0.4 起）：platforms 多选决定抓哪些平台并写入榜单快照；
        # 未出现 platforms 键的旧配置回退到各平台 *_rec_enabled 开关，平滑迁移。
        if "platforms" in config:
            chosen = {str(c) for c in (config.get("platforms") or [])}
        else:
            # 旧配置：优先布尔开关，缺失时按旧数值条数 > 0 判定
            chosen = set()
            for name in self._PLATFORM_CODES:
                flag = config.get(f"{name}_rec_enabled")
                if flag is None:
                    flag = self.__safe_num(config.get(f"{name}_rec_num"), 0) > 0
                if flag:
                    chosen.add(name)
        for name in self._PLATFORM_CODES:
            enabled = name in chosen
            setattr(self, f"_{name}_rec_enabled", enabled)
            setattr(self, f"_{name}_rec_num",
                    self._REC_BOARD_MAX if enabled else 0)

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
        logger.info("开始刷新猫眼榜单...")
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
        logger.info("猫眼订阅刷新完成")
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
                    release_date = ""
                    if identity:
                        release_date = identity.get("release_date", "")
                        item["media_source"] = identity.get("media_source", "")
                        item["media_id"] = identity.get("media_id", "")
                        item["poster"] = identity.get("poster", "")
                        item["overview"] = identity.get("overview", "")
                        item["year"] = identity.get("year", "")
                        item["title_cn"] = identity.get("title_cn", "")
                    elif title in prev_map:
                        # 本轮限额截断没轮到识别：沿用上一份快照的身份，海报不丢
                        p = prev_map[title]
                        release_date = p.get("release_date", "")
                        if not release_date:
                            # 上一份快照条目里已是完整日期的（此前轮次补全过），直接继承
                            p_rel = str(p.get("releaseInfo") or "").strip()
                            if re.match(r"^\d{4}[-年/.]", p_rel):
                                release_date = p_rel[:10]
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
                    # 全球榜数据源（Box Office Mojo）不带上映日期，条目里只有榜单年份；
                    # 识别到 TMDB 发行日期后补成与国内榜一致的「YYYY-MM-DD 上映」。
                    # 已是完整日期的（国内榜猫眼原生数据）原样保留
                    rel_now = str(item.get("releaseInfo") or "").strip()
                    if release_date and not re.match(r"^\d{4}[-年/.]", rel_now):
                        item["releaseInfo"] = f"{release_date} 上映"
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
        - 限额只数本轮真正发起识别的新条目，缓存命中的直接放行。

        ⚠️ 队列不再按榜单固定顺序走，而是「缺图 / 缺日期优先 + 连续失败次数少的优先」：
        以前按 board 顺序识别，全球年度票房榜排在 4 个电影榜最后，攒够 400 条待识别条目时
        它每一轮都被 80 条限额截断 —— 结果该榜新进榜条目长期没有海报（如 Obsession），
        上映日期也永远补不上（日期只能靠 TMDB 识别拿）。按缺失程度排序后，新条目最多等一轮。

        ⚠️ 「识别到身份」不等于「有海报」：TMDB 有些记录压根没有 poster（Obsession 匹配到
        tmdb/1436161 但图是空串）。这类条目以前被 TTL 分支当作有效缓存放回内存，既不重识别
        也不补图，一空就是一整个 TTL。现在所有「缓存里没图且上一份快照也没图」的条目都会走
        一次豆瓣兜底补图（见下面 B 段）。
        """
        if not boards:
            return
        if not hasattr(self, "_resolved_identity"):
            self._resolved_identity = {}
        if not hasattr(self, "_resolved_mediainfo"):
            self._resolved_mediainfo = {}
        current_time = datetime.datetime.now()
        raw_cache: dict = {}
        # 恢复持久缓存：识别成功的长期有效，兜底/空结果按 TTL 过期后重试
        try:
            cached = self.get_data("identity_cache") or {}
            if isinstance(cached, dict):
                raw_cache = cached
                now_ts = current_time.timestamp()
                for t, entry in cached.items():
                    if not isinstance(t, str) or not t or not isinstance(entry, dict):
                        continue
                    if entry.get("media_source") and entry.get("poster"):
                        # 「release_date」键是补上映日期能力加入后才有的：缺键的旧条目视为
                        # 过期（不回内存），本轮重识别补上日期——一次性收敛成本
                        if "release_date" in entry:
                            self._resolved_identity[t] = entry
                    elif now_ts - float(entry.get("ts") or 0) < self._IDENTITY_TTL_FALLBACK:
                        self._resolved_identity[t] = entry
        except Exception as e:
            logger.warning(f"读取识别缓存失败，本轮从零识别：{e}")

        # 上一份快照按标题建索引：判断「这条现在在页面上有没有图」，决定识别优先级
        prev_map = self.__prev_snapshot_map()

        # 收集待识别条目并排序：连续失败多的沉到最后，避免「认不出来的条目」每轮占满预算
        pending: List[tuple] = []
        order = 0
        for board in boards:
            mtype = self._BOARD_MEDIA_TYPES.get(board.get("type"), MediaType.TV)
            for item in board.get("items", []):
                title = item.get("title")
                order += 1
                if not isinstance(title, str) or not title.strip():
                    continue
                if title in self._resolved_identity:
                    continue
                prev = prev_map.get(title) or {}
                need_poster = not str(prev.get("poster") or "").strip()
                # releaseInfo 只有年份（全球榜的 Box Office Mojo 就是这种）也算「缺日期」
                need_date = not re.match(
                    r"^\d{4}[-年/.]", str(item.get("releaseInfo") or "").strip())
                miss = int((raw_cache.get(title) or {}).get("miss") or 0)
                pending.append((
                    miss,                                   # 连续失败次数少的先来
                    0 if (need_poster or need_date) else 1,  # 缺图/缺日期的先来
                    order,                                  # 同优先级保持榜单原顺序
                    title, item.get("releaseInfo"), mtype,
                ))
        pending.sort(key=lambda p: (p[0], p[1], p[2]))

        # 积压多时抬高一轮的上限，配合时间预算避免刷新被拖死
        limit = (self._RESOLVE_LIMIT_BACKLOG
                 if len(pending) > self._RESOLVE_LIMIT else self._RESOLVE_LIMIT)
        started = time.monotonic()
        resolved = 0
        timed_out = False
        for miss, _prio, _order, title, release_info, mtype in pending:
            if resolved >= limit:
                break
            if resolved and time.monotonic() - started > self._RESOLVE_TIME_BUDGET:
                timed_out = True
                break
            identity = self.__recognize_board_item(
                title, release_info, mtype, current_time) or {}
            resolved += 1
            # 没识别到媒体身份 = 这条「认不出来」，累计失败次数让它在后续轮次沉底
            identity["miss"] = 0 if identity.get("media_source") else miss + 1
            identity["ts"] = current_time.timestamp()
            # 新结果没图/没中文名时，保住之前修好的那份（缓存或上一份快照）：
            # 重新识别是为了补上映日期，不能顺手把已经补好的海报又抹成灰洞
            # （Obsession 就踩过：补到「痴迷」后下一轮重识别拿日期，图又没了）
            old = raw_cache.get(title) or {}
            prev_item = prev_map.get(title) or {}
            if not identity.get("poster"):
                for kept in (old.get("poster"), prev_item.get("poster")):
                    if isinstance(kept, str) and kept.startswith("http"):
                        identity["poster"] = kept
                        break
            if not identity.get("title_cn"):
                for kept in (old.get("title_cn"), prev_item.get("title_cn")):
                    if isinstance(kept, str) and kept.strip():
                        identity["title_cn"] = kept.strip()
                        break
            self._resolved_identity[title] = identity

        # B：兜底补图。会显示成灰洞的有三类条目，都走一次豆瓣检索（单次 HTTP，
        # 不占识别额度）：
        #   ① 本轮被限额/超时截断，还没轮到识别；
        #   ② 识别到了媒体身份，但 TMDB 那条记录本身没有海报（Obsession 就是：身份拿到了
        #      tmdb/1436161，poster 是空串）—— 这类以前被 TTL 分支当成「有效缓存」放回内存，
        #      结果既不再识别、也不补图，一空就是一整轮 TTL（2 天）；
        #   ③ 怎么也认不出来（空身份），兜底图也试一次。
        mtype_by_title: dict = {}
        for board in boards:
            mt = self._BOARD_MEDIA_TYPES.get(board.get("type"), MediaType.TV)
            for item in board.get("items", []):
                t = item.get("title")
                if isinstance(t, str) and t:
                    mtype_by_title.setdefault(t, mt)

        need_fallback: List[str] = []
        seen_fallback = set()
        cutoff_titles = {p[3] for p in pending[resolved:]}
        release_info_by_title = {p[3]: p[4] for p in pending}
        # ① 截断的、还没识别的
        for _m, _p, _o, title, _ri, _mt in pending[resolved:]:
            if title in self._resolved_identity or title in seen_fallback:
                continue
            if (prev_map.get(title) or {}).get("poster"):
                continue  # 上一份快照有图，__save_snapshot 会回填，不必再请求
            seen_fallback.add(title)
            need_fallback.append(title)
        # ②③ 已在缓存里（含本轮刚识别的）但没海报
        for title in mtype_by_title:
            if title in seen_fallback or title in cutoff_titles:
                continue
            identity = self._resolved_identity.get(title)
            if not identity or identity.get("poster"):
                continue
            if (prev_map.get(title) or {}).get("poster"):
                continue
            # 反复补不到图的（数据源确实没有）别每轮都再打一次
            if int(identity.get("miss") or 0) >= self._POSTER_FALLBACK_MAX_MISS:
                continue
            seen_fallback.add(title)
            need_fallback.append(title)

        fallback = 0
        self._douban_blocked = False
        for title in need_fallback:
            # 被豆瓣限流就本轮收手：剩余条目的 miss 不动，下一轮再试
            if fallback >= self._POSTER_FALLBACK_LIMIT or self._douban_blocked:
                break
            mtype = mtype_by_title.get(title, MediaType.TV)
            release_info = release_info_by_title.get(title)
            if release_info is None:
                release_info = (prev_map.get(title) or {}).get("releaseInfo")
            year = str((raw_cache.get(title) or {}).get("year")
                       or self.__release_year(release_info, current_time) or "")
            poster, cn_title = self.__douban_poster(title, mtype, year)
            fallback += 1
            identity = self._resolved_identity.get(title)
            if identity is None:
                # ① 还没识别过：只填兜底图，不伪造媒体身份
                identity = {
                    "media_source": "",
                    "media_id": "",
                    "poster": "",
                    "overview": "",
                    "year": year,
                    "type": mtype.value,
                    "title_cn": "",
                    "release_date": "",
                    "ts": current_time.timestamp(),
                    # 继承历史失败次数：不能从 0 重来，否则沉底排序失效
                    "miss": int((raw_cache.get(title) or {}).get("miss") or 0),
                }
            elif not identity.get("year"):
                identity["year"] = year
            if poster:
                identity["poster"] = poster
                if cn_title and cn_title != title and not identity.get("title_cn"):
                    identity["title_cn"] = cn_title
                identity["miss"] = 0
                logger.info(f"猫眼条目 {title} 缺海报，已用豆瓣检索补齐")
            else:
                # 补不到就累加 miss：达到上限后本轮不再尝试，也不会被排序挤到前面
                identity["miss"] = int(identity.get("miss") or 0) + 1
            identity.setdefault("ts", current_time.timestamp())
            self._resolved_identity[title] = identity

        left = len(pending) - resolved
        if left > 0:
            logger.warning(
                f"本次待识别 {len(pending)} 条，已识别 {resolved} 条"
                f"{'（超出时间预算 %ds）' % int(self._RESOLVE_TIME_BUDGET) if timed_out else ''}，"
                f"剩余 {left} 条下一轮优先处理（缺图/缺日期的排最前）")
        # 回写持久缓存（只存身份字典，MediaInfo 对象不序列化）
        try:
            self.save_data("identity_cache", self._resolved_identity)
        except Exception as e:
            logger.warning(f"保存识别缓存失败：{e}")

    def __prev_snapshot_map(self) -> dict:
        """上一份榜单快照按标题建索引（幂等，读不到就返回空表）。"""
        try:
            prev = self.get_data("snapshot") or {}
        except Exception as e:
            logger.debug(f"读取上一份榜单快照失败，按全新条目处理：{e}")
            return {}
        result: dict = {}
        if isinstance(prev, dict):
            for board in prev.get("boards") or []:
                for item in board.get("items") or []:
                    title = item.get("title")
                    if isinstance(title, str) and title:
                        result[title] = item
        return result

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
                # TMDB 原始发行日期（电影 release_date / 剧集 first_air_date），
                # 供全球榜条目把「只有年份」的上映信息补成完整日期（App 端直接渲染）
                raw_info = getattr(mediainfo, "tmdb_info", None) or {}
                # tmdb_info 理论上就是 dict；万一宿主换实现成了对象，这里直接 .get 会抛
                # AttributeError 把整轮刷新掀翻，所以先做类型兜底
                if not isinstance(raw_info, dict):
                    raw_info = {}
                rel_date = str(raw_info.get("release_date")
                               or raw_info.get("first_air_date")
                               or getattr(mediainfo, "release_date", None) or "").strip()[:10]
                return {
                    "media_source": mediainfo.media_source.value,
                    "media_id": str(mediainfo.media_id),
                    "poster": mediainfo.get_poster_image() or "",
                    "overview": mediainfo.overview or "",
                    "year": str(mediainfo.year or ""),
                    "type": mediainfo.type.value,
                    "title_cn": cn_title if cn_title and cn_title != title else "",
                    "release_date": rel_date,
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
            "release_date": "",
        }

    def __douban_poster(self, title: str, mtype: MediaType, year: str = "") -> tuple:
        """
        识别失败（或识别到了但没海报）时用豆瓣检索兜底补一张海报（只补图，不提供媒体身份）。

        豆瓣图片有防盗链（无 Referer 返回 418），App 直连时同样需要补 Referer，
        宿主图片代理则会自行带上，因此这里只负责给出可用的图片地址；
        同时带回豆瓣词条标题（多为中文），供英文榜单条目转中文展示。
        返回 (poster_url, cn_title)，检索失败时两者为空串。

        长英文片名在豆瓣常常整串搜不到（实测「Chiikawa the Movie: The Secret of Mermaid
        Island」返回空，但「Chiikawa」能命中《吉伊卡哇：人鱼之岛的秘密》），因此整串搜不到时
        按词逐步截短再试。截短检索会放宽匹配，必须靠 [year] 校验兜住 —— 只接受年份完全一致的
        候选，拿不到年份的条目不参与降级检索，宁可不补图也不张冠李戴。
        """
        def _query(keyword: str) -> list:
            # 节流：连打会被豆瓣拦（表现为返回空/异常），否则一轮补图全军覆没
            last = getattr(self, "_douban_last_at", 0.0)
            gap = time.monotonic() - last
            if gap < self._POSTER_FALLBACK_INTERVAL:
                time.sleep(self._POSTER_FALLBACK_INTERVAL - gap)
            try:
                data = RequestUtils(
                    headers={"User-Agent": self.get_random_user_agent()},
                    proxies=getattr(settings, "PROXY", None),
                    referer="https://movie.douban.com/",
                ).get_json("https://movie.douban.com/j/subject_suggest", params={"q": keyword})
            except Exception as e:
                # 抛异常基本就是被限流/网络不可达，标记后让调用方本轮收手
                self._douban_blocked = True
                logger.warning(f"猫眼条目 {title} 豆瓣补图请求失败（本轮停止补图）：{e}")
                return []
            finally:
                self._douban_last_at = time.monotonic()
            return [c for c in data if isinstance(c, dict)] if isinstance(data, list) else []

        def _pick(pool: list, strict_title: bool) -> tuple:
            if strict_title:
                # 优先同名词条；剧集优先带集数（豆瓣 suggestion 用 episode 标记剧集）
                same = [c for c in pool if c.get("title") == title]
                pool = same or pool
            if mtype == MediaType.TV:
                pool = sorted(pool, key=lambda c: 0 if c.get("episode") else 1)
            for candidate in pool:
                img = candidate.get("img")
                if isinstance(img, str) and img.startswith("http"):
                    # 缩略图换中等尺寸，避免 App 里糊掉
                    return (img.replace("/s_ratio_poster/", "/m_ratio_poster/"),
                            str(candidate.get("title") or "").strip())
            return "", ""

        # ① 整串检索：命中的都是原文匹配，直接采纳
        result = _pick(_query(title), strict_title=True)
        if result[0]:
            return result

        # ② 截短检索：按词逐步减少，命中后必须年份一致才采纳
        year = str(year or "").strip()
        if not re.fullmatch(r"(?:19|20)\d{2}", year):
            return "", ""
        words = [w for w in re.split(r"[\s:：\-—–·,，.。!！?？/]+", title) if w]
        for size in (1, 2, 3):
            if size >= len(words):
                continue  # 等于原长就是原串，①已经试过
            keyword = " ".join(words[:size])
            if len(keyword) < 3:
                continue
            matched = [c for c in _query(keyword) if str(c.get("year") or "") == year]
            result = _pick(matched, strict_title=False)
            if result[0]:
                logger.info(f"猫眼条目 {title} 豆瓣整串无结果，改用关键词「{keyword}」命中 {result[1]}")
                return result
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

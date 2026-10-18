"""
红果短剧榜单数据源（v3）。

抓取红果短剧官网（``hongguoduanju.com``）的 SSR 榜单页。官网没有公开的
榜单 API，但榜单页是服务端渲染的：榜单数据以 JSON 形式内联在 script 的
``data-fn-args`` 属性里，直接取 HTML 就能拿到结构化数据，**不需要签名、
不需要登录态**。

⚠️ 同一个 URL 会随机返回两种 SSR 输出（实测约各占一半），解析必须两种都认：
一种把 ``{isSuccess, rankList, pagination}`` 直接挂在 ``data-fn-name="r"``
脚本的参数第 3 项；另一种把同一份数据塞进 ``mergeLoaderData`` 的
``routerDataFnArgs``，且是「JSON 字符串再嵌一层 JSON」。只认其中任何一种，
表现都是「有的榜单抓得到、有的抓不到」，很容易被误判成限流。
``_extract_payload`` 统一按「递归找 rankList」定位，两种结构字段完全一致。

榜单页每次返回 20 条（``?page=N`` 翻页），四个官方热播榜：

- ``hot-drama``：红果热播榜
- ``hot-real-drama``：真人剧热播榜
- ``hot-comic-drama``：漫剧热播榜
- ``hot-ai-drama``：AI 剧热播榜

条目自带中文标题、竖版海报、热度/评分/收藏/点赞文本、题材标签与简介，
因此本数据源**不走宿主识别链**：短剧基本不在 TMDB/Douban 里，强行走识别
只会得到错误匹配。海报按字节 CDN 的缩放后缀从默认的 640 提到 1080
（源图上限 896×1280）。

⚠️ 官网只公开前 3 集的 360p 直链（``/video/public/{vid}/360p.mp4``），
第 4 集起返回 404，更高清晰度同样没有。``preview_url`` 只拼前三集。
"""

import datetime
import html as html_lib
import json
import re
import time
from typing import Any, Dict, List, Optional

import pytz

from app import schemas
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.network import RequestUtils
from app.sdk.media import resolve_media_identity

from .base import (
    RankBoard,
    RankSource,
    SOURCE_HONGGUO,
)


# 官网站点与榜单路由
SITE = "https://hongguoduanju.com"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 榜单代码 -> (SSR 路由, 展示名)；顺序即详情页与接口的展示顺序。
# 展示名不带「红果」前缀：官网这四个榜各自叫热播榜 / 真人剧热播榜 / 漫剧热播榜 /
# AI 剧热播榜，前缀在组装榜单名和推荐源名时统一加，避免拼成「红果红果热播榜」。
_BOARD_DEFS = (
    ('hot', 'hot-drama', '热播榜'),
    ('real', 'hot-real-drama', '真人剧热播榜'),
    ('comic', 'hot-comic-drama', '漫剧热播榜'),
    ('ai', 'hot-ai-drama', 'AI剧热播榜'),
)

# 只公开前若干集的试看直链
_PREVIEW_EPISODES = 3
_PREVIEW_QUALITY = '360p'

# 翻页保护：官网目前每个榜最多 5 页，多留一页余量
_MAX_PAGES = 6
# 翻页间隔：官网是 SSR 直出，节奏放慢避免被当成爬虫
_PAGE_INTERVAL = 0.4

# 内联数据挂在 script 的 data-fn-args 属性上（单引号或双引号包裹）。
# 直接全文扫属性而不是先匹配 <script> 标签：属性值本身带引号，遇到内容里含
# 「>」的标签也不会被截断。
_ARGS_RE = re.compile(r'''data-fn-args=(?:'([^']*)'|"([^"]*)")''', re.S)


def _safe_int(value, default: int = 0) -> int:
    """把字段转成整数，空串/非法值回退默认值。"""
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _upgrade_cover(url: str) -> str:
    """把海报缩略后缀从 640 提到 1080（源图上限 896×1280，再大不会放大）。"""
    text = str(url or "").strip()
    if not text:
        return ""
    return re.sub(r'(~tplv-)[a-z0-9]*:(\d+)(:\d+)?(\.[a-z0-9]+)?$',
                  r'\g<1>shrink:1080\g<3>\g<4>', text)


def _join_tags(value) -> str:
    """把标签数组收敛成逗号分隔字符串（快照只存标量，便于跨源复用）。"""
    if isinstance(value, (list, tuple)):
        return ",".join(str(v).strip() for v in value if str(v).strip())
    if value is None:
        return ""
    return str(value).strip()


def _find_rank_payload(node: Any, depth: int = 0) -> Optional[dict]:
    """在解析后的内联参数里递归找含 ``rankList`` 的字典。

    ⚠️ 官网同一个 URL 会随机返回两种 SSR 输出，只认一种会时灵时不灵：

    - A：数据直接挂在 ``data-fn-name="r"`` 脚本上，参数是 JSON 数组，第 3 项
      就是 ``{isSuccess, rankList, pagination}``；
    - B：页面被拆成流式分片，榜单被塞进 ``mergeLoaderData`` 的
      ``routerDataFnArgs`` 里，而且是「JSON 字符串里再嵌一层 JSON」，
      按固定下标取根本取不到。

    两种结构的字段名完全一致，所以统一按「找 rankList」定位，不区分来源。
    """
    if depth > 6:
        return None
    if isinstance(node, dict):
        if isinstance(node.get("rankList"), list):
            return node
        for value in node.values():
            found = _find_rank_payload(value, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_rank_payload(value, depth + 1)
            if found:
                return found
    elif isinstance(node, str):
        text = node.strip()
        # 只对看起来像 JSON 的字符串做二次解析，避免把普通文本当 JSON 反复试
        if text[:1] in ("{", "["):
            try:
                return _find_rank_payload(json.loads(text), depth + 1)
            except (ValueError, TypeError):
                return None
    return None


def _extract_payload(html_text: str) -> Optional[dict]:
    """从榜单页 HTML 里取出内联的榜单 JSON（兼容两种 SSR 输出）。"""
    if not html_text:
        return None
    for matched in _ARGS_RE.finditer(html_text):
        raw = matched.group(1)
        if raw is None:
            raw = matched.group(2)
        if not raw:
            continue
        try:
            args = json.loads(html_lib.unescape(raw))
        except (ValueError, TypeError):
            continue
        found = _find_rank_payload(args)
        if found:
            return found
    return None


def _safe_board_payload(board: dict, rows: List[Any]) -> Dict[str, Any]:
    """把存储中的榜单字典收窄为响应模型的合法字段。

    与 Netflix 源同理：快照是历史数据，升级后可能多字段或缺字段，显式挑字段并
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
            "series_id": str(row.get("series_id") or ""),
            "episode_cnt": _safe_int(row.get("episode_cnt")),
            "heat": str(row.get("heat") or ""),
            "score": str(row.get("score") or ""),
            "favorite": str(row.get("favorite") or ""),
            "likes": str(row.get("likes") or ""),
            "tags": str(row.get("tags") or ""),
            "status_tags": str(row.get("status_tags") or ""),
            "preview_url": str(row.get("preview_url") or ""),
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
        "type_name": text("type_name"),
        "category": text("category"),
        "category_name": text("category_name"),
        "url": text("url"),
        "count": _safe_int(board.get("count")),
        "updated_at": text("updated_at"),
        "items": items,
    }


class HongguoSource(RankSource):
    """红果短剧官网热播榜数据源。

    抓官网 SSR 榜单页，解析内联 JSON，补海报清晰度后落快照，并向主类提供统一
    榜单结构供 App、推荐页与详情页消费。

    短剧不在 TMDB/Douban 里，本数据源不做媒体识别，直接使用官网自带的中文
    标题、海报与简介；因此条目没有 media_source / media_id，宿主无法据此自动
    订阅，仅用于榜单展示。
    """

    source_id = SOURCE_HONGGUO
    source_name = "红果短剧榜单"

    # 榜单代码 -> 展示名（加 key 即自动进设置页多选）
    _BOARD_NAMES = {code: name for code, _route, name in _BOARD_DEFS}
    # 榜单代码 -> SSR 路由
    _BOARD_ROUTES = {code: route for code, route, _name in _BOARD_DEFS}
    # 默认抓取全部四个榜
    _DEFAULT_BOARDS = [code for code, _route, _name in _BOARD_DEFS]
    # 每个榜单最多抓取的条目数（官网每页 20 条）
    _DEFAULT_LIMIT = 20
    # 推荐页分类：红果全是剧集
    _RECOMMEND_TYPE = '电视剧'

    def __init__(self, host: Any):
        super().__init__(host)
        object.__setattr__(self, "_boards", list(self._DEFAULT_BOARDS))
        object.__setattr__(self, "_limit", self._DEFAULT_LIMIT)

    def apply_config(self, config: Dict[str, Any]) -> None:
        """接收主类剥离前缀后的红果配置。"""
        super().apply_config(config)
        boards = (config or {}).get("boards")
        if isinstance(boards, list) and boards:
            picked = [str(b) for b in boards if str(b) in self._BOARD_ROUTES]
            if picked:
                object.__setattr__(self, "_boards", picked)
        limit = _safe_int((config or {}).get("limit"), self._DEFAULT_LIMIT)
        if limit > 0:
            object.__setattr__(self, "_limit", limit)

    def fetch(self) -> Dict[str, Any]:
        """执行一次红果榜单抓取，返回摘要字典。"""
        return self.__refresh_hongguo() or {}

    def rank_boards(self, board_type: str = None, kind: str = None,
                    limit: int = None) -> List[RankBoard]:
        """
        把快照里的红果榜单转成统一模型。

        :param board_type: 榜单代码（hot/real/comic/ai），不传返回快照里全部榜单
        :param kind: 未使用（红果全部是剧集），保留以统一签名
        :param limit: 每个榜单最多返回的条目数
        """
        selected: List[RankBoard] = []
        for board in self.boards():
            if board_type and str(board.get("type") or "") != str(board_type):
                continue
            rows = board.get("items") or []
            if limit and limit > 0:
                rows = rows[:limit]
            payload = _safe_board_payload(board, rows)
            payload["source"] = self.source_id
            # count 统一表示「本次返回的条目数」（存储里的 count 是抓取时原始条数，
            # 被 limit 截断后会与 items 长度不符，跨源聚合时容易误读）
            payload["count"] = len(payload["items"])
            payload["board_type"] = payload.get("type") or ""
            selected.append(RankBoard(**payload))
        return selected

    def recommend_sources(self) -> list:
        """按当前配置构造要注册到推荐页的红果数据源（每个榜单一个）。"""
        sources = []
        for code in self._boards:
            name = self._BOARD_NAMES.get(code, code)
            sources.append(schemas.RecommendMediaSource(
                name=f"红果{name}",
                api_path=(f"plugin/GlobalRankPro/rank-list?"
                          f"source={self.source_id}&board_type={code}"),
                type=self._RECOMMEND_TYPE,
            ))
        return sources

    def __fetch_html(self, url: str) -> str:
        """下载榜单页 HTML。

        响应不一定带 charset，直接取 ``response.text`` 会让 requests 靠嗅探猜
        编码，中文榜单页经常被猜成 latin-1 系导致整页乱码。这里按字节取回后
        显式按 UTF-8 解码。
        """
        response = RequestUtils(
            headers={"User-Agent": _UA},
            proxies=getattr(settings, "PROXY", None),
        ).get_res(url)
        if not response:
            raise RuntimeError(f"红果榜单页请求失败：{url}")
        status = getattr(response, "status_code", None)
        if status and int(status) != 200:
            raise RuntimeError(f"红果榜单页返回 {status}：{url}")
        content = getattr(response, "content", None)
        if content is None:
            return response.text or ""
        if isinstance(content, str):
            return content
        return content.decode("utf-8", errors="replace")

    def __to_item(self, row: dict) -> Dict[str, Any]:
        """把榜单页的一条原始记录转成统一快照条目。"""
        vids = row.get("episodeVids") or []
        if not isinstance(vids, list):
            vids = []
        preview = ",".join(
            f"{SITE}/video/public/{vid}/{_PREVIEW_QUALITY}.mp4"
            for vid in vids[:_PREVIEW_EPISODES] if str(vid).strip())
        return {
            "rank": _safe_int(row.get("rank")),
            "title": str(row.get("title") or ""),
            # 标题本身已是中文，title_cn 同源，保证跨源展示逻辑一致
            "title_cn": str(row.get("title") or ""),
            "series_id": str(row.get("seriesId") or row.get("id") or ""),
            "episode_cnt": len(vids),
            "heat": str(row.get("heatText") or ""),
            "score": str(row.get("scoreText") or ""),
            "favorite": str(row.get("favoriteText") or ""),
            "likes": str(row.get("likeText") or ""),
            "tags": _join_tags(row.get("tags")),
            "status_tags": _join_tags(row.get("statusTags")),
            "preview_url": preview,
            "poster": _upgrade_cover(row.get("cover")),
            "overview": str(row.get("description") or ""),
            "media_source": "",
            "media_id": "",
            "year": "",
            "release_date": "",
        }

    def __fetch_board(self, code: str) -> Optional[dict]:
        """抓取单个榜单（按 limit 逐页取），返回榜单字典。"""
        route = self._BOARD_ROUTES.get(code)
        if not route:
            return None
        name = self._BOARD_NAMES.get(code, code)
        items: List[dict] = []
        page = 1
        while len(items) < self._limit and page <= _MAX_PAGES:
            url = f"{SITE}/rank/{route}"
            if page > 1:
                url = f"{url}?page={page}"
            payload = _extract_payload(self.__fetch_html(url))
            if not payload or not payload.get("isSuccess"):
                break
            rows = payload.get("rankList") or []
            if not isinstance(rows, list) or not rows:
                break
            for row in rows:
                if isinstance(row, dict):
                    items.append(self.__to_item(row))
            pagination = payload.get("pagination") or {}
            if not pagination.get("nextUrl"):
                break
            page += 1
            if len(items) < self._limit:
                time.sleep(_PAGE_INTERVAL)
        if not items:
            return None
        items = items[:self._limit]
        return {
            "board": f"红果{name}",
            "type": code,
            "type_name": name,
            "category": code,
            "category_name": name,
            "url": f"{SITE}/rank/{route}",
            "count": len(items),
            "updated_at": "",
            "items": items,
        }

    def __refresh_hongguo(self) -> Dict[str, Any]:
        """抓取并解析红果榜单，落快照。"""
        if not self._boards:
            return {"board_count": 0, "item_count": 0,
                    "message": "未配置任何红果榜单"}
        boards: List[dict] = []
        for code in self._boards:
            try:
                board = self.__fetch_board(code)
            except Exception as e:
                logger.error(f"红果{self._BOARD_NAMES.get(code, code)}抓取失败：{e}")
                continue
            if board:
                boards.append(board)
            time.sleep(_PAGE_INTERVAL)
        if not boards:
            logger.error("红果榜单未抓取到任何条目（网络异常或页面结构变化）")
            return {"board_count": 0, "item_count": 0,
                    "message": "抓取失败：网络异常或页面结构变化"}

        now = datetime.datetime.now(tz=pytz.timezone(settings.TZ))
        updated_at = now.strftime("%Y-%m-%d %H:%M:%S")
        total = sum(len(board["items"]) for board in boards)
        for board in boards:
            board["updated_at"] = updated_at
        self.save_data("snapshot", {
            "updated_at": updated_at,
            "total": total,
            "boards": boards,
        })
        logger.info(f"红果榜单刷新完成：{len(boards)} 个榜单 {total} 条")
        return {"board_count": len(boards), "item_count": total, "message": ""}

    # ==================== 对外接口 ====================

    def rank_list(self, board_type: str = None, kind: str = None,
                  page: int = 1, limit: int = None
                  ) -> schemas.Response[List[schemas.MediaInfo]]:
        """
        推荐页/浏览页使用的榜单列表接口。

        扁平返回指定榜单（默认快照里第一个榜）的全部条目，按名次排序；
        board_type 不传时返回全部榜单，先按榜单顺序、再按名次。
        """
        boards = self.boards()
        if board_type:
            boards = [b for b in boards
                      if str(b.get("type") or "") == str(board_type)]
        rows: List[dict] = []
        for index, board in enumerate(boards):
            for row in (board.get("items") or []):
                if isinstance(row, dict):
                    rows.append((index, row))
        rows.sort(key=lambda pair: (pair[0], _safe_int(pair[1].get("rank"))))

        result: List[schemas.MediaInfo] = []
        for _index, row in rows:
            media_source, media_id = resolve_media_identity(
                media_source=row.get("media_source"),
                media_id=row.get("media_id"),
            )
            title = str(row.get("title_cn") or "") or str(row.get("title") or "")
            media = schemas.MediaInfo(
                media_source=media_source,
                media_id=media_id,
                title=title,
                year=str(row.get("year") or "") or None,
                type=self._RECOMMEND_TYPE,
                poster_path=str(row.get("poster") or "") or None,
                overview=str(row.get("overview") or "") or None,
            )
            if media.title and media.year:
                media.title_year = f"{media.title} ({media.year})"
            result.append(media)
            if limit and limit > 0 and len(result) >= limit:
                break
        return schemas.Response(success=True, data=result)

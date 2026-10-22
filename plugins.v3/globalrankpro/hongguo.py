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

海报是字节图床（``p3/p6-novel.byteimg.com``）的绝对地址。桌面浏览器直连没问题，
但手机端（移动网络解析不稳、App 内 WebView 丢跨域大图）经常出不来，所以海报
一律换成宿主自己的 ``/poster`` 接口转发：客户端只要能访问 MoviePilot 就能出图。
只有宿主既没配 ``APP_DOMAIN``、又拿不到请求地址时，才退回官网原图地址。
"""

import datetime
import hashlib
import html as html_lib
import json
import re
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit

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

# ---- 海报代理 ----
# 红果海报是字节图床的绝对地址（p3/p6-novel.byteimg.com/novel-pic/…）。桌面浏览器
# 直连没问题，但手机端经常拿不到：移动网络对字节 CDN 的解析/连通性不稳定，App 内
# WebView 的图片加载器也可能把跨域大图直接丢掉。把海报改成经宿主转发的地址后，
# 客户端只要能打开 MoviePilot 就能出图，不再依赖第三方 CDN。
#
# 只允许代理字节图床下的图片：插件接口是匿名可访问的（<img> 带不了请求头），
# 不限制就等于给宿主机开了个开放代理。
#   域名后缀 -> 允许的路径前缀（官网现在走 novel-pic，早期/部分封面走电商 CDN 的 obj）
_POSTER_ALLOWED = (
    (".byteimg.com", ("/novel-pic/",)),
    (".ecombdimg.com", ("/obj/",)),
    # Apple Music 专辑封面（音乐榜 apple 源）。宿主的图片代理白名单
    # （SECURITY_IMAGE_DOMAINS）默认没有 mzstatic.com，而手机又连不上境外的
    # mzstatic —— 代理被拒 + 直连不通 = 封面全空。这里让插件代为转发：
    # 容器能出网、并且图片会落到本地盘缓存，一张只回源一次。
    (".mzstatic.com", ("/image/",)),
)
# 进程内缓存：一屏 20 张、推荐页更多，翻页/回退会反复请求同一批图
_POSTER_CACHE: "OrderedDict[str, Tuple[bytes, str]]" = OrderedDict()
_POSTER_CACHE_MAX = 64
# 海报是静态资源，让浏览器长期缓存，减少宿主回源
_POSTER_CACHE_CONTROL = "public, max-age=604800"
# 落盘缓存目录（v1.4.3 优化③）：v3 每次调度重建插件实例，内存 LRU 必丢；
# 图放宿主 config 目录下，跨重启复用，服务端基本零回源
_POSTER_DISK_DIR = None  # 惰性初始化（首次用到时创建）


def _poster_disk_path(target: str):
    """海报落盘路径：``<config>/cache/hongguo_posters/<sha1>.img`` + ``.meta``。

    宿主拿不到可写目录时返回 ``None``（只走内存缓存）。``.meta`` 存 Content-Type。
    """
    global _POSTER_DISK_DIR
    try:
        if _POSTER_DISK_DIR is None:
            config_dir = str(getattr(settings, "CONFIG_DIR", "") or "/config")
            _POSTER_DISK_DIR = Path(config_dir) / "cache" / "hongguo_posters"
        digest = hashlib.sha1(target.encode("utf-8")).hexdigest()
        base = _POSTER_DISK_DIR / digest
        return (base.with_suffix(".img"), base.with_suffix(".meta"))
    except Exception:
        return None


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
            # 红果不做媒体识别，恒空；字段与另两源对齐，App 端统一取值
            "media_type": str(row.get("media_type") or ""),
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

    # ------------------------------------------------------------------
    # 海报代理
    # ------------------------------------------------------------------
    def _plugin_id(self) -> str:
        """宿主插件 ID（即插件类名），用于拼插件接口路径。"""
        return type(self._host).__name__ or "GlobalRankPro"

    def _api_base(self) -> str:
        """宿主对外访问地址（含 ``/api/v1``），取不到时返回空串。

        优先用宿主配置的 ``APP_DOMAIN``：它和用户实际访问的域名一致，手机、
        桌面、App 拿到的都是同一个可访问地址。没配就退回直连 CDN 原图。
        """
        domain = str(getattr(settings, "APP_DOMAIN", "") or "").strip().rstrip("/")
        if not domain:
            return ""
        prefix = str(getattr(settings, "API_V1_STR", "/api/v1") or "/api/v1")
        return f"{domain}/{prefix.strip('/')}"

    def poster_url_for(self, raw: str, base: str = "") -> str:
        """把 CDN 原图地址换成宿主转发地址（取不到宿主地址时原样返回）。

        :param base: 请求方实际访问的宿主地址（来自 ``request.base_url``），
                     仅作 ``APP_DOMAIN`` 未配置时的兜底。默认优先用
                     ``APP_DOMAIN``：它是管理员显式配置的对外地址，反向代理
                     改了 Host 头时也不会拼错。
        """
        url = str(raw or "").strip()
        if not url:
            return url
        prefix = str(getattr(settings, "API_V1_STR", "/api/v1") or "/api/v1")
        root = self._api_base()
        if not root:
            host = str(base or "").strip().rstrip("/")
            if not host:
                return url
            root = f"{host}/{prefix.strip('/')}"
        return f"{root}/plugin/{self._plugin_id()}/poster?u={quote(url, safe='')}"

    def poster_response(self, url: str):
        """下载并回传海报字节（插件 ``/poster`` 接口的实现）。

        只放行字节图床白名单里的图片，其余一律 403——这个接口是匿名可访问的，
        不做白名单就等于开放代理。
        """
        from fastapi import HTTPException
        from fastapi.responses import Response

        target = str(url or "").strip()
        if not target:
            raise HTTPException(status_code=400, detail="缺少海报地址")

        parts = urlsplit(target)
        host = (parts.hostname or "").lower()
        allowed = any(
            parts.scheme in ("http", "https")
            and host.endswith(suffix)
            and parts.path.startswith(prefixes)
            for suffix, prefixes in _POSTER_ALLOWED
        )
        if not allowed:
            raise HTTPException(status_code=403, detail="不允许代理该地址")

        cached = _POSTER_CACHE.get(target)
        if cached is not None:
            _POSTER_CACHE.move_to_end(target)
            return Response(content=cached[0], media_type=cached[1],
                            headers={"Cache-Control": _POSTER_CACHE_CONTROL})

        # 落盘缓存（v1.4.3 优化③）：内存 LRU 在 v3 每次调度重建实例时必丢，
        # 重启后同一张海报反复回字节图床下载。先查磁盘，命中就不出门。
        disk = _poster_disk_path(target)
        if disk is not None:
            img_file, meta_file = disk
            if img_file.exists() and meta_file.exists():
                try:
                    content = img_file.read_bytes()
                    media_type = meta_file.read_text("utf-8").strip() or "image/jpeg"
                    if content and media_type.startswith("image/"):
                        _POSTER_CACHE[target] = (content, media_type)
                        while len(_POSTER_CACHE) > _POSTER_CACHE_MAX:
                            _POSTER_CACHE.popitem(last=False)
                        return Response(
                            content=content, media_type=media_type,
                            headers={"Cache-Control": _POSTER_CACHE_CONTROL})
                except Exception as e:
                    logger.warning(f"红果海报读盘缓存失败，回源下载：{e}")

        try:
            response = RequestUtils(
                headers={"User-Agent": _UA, "Referer": f"{SITE}/"},
                proxies=getattr(settings, "PROXY", None),
            ).get_res(target)
        except Exception as e:
            logger.error(f"红果海报下载异常：{e}")
            raise HTTPException(status_code=502, detail="海报下载失败")

        status = getattr(response, "status_code", None)
        try:
            code = int(status) if status is not None else 0
        except (TypeError, ValueError):
            code = 0
        content = getattr(response, "content", b"") or b""
        if not response or code != 200 or not isinstance(content, (bytes, bytearray)) \
                or not content:
            raise HTTPException(status_code=502,
                                detail=f"海报下载失败（{code or '无响应'}）")

        media_type = "image/jpeg"
        headers_obj = getattr(response, "headers", None)
        if headers_obj:
            media_type = str(headers_obj.get("Content-Type") or media_type).split(";")[0]
        if not media_type.startswith("image/"):
            raise HTTPException(status_code=502, detail="海报响应不是图片")

        _POSTER_CACHE[target] = (content, media_type)
        while len(_POSTER_CACHE) > _POSTER_CACHE_MAX:
            _POSTER_CACHE.popitem(last=False)
        # 写盘（失败不影响返回，内存缓存仍有效）
        if disk is not None:
            img_file, meta_file = disk
            try:
                img_file.parent.mkdir(parents=True, exist_ok=True)
                img_file.write_bytes(content)
                meta_file.write_text(media_type, "utf-8")
            except Exception as e:
                logger.warning(f"红果海报写盘缓存失败：{e}")
        return Response(content=content, media_type=media_type,
                        headers={"Cache-Control": _POSTER_CACHE_CONTROL})

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
        # 上一次快照：某个榜这次抓空时用它顶上，避免该榜在 App 里整个消失
        prev: Dict[str, dict] = {}
        try:
            old = self.get_data("snapshot") or {}
            for b in (old.get("boards") or []):
                if isinstance(b, dict) and b.get("type") and b.get("items"):
                    prev[str(b["type"])] = b
        except Exception as e:
            logger.warning(f"读取红果旧快照失败（忽略，按全新抓取处理）：{e}")

        boards: List[dict] = []
        fresh: set = set()
        for code in self._boards:
            name = self._BOARD_NAMES.get(code, code)
            board = None
            # 抓空重试一次：官网偶发返回空 rankList（限流 / SSR 抖动），
            # 以前这里直接跳过 → 该榜从快照里消失，App 分榜栏跟着少一个
            for attempt in range(2):
                try:
                    board = self.__fetch_board(code)
                except Exception as e:
                    logger.error(f"红果{name}抓取失败（第 {attempt + 1} 次）：{e}")
                    board = None
                if board:
                    break
                if attempt == 0:
                    time.sleep(_PAGE_INTERVAL)
            if board:
                boards.append(board)
                fresh.add(code)
            else:
                kept = prev.get(code)
                if kept:
                    boards.append(kept)
                    logger.warning(
                        f"红果{name}本次未抓到条目，沿用上次快照"
                        f"（{kept.get('updated_at')}，{len(kept.get('items') or [])} 条）"
                    )
                else:
                    logger.warning(f"红果{name}本次未抓到条目，且没有可沿用的旧数据")
            time.sleep(_PAGE_INTERVAL)
        if not boards:
            logger.error("红果榜单未抓取到任何条目（网络异常或页面结构变化）")
            return {"board_count": 0, "item_count": 0,
                    "message": "抓取失败：网络异常或页面结构变化"}

        now = datetime.datetime.now(tz=pytz.timezone(settings.TZ))
        updated_at = now.strftime("%Y-%m-%d %H:%M:%S")
        total = sum(len(board["items"]) for board in boards)
        for board in boards:
            # 只有真抓到条目的榜才盖新时间，沿用的旧榜保留它自己的时间戳，
            # 免得 App 上把一天前的数据显示成「刚刚更新」
            if str(board.get("type")) in fresh:
                board["updated_at"] = updated_at
        self.save_data("snapshot", {
            "updated_at": updated_at,
            "total": total,
            "boards": boards,
        })
        stale = [self._BOARD_NAMES.get(c, c) for c in self._boards if c not in fresh]
        logger.info(
            f"红果榜单刷新完成：{len(boards)} 个榜单 {total} 条"
            + (f"（沿用旧数据：{'、'.join(stale)}）" if stale else "")
        )
        return {"board_count": len(boards), "item_count": total,
                "message": (f"以下榜单未抓到条目，沿用上次数据：{'、'.join(stale)}"
                            if stale else "")}

    # ==================== 对外接口 ====================

    def rank_list(self, board_type: str = None, kind: str = None,
                  page: int = 1, limit: int = None, base: str = ""
                  ) -> schemas.Response[List[schemas.MediaInfo]]:
        """
        推荐页/浏览页使用的榜单列表接口。

        扁平返回指定榜单（默认快照里第一个榜）的全部条目，按名次排序；
        board_type 不传时返回全部榜单，先按榜单顺序、再按名次。

        :param base: 调用方实际访问宿主的地址，用于拼海报转发地址
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
                poster_path=(self.poster_url_for(
                    str(row.get("poster") or ""), base) or None),
                overview=str(row.get("overview") or "") or None,
            )
            if media.title and media.year:
                media.title_year = f"{media.title} ({media.year})"
            result.append(media)
            if limit and limit > 0 and len(result) >= limit:
                break
        return schemas.Response(success=True, data=result)

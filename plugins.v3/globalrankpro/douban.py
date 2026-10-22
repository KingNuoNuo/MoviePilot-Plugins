"""豆瓣检索：给榜单条目补海报与评分。

为什么放在插件侧
----------------
猫眼榜单（热度榜 / 网络剧榜 / 综艺榜 / 票房榜）和 Netflix 榜单本身都不带评分。
要让 App 显示评分，只能额外去豆瓣查一次；如果让 App 自己去查，海报墙一屏十几条
就是十几次跨境 HTTPS 请求，既慢又容易被豆瓣限流（实测连打几次后直接返回空）。

宿主机网络更接近豆瓣，且结果可以持久缓存、全端共享，所以评分统一在插件里补好，
随 ``/rank`` 一次性吐给 App（字段 ``vote_average``）。

豆瓣接口的坑（2026-10 实测）
---------------------------
- **旧检索接口 ``movie.douban.com/j/subject_suggest`` 已失效**：无论查什么都返回
  空数组 ``[]``（HTTP 200），看起来像「豆瓣没这部片」，其实是接口不再给数据。
  ``movie.douban.com/j/search_subjects`` 还把 ``tag`` 当分类标签而不是关键词。
- 现在用移动站的 **``m.douban.com/rexxar/api/v2/search``**：一次请求直接带
  ``rating.value``，中文名 / 年份 / 封图都有 —— 不用再补第二次 ``subject_abstract``，
  **一条 = 1 个请求**（旧方案是 2 个），限流风险和对豆瓣的压力都减半。
- 必须带 ``Referer: https://m.douban.com/``；请求过密会被拦（表现为返回空或
  「检测到异常请求」文案）。这里做了全局节流与熔断：连续失败即整体收手一段时间，
  已拿到的照常落库。
"""
from __future__ import annotations

import datetime
import random
import re
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.network import RequestUtils

_SEARCH_URL = "https://m.douban.com/rexxar/api/v2/search"
# v1.4.0 两步兜底：rexxar /search 会被定向封禁（HTTP 403，IP 下所有 UA 全灭，
# 但同域的 subject 详情接口仍然 200）。封禁期间改走
# subject_suggest（拿 id/type，无评分）→ rexxar subject 详情（拿 rating.value）。
_SUGGEST_URL = "https://movie.douban.com/j/subject_suggest"
_SUBJECT_URL = "https://m.douban.com/rexxar/api/v2/{kind}/{sid}"
# v1.4.1 三级兜底：suggest 也开始「200 + 空数组」式软封锁后，最后退到
# m 站搜索页（服务端渲染，HTML 里内联 /subject/<id> 链接）→ subject 详情。
_MSEARCH_URL = "https://m.douban.com/search/?query={q}"
_MSEARCH_SID_RE = re.compile(r"/subject/(\d+)")
# m 站搜索页要手机 UA 才出结果（桌面 UA 返回空壳页，实测）
_MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
# /search 被封后的跳过窗口：期间不浪费请求打它，直接走两步
_SEARCH_DEAD_COOLDOWN = 1800.0
_REFERER = "https://m.douban.com/"

# 请求间隔：连打会被拦（表现为返回空 / 抛异常）；0.35s 会触发瞬时限流，1.2s 实测稳定
_REQUEST_INTERVAL = 1.2
# CookieCloud 取豆瓣 Cookie 的缓存时长（秒）；浏览器重新同步即生效，插件不用重启
_COOKIE_TTL = 6 * 3600
# 取 Cookie 失败的短期重试间隔：CookieCloud 服务/网络偶发抽风时不能一失败就
# 6 小时不带 Cookie 跑（豆瓣匿名通道早被封，等于 6 小时查分全灭）
_COOKIE_RETRY = 300.0
_cookie_cache: Optional[str] = None
_cookie_ts = 0.0
# 并发线程同时发现 Cookie 过期时只让一个去下载（下载的是 800KB 的整包）
_COOKIE_LOCK = threading.Lock()
# 熔断冷却：一旦某次请求抛异常、或连续多次拿到空结果，这段时间内不再打豆瓣
# v1.4.0：600→120 —— 榜单场景全是当年新片，「豆瓣真搜不到」的连空是常态不是限流，
# 熔断 10 分钟会把能查到的片也一并断粮；120s 够穿过一次豆瓣的短暂拦截窗口
_BLOCK_COOLDOWN = 120.0
# 连续多少次「查了但一条候选都没有」才启动复核（正常检索几乎不会连空）
# v1.4.0：12→20 —— 20 条的榜单里新片占一半以上，12 在真实场景必触发
_EMPTY_STREAK_LIMIT = 20
# 保底探针：一部豆瓣必有、常年有分的经典片。连空到阈值先拿它试一次，
# 探针命中 = 豆瓣没拦我们，是这些新片真没有 → 清零继续；探针也空 = 真被限流 → 熔断
_PROBE_TITLE = "霸王别姬"

# 评分缓存有效期：豆瓣评分变动很慢，30 天足够；还没开分的条目（新片）3 天后再试
RATING_TTL = 30 * 86400
RATING_TTL_MISS = 3 * 86400

# 检索策略版本：换接口或换匹配规则时 +1，老缓存自动判过期重查。
# v1 = movie.douban.com/j/subject_suggest（该接口已失效，之前存下来的空结果全是假的）
# v2 = m.douban.com/rexxar/api/v2/search（一次请求带 rating.value）
# v3 = 修好「rexxar 返回 None」的排障（v2 存下来的空值多半是拿不到响应，不是真没分）
# v4 = v1.4.0 两步兜底上线：今天 /search 被封期间按「真没有」缓存下来的空值全是假的，作废重查
RATING_VER = 7

# 「未开分」空缺的缓存版本，独立于 RATING_VER：
# 有分的条目走 RATING_VER（换检索策略才作废）；无分的空缺走 MISS_RATING_VER
# —— 豆瓣瞬时不稳定（拦截窗口 / 偶发空池）会把「其实有分的片」误存成无分，
# 这种毒缓存一存就是 RATING_TTL_MISS 三天，App 端怎么重试都被缓存挡回。
# 缓存判定规则变化时 +1，即可只冲掉空缺缓存、保住 30 天的有效评分。
# v8 (v1.4.3)：v1.4.2 期间豆瓣抖动，一批带年份键的假空缺被缓存（App 重试 4 轮全被挡），
# 升一档全部作废重查；同时上线「空缺可信度」判定（见 [miss_cacheable]）。
MISS_RATING_VER = 9

_UAS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
)

_last_request_at = 0.0
# 全局限流闸：等间隔 + 发请求必须在锁内完成，否则并发线程同时看到「间隔已到」
# 一起放行，60 条并发查询两秒内全砸向豆瓣触发瞬时限流（2026-10-20 实锤）
_RATE_LOCK = threading.Lock()
# 共享评分缓存（插件数据键 douban_rating）的读-改-写互斥。
# ⚠️ 这份缓存有**三个**写入方：/rating 接口、猫眼榜单补分、Netflix 榜单补分。
# 后两者经由 [fill_ratings] 的 store_get / store_save 回调写入，读-改-写的跨度
# 是整整一轮识别（几分钟、几十次网络请求）。共用一把锁 + 写前重读最新值，
# 才不会让「几分钟前读到的旧副本」覆盖掉 App 刚查到的评分。
RATING_STORE_LOCK = threading.Lock()


@contextmanager
def rating_store_lock():
    """获取共享评分缓存的写锁（供 /rating 与 fill_ratings 共用）。

    包成 context manager 是为了让「拿到锁之后抛异常」也不会把锁带死 ——
    一次网络抖动就锁死整份缓存写回，比不锁更糟。
    """
    RATING_STORE_LOCK.acquire()
    try:
        yield
    finally:
        RATING_STORE_LOCK.release()
_blocked_until = 0.0
_empty_streak = 0
_probe_depth = 0
_search_dead_until = 0.0
# 最近一次原始响应（只给 /rating?debug=1 排障用，正常路径不读）
last_raw: Any = None
# 最近一次 HTTP 状态码（0 = 还没发过请求 / 抛了异常）
last_status = 0


def is_blocked() -> bool:
    """本轮是否已被豆瓣限流（熔断中）。"""
    return time.monotonic() < _blocked_until


def _trip(reason: str) -> None:
    global _blocked_until, _empty_streak
    _blocked_until = time.monotonic() + _BLOCK_COOLDOWN
    # 冷却结束从零计：不清的话冷却一过、第一条查不到就会立刻再次熔断
    _empty_streak = 0
    logger.warning(
        f"豆瓣请求失败，{int(_BLOCK_COOLDOWN / 60)} 分钟内不再尝试：{reason}")


def _confirm_block() -> None:
    """连空到阈值后的复核：拿保底片探一次，命中说明只是这些片真没有。

    v1.4.0：新片榜单里「豆瓣真搜不到」的连空是常态，直接按限流熔断会把
    后面能查到的片也一并断粮。只有探针也拿不到结果才真的熔断。
    探针自身的空结果会再进 [_confirm_block]（streak 又 +1），用 [_probe_depth]
    拦住：探针失败 = 实锤，直接熔断，不再递归。
    """
    global _empty_streak, _probe_depth
    if _empty_streak < _EMPTY_STREAK_LIMIT:
        return
    if _probe_depth:
        _trip(f"连续 {_empty_streak} 次检索为空（含保底探针）")
        return
    _probe_depth += 1
    try:
        probe_pool = _search(_PROBE_TITLE)
        if not probe_pool:
            # /search 被封的窗口里探针也拿不到，改走 suggest 两步 ——
            # suggest+subject 能通就说明豆瓣没拦我们这个 IP
            cand = _suggest_pick(_PROBE_TITLE, None)
            if cand:
                probe_pool = [_subject_rate(str(cand.get("type")), str(cand.get("id")))]
    finally:
        _probe_depth -= 1
    if probe_pool:
        logger.info(f"连续 {_empty_streak} 次检索为空，但保底探针命中，豆瓣未限流，继续")
        _empty_streak = 0
    else:
        _trip(f"连续 {_empty_streak} 次检索为空且保底探针失效")


def _douban_cookie() -> str:
    """从 CookieCloud 取豆瓣登录 Cookie（v1.4.2），取不到返回空串。

    豆瓣对匿名爬取的封锁越来越狠（403 / 400 / 「有异常请求请登录」页），带登录态
    的请求走的是另一套宽容得多的配额。Cookie 数据 6 小时刷新一次 —— 浏览器那边
    重新同步 CookieCloud 即可生效，插件不用重启。

    ⚠️ 两处细节：
    - 取 Cookie 是整包下载（实测 800KB 级），必须在豆瓣请求的限流锁**外面**做，
      否则一次下载就把所有豆瓣查询堵在后面；
    - 失败只冷却 [_COOKIE_RETRY]（5 分钟），成功才按 [_COOKIE_TTL] 缓存 —— 早先
      失败也按 6 小时记，CookieCloud 抖一下就有 6 小时全部匿名请求。
    """
    global _cookie_cache, _cookie_ts
    now = time.time()
    ttl = _COOKIE_TTL if _cookie_cache else _COOKIE_RETRY
    if _cookie_cache is not None and now - _cookie_ts < ttl:
        return _cookie_cache
    if not _COOKIE_LOCK.acquire(blocking=False):
        # 另一个线程正在取，本次先用上一次的结果（可能是空，下一轮自然补上）
        return _cookie_cache or ""
    try:
        # 拿到锁后再判一次：等锁期间别的线程可能已经取好了
        now = time.time()
        ttl = _COOKIE_TTL if _cookie_cache else _COOKIE_RETRY
        if _cookie_cache is not None and now - _cookie_ts < ttl:
            return _cookie_cache
        _cookie_ts = now
        _cookie_cache = ""
        try:
            from app.helper.cookiecloud import CookieCloudHelper
            data, err = CookieCloudHelper().download()
            # data = {域名: 已拼好的 Cookie 字符串}；key 可能带端口等后缀，模糊匹配
            cookie = ""
            for domain, value in (data or {}).items():
                if "douban.com" in str(domain) and isinstance(value, str) \
                        and value.strip():
                    cookie = value.strip()
                    break
            if err:
                logger.info(f"CookieCloud 未取到豆瓣 Cookie：{err}")
            if cookie:
                _cookie_cache = cookie
                logger.info("CookieCloud 命中豆瓣 Cookie，豆瓣请求改走登录态")
            else:
                logger.info("CookieCloud 无豆瓣 Cookie，先按匿名请求（5 分钟后再试）")
        except Exception as exc:
            logger.info(f"CookieCloud 取豆瓣 Cookie 异常：{exc}")
        return _cookie_cache
    finally:
        _COOKIE_LOCK.release()


def _get_json(url: str, params: Dict[str, Any]) -> Any:
    """带节流与熔断的 GET，失败一律返回 None（调用方按「没查到」处理）。

    必须走 ``RequestUtils.request``（返回 Response 对象）而不是 ``get`` / ``get_json``：
    - ``get`` 返回的是**响应文本字符串**，没有 status_code / json()，200 也会被判成失败；
    - ``get_json`` 在「非 200 / 非 JSON」时一律返回 None，排障时分不清「空列表」和「被拦」。
    状态码与前 300 字留在 [last_raw]（只给 debug=1 看）。
    """
    global _last_request_at, last_raw, last_status
    if is_blocked():
        return None
    # Cookie 在锁外取：CookieCloud 下载是整包（800KB 级），放进限流锁会把
    # 排在后面的豆瓣查询一起堵住；取 Cookie 本身也有自己的短锁
    cookie = _douban_cookie()
    with _RATE_LOCK:
        gap = time.monotonic() - _last_request_at
        if gap < _REQUEST_INTERVAL:
            time.sleep(_REQUEST_INTERVAL - gap)
        _last_request_at = time.monotonic()
        # 上一次请求的状态码不能留给本次判断：异常分支不会更新 status_code，
        # 上一次的 403 会让调用方误判「/search 又被封了」
        last_status = 0
        try:
            headers = {"User-Agent": random.choice(_UAS), "Referer": _REFERER}
            if cookie:
                headers["Cookie"] = cookie
            client = RequestUtils(
                headers=headers,
                proxies=getattr(settings, "PROXY", None),
            )
            res = client.request("get", url, params=params, raise_exception=True)
            status = getattr(res, "status_code", None)
            text = getattr(res, "text", "") or ""
            last_status = int(status or 0)
            last_raw = f"HTTP {status} | {text[:300]}"
            if status != 200:
                return None
            try:
                return res.json()
            except Exception:
                return None
        except Exception as err:
            last_raw = f"EXC: {err}"
            _trip(str(err))
            return None


def _rate_text(value: Any) -> str:
    """把 rexxar 的 ``rating.value``（数字，0 表示未开分）收敛成 "8.4" / "9.0"。"""
    try:
        rate = float(value or 0)
    except (TypeError, ValueError):
        return ""
    if rate <= 0:
        return ""
    return f"{rate:.1f}"


def _search(keyword: str) -> List[dict]:
    """搜一次豆瓣，返回规范化候选（title / year / rate / kind / img / id）。

    一次请求就带评分（``rating.value``），不再需要第二次 ``subject_abstract``。
    v1.4.0：/search 被 403 封禁的窗口内直接返回空（不计连空，那不是豆瓣拦我们、
    是这个端点被定向封了），调用方 [lookup] 会改走 suggest+subject 两步。
    """
    global _empty_streak, _search_dead_until
    if time.monotonic() < _search_dead_until:
        return []
    data = _get_json(_SEARCH_URL, {"q": keyword, "start": 0, "count": 10})
    if data is None and last_status in (403, 419):
        _search_dead_until = time.monotonic() + _SEARCH_DEAD_COOLDOWN
        logger.warning(
            f"rexxar /search 返回 {last_status}，{_SEARCH_DEAD_COOLDOWN / 60:.0f} 分钟内改走 suggest+subject")
        return []
    items: Any = None
    if isinstance(data, dict):
        subjects = data.get("subjects")
        if isinstance(subjects, dict):
            items = subjects.get("items")
    if not isinstance(items, list):
        items = []
    out: List[dict] = []
    for item in items:
        target = item.get("target") if isinstance(item, dict) else None
        if not isinstance(target, dict):
            continue
        uri = str(target.get("uri") or "")
        rating = target.get("rating")
        if not isinstance(rating, dict):
            rating = {}
        out.append({
            "id": str(target.get("id") or ""),
            "title": str(target.get("title") or "").strip(),
            "year": str(target.get("year") or "").strip(),
            "img": str(target.get("cover_url") or ""),
            # 类型只能从 uri 认（`douban://douban.com/tv/<id>`）；同名电影/剧集要靠它区分
            "kind": "tv" if "/tv/" in uri else ("movie" if "/movie/" in uri else ""),
            "rate": _rate_text(rating.get("value")),
        })
    # 连续拿到空结果 = 多半被限流（正常检索不会一直空），到阈值先复核再熔断
    if out:
        _empty_streak = 0
    else:
        _empty_streak += 1
        _confirm_block()
    return out


def _pick(pool: Sequence[dict], title: str, prefer_tv: Optional[bool]) -> dict:
    """从候选里挑一条：同名优先，再按电影/剧集偏好排序。"""
    same = [c for c in pool if str(c.get("title") or "").strip() == title]
    if same:
        pool = same
    if prefer_tv is True:
        pool = sorted(pool, key=lambda c: 0 if c.get("kind") == "tv" else 1)
    elif prefer_tv is False:
        pool = sorted(pool, key=lambda c: 0 if c.get("kind") == "movie" else 1)
    for candidate in pool:
        if str(candidate.get("id") or ""):
            return candidate
    return {}


def _poster_of(candidate: dict) -> str:
    img = candidate.get("img")
    if isinstance(img, str) and img.startswith("http"):
        # rexxar 给的是大图（cover_url 已带签名），直接用
        return img
    return ""


def _title_of(candidate: dict) -> str:
    return str(candidate.get("title") or "").strip()


def _suggest_pick(keyword: str, prefer_tv: Optional[bool]) -> dict:
    """subject_suggest 检索：返回 ``{id, type, title, year}`` 候选（没有评分）。

    v1.4.0 两步兜底的第一步。同名优先、再按电影/剧集偏好排序，与 [_pick] 同一思路。
    """
    data = _get_json(_SUGGEST_URL, {"q": keyword})
    if not isinstance(data, list):
        return {}
    pool = [c for c in data if isinstance(c, dict)]
    same = [c for c in pool if str(c.get("title") or "").strip() == keyword]
    if same:
        pool = same
    if prefer_tv is True:
        pool = sorted(pool, key=lambda c: 0 if c.get("type") == "tv" else 1)
    elif prefer_tv is False:
        pool = sorted(pool, key=lambda c: 0 if c.get("type") == "movie" else 1)
    for candidate in pool:
        sid = str(candidate.get("id") or "").strip()
        kind = str(candidate.get("type") or "").strip()
        if sid and kind in ("movie", "tv"):
            return candidate
    return {}


def _msearch_pick(keyword: str, prefer_tv: Optional[bool]) -> dict:
    """m 站搜索页兜底（v1.4.1）：返回 ``{id, type, title, year}`` 候选（无评分）。

    ``m.douban.com/search/?query=`` 是服务端渲染的，HTML 里直接内联 ``/subject/<id>``
    链接（取前 3 个 sid 就够，多的是无关书影音）。需手机 UA，桌面 UA 返回空壳页。
    返回的 sid 不知类型，交由 [_subject_rate] 按 movie/tv 各试一次。
    """
    global _last_request_at, last_raw
    cookie = _douban_cookie()  # 与 _get_json 同理，取 Cookie 放在限流锁外面
    with _RATE_LOCK:
        gap = time.monotonic() - _last_request_at
        if gap < _REQUEST_INTERVAL:
            time.sleep(_REQUEST_INTERVAL - gap)
        _last_request_at = time.monotonic()
        try:
            headers = {"User-Agent": _MOBILE_UA, "Referer": _REFERER}
            if cookie:
                headers["Cookie"] = cookie
            client = RequestUtils(
                headers=headers,
                proxies=getattr(settings, "PROXY", None),
            )
            res = client.request("get", _MSEARCH_URL.format(q=keyword), raise_exception=True)
            status = getattr(res, "status_code", None)
            html = getattr(res, "text", "") or ""
            last_raw = f"HTTP {status} | {html[:300]}"
            if status != 200:
                return {}
            sids: List[str] = []
            for m in _MSEARCH_SID_RE.finditer(html):
                sid = m.group(1)
                if sid not in sids:
                    sids.append(sid)
                if len(sids) >= 3:
                    break
        except Exception as exc:
            last_raw = f"EXC {exc}"
            logger.info(f"m 站搜索页兜底失败：{exc}")
            return {}
    # 同名优先没有候选元数据可比，直接按第一个 sid 试；类型电影/剧集各打一次详情，
    # prefer_tv 决定先试哪个 —— 详情接口会回真实 title 供上层校验
    kinds = ("tv", "movie") if prefer_tv else ("movie", "tv")
    for sid in sids:
        for kind in kinds:
            subject = _subject_rate(kind, sid)
            if subject:
                return {"id": sid, "type": kind, **subject}
    return {}


def _subject_rate(kind: str, sid: str) -> dict:
    """rexxar subject 详情：返回与 [_search] 同构的单条候选（带 ``rating.value``）。

    v1.4.0 两步兜底的第二步。/search 被封时这个接口仍然 200 —— 封的是端点不是 IP。
    """
    data = _get_json(_SUBJECT_URL.format(kind=kind, sid=sid), {})
    if not isinstance(data, dict):
        return {}
    rating = data.get("rating")
    if not isinstance(rating, dict):
        rating = {}
    uri = str(data.get("uri") or "")
    return {
        "id": str(data.get("id") or sid),
        "title": str(data.get("title") or "").strip(),
        "year": str(data.get("year") or "").strip(),
        "img": str(data.get("cover_url") or data.get("pic") or ""),
        "kind": "tv" if "/tv/" in uri else ("movie" if "/movie/" in uri else kind),
        "rate": _rate_text(rating.get("value")),
    }


def _year_of(candidate: dict) -> str:
    return str(candidate.get("year") or "").strip()


def lookup(
    title: str,
    *,
    keywords: Optional[Iterable[str]] = None,
    year: str = "",
    prefer_tv: Optional[bool] = None,
    need_poster: bool = True,
    need_rating: bool = True,
) -> Dict[str, Any]:
    """
    按标题在豆瓣找词条，取海报与评分。

    :param title: 榜单原始标题（用于同名判定与日志）
    :param keywords: 检索词优先级列表；Netflix 这类英文片名传 `[中文名, 英文名]` 命中率最高
    :param year: 年份，用于「截短检索」时的严格校验（宁可空缺也不张冠李戴）
    :param prefer_tv: True 表示剧集、False 表示电影，同名候选按它排序
    :param need_poster: 需要海报时才解析封图
    :param need_rating: 需要评分（评分随搜索结果一起回来，不再额外请求）

    返回 ``{"id", "rate", "poster", "title"}``，查不到的项为空串。
    """
    result = {"id": "", "rate": "", "poster": "", "title": ""}
    if not title or is_blocked():
        return result

    words = [w for w in re.split(r"[\s:：\-—–·,，.。!！?？/]+", title) if w]
    queries: List[str] = []
    for keyword in (keywords or ()):
        keyword = str(keyword or "").strip()
        if keyword and keyword not in queries:
            queries.append(keyword)
    if title not in queries:
        queries.append(title)

    def settle(candidate: dict) -> Dict[str, Any]:
        result["id"] = str(candidate.get("id") or "")
        result["title"] = _title_of(candidate)
        if need_poster:
            result["poster"] = _poster_of(candidate)
        if need_rating:
            result["rate"] = str(candidate.get("rate") or "")
        return result

    # ① 整串检索：命中的都是原文匹配，直接采纳（不校验年份，同名已经足够严格）
    for query in queries:
        pool = _search(query)
        if not pool:
            continue
        candidate = _pick(pool, query, prefer_tv)
        if candidate:
            return settle(candidate)

    # ①.5 suggest 两步兜底（v1.4.0）：/search 被定向封禁（403）或整串搜不到时，
    #     subject_suggest 拿 id/type → subject 详情拿评分。霸王别姬这类经典片必命中，
    #     所以这一步失败基本可以断定「真没有」，不需要再截短。
    for query in queries:
        cand = _suggest_pick(query, prefer_tv)
        if not cand:
            continue
        subject = _subject_rate(str(cand.get("type")), str(cand.get("id")))
        if subject:
            return settle(subject)

    # ①.6 m 站搜索页三级兜底（v1.4.1）：suggest 也被「200 + 空数组」软封锁时，
    #     最后一条路 —— HTML 里抠 sid → subject 详情。真实 title 在详情里，
    #     与检索词不同的（搜出幕后纪录片之类）不算命中，宁缺勿错。
    for query in queries:
        cand = _msearch_pick(query, prefer_tv)
        if not cand:
            continue
        subject = _subject_rate(str(cand.get("type")), str(cand.get("id")))
        if subject and _title_of(subject) == query:
            return settle(subject)

    # ② 截短检索：只对带年份的条目前瞻降级，且年份必须完全一致。
    #    长英文片名在豆瓣常常整串搜不到（实测「Chiikawa the Movie: The Secret of
    #    Mermaid Island」返回空，但「Chiikawa」能命中《吉伊卡哇：人鱼之岛的秘密》），
    #    所以按词逐步减少检索词再试；拿不到年份的条目不参与降级，宁可不补也不认错。
    year = str(year or "").strip()
    if not re.fullmatch(r"(?:19|20)\d{2}", year):
        return result
    for size in (1, 2, 3):
        if size >= len(words):
            continue
        keyword = " ".join(words[:size])
        if len(keyword) < 3:
            continue
        matched = [c for c in _search(keyword) if _year_of(c) == year]
        candidate = _pick(matched, keyword, prefer_tv)
        if candidate:
            logger.info(
                f"豆瓣整串无结果，改用关键词「{keyword}」命中 {_title_of(candidate)}")
            return settle(candidate)
    return result


def probe() -> List[str]:
    """连通性探针（``/rating?debug=2``）：把几种取数方式对几个豆瓣地址各打一次。

    线上症状是「rexxar 拿不到任何东西，而 old suggest 接口至少还能返回 JSON 空数组」，
    需要区分「域名不通 / 方法用错 / 被拦」这三件事，所以这里两种方法都试。
    结果只用于排障，不写缓存、不参与限流判断。
    """
    targets = (
        ("movie首页", "https://movie.douban.com/"),
        ("m首页", "https://m.douban.com/"),
        ("rexxar", f"{_SEARCH_URL}?q=test&start=0&count=1"),
    )
    out: List[str] = []
    for name, url in targets:
        try:
            client = RequestUtils(
                headers={"User-Agent": random.choice(_UAS), "Referer": _REFERER},
                proxies=getattr(settings, "PROXY", None),
            )
            res = client.request("get", url, raise_exception=True)
            status = getattr(res, "status_code", None)
            text = getattr(res, "text", "") or ""
            out.append(f"{name}.request=HTTP{status}/len{len(text)}")
        except Exception as err:
            out.append(f"{name}.request=EXC:{type(err).__name__}:{err}")
    return out


def cache_key(title: str, year: str = "", media_type: str = "") -> str:
    """搜索评分缓存的键：标题 + 年份 + 类型。

    同一部片重拍要区分开（「射雕英雄传」有 1983 与 2017 两版），所以年份进键；
    类型只影响「剧集/电影候选排序」，也一并进键，避免剧/影互相覆盖。
    """
    return "|".join((
        str(title or "").strip().lower(),
        str(year or "").strip(),
        str(media_type or "").strip().lower(),
    ))


def is_fresh(entry: Dict[str, Any], now_ts: float) -> bool:
    """持久缓存里的评分是否还能用（命中 30 天 / 未开分 3 天）。

    版本不匹配的一律判过期：换接口后，旧版本存下来的「查不到」是假结论。
    有分与无分各走各的版本号（[RATING_VER] / [MISS_RATING_VER]）——
    冲掉毒缓存时只作废空缺、不连累 30 天的有效评分。
    """
    if not isinstance(entry, dict) or "rating_ts" not in entry:
        return False
    try:
        ts = float(entry.get("rating_ts") or 0)
    except (TypeError, ValueError):
        return False
    has_rate = str(entry.get("vote_average") or "0") not in ("", "0", "0.0")
    if has_rate:
        if entry.get("rating_ver") != RATING_VER:
            return False
        ttl = RATING_TTL
    else:
        if entry.get("rating_ver") != MISS_RATING_VER:
            return False
        ttl = RATING_TTL_MISS
    return now_ts - ts < ttl


def miss_cacheable() -> bool:
    """本轮「查了但没分」是否可信（可信才允许把空缺写进缓存）。

    依据是 [_search] 维护的连空计数：空缺若发生在「搜索池整个是空」的请求之后
    （``_empty_streak > 0``），大概率是豆瓣瞬时拦截/抖动，不是真没有 —— 这种空缺
    一写就是 3 天，把有分的片也锁死。连空为 0 说明最后那次检索池非空，
    空缺来自「有候选但标题没对上」（m 站兜底的宁缺勿错校验），这才可信。
    """
    return _empty_streak == 0


def prune_cache(cache: Dict[str, Any], now_ts: float) -> int:
    """清掉超过最长有效期（[RATING_TTL]）的缓存条目，返回删除条数。

    ``douban_rating`` 是按「标题|年份|类型」长期累积的：搜索结果里的片子几乎
    不重复，条目只增不减（实测 300+ 条 / 40KB，写一次是整份覆盖）。这里按时间
    淘汰老条目，容量随真实活跃片量收敛，不会无限长。
    """
    if not isinstance(cache, dict):
        return 0
    removed = 0
    for key in list(cache.keys()):
        entry = cache.get(key)
        try:
            ts = float((entry or {}).get("rating_ts") or 0)
        except (TypeError, ValueError):
            ts = 0
        if ts <= 0 or now_ts - ts > RATING_TTL:
            cache.pop(key, None)
            removed += 1
    return removed


# ---------------- 近期结果 memo（防并发重复查豆瓣） ----------------
# App 同屏三个榜 + 搜索页会并发打多个 /rating，同一标题可能在几十秒内被重复查。
# 这里按 cache_key 存最近的结果（含空缺），2 分钟内直接命中，不再发请求。
# 与持久缓存的分工：持久缓存管「天」，这里只管「秒级并发的去重」。
_RECENT_TTL = 120.0
_recent: Dict[str, Tuple[float, str]] = {}
_recent_lock = threading.Lock()


def recent_get(key: str) -> Optional[str]:
    """取近期查过的评分（``None`` = 没查过/已过期；空串 = 查过但无分）。"""
    now = time.monotonic()
    with _recent_lock:
        hit = _recent.get(key)
        if hit is None:
            return None
        if now - hit[0] >= _RECENT_TTL:
            _recent.pop(key, None)
            return None
        return hit[1]


def recent_put(key: str, rate: str) -> None:
    """记下本轮查分结果（含空缺），供 2 分钟内的并发请求直接命中。"""
    with _recent_lock:
        if len(_recent) > 512:
            _recent.clear()
        _recent[key] = (time.monotonic(), str(rate or ""))


def fill_ratings(
    pairs: Iterable[Tuple[str, Dict[str, Any]]],
    *,
    budget: int,
    now: Optional[datetime.datetime] = None,
    store_get=None,
    store_save=None,
) -> int:
    """
    给一批身份字典补豆瓣评分，原地写入 ``vote_average`` / ``rating_ts``。

    :param pairs: ``(榜单原始标题, 身份字典)`` 序列；身份字典里读 ``title_cn`` /
                  ``year`` / ``type`` 决定检索词与年份校验
    :param budget: 本轮最多查多少条（现在一条 = 1 个请求）
    :param store_get: 可选，返回共享评分缓存 dict 的回调（主类 ``douban_rating`` 键）。
                  传入后与 ``/rating`` 接口共用同一份缓存 —— 同一标题在榜单补分和
                  搜索查分两条路上只查一次豆瓣（v1.4.3 优化①）。
    :param store_save: 可选，与 ``store_get`` 配对的写回回调 ``save(cache_dict)``。
    :return: 实际消耗的额度

    已经缓存过且未过期的条目直接跳过，因此稳态下（榜单条目重复率极高）
    每轮只会为「新进榜的几条」付出请求。
    """
    if budget <= 0:
        return 0
    now_ts = (now or datetime.datetime.now()).timestamp()
    shared: Dict[str, Any] = {}
    # ⚠️ 只记「本轮新增/更新的键」，不直接改 store_get() 拿到的整份 dict：
    # 这一轮要跑几分钟（每条 1 个请求、全局 1.2s 间隔），期间 App 的 /rating 请求
    # 会往同一个键里写别的片的评分。写回时若拿开头那份旧副本整份覆盖，就把别人
    # 刚查到的分抹掉了 —— 表现是「明明查到了，App 端下次进榜又变空」且反复出现。
    # 写回阶段在 [rating_store_lock] 内重读最新值再只并入本轮的改动。
    updates: Dict[str, Dict[str, Any]] = {}
    if store_get is not None:
        try:
            loaded = store_get()
            if isinstance(loaded, dict):
                shared = loaded
        except Exception as exc:
            logger.warning(f"读取共享评分缓存失败，本轮不共用：{exc}")
    used = 0
    for title, entry in pairs:
        if used >= budget or is_blocked():
            break
        if not isinstance(entry, dict) or not title:
            continue
        if is_fresh(entry, now_ts):
            continue
        keywords = []
        cn_title = str(entry.get("title_cn") or "").strip()
        if cn_title and cn_title != title:
            keywords.append(cn_title)
        keywords.append(title)
        year = str(entry.get("year") or "").strip()
        entry_type = str(entry.get("type") or "").strip().lower()
        if entry_type in ("电视剧", "tv", "show", "动漫", "anime"):
            prefer_tv: Optional[bool] = True
        elif entry_type in ("电影", "movie", "film"):
            prefer_tv = False
        else:
            prefer_tv = None
        key = cache_key(title, year, entry_type)
        # 共享缓存命中（/rating 那边查过的）直接抄，不再打豆瓣
        shared_entry = shared.get(key)
        if is_fresh(shared_entry, now_ts):
            entry["vote_average"] = str((shared_entry or {}).get("vote_average") or "")
            entry["rating_ts"] = now_ts
            entry["rating_ver"] = RATING_VER if entry["vote_average"] else MISS_RATING_VER
            continue
        # 秒级并发 memo：同一标题几分钟内刚查过（含空缺）也直接用
        memo = recent_get(key)
        if memo is not None:
            entry["vote_average"] = memo
            entry["rating_ts"] = now_ts
            entry["rating_ver"] = RATING_VER if memo else MISS_RATING_VER
            continue
        # 单条查询异常不能掀翻整轮：榜单补分是「锦上添花」，网络抖一下不该让
        # 猫眼/Netflix 整轮刷新的评分段报废（异常往上抛会被调用方的 try 吞掉，
        # 表现是这轮所有条目都没补上分，且日志只有一行笼统的 warning）
        try:
            info = lookup(title, keywords=keywords, year=year,
                          prefer_tv=prefer_tv, need_poster=False, need_rating=True)
        except Exception as exc:
            logger.warning(f"豆瓣评分补全异常（{title}），跳过该条：{exc}")
            continue
        used += 1
        rate = str((info or {}).get("rate") or "")
        recent_put(key, rate)
        if rate:
            entry["rating_ts"] = now_ts
            entry["rating_ver"] = RATING_VER
            entry["vote_average"] = rate
            logger.info(f"豆瓣评分补全：{title} -> {rate}")
        elif miss_cacheable():
            # 空缺可信（检索池非空、只是标题没对上）才记「无分」，3 天后重试；
            # 空缺可疑（豆瓣抖动/拦截窗口）什么都不写 —— 不留标记，下一轮自动重查，
            # 避免把有分的片误存成无分锁死三天（2026-10-20 App 端全榜无分的根因）
            entry["rating_ts"] = now_ts
            entry["rating_ver"] = MISS_RATING_VER
            entry["vote_average"] = ""
        # 记进本轮改动（不直接改 shared 整份），写回时合并。
        # ⚠️ 空缺只有在共享缓存里**没有更新鲜的好分**时才允许写 —— 否则身份缓存
        # 条目丢了 rating 标记后重查失败，会把 /rating 刚查到的好分覆盖成空，
        # App 端从此 3 天看不到这条的分（2026-10-20 下午二次毒缓存的根因）
        se = shared.get(key)
        shared_has_fresh_rate = bool(
            se and str(se.get("vote_average") or "") and is_fresh(se, now_ts))
        if rate or (miss_cacheable() and not shared_has_fresh_rate):
            updates[key] = {"vote_average": rate,
                             "rating_ts": now_ts,
                             "rating_ver": RATING_VER if rate else MISS_RATING_VER}
    if updates and store_save is not None:
        # 与 /rating 共用同一把锁 + 写前重读：只把本轮改动并进「当前最新」，
        # 绝不拿几分钟前读到的旧副本整份覆盖（见上面 updates 的说明）
        try:
            with rating_store_lock():
                latest: Dict[str, Any] = {}
                if store_get is not None:
                    try:
                        loaded = store_get()
                        if isinstance(loaded, dict):
                            latest = loaded
                    except Exception as exc:
                        logger.warning(f"写回前重读共享评分缓存失败：{exc}")
                latest.update(updates)
                store_save(latest)
        except Exception as exc:
            logger.warning(f"写回共享评分缓存失败：{exc}")
    return used

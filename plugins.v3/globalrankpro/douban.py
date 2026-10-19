"""豆瓣检索：给榜单条目补海报与评分。

为什么放在插件侧
----------------
猫眼榜单（热度榜 / 网络剧榜 / 综艺榜 / 票房榜）和 Netflix 榜单本身都不带评分。
要让 App 显示评分，只能额外去豆瓣查一次；如果让 App 自己去查，海报墙一屏十几条
就是十几次跨境 HTTPS 请求，既慢又容易被豆瓣限流（实测连打几次后直接返回空）。

宿主机网络更接近豆瓣，且结果可以持久缓存、全端共享，所以评分统一在插件里补好，
随 ``/rank`` 一次性吐给 App（字段 ``vote_average``）。

豆瓣接口的两个坑
----------------
- ``j/subject_suggest``（检索）**不带评分**，只给 id / 海报 / 标题，评分要再打一次
  ``j/subject_abstract`` 才有 ``subject.rate``。因此**一条 = 2 个请求**，必须按轮
  分摊 + 持久缓存，否则单次刷新就能把豆瓣打限流。
- 两个接口都要带 ``Referer: https://movie.douban.com/``，且请求过密会返回空列表或
  异常。这里做了全局节流与熔断：连续失败即整体收手一段时间，已拿到的照常落库。
"""
from __future__ import annotations

import datetime
import random
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.network import RequestUtils

_SUGGEST_URL = "https://movie.douban.com/j/subject_suggest"
_ABSTRACT_URL = "https://movie.douban.com/j/subject_abstract"
_REFERER = "https://movie.douban.com/"

# 请求间隔：连打会被拦（表现为返回空 / 抛异常），0.35s 实测稳定
_REQUEST_INTERVAL = 0.35
# 熔断冷却：一旦某次请求抛异常就认定被限流，这段时间内不再打豆瓣
_BLOCK_COOLDOWN = 600.0

# 评分缓存有效期：豆瓣评分变动很慢，30 天足够；还没开分的条目（新片）3 天后再试
RATING_TTL = 30 * 86400
RATING_TTL_MISS = 3 * 86400

_UAS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
)

_last_request_at = 0.0
_blocked_until = 0.0


def is_blocked() -> bool:
    """本轮是否已被豆瓣限流（熔断中）。"""
    return time.monotonic() < _blocked_until


def _trip(reason: str) -> None:
    global _blocked_until
    _blocked_until = time.monotonic() + _BLOCK_COOLDOWN
    logger.warning(
        f"豆瓣请求失败，{int(_BLOCK_COOLDOWN / 60)} 分钟内不再尝试：{reason}")


def _get_json(url: str, params: Dict[str, Any]) -> Any:
    """带节流与熔断的 GET，失败一律返回 None（调用方按「没查到」处理）。"""
    global _last_request_at
    if is_blocked():
        return None
    gap = time.monotonic() - _last_request_at
    if gap < _REQUEST_INTERVAL:
        time.sleep(_REQUEST_INTERVAL - gap)
    _last_request_at = time.monotonic()
    try:
        return RequestUtils(
            headers={"User-Agent": random.choice(_UAS)},
            proxies=getattr(settings, "PROXY", None),
            referer=_REFERER,
        ).get_json(url, params=params)
    except Exception as err:
        _trip(str(err))
        return None


def _suggest(keyword: str) -> List[dict]:
    data = _get_json(_SUGGEST_URL, {"q": keyword})
    if not isinstance(data, list):
        return []
    return [c for c in data if isinstance(c, dict)]


def _rating(subject_id: str) -> str:
    """取豆瓣评分（字符串，如 "8.7"）；未开分返回空串。"""
    data = _get_json(_ABSTRACT_URL, {"subject_id": subject_id})
    if not isinstance(data, dict):
        return ""
    subject = data.get("subject")
    if not isinstance(subject, dict):
        return ""
    rate = str(subject.get("rate") or "").strip()
    return rate if re.fullmatch(r"\d+(?:\.\d+)?", rate) else ""


def _pick(pool: Sequence[dict], title: str, prefer_tv: Optional[bool]) -> dict:
    """从候选里挑一条：同名优先，剧集优先带集数标记的。"""
    same = [c for c in pool if str(c.get("title") or "").strip() == title]
    if same:
        pool = same
    if prefer_tv is True:
        pool = sorted(pool, key=lambda c: 0 if c.get("episode") else 1)
    for candidate in pool:
        if str(candidate.get("id") or "").isdigit():
            return candidate
    return {}


def _poster_of(candidate: dict) -> str:
    img = candidate.get("img")
    if isinstance(img, str) and img.startswith("http"):
        # suggest 给的是缩略图，换成中等尺寸，避免 App 里糊掉
        return img.replace("/s_ratio_poster/", "/m_ratio_poster/")
    return ""


def _title_of(candidate: dict) -> str:
    return str(candidate.get("title") or "").strip()


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
    :param prefer_tv: True 表示剧集，候选排序时优先带集数标记的
    :param need_poster: 需要海报（缺图兜底）时才去解析 img
    :param need_rating: 需要评分（会多打一次 abstract 接口）

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
        subject_id = str(candidate.get("id") or "")
        result["id"] = subject_id
        result["title"] = _title_of(candidate)
        if need_poster:
            result["poster"] = _poster_of(candidate)
        if need_rating and subject_id:
            result["rate"] = _rating(subject_id)
        return result

    # ① 整串检索：命中的都是原文匹配，直接采纳（不校验年份，同名已经足够严格）
    for query in queries:
        pool = _suggest(query)
        if not pool:
            continue
        candidate = _pick(pool, query, prefer_tv)
        if candidate:
            return settle(candidate)

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
        matched = [c for c in _suggest(keyword) if _year_of(c) == year]
        candidate = _pick(matched, keyword, prefer_tv)
        if candidate:
            logger.info(
                f"豆瓣整串无结果，改用关键词「{keyword}」命中 {_title_of(candidate)}")
            return settle(candidate)
    return result


def cache_key(title: str, year: str = "", media_type: str = "") -> str:
    """搜索评分缓存的键：标题 + 年份 + 类型。

    同一部片重拍要区分开（「射雕英雄传」有 1983 与 2017 两版），所以年份进键；
    类型只影响「剧集优先带集数」的候选排序，也一并进键，避免剧/影互相覆盖。
    """
    return "|".join((
        str(title or "").strip().lower(),
        str(year or "").strip(),
        str(media_type or "").strip().lower(),
    ))


def is_fresh(entry: Dict[str, Any], now_ts: float) -> bool:
    """持久缓存里的评分是否还能用（命中 30 天 / 未开分 3 天）。"""
    if not isinstance(entry, dict) or "rating_ts" not in entry:
        return False
    try:
        ts = float(entry.get("rating_ts") or 0)
    except (TypeError, ValueError):
        return False
    ttl = RATING_TTL if str(entry.get("vote_average") or "0") not in ("", "0", "0.0") \
        else RATING_TTL_MISS
    return now_ts - ts < ttl


def fill_ratings(
    pairs: Iterable[Tuple[str, Dict[str, Any]]],
    *,
    budget: int,
    now: Optional[datetime.datetime] = None,
) -> int:
    """
    给一批身份字典补豆瓣评分，原地写入 ``vote_average`` / ``rating_ts``。

    :param pairs: ``(榜单原始标题, 身份字典)`` 序列；身份字典里读 ``title_cn`` /
                  ``year`` / ``type`` 决定检索词与年份校验
    :param budget: 本轮最多查多少条（每条 2 个请求，务必限量）
    :return: 实际消耗的额度

    已经缓存过且未过期的条目直接跳过，因此稳态下（榜单条目重复率极高）
    每轮只会为「新进榜的几条」付出请求。
    """
    if budget <= 0:
        return 0
    now_ts = (now or datetime.datetime.now()).timestamp()
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
        prefer_tv = True if entry_type in ("电视剧", "tv", "show") else None
        info = lookup(title, keywords=keywords, year=year,
                      prefer_tv=prefer_tv, need_poster=False, need_rating=True)
        used += 1
        entry["rating_ts"] = now_ts
        if info.get("rate"):
            entry["vote_average"] = info["rate"]
            logger.info(f"豆瓣评分补全：{title} -> {info['rate']}")
        else:
            entry["vote_average"] = ""
    return used

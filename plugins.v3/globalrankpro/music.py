"""
音乐榜单数据源（v3）。

三家平台里只有一家是真正开放的，另外两家走的是站点自己用的 JSON 接口
（实测均免登录、免签名、免 key）：

- **网易云**：``GET music.163.com/api/playlist/detail?id=<榜单id>``
  榜单 id 是网易云自己的一套固定值（热歌 3778678 / 新歌 3779629 /
  飙升 19723756 / 原创 2884035）。``tracks[]`` 用的是**旧版字段名**
  ``artists`` / ``album``（不是新版 API 的 ``ar`` / ``al``），
  ``lastRank`` 给上期名次（0 表示新入榜），``album.picUrl`` 是封面。
  只要带 ``Referer: https://music.163.com`` 就通，不需要 cookie。
- **QQ 音乐**：``GET c.y.qq.com/v8/fcg-bin/fcg_v8_toplist_cp.fcg?topid=<n>...``
  ⚠️ 返回体**开头带一个空格**，``json.loads`` 前必须 strip。
  ``topinfo.ListName`` 是榜名，``update_time`` 是更新日期，
  封面按 ``albummid`` 拼 ``/music/photo_new/T002R300x300M000<mid>.jpg``。
- **Apple Music**：``rss.applemarketingtools.com/api/v2/cn/music/most-played/{n}/songs.json``
  是 Apple 官方 Marketing Tools 对外公开的 JSON Feed（302 到
  ``rss.marketingtools.apple.com``），三家当中**唯一有正式文档、明确允许使用**的。
  缺点只有「播放最多」一种口径，没有新歌 / 飙升榜。

⚠️ 网易云与 QQ 的是站点内部接口，没有可用性承诺、随时可能加风控。
因此网关侧一律「服务端抓取 + 落盘快照」，App 只读宿主快照、不直连这三家 ——
手机 IP 比机房 IP 更容易被风控，而且在客户端做抓取等于把风控风险摊到每个用户身上。

条目映射：歌名进 ``title`` / ``title_cn``（同源，不走媒体识别），
封面进 ``poster``，歌手 / 专辑 / 时长 / 上期名次各有专字段。
音乐条目**不做宿主识别**：它们在 TMDB / 豆瓣里没有对应条目，强行走识别只会
得到错误匹配（跟红果短剧同一个理由）。
"""

import datetime
import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import pytz

from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.network import RequestUtils

from .base import (
    RankBoard,
    RankSource,
    SOURCE_MUSIC,
)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 平台代码 -> 展示名
_PLATFORM_NAMES = {
    "netease": "网易云",
    "qq": "QQ音乐",
    "apple": "Apple Music",
}

# 平台代码 -> 抓取时带的 Referer（Apple 不需要）
_PLATFORM_REFERERS = {
    "netease": "https://music.163.com",
    "qq": "https://y.qq.com",
    "apple": "",
}

# 榜单定义：(平台, 榜单代码, 榜单短名, 平台原生 id)
_BOARD_DEFS: List[Tuple[str, str, str, str]] = [
    ("netease", "hot", "热歌榜", "3778678"),
    ("netease", "new", "新歌榜", "3779629"),
    ("netease", "soaring", "飙升榜", "19723756"),
    ("netease", "original", "原创榜", "2884035"),
    ("qq", "hot", "热歌榜", "26"),
    ("qq", "new", "新歌榜", "27"),
    ("qq", "soaring", "飙升榜", "62"),
    ("qq", "index", "流行指数榜", "4"),
    ("apple", "most-played", "热门歌曲", ""),
]

# 每个榜单最多抓多少条。网易云热歌榜给 200 条、QQ 热歌榜给 300 条，
# 全存下来快照会很大（App 一次只展示一屏），默认留 50 条足够。
_DEFAULT_LIMIT = 50
# Apple Feed 的上限是 100
_APPLE_MAX_LIMIT = 100
# 两次抓取之间的间隔，避免九个榜连着打把对方打出风控
_FETCH_INTERVAL = 0.4
# 并发抓取的线程数：九个榜串行要 20 秒以上，四路并发压到 6 秒左右
_MAX_WORKERS = 4


def _safe_int(value: Any, default: int = 0) -> int:
    """把任意值收成 int，失败返回 default。"""
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _fmt_duration(seconds: Any) -> int:
    """时长统一成「秒」，非正值返回 0。"""
    value = _safe_int(seconds, 0)
    return value if value > 0 else 0


def _https(url: str) -> str:
    """把 http 图床地址抬到 https（App 侧明文请求会被拦）。"""
    text = str(url or "").strip()
    if text.startswith("http://"):
        return "https://" + text[len("http://"):]
    return text


class MusicSource(RankSource):
    """音乐榜单数据源：网易云 / QQ音乐 / Apple Music 三平台。

    三个平台结构差异不小（网易云有四个分榜、QQ 也有四个、Apple 只有一个），
    但对外统一成「一个平台下的若干分榜」，每个分榜一份 ``RankBoard``，
    ``platform`` 字段区分平台、``board_type`` 区分分榜（``netease_hot`` 这种）。
    """

    source_id = SOURCE_MUSIC
    source_name = "音乐榜单"

    # 榜单代码（平台_分榜）-> 展示名，加 key 即自动进设置页多选
    _BOARD_NAMES = {
        f"{platform}_{code}": f"{_PLATFORM_NAMES[platform]}{name}"
        for platform, code, name, _native in _BOARD_DEFS
    }
    # 榜单代码 -> (平台, 分榜代码, 原生 id)
    _BOARD_META = {
        f"{platform}_{code}": (platform, code, native)
        for platform, code, _name, native in _BOARD_DEFS
    }
    _DEFAULT_BOARDS = [key for key in _BOARD_NAMES]

    def __init__(self, host: Any):
        super().__init__(host)
        object.__setattr__(self, "_boards", list(self._DEFAULT_BOARDS))
        object.__setattr__(self, "_limit", _DEFAULT_LIMIT)

    def apply_config(self, config: Dict[str, Any]) -> None:
        """接收主类剥离前缀后的音乐配置。"""
        super().apply_config(config)
        boards = (config or {}).get("boards")
        if isinstance(boards, list) and boards:
            picked = [str(b) for b in boards if str(b) in self._BOARD_META]
            if picked:
                object.__setattr__(self, "_boards", picked)
        limit = _safe_int((config or {}).get("limit"), _DEFAULT_LIMIT)
        if limit > 0:
            object.__setattr__(self, "_limit", limit)

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------
    def _get_json(self, url: str, referer: str = "") -> Any:
        """取 JSON。响应体先按 UTF-8 解码再解析，失败抛 RuntimeError。"""
        headers = {"User-Agent": _UA, "Accept": "application/json, text/plain, */*"}
        if referer:
            headers["Referer"] = referer
        response = RequestUtils(
            headers=headers,
            proxies=getattr(settings, "PROXY", None),
        ).get_res(url)
        if not response:
            raise RuntimeError(f"请求失败：{url}")
        status = getattr(response, "status_code", None)
        if status and int(status) != 200:
            raise RuntimeError(f"返回 {status}：{url}")
        content = getattr(response, "content", None)
        if content is None:
            text = getattr(response, "text", "") or ""
        elif isinstance(content, (bytes, bytearray)):
            text = bytes(content).decode("utf-8", errors="replace")
        else:
            text = str(content)
        # ⚠️ QQ 的返回体开头有一个空格，不 strip 会直接 JSONDecodeError
        text = text.strip()
        if not text:
            raise RuntimeError(f"空响应：{url}")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"JSON 解析失败（{exc}）：{url}") from exc

    # ------------------------------------------------------------------
    # 三平台解析
    # ------------------------------------------------------------------
    def _fetch_netease(self, native_id: str, limit: int) -> Dict[str, Any]:
        """网易云单个分榜。"""
        url = f"https://music.163.com/api/playlist/detail?id={quote(native_id)}"
        payload = self._get_json(url, referer=_PLATFORM_REFERERS["netease"])
        result = payload.get("result") or {}
        if not isinstance(result, dict):
            raise RuntimeError("网易云返回结构异常（result 不是对象）")
        rows = result.get("tracks") or []
        if not isinstance(rows, list) or not rows:
            raise RuntimeError("网易云返回空曲目列表")
        items: List[Dict[str, Any]] = []
        for index, row in enumerate(rows[:limit]):
            if not isinstance(row, dict):
                continue
            artists = [
                str(a.get("name") or "")
                for a in (row.get("artists") or [])
                if isinstance(a, dict) and a.get("name")
            ]
            album = row.get("album") if isinstance(row.get("album"), dict) else {}
            poster = _https(str(album.get("picUrl") or ""))
            # 网易云图床支持 ?param=WxH 取缩略图，原图动辄上千像素
            if poster and "?" not in poster:
                poster = f"{poster}?param=300y300"
            # 单曲数字 id：跳转与「在网易云打开」都要用，缺了就当没有
            song_id = str(row.get("id") or "").strip()
            if not song_id.isdigit():
                song_id = ""
            items.append({
                "rank": index + 1,
                "title": str(row.get("name") or ""),
                "title_cn": str(row.get("name") or ""),
                "artist": " / ".join(artists),
                "album": str(album.get("name") or ""),
                "duration": _fmt_duration(
                    round(_safe_int(row.get("duration"), 0) / 1000)),
                "poster": poster,
                "last_rank": _safe_int(row.get("lastRank"), 0),
                "genre": "",
                "song_id": song_id,
                "song_url": (f"https://music.163.com/#/song?id={song_id}"
                             if song_id else ""),
            })
        # updateTime 是毫秒时间戳
        stamp = _safe_int(result.get("updateTime"), 0)
        updated = ""
        if stamp > 0:
            updated = datetime.datetime.fromtimestamp(
                stamp / 1000, tz=pytz.timezone(settings.TZ)
            ).strftime("%Y-%m-%d")
        return {
            "board": str(result.get("name") or ""),
            "items": items,
            "updated_at": updated,
            "url": f"https://music.163.com/#/discover/toplist?id={native_id}",
        }

    def _fetch_qq(self, native_id: str, limit: int) -> Dict[str, Any]:
        """QQ 音乐单个分榜。"""
        url = ("https://c.y.qq.com/v8/fcg-bin/fcg_v8_toplist_cp.fcg"
               f"?topid={quote(native_id)}&format=json&page=detail&tpl=3&type=top")
        payload = self._get_json(url, referer=_PLATFORM_REFERERS["qq"])
        rows = payload.get("songlist") or []
        if not isinstance(rows, list) or not rows:
            raise RuntimeError("QQ 音乐返回空曲目列表")
        items: List[Dict[str, Any]] = []
        for index, row in enumerate(rows[:limit]):
            if not isinstance(row, dict):
                continue
            data = row.get("data") if isinstance(row.get("data"), dict) else {}
            singers = [
                str(s.get("name") or "")
                for s in (data.get("singer") or [])
                if isinstance(s, dict) and s.get("name")
            ]
            album_mid = str(data.get("albummid") or "")
            poster = (f"https://y.qq.com/music/photo_new/"
                      f"T002R300x300M000{album_mid}.jpg") if album_mid else ""
            # QQ 的单曲主键是 songmid（songid 是数字 id，网页详情页用 mid）
            song_mid = str(data.get("songmid") or "").strip()
            items.append({
                "rank": _safe_int(row.get("rank"), index + 1) or (index + 1),
                "title": str(data.get("songname") or ""),
                "title_cn": str(data.get("songname") or ""),
                "artist": " / ".join(singers),
                "album": str(data.get("albumname") or ""),
                "duration": _fmt_duration(data.get("interval")),
                "poster": poster,
                # QQ 榜单接口不带上期名次
                "last_rank": 0,
                "genre": "",
                "song_id": song_mid,
                # ⚠️ 别用 y.qq.com/n/ryqq/songDetail/<mid>：那个路由对非浏览器 UA
                # 返回 302、跟随后是白屏（实测 Vis 之类轻量浏览器直接空白页）。
                # i.y.qq.com 的移动版分享页返回 200 且体积只有 1.6 KB，能正常展示歌曲并引导装 App。
                "song_url": (f"https://i.y.qq.com/n2/m/share/details/song.html?songmid={song_mid}"
                             if song_mid else ""),
            })
        topinfo = payload.get("topinfo") if isinstance(payload.get("topinfo"), dict) else {}
        # 榜名形如「巅峰榜·热歌」，去掉「巅峰榜·」前缀更好放
        board_name = str(topinfo.get("ListName") or "").replace("巅峰榜·", "")
        return {
            "board": board_name,
            "items": items,
            "updated_at": str(payload.get("update_time") or payload.get("date") or ""),
            "url": f"https://y.qq.com/n/ryqq/toplist/{native_id}",
        }

    def _fetch_apple(self, limit: int) -> Dict[str, Any]:
        """Apple Music 中国大陆「播放最多」榜（Apple 官方公开 Feed）。"""
        count = max(1, min(limit, _APPLE_MAX_LIMIT))
        url = ("https://rss.applemarketingtools.com/api/v2/cn/music/"
               f"most-played/{count}/songs.json")
        payload = self._get_json(url)
        feed = payload.get("feed") if isinstance(payload.get("feed"), dict) else {}
        rows = feed.get("results") or []
        if not isinstance(rows, list) or not rows:
            raise RuntimeError("Apple 返回空曲目列表")
        items: List[Dict[str, Any]] = []
        for index, row in enumerate(rows[:count]):
            if not isinstance(row, dict):
                continue
            # 官方只给 100×100 的封面，把尺寸段换掉就能拿到 300×300
            artwork = str(row.get("artworkUrl100") or "")
            poster = artwork.replace("100x100bb", "300x300bb")
            genres = [
                str(g.get("name") or "")
                for g in (row.get("genres") or [])
                if isinstance(g, dict) and g.get("name")
            ]
            items.append({
                "rank": index + 1,
                "title": str(row.get("name") or ""),
                "title_cn": str(row.get("name") or ""),
                "artist": str(row.get("artistName") or ""),
                # Feed 不提供专辑名
                "album": "",
                # Feed 不提供时长
                "duration": 0,
                "poster": poster,
                "last_rank": 0,
                "genre": genres[0] if genres else "",
                # Apple 的 id 就是 trackId，url 是官方给出的歌曲页（带 ?i=）
                "song_id": str(row.get("id") or "").strip(),
                "song_url": str(row.get("url") or "").strip(),
            })
        return {
            "board": "热门歌曲",
            "items": items,
            # Feed 没有榜单日期，用抓取日期兜底（调用方会再盖一次）
            "updated_at": "",
            "url": "https://music.apple.com/cn/playlist/pl.f4d106fed2bd41149aaacabb233eb5eb",
        }

    def _fetch_one(self, board_key: str) -> Optional[Dict[str, Any]]:
        """抓一个分榜，返回统一结构的榜单字典。"""
        meta = self._BOARD_META.get(board_key)
        if not meta:
            return None
        platform, code, native_id = meta
        limit = self._limit
        if platform == "netease":
            parsed = self._fetch_netease(native_id, limit)
        elif platform == "qq":
            parsed = self._fetch_qq(native_id, limit)
        elif platform == "apple":
            parsed = self._fetch_apple(limit)
        else:
            return None
        board_name = str(parsed.get("board") or "").strip()
        short_name = self._BOARD_NAMES.get(board_key, board_key).replace(
            _PLATFORM_NAMES.get(platform, ""), "")
        return {
            "type": board_key,
            "platform": platform,
            "platform_name": _PLATFORM_NAMES.get(platform, platform),
            "board_name": board_name or short_name,
            # 完整展示名：平台 + 分榜（App 侧分榜下拉直接用）
            "board": f"{_PLATFORM_NAMES.get(platform, platform)}{board_name or short_name}",
            "url": str(parsed.get("url") or ""),
            "updated_at": str(parsed.get("updated_at") or ""),
            "items": parsed.get("items") or [],
        }

    # ------------------------------------------------------------------
    # 抓取入口
    # ------------------------------------------------------------------
    def fetch(self) -> Dict[str, Any]:
        """执行一次三平台音乐榜单抓取，落快照。"""
        return self.__refresh_music() or {}

    def __refresh_music(self) -> Dict[str, Any]:
        if not self._boards:
            return {"board_count": 0, "item_count": 0,
                    "message": "未配置任何音乐榜单"}

        # 上一次快照：某个榜这次抓空时用它顶上，避免该榜在 App 里整个消失
        prev: Dict[str, dict] = {}
        try:
            old = self.get_data("snapshot") or {}
            for board in (old.get("boards") or []):
                if isinstance(board, dict) and board.get("type") and board.get("items"):
                    prev[str(board["type"])] = board
        except Exception as exc:
            logger.warning(f"读取音乐旧快照失败（忽略，按全新抓取处理）：{exc}")

        boards: List[dict] = []
        fresh: set = set()
        failed: List[str] = []

        def run(board_key: str) -> Tuple[str, Optional[dict], str]:
            try:
                return board_key, self._fetch_one(board_key), ""
            except Exception as exc:  # noqa: BLE001 - 单个榜失败不该拖垮整轮
                return board_key, None, str(exc)

        # 四路并发：九个榜串行 20 秒以上，并发后压到 6 秒左右
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
            results = list(pool.map(run, list(self._boards)))

        for board_key, board, error in results:
            name = self._BOARD_NAMES.get(board_key, board_key)
            if board and board.get("items"):
                boards.append(board)
                fresh.add(board_key)
            else:
                failed.append(name)
                if error:
                    logger.error(f"{name}抓取失败：{error}")
                kept = prev.get(board_key)
                if kept:
                    boards.append(kept)
                    logger.warning(
                        f"{name}本次未抓到条目，沿用上次快照"
                        f"（{kept.get('updated_at') or '无日期'}，"
                        f"{len(kept.get('items') or [])} 条）"
                    )
                else:
                    logger.warning(f"{name}本次未抓到条目，且没有可沿用的旧数据")

        if not boards:
            logger.error("音乐榜单未抓取到任何条目（网络异常或接口结构变化）")
            return {"board_count": 0, "item_count": 0,
                    "message": "抓取失败：网络异常或接口结构变化"}

        now = datetime.datetime.now(tz=pytz.timezone(settings.TZ))
        updated_at = now.strftime("%Y-%m-%d %H:%M:%S")
        total = sum(len(board["items"]) for board in boards)
        for board in boards:
            # 只有真抓到条目的榜才盖新时间；沿用的旧榜保留自己的时间戳，
            # 免得 App 上把一周前的数据显示成「刚刚更新」
            if str(board.get("type")) in fresh:
                board["updated_at"] = updated_at
        self.save_data("snapshot", {
            "updated_at": updated_at,
            "total": total,
            "boards": boards,
        })
        logger.info(
            f"音乐榜单刷新完成：{len(boards)} 个榜单 {total} 条"
            + (f"（未抓到：{'、'.join(failed)}）" if failed else "")
        )
        return {
            "board_count": len(boards),
            "item_count": total,
            "message": (f"以下榜单未抓到条目，沿用上次数据：{'、'.join(failed)}"
                        if failed else ""),
        }

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def rank_boards(self, board_type: str = None, kind: str = None,
                    limit: int = None) -> List[RankBoard]:
        """把快照里的音乐榜单转成统一模型。

        :param board_type: 平台代码（``netease`` / ``qq`` / ``apple``）
                           或完整榜代码（``netease_hot``），不传返回全部
        :param kind: 未使用，保留以统一签名
        :param limit: 每个榜单最多返回的条目数
        """
        wanted = str(board_type or "").strip()
        selected: List[RankBoard] = []
        for board in self.boards():
            code = str(board.get("type") or "")
            if wanted and code != wanted and not code.startswith(f"{wanted}_"):
                continue
            rows = board.get("items") or []
            if limit and limit > 0:
                rows = rows[:limit]
            payload = self.__safe_board_payload(board, rows)
            payload["source"] = self.source_id
            payload["count"] = len(payload["items"])
            # 与猫眼/红果同义：board_type 是跨源唯一的榜单标识
            payload["board_type"] = code
            selected.append(RankBoard(**payload))
        return selected

    @staticmethod
    def __safe_board_payload(board: dict, rows: List[Any]) -> Dict[str, Any]:
        """把存储中的榜单字典收窄为响应模型的合法字段。

        快照是历史数据，升级后可能多字段或缺字段，显式挑字段并补默认值，
        避免旧数据触发响应模型校验失败导致整个接口 500。
        """
        items = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "")
            items.append({
                "rank": _safe_int(row.get("rank")),
                "title": title,
                "title_cn": str(row.get("title_cn") or title),
                "poster": str(row.get("poster") or ""),
                "artist": str(row.get("artist") or ""),
                "album": str(row.get("album") or ""),
                "duration": _safe_int(row.get("duration")),
                "last_rank": _safe_int(row.get("last_rank")),
                "genre": str(row.get("genre") or ""),
                # 平台单曲 id + 详情页地址：App 的「去平台听」全靠这两个。
                # ⚠️ 这里是显式白名单，抓取端新增字段必须同步加进来，
                # 否则会被静默丢弃（响应里字段在、值恒空，很难排查）。
                "song_id": str(row.get("song_id") or ""),
                "song_url": str(row.get("song_url") or ""),
            })
        return {
            "board": str(board.get("board") or ""),
            "board_name": str(board.get("board_name") or ""),
            "platform": str(board.get("platform") or ""),
            "platform_name": str(board.get("platform_name") or ""),
            "url": str(board.get("url") or ""),
            "updated_at": str(board.get("updated_at") or ""),
            "count": len(items),
            "items": items,
        }

    def rank_list(self, board_type: str = None, kind: str = None,
                  page: int = 1, limit: int = None, base: str = ""
                  ) -> list:
        """推荐页用的扁平列表。

        音乐条目在宿主的媒体库里没有对应对象（不走识别），塞进
        ``RecommendMediaSource`` 只会得到对不上的空卡片，因此这里返回空列表 ——
        音乐榜只在 App 的榜单页展示，不进推荐流。
        """
        return []

    def recommend_sources(self) -> list:
        """音乐榜不注册到宿主推荐页（理由见 [rank_list]）。"""
        return []

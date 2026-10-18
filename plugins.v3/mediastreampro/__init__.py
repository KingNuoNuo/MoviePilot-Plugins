"""媒体流播放插件（MediaStreamPro）。

给客户端一条能直接喂播放器的视频流：``GET /stream?path=<媒体库内绝对路径>``，
返回原始文件（``FileResponse``，Starlette 自带 Range 支持，可拖动进度）。

设计取向（都是刻意的）：

- **装好即用，没有开关**：本插件只做一件事，不需要配置项；``get_state()`` 恒为 True，
  配置页只放一张说明卡。
- **媒体库根目录不写死、也不落配置**：每次请求实时读宿主的「目录配置」
  （``systemconfig["Directories"][*].library_path``）。这样宿主里换了媒体库路径、
  加了一个新库，插件不用重装、不用重启、也不用再配一遍。
  ``download_path`` 刻意**不放行** —— 播放只面向已整理入库的文件，权限面最小。
- **认证走宿主**：``auth="bear"``（JWT / API Key / token），插件自己不做任何 token 校验，
  也不接受把凭证明文拼进 URL。客户端用 ``Authorization: Bearer`` 头。
- **路径白名单**：``realpath`` 归一后必须落在某个媒体库根目录内（按目录分隔符比较，
  挡住 ``/DSM/media2`` 这类前缀串攻击），并且扩展名必须在视频白名单里。

三个对外接口：

- ``GET /stream?path=...``：视频文件流（Range / 拖动进度）；
- ``HEAD /stream?path=...``：只回头（文件长度、类型、是否支持 Range），供外部播放器
  或下载器先探一次再决定怎么播 —— 老版本没注册 HEAD，宿主路由直接 405；
- ``GET /season?path=...``：**同一季的集列表**。客户端只靠宿主的整理记录拼不出完整选集
  （`/history/transfer` 只留得下最近几天，且逐集下载时每集是独立种子、没有共同的
  ``download_hash``），所以这里直接扫媒体库里这个文件所在的目录，按「剧名主干 + 季号」认亲。
  不递归、不跨目录：宿主整理规则默认就是「剧名/Season 2/」这种结构，同一季必定同目录。
- ``GET /ping``：可用性探测 + 当前放行的媒体库目录（App 用它决定是否显示播放按钮）；
- 无表单配置项。
"""
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse, Response

from app.plugins import _PluginBase
from app.sdk.logging import logger

from .app_promo import promo_card

# 只放行视频容器，别的一律不认（同目录下还有 nfo / jpg / srt，不该被这条流读出去）
VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mkv", ".mov", ".webm", ".ts", ".m2ts",
    ".avi", ".flv", ".wmv", ".rmvb", ".rm", ".mpg", ".mpeg",
    ".3gp", ".ogv",
}

# 扩展名 → MIME。ExoPlayer 主要靠嗅探，但 Content-Type 给对能让部分客户端少走弯路
MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/x-m4v",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".ts": "video/mp2t",
    ".m2ts": "video/mp2t",
    ".avi": "video/x-msvideo",
    ".flv": "video/x-flv",
    ".wmv": "video/x-ms-wmv",
    ".rmvb": "application/vnd.rn-realmedia-vbr",
    ".rm": "application/vnd.rn-realmedia",
    ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg",
    ".3gp": "video/3gpp",
    ".ogv": "video/ogg",
}

# 宿主目录配置读不到时的兜底（不是配置项，只是让常见部署里插件立刻可用）
FALLBACK_ROOTS = ("/DSM/media", "/media")

# ---------- 选集识别（/season 用） ----------
# 季 + 集：S02E05 / s02.e05 / S02-E05（前后允许分隔符，避免把 S02E05 里的数字当别的）
_SEASON_EPISODE = re.compile(r"(?i)(?:^|[^0-9a-z])s(\d{1,3})[\s._-]*e(\d{1,4})(?:[^0-9]|$)")
# 只有集号：EP05 / E05 / Episode.05
_EPISODE_EN = re.compile(r"(?i)(?:^|[^0-9a-z])(?:e|ep|episode)[\s._-]*(\d{1,4})(?:[^0-9]|$)")
# 只有集号（中文）：第 5 集 / 第5话
_EPISODE_CN = re.compile(r"第\s*(\d{1,4})\s*[集话話]")
# 一次最多回多少集：防着有人把整季库塞进一个目录（正常一部剧一季几十集）
MAX_SEASON_FILES = 500


def parse_season_episode(filename: str) -> Optional[Tuple[Optional[int], int]]:
    """从文件名解析 ``(季号, 集号)``；认不出集号返回 None。

    季号可能为 None（``EP05`` 这种命名不给季）—— 此时只和同样没有季号的文件认亲，
    免得把「无季标记的第二季」拼进第一季。电影、花絮这类本来就没有集号的直接返回 None，
    客户端会当成单片处理。
    """
    stem = os.path.splitext(os.path.basename(filename))[0]
    match = _SEASON_EPISODE.search(stem)
    if match:
        return int(match.group(1)), int(match.group(2))
    for pattern in (_EPISODE_EN, _EPISODE_CN):
        match = pattern.search(stem)
        if match:
            return None, int(match.group(1))
    return None


def show_stem(filename: str) -> str:
    """剧名主干：抹掉季集标记及其后的规格（2160p / WEB-DL / 字幕组），只留能认亲的部分。

    只保留字母数字与汉字 —— 空格、点、下划线、方括号、各站点的标记都会被归一掉，
    于是 ``某剧.S02E05.2160p.WEB-DL.mkv`` 与 ``某剧 第05集.mkv`` 都能落到 ``某剧``。
    """
    stem = os.path.splitext(os.path.basename(filename))[0]
    cut = len(stem)
    for pattern in (_SEASON_EPISODE, _EPISODE_EN, _EPISODE_CN):
        match = pattern.search(stem)
        if match:
            cut = min(cut, match.start())
    stem = stem[:cut]
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", stem.lower())


class MediaStreamPro(_PluginBase):
    """媒体库文件流转发：把已入库的视频按原文件喂给客户端播放器。"""

    # 插件名称
    plugin_name = "媒体流播放"
    # 插件描述
    plugin_desc = ("把已整理入库的视频按原始文件流式传给客户端播放（支持拖动进度）。"
                   "自动跟随宿主「目录配置」里的媒体库目录，装好即用，无需配置。")
    # 插件图标
    plugin_icon = ("https://raw.githubusercontent.com/KingNuoNuo/"
                   "MoviePilot-Plugins/main/icons/mediastream.png")
    # 插件版本
    plugin_version = "1.1.0"
    # 插件作者
    plugin_author = "KingNuoNuo"
    # 作者主页
    author_url = "https://github.com/KingNuoNuo"
    # 插件配置项ID前缀
    plugin_config_prefix = "mediastreampro_"
    # 加载顺序
    plugin_order = 7
    # 可使用的用户级别：0 = 不做站点用户认证也能加载
    auth_level = 0

    def __init__(self):
        super().__init__()

    # ---------- 生命周期 ----------

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        """无配置项：功能常开。

        这里刻意不读 ``config["enabled"]`` —— 插件没有开关，安装即生效。
        媒体库根目录也不缓存（宿主随时可能在「目录配置」里改路径），见 [media_roots]。
        """
        self._enabled = True
        roots = self.media_roots()
        if roots:
            logger.info(f"媒体流播放已就绪，放行目录：{'、'.join(roots)}")
        else:
            logger.warning("媒体流播放未读到任何媒体库目录，请在宿主「设置 → 目录」里配置媒体库路径")

    def get_state(self) -> bool:
        """恒为运行中：本插件没有开关。"""
        return True

    def stop_service(self) -> None:
        """停止插件：**没有东西要停，但必须存在**。

        `_PluginBase` 把它列为抽象方法，缺了的话宿主加载时会直接抛
        ``Can't instantiate abstract class MediaStreamPro without an implementation
        for abstract method 'stop_service'``，整个插件加载失败（v1.0.0 就是这么挂的）。

        本插件没有定时任务、没有常驻线程、也没有需要收尾的连接 —— 每次请求实时
        读目录配置、`FileResponse` 由 Starlette 负责关闭文件句柄 —— 所以这里什么都不做。
        """

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """配置页只有两件事：App 推荐卡 + 「装好即用」说明，没有可配项。"""
        return (
            [
                promo_card(),
                {
                    "component": "VForm",
                    "content": [
                        {
                            "component": "VRow",
                            "content": [
                                {
                                    "component": "VCol",
                                    "props": {"cols": 12},
                                    "content": [
                                        {
                                            "component": "VAlert",
                                            "props": {
                                                "type": "info",
                                                "variant": "tonal",
                                                "density": "comfortable",
                                                "class": "mb-3",
                                                "title": "装好即用，无需配置",
                                                "text": ("本插件把已整理入库的视频按原始文件流式转发给客户端，"
                                                         "支持拖动进度。放行的目录自动取自宿主"
                                                         "「设置 → 目录」里的媒体库路径，"
                                                         "改了目录即刻生效，不用重装或重启。"),
                                            },
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
            {},
        )

    def get_page(self) -> Optional[List[dict]]:
        """详情页：App 推荐卡 + 当前放行的媒体库目录与用法。"""
        roots = self.media_roots()
        rows = [
            {
                "component": "VListItem",
                "props": {"density": "compact"},
                "content": [
                    {
                        "component": "VListItemTitle",
                        "props": {"class": "text-body-2 text-medium-emphasis"},
                        "text": root,
                    }
                ],
            }
            for root in roots
        ] or [
            {
                "component": "VAlert",
                "props": {
                    "type": "warning",
                    "variant": "tonal",
                    "text": "未读到媒体库目录：请先在「设置 → 目录」里配置媒体库路径。",
                },
            }
        ]

        return [
            promo_card(),
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "tonal", "class": "mb-3"},
                                "content": [
                                    {
                                        "component": "VCardTitle",
                                        "props": {"class": "text-subtitle-1"},
                                        "text": "放行的媒体库目录",
                                    },
                                    {
                                        "component": "VCardText",
                                        "props": {"class": "pa-0"},
                                        "content": [
                                            {
                                                "component": "VList",
                                                "props": {"density": "compact"},
                                                "content": rows,
                                            }
                                        ],
                                    },
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "tonal", "class": "mb-3"},
                                "content": [
                                    {
                                        "component": "VCardTitle",
                                        "props": {"class": "text-subtitle-1"},
                                        "text": "接口",
                                    },
                                    {
                                        "component": "VCardText",
                                        "props": {"class": "text-body-2 text-medium-emphasis"},
                                        "text": ("GET /stream?path=<媒体文件绝对路径>"
                                                 "（需在请求头带 Authorization: Bearer <token>，"
                                                 "返回支持 Range 的文件流；HEAD 只回头不回文件体）"
                                                 " ｜ GET /season?path=<媒体文件绝对路径>"
                                                 "（返回同一季还有哪些集，供客户端做选集）"
                                                 " —— 两者都只放行媒体库目录，下载目录不放行。"),
                                    },
                                ],
                            }
                        ],
                    },
                ],
            }
        ]

    # ---------- 对外接口 ----------

    def get_api(self) -> List[Dict[str, Any]]:
        """注册接口。

        认证统一用宿主的 ``bear`` 模式（JWT / API 令牌 / token 查询参数都由宿主校验），
        插件自己不解析任何凭据。客户端（MoviePilotApp）走 ``Authorization`` 头，
        **不把 token 拼进 URL** —— URL 会落进 nginx access.log、Referer 与播放器外链。
        """
        return [
            {
                "path": "/stream",
                "endpoint": self.stream,
                "methods": ["GET", "HEAD"],
                "auth": "bear",
                "summary": "视频文件流",
                "description": "按 path（媒体库内的绝对路径）返回原始视频文件，"
                               "支持 HTTP Range（客户端可拖动进度）；HEAD 只回头不回文件体。"
                               "只放行宿主目录配置里媒体库路径下的视频文件。",
            },
            {
                "path": "/season",
                "endpoint": self.season,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "同季集列表",
                "description": "按 path（媒体库内某个视频的绝对路径）扫它所在目录，"
                               "返回同一部剧同一季的其它集，供客户端拼选集。"
                               "按「剧名主干 + 季号」认亲，不递归、不跨目录。",
            },
            {
                "path": "/ping",
                "endpoint": self.ping,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "可用性探测",
                "description": "返回当前放行的媒体库目录，供客户端判断是否显示播放入口。",
            },
        ]

    def ping(self) -> Any:
        """可用性探测：客户端进下载页时探一次，决定是否显示播放按钮。"""
        return {
            "success": True,
            "data": {
                "version": self.plugin_version,
                "roots": self.media_roots(),
            },
        }

    def stream(self, request: Request, path: str = "") -> Any:
        """返回视频文件流（Range 由 Starlette 的 FileResponse 处理）。

        ``path`` 是普通标量默认参数 → FastAPI 当成查询参数，不需要额外声明。
        ``request`` 只用来区分 GET / HEAD：HEAD 自己构造一个只有响应头的 200，
        不走 FileResponse —— 部分 Starlette 版本对 HEAD 仍会去打开文件、甚至在
        生成器里读数据，白白占一次句柄与一次磁盘预读。
        """
        target = self.resolve(path)
        if target is None:
            # 越权路径 / 非法扩展名 / 空参数统一 403，不回显具体原因，免得被当成探测工具
            logger.warning(f"媒体流播放拒绝请求：{path!r}")
            return JSONResponse(
                {"success": False, "message": "路径不在可播放范围内"}, status_code=403
            )
        if not os.path.isfile(target):
            return JSONResponse(
                {"success": False, "message": "文件不存在或已被移走"}, status_code=404
            )
        ext = os.path.splitext(target)[1].lower()
        media_type = MEDIA_TYPES.get(ext, "application/octet-stream")
        headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}

        if request.method.upper() == "HEAD":
            # 外置播放器 / 下载器常用的探路请求：长度 + 类型 + 支持 Range 就够了。
            # 这里刻意不看 Range 头，HEAD 一律回 200 + 完整长度（不是 206），
            # 与 nginx / Apache 对 HEAD 的处理一致；真取分片请用 GET。
            try:
                size = os.path.getsize(target)
            except OSError as error:
                logger.warning(f"媒体流播放读取失败：{error}")
                return JSONResponse(
                    {"success": False, "message": "文件读取失败"}, status_code=500
                )
            headers["Content-Length"] = str(size)
            logger.debug(f"媒体流播放 HEAD：{os.path.basename(target)}（{size} 字节）")
            return Response(status_code=200, headers=headers, media_type=media_type)

        logger.debug(f"媒体流播放：{os.path.basename(target)}")
        return FileResponse(
            target,
            media_type=media_type,
            # 文件名不放 Content-Disposition（inline 播放，浏览器里也不会触发下载）
            headers=headers,
        )

    # ---------- 内部 ----------

    def season(self, path: str = "") -> Any:
        """同一季的集列表：扫当前文件所在目录，按「剧名主干 + 季号」认亲。

        为什么不在宿主整理记录里拼：``/history/transfer`` 只保留最近几天的回执，
        更早入库的集根本查不到；而逐集下载时每集是独立种子，没有共同的 ``download_hash``。
        直接扫媒体库没有这两个限制 —— 文件在，集就在。

        只用**当前文件所在的那一个目录**：不递归、不跨目录。宿主整理规则默认是
        「剧名/Season 2/」，同一季必定同目录；跨目录扫会把别的剧、预告、花絮全带进来。
        """
        target = self.resolve(path)
        if target is None:
            return JSONResponse(
                {"success": False, "message": "路径不在可播放范围内"}, status_code=403
            )
        if not os.path.isfile(target):
            return JSONResponse(
                {"success": False, "message": "文件不存在或已被移走"}, status_code=404
            )

        parsed = parse_season_episode(target)
        if parsed is None:
            # 认不出集号（电影、花絮、命名诡异）→ 只回它自己，客户端按单片处理
            return {"success": True, "data": {"season": None, "episodes": [self._episode_item(target, None)]}}

        season_no, episode_no = parsed
        stem = show_stem(target)
        if not stem:
            # 文件名里除了集号什么都没有，没法认亲，同样只回自己
            return {"success": True, "data": {"season": season_no, "episodes": [self._episode_item(target, episode_no)]}}

        roots = self.media_roots()

        def allowed(candidate: str) -> bool:
            """realpath 后仍在某个媒体库根内（挡符号链接逃逸），复用 /stream 同一套口径。"""
            return any(
                candidate == root or candidate.startswith(root + os.sep) for root in roots
            )

        found: List[Dict[str, Any]] = []
        try:
            with os.scandir(os.path.dirname(target)) as entries:
                for entry in entries:
                    if len(found) >= MAX_SEASON_FILES:
                        break
                    try:
                        if not entry.is_file() or os.path.splitext(entry.name)[1].lower() not in VIDEO_EXTENSIONS:
                            continue
                    except OSError:
                        continue
                    if show_stem(entry.name) != stem:
                        continue
                    candidate = os.path.realpath(entry.path)
                    if not allowed(candidate):
                        continue
                    item_parsed = parse_season_episode(entry.name)
                    if item_parsed is None or item_parsed[0] != season_no:
                        continue
                    found.append(self._episode_item(candidate, item_parsed[1]))
        except OSError as error:
            logger.warning(f"扫描同季文件失败：{error}")

        if not found:
            return {"success": True, "data": {"season": season_no, "episodes": [self._episode_item(target, episode_no)]}}

        # 同一集可能有多个版本（2160p / 1080p 同时留着）→ 同集号只留一个：
        # 正在播的那份优先（免得客户端按 path 找不到当前集），否则留体积最大的
        dedup: Dict[Optional[int], Dict[str, Any]] = {}
        for item in found:
            key = item["episode"]
            previous = dedup.get(key)
            if previous is None:
                dedup[key] = item
            elif item["path"] == target:
                dedup[key] = item
            elif previous["path"] != target and item["size"] > previous["size"]:
                dedup[key] = item

        episodes = [dedup[key] for key in sorted(dedup, key=lambda n: (n is None, n or 0))]
        logger.debug(f"同季集列表：{stem} S{season_no} 共 {len(episodes)} 集")
        return {"success": True, "data": {"season": season_no, "stem": stem, "episodes": episodes}}

    @staticmethod
    def _episode_item(file_path: str, episode: Optional[int]) -> Dict[str, Any]:
        """一条选集项：客户端只用 ``path`` 与 ``label``，``size`` 留给同集多版本去重。"""
        try:
            size = os.path.getsize(file_path)
        except OSError:
            size = 0
        return {
            "path": file_path,
            "episode": episode,
            "label": str(episode) if episode is not None else "正片",
            "name": os.path.basename(file_path),
            "size": size,
        }

    def media_roots(self) -> List[str]:
        """当前放行的媒体库根目录（实时读宿主「目录配置」，去重保序）。"""
        roots: List[str] = []
        try:
            directories = self.systemconfig.get("Directories") or []
        except Exception as error:  # 宿主配置服务未就绪时不该让接口 500
            logger.warning(f"读取宿主目录配置失败：{error}")
            directories = []

        for directory in directories:
            if not isinstance(directory, dict):
                continue
            # 只取媒体库路径（library_path），下载路径 download_path 刻意不放行
            raw = (directory.get("library_path") or "").strip()
            if not raw:
                continue
            root = os.path.realpath(raw)
            if os.path.isdir(root) and root not in roots:
                roots.append(root)

        if not roots:
            # 兜底：目录配置为空（或还没配好）时，按常见挂载点放行，避免插件装了却完全不可用
            for raw in FALLBACK_ROOTS:
                root = os.path.realpath(raw)
                if os.path.isdir(root) and root not in roots:
                    roots.append(root)
        return roots

    def resolve(self, raw_path: str) -> Optional[str]:
        """把请求路径归一成可播放的绝对路径；不在白名单内返回 None。"""
        raw = (raw_path or "").strip()
        if not raw or "\x00" in raw or not os.path.isabs(raw):
            return None
        # realpath 会消掉 ``..``、重复分隔符与符号链接，之后的前缀比较才有意义
        target = os.path.realpath(raw)
        if os.path.splitext(target)[1].lower() not in VIDEO_EXTENSIONS:
            return None
        for root in self.media_roots():
            # 用 root + 分隔符 比较，挡住 /DSM/media2 被 /DSM/media 放行这种前缀串
            if target == root or target.startswith(root + os.sep):
                return target
        return None

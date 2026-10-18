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
- ``GET /ping``：可用性探测 + 当前放行的媒体库目录（App 用它决定是否显示播放按钮）；
- 无表单配置项。
"""
import os
from typing import Any, Dict, List, Optional, Tuple

from fastapi.responses import FileResponse, JSONResponse

from app.plugins import _PluginBase
from app.sdk.logging import logger

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
    plugin_version = "1.0.0"
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

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """配置页只放说明：没有可配项，因此不渲染任何输入控件。"""
        return (
            [
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
        """详情页：显示当前放行的媒体库目录与用法。"""
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
                                        "text": ("GET /api/v1/plugin/MediaStreamPro/stream?path=<媒体文件绝对路径>"
                                                 "（需在请求头带 Authorization: Bearer <token>，"
                                                 "返回支持 Range 的文件流；不放行下载目录）"),
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
                "methods": ["GET"],
                "auth": "bear",
                "summary": "视频文件流",
                "description": "按 path（媒体库内的绝对路径）返回原始视频文件，"
                               "支持 HTTP Range（客户端可拖动进度）。"
                               "只放行宿主目录配置里媒体库路径下的视频文件。",
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

    def stream(self, path: str = "") -> Any:
        """返回视频文件流（Range 由 Starlette 的 FileResponse 处理）。

        ``path`` 是普通标量默认参数 → FastAPI 当成查询参数，不需要额外声明。
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
        logger.debug(f"媒体流播放：{os.path.basename(target)}")
        return FileResponse(
            target,
            media_type=MEDIA_TYPES.get(ext, "application/octet-stream"),
            # 文件名不放 Content-Disposition（inline 播放，浏览器里也不会触发下载）
            headers={"Accept-Ranges": "bytes", "Cache-Control": "no-store"},
        )

    # ---------- 内部 ----------

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

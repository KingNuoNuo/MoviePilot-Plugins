"""媒体流播放插件（MediaStreamPro）。

给客户端一条能直接喂播放器的视频流：``GET /stream?path=<媒体库内绝对路径>``，
返回原始文件（``FileResponse``，Starlette 自带 Range 支持，可拖动进度）。

v1.3.0 起 **支持 STRM 播放**（网盘直链模式）：

- 请求路径以 ``.strm`` 结尾 → 读出文件里的网盘直链（http/https），**302 跳转**给播放器，
  流量不过 NAS（Range / 拖动进度由播放器对网盘直链直接续传）；
- 请求的是视频文件但**本地不存在**、且配置了「STRM 目录」→ 去 STRM 目录树里找
  同名 ``<文件名>.strm``（TgtoDrive 生成的 STRM 与媒体文件同名），找到即按上面跳转；
- STRM 目录两个来源：「跟随 TgtoDrive 插件」开关打开时，实时读 TgtoDriveSync 插件
  配置里的 ``strm_dir``（走插件基类 ``get_config`` 直接读宿主配置库，结果缓存，失败
  沿用旧值）；关闭时用本插件自己填的路径。

v1.5.0 起 **STRM 直链替客户端解析**（默认开）：

- STRM 里的直链 host 常是容器名（如 ``TgtoDrive``），手机和 MoviePilot 容器都解析不了，
  导致 302 给手机后手机 DNS 失败、播不了。现在默认由插件**先请求直链、跟随 302 跳转链**
  （最多 ``MAX_STRM_HOPS`` 跳），拿到最终的公网 CDN 直链再 302 给手机 —— 这正是
  Emby / Jellyfin 服务端在 NAS 上做的事，手机最终连的是公网 CDN（如
  ``vip-download-cdn.123295.com``），内外网都能播、流量不过 NAS。
- ``strm_resolve_host`` 配置项：STRM 直链里的 host 在容器内 ``getaddrinfo`` 失败时，
  先替换成这个地址（如运行 TgtoDrive 的主机内网 IP ``10.0.0.4``）再请求。
- ``strm_follow_redirect`` 开关：关掉则退回 v1.4.0 行为（直接 302 原始直链）。

设计取向（都是刻意的）：

- **放行目录实时读宿主「目录配置」**（``systemconfig["Directories"]``）：
  **媒体库路径（library_path）+ 下载路径（download_path）都放行**。v1.1.0 只放媒体库，
  v1.2.0 起客户端要在「下载完成」页签直接播还没整理（甚至整理失败）的文件，下载目录一并放行。
  这样宿主里换了路径、加了新库，插件不用重装、不用重启、也不用再配一遍。
  v1.3.0 起 STRM 目录（生效值）也加入放行范围，``.strm`` 文件本身可以直接请求。
- **认证走宿主**：``auth="bear"``（JWT / API Key / token），插件自己不做任何 token 校验，
  也不接受把凭证明文拼进 URL。客户端用 ``Authorization: Bearer`` 头。
- **路径白名单**：``realpath`` 归一后必须落在某个媒体库根目录内（按目录分隔符比较，
  挡住 ``/DSM/media2`` 这类前缀串攻击），并且扩展名必须是视频白名单或 ``.strm``。

四个对外接口：

- ``GET /stream?path=...``：视频文件流（Range / 拖动进度）；STRM → 302 网盘直链；
- ``HEAD /stream?path=...``：只回头（文件长度、类型、是否支持 Range），供外部播放器
  或下载器先探一次再决定怎么播 —— 老版本没注册 HEAD，宿主路由直接 405；
- ``GET /season?path=...``：**同一季的集列表**。客户端只靠宿主的整理记录拼不出完整选集
  （`/history/transfer` 只留得下最近几天，且逐集下载时每集是独立种子、没有共同的
  ``download_hash``），所以这里直接扫媒体库里这个文件所在的目录，按「剧名主干 + 季号」认亲。
  不递归、不跨目录：宿主整理规则默认就是「剧名/Season 2/」这种结构，同一季必定同目录。
  STRM 文件不做同季扫描（网盘直链没有目录结构可扫），只回它自己。
- ``GET /ping``：可用性探测 + 当前放行的媒体库目录与生效的 STRM 目录
  （App 用它决定是否显示播放按钮）；
- ``GET /downloads``：递归扫下载目录里的视频文件清单（path/name/size/mtime），
  供客户端把「下载完成」历史记录按种子名匹配到磁盘上的真实文件 —— 文件被删了就匹配不到，
  客户端据此把 ▶ 换成「已删除」；
"""
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from app.plugins import _PluginBase
from app.sdk.logging import logger

from .app_promo import promo_card

# 只放行视频容器，别的一律不认（同目录下还有 nfo / jpg / srt，不该被这条流读出去）
VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mkv", ".mov", ".webm", ".ts", ".m2ts",
    ".avi", ".flv", ".wmv", ".rmvb", ".rm", ".mpg", ".mpeg",
    ".3gp", ".ogv",
}

# STRM 文件：不是视频容器，是「指向网盘直链的文本」—— /stream 读出直链后 302 跳转
STRM_EXTENSION = ".strm"

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

# ---------- 下载目录扫描（/downloads 用） ----------
# 递归最大深度：qB 的保存目录一般是「下载根/种子名/文件」两层，4 层兜住再套一层盘符的部署
MAX_SCAN_DEPTH = 4
# 清单条数上限：下载目录里全是视频的场景（电影库混用下载目录）也就几百个，防异常目录树拖死接口
MAX_SCAN_FILES = 2000

# ---------- STRM（v1.3.0 起，v1.4.0 改为有 STRM 就优先走 STRM） ----------
# 「跟随 TgtoDrive 插件」读到的 strm_dir 缓存秒数：避免每次播放都打一遍宿主本机 API
STRM_FOLLOW_TTL = 60
# 跟随模式的缓存（模块级，进程内共享）
_strm_follow_cache = {"ts": 0.0, "dir": ""}
_strm_follow_lock = threading.Lock()
# 解析直链时最多跟随的 302 跳数（TgtoDrive→CDN→真实源，3 跳封顶）
MAX_STRM_HOPS = 4
# 解析直链时单跳请求超时（秒）
STRM_RESOLVE_TIMEOUT = 6


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
    plugin_desc = ("把已整理入库与下载目录里的视频按原始文件流式传给客户端播放（支持拖动进度）；"
                   "支持 STRM 网盘直链播放（插件替客户端解析容器名、跟随跳转到最终 CDN，"
                   "302 跳转后流量不过 NAS）。"
                   "自动跟随宿主「目录配置」里的媒体库与下载路径，STRM 目录可跟随 TgtoDrive 插件。")
    # 插件图标
    plugin_icon = ("https://raw.githubusercontent.com/KingNuoNuo/"
                   "MoviePilot-Plugins/main/icons/mediastream.png")
    # 插件版本
    plugin_version = "1.5.3"
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
        # STRM 配置（init_plugin 会覆盖；给默认值防请求先于初始化）
        self._strm_dir: str = ""
        self._strm_follow: bool = False
        # STRM 直链里 host 是容器/手机都解析不了的名字（如 TgtoDrive）时替换成的地址
        self._strm_resolve_host: str = ""
        # 是否由插件先走完 302 跳转链、把最终 CDN 直链给手机（默认开）
        self._strm_follow_redirect: bool = True

    # ---------- 生命周期 ----------

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        """读 STRM 配置；其余功能无开关常开。

        媒体库根目录不缓存（宿主随时可能在「目录配置」里改路径），见 [media_roots]。
        STRM 目录的「生效值」每次请求实时算（跟随模式要拿 TgtoDriveSync 的最新配置），
        这里只保存本插件自己的配置项。
        """
        self._enabled = True
        config = config or {}
        self._strm_dir = str(config.get("strm_dir") or "").strip()
        self._strm_follow = bool(config.get("strm_follow_tgtodrive"))
        self._strm_resolve_host = str(config.get("strm_resolve_host") or "").strip()
        # strm_follow_redirect 默认开：未在配置里显式关闭时视为 True
        self._strm_follow_redirect = config.get("strm_follow_redirect", True) is not False
        roots = self.media_roots()
        strm_dir = self.effective_strm_dir()
        if roots:
            logger.info(f"媒体流播放已就绪，放行目录：{'、'.join(roots)}")
        else:
            logger.warning("媒体流播放未读到任何媒体库目录，请在宿主「设置 → 目录」里配置媒体库路径")
        if strm_dir:
            source = "TgtoDrive 插件（跟随）" if self._strm_follow and not self._strm_dir else "本插件配置"
            logger.info(f"媒体流播放 STRM 目录已启用：{strm_dir}（{source}）")
        else:
            logger.info("媒体流播放未配置 STRM 目录，仅支持本地文件播放")

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
        """配置页：App 推荐卡 + STRM 配置 + 说明卡。"""
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
                                            "component": "VSwitch",
                                            "props": {
                                                "model": "strm_follow_tgtodrive",
                                                "label": "跟随 TgtoDrive 插件的 STRM 目录",
                                                "hint": "开启后自动使用「TgtoDrive STRM 同步」插件里配置的 STRM 目录，下方路径可不填",
                                                "persistent-hint": True,
                                            },
                                        }
                                    ],
                                }
                            ],
                        },
                        {
                            "component": "VRow",
                            "content": [
                                {
                                    "component": "VCol",
                                    "props": {"cols": 12},
                                    "content": [
                                        {
                                            "component": "VTextField",
                                            "props": {
                                                "model": "strm_dir",
                                                "label": "STRM 目录（容器内路径，可选）",
                                                "placeholder": "例如 /media-strm（未开跟随开关时生效）",
                                            },
                                        }
                                    ],
                                }
                            ],
                        },
                        {
                            "component": "VRow",
                            "content": [
                                {
                                    "component": "VCol",
                                    "props": {"cols": 12},
                                    "content": [
                                        {
                                            "component": "VSwitch",
                                            "props": {
                                                "model": "strm_follow_redirect",
                                                "label": "替客户端解析网盘直链",
                                                "hint": "默认开。开启后由插件先请求 STRM 里的直链、跟随跳转拿到最终 CDN 地址再 302 给客户端——手机不用解析容器名（如 TgtoDrive），内外网都能播",
                                                "persistent-hint": True,
                                            },
                                        }
                                    ],
                                }
                            ],
                        },
                        {
                            "component": "VRow",
                            "content": [
                                {
                                    "component": "VCol",
                                    "props": {"cols": 12},
                                    "content": [
                                        {
                                            "component": "VTextField",
                                            "props": {
                                                "model": "strm_resolve_host",
                                                "label": "直链主机名替换（可选）",
                                                "placeholder": "如 TgtoDrive 在容器里解析不了，填 10.0.0.4 会把直链里的 host 换成它",
                                                "hint": "STRM 直链里写的是容器名 TgtoDrive，手机与 MoviePilot 容器都解析不了。填运行 TgtoDrive 的主机内网 IP（如 10.0.0.4），插件请求直链时会先把它替换掉，再跟随跳转拿到最终 CDN 地址。",
                                                "persistent-hint": True,
                                            },
                                        }
                                    ],
                                }
                            ],
                        },
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
                                                "title": "放行目录自动跟随宿主，STRM 可选",
                                                "text": ("本插件把已整理入库与下载目录里的视频按原始文件流式转发给客户端，"
                                                         "支持拖动进度。放行目录自动取自宿主「设置 → 目录」里的媒体库与下载路径，"
                                                         "改了即刻生效。配置 STRM 目录（或开启跟随 TgtoDrive 插件）后，"
                                                         "整理记录里的 STRM 影片也能播：插件读出网盘直链后 302 跳转，"
                                                         "流量不过 NAS。"),
                                            },
                                        }
                                    ],
                                }
                            ],
                        },
                    ],
                },
            ],
            {
                "strm_follow_tgtodrive": False,
                "strm_dir": "",
            },
        )

    def get_page(self) -> Optional[List[dict]]:
        """详情页：App 推荐卡 + 当前放行的媒体库目录、STRM 状态与用法。"""
        roots = self.media_roots()
        strm_dir = self.effective_strm_dir()
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
        rows.append(
            {
                "component": "VListItem",
                "props": {"density": "compact"},
                "content": [
                    {
                        "component": "VListItemTitle",
                        "props": {"class": "text-body-2 text-medium-emphasis"},
                        "text": f"STRM：{strm_dir or '未配置（不支持 STRM 播放）'}"
                                + ("（跟随 TgtoDrive 插件）" if strm_dir and self._strm_follow else ""),
                    }
                ],
            }
        )

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
                                                 "返回支持 Range 的文件流；STRM 文件或配置了 STRM 目录时的缺失文件，"
                                                 "读出网盘直链后 302 跳转；HEAD 只回头不回文件体）"
                                                 " ｜ GET /season?path=<媒体文件绝对路径>"
                                                 "（返回同一季还有哪些集，供客户端做选集）"
                                                 " —— 两者都放行媒体库与下载目录（宿主「设置 → 目录」）与 STRM 目录。"),
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
                "path": "/downloads",
                "endpoint": self.downloads,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "下载目录视频清单",
                "description": "递归扫所有下载目录（限深度与条数），返回磁盘上真实存在的视频文件，"
                               "供客户端把「下载完成」历史按种子名匹配到文件；文件被删则匹配不到。",
            },
            {
                "path": "/strm-check",
                "endpoint": self.strm_check,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "批量判断是否 STRM 媒体",
                "description": "POST JSON {paths: [...]}，逐个判断能否在 STRM 目录里对上同名 .strm，"
                               "返回 {path: bool} 映射，供客户端在列表里打 STRM 标识。",
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

    async def strm_check(self, request: Request) -> Any:
        """批量判断一批媒体路径是否有对应 STRM（供客户端打 STRM 标识）。

        POST JSON ``{"paths": [...]}``（媒体库内绝对路径）。对每条路径先 resolve 放行，
        再复用 [_find_strm_for] 判是否有同名 ``.strm``；路径本身就是 ``.strm`` 也算有。
        返回 ``{path: bool}`` 映射。未配置 STRM 目录时返回空映射（客户端自然不画标识）。
        """
        strm_dir = self.effective_strm_dir()
        if not strm_dir:
            # 没配置 STRM 目录 → 空映射，客户端自然不画标识
            return {"success": True, "data": {"strm": {}}}

        body: Dict[str, Any] = {}
        try:
            # Starlette 的 request.json() 是 async 方法，必须 await，否则拿到 coroutine
            payload = await request.json() if hasattr(request, "json") else None
            if isinstance(payload, dict):
                body = payload
        except Exception:
            body = {}
        raw = body.get("paths")
        if not isinstance(raw, list):
            raw = []

        result: Dict[str, bool] = {}
        for path in raw:
            if not isinstance(path, str) or not path:
                continue
            target = self.resolve(path)
            if target is None:
                # 越权路径：不放进结果，客户端拿不到就当非 strm
                continue
            if target.lower().endswith(STRM_EXTENSION):
                result[path] = True
                continue
            result[path] = self._find_strm_for(target) is not None

        return {"success": True, "data": {"strm": result}}

    def ping(self) -> Any:
        """可用性探测：客户端进下载页时探一次，决定是否显示播放按钮。"""
        return {
            "success": True,
            "data": {
                "version": self.plugin_version,
                "roots": self.media_roots(),
                "strm_dir": self.effective_strm_dir(),
            },
        }

    def stream(self, request: Request, path: str = "") -> Any:
        """返回视频文件流（Range 由 Starlette 的 FileResponse 处理）。

        配置了 STRM 目录时**优先 302 网盘直链**：只要能对上同名 .strm，哪怕本地
        （网盘挂载）文件读得到也走直链，不占 NAS 流量；对不上才直读本地文件。

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

        # STRM：请求的就是 .strm 文件 → 读出直链 302 跳转
        if target.lower().endswith(STRM_EXTENSION):
            return self._stream_strm(request, target)

        # STRM 优先：配置了 STRM 目录且能对上同名 .strm → 一律 302 网盘直链，
        # 哪怕本地（网盘挂载）文件读得到也不走 NAS 流量；对不上才直读本地。
        if self.effective_strm_dir():
            strm_file = self._find_strm_for(target)
            if strm_file:
                return self._stream_strm(request, strm_file)

        # 走到这说明既没有 .strm 也没有可用直链：本地文件存在才回文件流
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

    def _stream_strm(self, request: Request, strm_file: str) -> Any:
        """STRM 播放：读出网盘直链后 302 跳转（GET / HEAD 同样处理）。

        刻意选 302 而不是代理转发：网盘直链的流量直接走 播放器 ↔ 网盘 CDN，
        不占 NAS 带宽，Range / 拖动进度由播放器对直链续传。TgtoDrive 生成的
        直链本来就是给 Emby / Jellyfin 直接 GET 的，无需带任何特殊头。

        v1.5.0 起：STRM 里的直链 host 常是容器名（如 ``TgtoDrive``），手机和
        MoviePilot 容器都解析不了。默认由插件**先走完跳转链**（经可替换的 host
        请求第一跳、跟随 302），拿到最终的公网 CDN 直链再 302 给手机 —— 这正是
        Emby 服务端在 NAS 上做的事。若关闭「替客户端解析」，则退化为直接 302 原始直链。
        """
        url = self._read_strm_url(strm_file)
        if not url:
            logger.warning(f"媒体流播放 STRM 无效（读不出直链）：{strm_file}")
            return JSONResponse(
                {"success": False, "message": "STRM 文件无效"}, status_code=404
            )

        if self._strm_follow_redirect:
            resolved = self._resolve_strm_url(url)
            if resolved:
                logger.info(
                    f"媒体流播放 STRM 直链已解析：{os.path.basename(strm_file)}"
                )
                return RedirectResponse(resolved, status_code=302)
            # 解析失败不致命：退回原始直链（跟老版本一致），至少内网/本机还能用
            logger.warning(f"媒体流播放 STRM 直链解析失败，退回原始直链：{url[:80]}")
        logger.info(f"媒体流播放 STRM 跳转：{os.path.basename(strm_file)}")
        return RedirectResponse(url, status_code=302)

    def _resolve_strm_url(self, url: str) -> Optional[str]:
        """把 STRM 直链走完 302 跳转链，返回最终可直连的 CDN 地址。

        - ``strm_resolve_host`` 非空时，先把直链里的 host（仅 hostname 部分）替换成该值：
          TgtoDrive 这类容器名在 MoviePilot 容器里 ``getaddrinfo`` 都失败，必须换成
          可达的地址（如运行 TgtoDrive 的主机内网 IP ``10.0.0.4``）。
        - 然后逐跳请求（``allow_redirects=False``），最多 ``MAX_STRM_HOPS`` 次，
          每次读 ``Location`` 继续跟，**直到没有 Location（走到链尾）才停**。
          TgtoDrive→CDN→真实源 通常 2~3 跳，链尾就是公网 CDN 直链。
        - 全程 HEAD 优先、失败退回 GET（只读头，不下载视频体），单跳超时
          ``STRM_RESOLVE_TIMEOUT`` 秒，整体异常兜底返回 None（调用方退回原始直链）。
        """
        try:
            from urllib.parse import urlsplit, urlunsplit, urljoin

            current = url

            # 替换 host：只动 hostname，保留协议/端口/路径/查询
            if self._strm_resolve_host:
                parts = urlsplit(current)
                host = parts.hostname or ""
                if host and self._is_container_host(host):
                    # 解析不了 → 换成配置的地址（可为纯 IP 或 IP:端口）
                    new_netloc = self._strm_resolve_host
                    if ":" in new_netloc and not new_netloc.startswith("["):
                        host_part, _, port_part = new_netloc.rpartition(":")
                        if host_part and port_part.isdigit():
                            netloc = f"{host_part}:{port_part}"
                        else:
                            netloc = new_netloc
                    else:
                        netloc = new_netloc + (
                            f":{parts.port}" if parts.port else ""
                        )
                    current = urlunsplit(
                        (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
                    )

            # 逐跳跟随，直到没有 Location（链尾）
            for _ in range(MAX_STRM_HOPS):
                resp = requests.request(
                    "HEAD",
                    current,
                    allow_redirects=False,
                    timeout=STRM_RESOLVE_TIMEOUT,
                    headers={"User-Agent": "MediaStreamPro/1.5"},
                )
                # HEAD 不被支持时退回 GET（也只取头，stream=True 不读体）
                if resp.status_code in (405, 501, 403):
                    resp.close()
                    resp = requests.request(
                        "GET",
                        current,
                        allow_redirects=False,
                        timeout=STRM_RESOLVE_TIMEOUT,
                        stream=True,
                        headers={"User-Agent": "MediaStreamPro/1.5"},
                    )
                location = resp.headers.get("Location")
                resp.close()
                if not location:
                    # 没有下一跳，当前地址就是终点（可能是 CDN 直链或真实视频源）
                    return current
                current = urljoin(current, location)

            # 跳数用尽，返回最后拿到的地址
            return current
        except Exception as error:
            logger.warning(f"媒体流播放解析直链异常：{error}")
            return None

    @staticmethod
    def _is_container_host(host: str) -> bool:
        """粗判 host 是不是「容器名/内网主机名」这类需要替换的 host。

        容器名（TgtoDrive）、mDNS 名（*.local）在容器里解析不了，需要替换；
        公网域名、IP 不需要。判定：无点号的裸主机名、或 .local 结尾 → 容器名。
        """
        if not host:
            return False
        if host.endswith(".local"):
            return True
        # 无点号的裸主机名（TgtoDrive、mybridge 别名）→ 容器名
        return "." not in host and ":" not in host

    # ---------- STRM ----------

    def effective_strm_dir(self) -> str:
        """生效的 STRM 目录：跟随模式优先取 TgtoDriveSync 的配置，取不到退回本插件填写值。"""
        if self._strm_follow:
            followed = self._tgtodrive_strm_dir()
            if followed:
                return followed
        return self._strm_dir

    def _tgtodrive_strm_dir(self) -> str:
        """读「TgtoDrive STRM 同步」插件配置里的 strm_dir（宿主插件配置库，缓存）。

        为什么走 ``self.get_config`` 而不是宿主本机 API：v1.5.2 之前这里是
        ``requests.get("http://127.0.0.1:3001/api/v1/plugin/TgtoDriveSync")`` 自调，
        而宿主的 uvicorn 是**单 worker** —— 当 strm-check 请求进来时，它同步阻塞地
        去请求同一个 127.0.0.1:3001，等于自己等自己，5 秒超时后 strm_dir 变空、
        且缓存只在成功时刷新 → 之后每次 strm-check 都死锁超时，客户端拿到的 STRM
        标识就「出现一会后全部消失」。改成走插件基类的 ``get_config``（直接读宿主
        SQLite 的 plugininstance 表，毫秒级、无网络、无死锁）。
        失败时保留旧缓存值（stale-while-error），不再因为一次抖动清空目录。
        """
        with _strm_follow_lock:
            cached = _strm_follow_cache
            if cached["dir"] and time.time() - cached["ts"] < STRM_FOLLOW_TTL:
                return cached["dir"]
        strm_dir = ""
        try:
            # 插件基类提供的跨插件配置读取：读本地 plugininstance 表，不经 HTTP
            cfg = self.get_config("TgtoDriveSync") or {}
            strm_dir = str(cfg.get("strm_dir") or "").strip()
        except Exception as error:
            logger.warning(f"读取 TgtoDrive 插件 STRM 目录失败：{error}")
            strm_dir = ""
        if strm_dir:
            with _strm_follow_lock:
                _strm_follow_cache.update({"ts": time.time(), "dir": strm_dir})
        else:
            # 本次没读到：保留上次成功的缓存（哪怕已过期），避免一次抖动清空角标
            with _strm_follow_lock:
                prev = _strm_follow_cache["dir"]
            if prev:
                logger.info("未读到 TgtoDrive STRM 目录，沿用上次缓存值")
                return prev
        return strm_dir

    def _find_strm_for(self, target: str) -> Optional[str]:
        """为媒体文件在 STRM 目录树里找同名 ``<文件名>.strm``（不论本地文件是否存在）。

        TgtoDrive 生成的 STRM 与媒体文件同名（``阿凡达.mkv`` → ``阿凡达.strm``），
        但目录结构镜像的是网盘而不是本地媒体库，所以先试同位置、再全树找一遍。
        找不到返回 None（调用方维持 404）。
        """
        strm_dir = self.effective_strm_dir()
        if not strm_dir or not os.path.isdir(strm_dir):
            return None
        want = os.path.splitext(os.path.basename(target))[0] + STRM_EXTENSION
        # 快路径：STRM 树与请求路径同构时一步命中
        direct = os.path.join(strm_dir, want)
        if os.path.isfile(direct):
            return direct
        # 慢路径：整树扫一遍（只在播放时触发一次，量级可接受）
        try:
            for dirpath, _, filenames in os.walk(strm_dir):
                if want in filenames:
                    return os.path.join(dirpath, want)
        except OSError as error:
            logger.warning(f"扫描 STRM 目录失败：{error}")
        return None

    @staticmethod
    def _read_strm_url(strm_file: str) -> Optional[str]:
        """读 STRM 文件内容，返回第一条 http(s) 直链；注释行跳过，读不了/不是 URL 返回 None。"""
        try:
            with open(strm_file, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.lower().startswith(("http://", "https://")):
                        return line
                    return None
        except OSError as error:
            logger.warning(f"媒体流播放读取 STRM 失败：{error}")
        return None

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
            # 本地文件缺失：若能在 STRM 目录里对上同名 .strm，说明是网盘直链模式，
            # 没有「同季同目录」可扫 —— 只回它自己，客户端按单片/单集处理
            if self._find_strm_for(target) or target.lower().endswith(STRM_EXTENSION):
                parsed = parse_season_episode(target)
                season_no, episode_no = parsed if parsed else (None, None)
                return {
                    "success": True,
                    "data": {"season": season_no, "episodes": [self._episode_item(target, episode_no)]},
                }
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

        roots = self.play_roots()

        def allowed(candidate: str) -> bool:
            """realpath 后仍在某个放行根内（挡符号链接逃逸），复用 /stream 同一套口径。"""
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

    def downloads(self) -> Any:
        """下载目录里的视频清单：递归扫所有 ``download_path``，返回磁盘上真实存在的文件。

        客户端拿它和「下载完成」历史（``torrent_name``）做文件名匹配：
        匹配得上 → 可播；匹配不上 → 文件多半已被删（下载器删种带文件、手动清理），
        客户端把 ▶ 换成「已删除」。所以这里**只回现存文件**，不做任何缓存 ——
        刚删完文件就该立刻反映出来。

        限制：深度 [MAX_SCAN_DEPTH] 层、总量 [MAX_SCAN_FILES] 条，超了就截断
        （正常部署远用不满；截断只影响极端目录树，不影响播放权限 —— /stream 仍按白名单校验）。
        """
        found: List[Dict[str, Any]] = []
        roots = self.download_roots()
        for root in roots:
            self._scan_videos(root, 0, found)
            if len(found) >= MAX_SCAN_FILES:
                break
        logger.debug(f"下载目录扫描：{len(roots)} 个根，{len(found)} 个视频文件")
        return {"success": True, "data": {"files": found[:MAX_SCAN_FILES]}}

    def _scan_videos(self, directory: str, depth: int, out: List[Dict[str, Any]]) -> None:
        """递归收集 [directory] 下的视频文件（限深度；条数超限即止）。"""
        if depth > MAX_SCAN_DEPTH or len(out) >= MAX_SCAN_FILES:
            return
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if len(out) >= MAX_SCAN_FILES:
                        return
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            self._scan_videos(entry.path, depth + 1, out)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        if os.path.splitext(entry.name)[1].lower() not in VIDEO_EXTENSIONS:
                            continue
                        out.append({
                            "path": os.path.realpath(entry.path),
                            "name": entry.name,
                            "size": entry.stat(follow_symlinks=False).st_size,
                            "mtime": int(entry.stat(follow_symlinks=False).st_mtime),
                        })
                    except OSError:
                        continue
        except OSError as error:
            logger.warning(f"扫描下载目录失败：{directory}：{error}")

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

    def download_roots(self) -> List[str]:
        """当前放行的下载根目录（实时读宿主「目录配置」里的 download_path，去重保序）。

        v1.2.0 起「下载完成」页签要直接播没整理的文件，下载目录与媒体库一并放行；
        读不到（配置未就绪 / 一个下载目录都没配）就返回空列表 —— 不搞兜底，
        下载目录猜错了放行反而危险。
        """
        roots: List[str] = []
        try:
            directories = self.systemconfig.get("Directories") or []
        except Exception as error:
            logger.warning(f"读取宿主目录配置失败：{error}")
            directories = []

        for directory in directories:
            if not isinstance(directory, dict):
                continue
            raw = (directory.get("download_path") or "").strip()
            if not raw:
                continue
            root = os.path.realpath(raw)
            if os.path.isdir(root) and root not in roots:
                roots.append(root)
        return roots

    def play_roots(self) -> List[str]:
        """/stream 与 /season 的放行目录 = 媒体库 + 下载目录（去重保序）。"""
        roots = self.media_roots()
        for root in self.download_roots():
            if root not in roots:
                roots.append(root)
        return roots

    def resolve(self, raw_path: str) -> Optional[str]:
        """把请求路径归一成可播放的绝对路径；不在白名单内返回 None。

        v1.3.0 起同时放行 ``.strm`` 文件（读出直链后跳转），并把生效的 STRM 目录
        加入白名单 —— STRM 树不一定挂在宿主「目录配置」的媒体库/下载路径下面。
        """
        raw = (raw_path or "").strip()
        if not raw or "\x00" in raw or not os.path.isabs(raw):
            return None
        # realpath 会消掉 ``..``、重复分隔符与符号链接，之后的前缀比较才有意义
        target = os.path.realpath(raw)
        ext = os.path.splitext(target)[1].lower()
        if ext != STRM_EXTENSION and ext not in VIDEO_EXTENSIONS:
            return None
        roots = self.play_roots()
        strm_dir = self.effective_strm_dir()
        if strm_dir:
            root = os.path.realpath(strm_dir)
            if root not in roots:
                roots.append(root)
        for root in roots:
            # 用 root + 分隔符 比较，挡住 /DSM/media2 被 /DSM/media 放行这种前缀串
            if target == root or target.startswith(root + os.sep):
                return target
        return None

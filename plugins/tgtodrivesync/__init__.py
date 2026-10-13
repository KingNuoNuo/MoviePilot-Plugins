"""
TgtoDrive STRM 同步插件

MoviePilot 整理入库（TransferComplete 事件）后，自动调用 TgtoDrive 的 OpenAPI
触发指定网盘的 STRM 同步，等待同步完成后刷新媒体服务器（Emby / 飞牛影视），
并发送通知。

TgtoDrive 侧依赖：
    POST {TGTO_URL}/api/strm-sync/run
    Header: Authorization: Bearer <OpenAPI Token>
    Body:   {"source": "123云盘"}

Emby 刷新侧：
    优先复用 MoviePilot 已配置的媒体服务器（settings.MEDIASERVER）；
    也可在插件中单独填写 Emby 地址与 API Key 以指向其它 Emby 实例。

飞牛影视（fnOS trimemedia）刷新侧：
    走飞牛影视网页接口，需要 authx 签名（依赖 secret 与 api_key）。
    该接口非官方开放 API，为抓包所得，飞牛版本升级后可能失效。
"""
import hashlib
import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.modules.emby import Emby
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType
from app.log import logger

# TgtoDrive 支持的 STRM 同步来源（取自前端 ORGANIZE_SOURCE_ORDER）
STRM_SOURCES = [
    "123云盘",
    "123云盘音乐",
    "光鸭云盘",
    "光鸭云盘音乐",
    "115网盘",
    "115网盘音乐",
    "115网盘分享",
]


class FnvClient:
    """飞牛影视（fnOS trimemedia）客户端。

    使用飞牛影视网页接口，每个请求都需携带 authx 签名：
        authx = nonce=<6位随机>&timestamp=<毫秒>&sign=<md5>
        sign  = md5("_".join([secret, path, nonce, timestamp, body_hash, api_key]))
        body_hash：GET 且有 params 时，对排序后的 params urlencode 再 md5；
                   其余情况对请求体 JSON（键排序、紧凑分隔符）再 md5。
    """

    API_LOGIN = "/v/api/v1/login"
    API_MDB_LIST = "/v/api/v1/mdb/list"
    API_MDB_SCAN = "/v/api/v1/mdb/scan/{}"
    API_MDB_SCAN_ALL = "/v/api/v1/mdb/scanall"
    API_TASK_STOP = "/v/api/v1/task/stop"
    APP_NAME = "trimemedia-web"

    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
        secret: str = "",
        api_key: str = "",
        token: str = "",
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.username = username
        self.password = password
        self.secret = secret
        self.api_key = api_key
        self.token = token or ""
        self.session = requests.Session()

    @staticmethod
    def _md5(text: str) -> str:
        """计算小写 MD5。"""
        return hashlib.md5(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _serialize(data: Any) -> str:
        """把请求体序列化为紧凑 JSON 字符串（键排序）。"""
        if isinstance(data, dict):
            return json.dumps(data, sort_keys=True, separators=(",", ":"))
        if isinstance(data, str):
            return data
        return ""

    def _authx(self, method: str, path: str, params: Optional[dict] = None,
               data: Any = None) -> str:
        """生成 authx 签名字符串。"""
        nonce = str(random.randint(100000, 999999))
        timestamp = str(int(time.time() * 1000))
        if method.lower() == "get" and params:
            raw = urlencode(sorted(params.items()))
        else:
            raw = self._serialize(data)
        body_hash = self._md5(raw)
        sign = self._md5(
            "_".join([self.secret, path, nonce, timestamp, body_hash, self.api_key])
        )
        return f"nonce={nonce}&timestamp={timestamp}&sign={sign}"

    def _request(self, method: str, path: str, params: Optional[dict] = None,
                 data: Any = None) -> dict:
        """发送带签名的请求并返回 JSON。"""
        url = f"{self.base_url}{path}"
        headers = {
            "Content-Type": "application/json",
            "authx": self._authx(method, path, params, data),
        }
        if self.token:
            headers["Authorization"] = self.token
        resp = self.session.request(
            method,
            url,
            headers=headers,
            params=params,
            json=data if data is not None else {},
            timeout=(5, 30),
        )
        resp.raise_for_status()
        return resp.json()

    def login(self) -> bool:
        """登录飞牛影视并保存 token。"""
        payload = {
            "username": self.username,
            "password": self.password,
            "app_name": self.APP_NAME,
        }
        result = self._request("post", self.API_LOGIN, data=payload)
        token = (result or {}).get("data", {}).get("token")
        if token:
            self.token = token
            return True
        return False

    def list_libraries(self) -> List[dict]:
        """获取媒体库列表。"""
        result = self._request("get", self.API_MDB_LIST)
        return (result or {}).get("data") or []

    def find_library_id(self, name: str) -> Optional[str]:
        """按名称查找媒体库 guid。"""
        for library in self.list_libraries():
            if library.get("name") == name:
                return library.get("guid")
        return None

    def scan_all(self) -> Optional[int]:
        """触发扫描全部媒体库，返回业务 code。"""
        result = self._request("post", self.API_MDB_SCAN_ALL, data={})
        return (result or {}).get("code")

    def scan_library(self, library_id: str) -> Optional[int]:
        """触发扫描指定媒体库，返回业务 code。"""
        result = self._request("post", self.API_MDB_SCAN.format(library_id), data={})
        return (result or {}).get("code")

    def stop_task(self, library_id: str) -> Optional[int]:
        """停止指定媒体库的刮削任务。"""
        result = self._request(
            "post",
            self.API_TASK_STOP,
            data={"guid": library_id, "type": "TaskItemScrap"},
        )
        return (result or {}).get("code")


class TgtoDriveSync(_PluginBase):
    """TgtoDrive STRM 同步插件。"""

    # 插件名称
    plugin_name = "TgtoDrive STRM 同步"
    # 插件描述
    plugin_desc = "整理入库后自动触发 TgtoDrive 生成网盘 STRM，完成后刷新 Emby / 飞牛影视媒体库。"
    # 插件图标（使用官方图标库中的通用刷新图标）
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/refresh2.png"
    # 插件版本
    plugin_version = "1.1.0"
    # 插件作者
    plugin_author = "KingNuoNuo"
    # 作者主页
    author_url = "https://github.com/KingNuoNuo"
    # 插件配置项ID前缀
    plugin_config_prefix = "tgtodrivesync_"
    # 加载顺序
    plugin_order = 15
    # 可使用的用户级别
    auth_level = 1

    # ---- 基础配置 ----
    _enabled: bool = False
    _tgtodrive_url: str = ""
    _openapi_token: str = ""
    _source: str = "123云盘"
    _wait_seconds: int = 60
    _debounce_seconds: int = 30
    _notify: bool = True

    # ---- Emby ----
    _refresh_mode: str = "all"
    _emby_url: str = ""
    _emby_api_key: str = ""

    # ---- 飞牛影视 ----
    _fn_enabled: bool = False
    _fn_url: str = ""
    _fn_username: str = ""
    _fn_password: str = ""
    _fn_secret: str = ""
    _fn_api_key: str = ""
    _fn_token: str = ""
    _fn_scan_mode: str = "all"
    _fn_library_name: str = ""

    # ---- 运行时状态 ----
    _executor: Optional[ThreadPoolExecutor] = None
    _lock = threading.Lock()
    _timer: Optional[threading.Timer] = None
    _running: bool = False
    _pending_info: dict = {}

    def init_plugin(self, config: dict = None):
        """加载配置并初始化运行状态。"""
        self.stop_service()

        self._enabled = False
        self._tgtodrive_url = ""
        self._openapi_token = ""
        self._source = "123云盘"
        self._wait_seconds = 60
        self._debounce_seconds = 30
        self._notify = True
        self._refresh_mode = "all"
        self._emby_url = ""
        self._emby_api_key = ""
        self._fn_enabled = False
        self._fn_url = ""
        self._fn_username = ""
        self._fn_password = ""
        self._fn_secret = ""
        self._fn_api_key = ""
        self._fn_token = ""
        self._fn_scan_mode = "all"
        self._fn_library_name = ""

        if config:
            self._enabled = bool(config.get("enabled"))
            self._tgtodrive_url = str(config.get("tgtodrive_url") or "").strip()
            self._openapi_token = str(config.get("openapi_token") or "").strip()
            self._source = str(config.get("source") or "123云盘").strip()
            self._wait_seconds = self._safe_int(config.get("wait_seconds"), 60)
            self._debounce_seconds = self._safe_int(config.get("debounce_seconds"), 30)
            self._notify = bool(config.get("notify", True))
            self._refresh_mode = str(config.get("refresh_mode") or "all").strip()
            self._emby_url = str(config.get("emby_url") or "").strip().rstrip("/")
            self._emby_api_key = str(config.get("emby_api_key") or "").strip()
            self._fn_enabled = bool(config.get("fn_enabled"))
            self._fn_url = str(config.get("fn_url") or "").strip().rstrip("/")
            self._fn_username = str(config.get("fn_username") or "").strip()
            self._fn_password = str(config.get("fn_password") or "").strip()
            self._fn_secret = str(config.get("fn_secret") or "").strip()
            self._fn_api_key = str(config.get("fn_api_key") or "").strip()
            self._fn_token = str(config.get("fn_token") or "").strip()
            self._fn_scan_mode = str(config.get("fn_scan_mode") or "all").strip()
            self._fn_library_name = str(config.get("fn_library_name") or "").strip()

        if self._source not in STRM_SOURCES:
            self._source = "123云盘"

        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tgtodrive_sync")

        if self._enabled:
            logger.info(
                f"TgtoDrive STRM 同步插件已启用：地址={self._tgtodrive_url or '(未配置)'}，"
                f"来源={self._source}，等待={self._wait_seconds}s，"
                f"Emby刷新={self._refresh_mode}，飞牛刷新={'启用' if self._fn_enabled else '关闭'}"
            )
            if not self._tgtodrive_url or not self._openapi_token:
                logger.warning("TgtoDrive STRM 同步插件：TgtoDrive 地址或 OpenAPI Token 未配置，触发时将失败")

    @staticmethod
    def _safe_int(value: Any, default: int) -> int:
        """安全地把配置值转换为非负整数。"""
        try:
            result = int(float(value))
            return result if result >= 0 else default
        except (TypeError, ValueError):
            return default

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册远程命令。"""
        return [
            {
                "cmd": "/tgtodrive_sync",
                "event": EventType.PluginAction,
                "desc": "立即触发网盘STRM同步并刷新媒体库",
                "category": "命令菜单",
                "data": {"action": "tgtodrive_sync"},
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """注册插件 API。"""
        return [
            {
                "path": "/trigger",
                "endpoint": self.api_trigger,
                "methods": ["GET"],
                "summary": "手动触发STRM同步并刷新媒体库",
                "description": "立即调用 TgtoDrive 生成网盘 STRM，等待后刷新 Emby / 飞牛影视媒体库。",
            },
            {
                "path": "/test",
                "endpoint": self.api_test,
                "methods": ["GET"],
                "summary": "测试TgtoDrive连接",
                "description": "使用当前配置的地址与 Token 访问 TgtoDrive OpenAPI 规范，验证连通性。",
            },
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """拼装插件配置页面。"""
        source_items = [{"title": s, "value": s} for s in STRM_SOURCES]
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {"component": "VSwitch",
                                     "props": {"model": "enabled", "label": "启用插件"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {"component": "VSwitch",
                                     "props": {"model": "notify", "label": "发送通知"}}
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {"component": "VTextField",
                                     "props": {"model": "tgtodrive_url",
                                               "label": "TgtoDrive 地址",
                                               "placeholder": "http://10.0.0.4:12366"}}
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
                                    {"component": "VTextField",
                                     "props": {"model": "openapi_token",
                                               "label": "TgtoDrive OpenAPI Token",
                                               "placeholder": "在 TgtoDrive「实用工具 → OpenAPI」创建并复制",
                                               "type": "password"}}
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {"component": "VSelect",
                                     "props": {"model": "source",
                                               "label": "STRM 同步来源",
                                               "items": source_items}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {"component": "VSelect",
                                     "props": {"model": "refresh_mode",
                                               "label": "Emby 刷新方式",
                                               "items": [
                                                   {"title": "刷新全部媒体库", "value": "all"},
                                                   {"title": "按入库条目刷新", "value": "item"},
                                               ]}}
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {"component": "VTextField",
                                     "props": {"model": "wait_seconds",
                                               "label": "触发后等待秒数（等待 STRM 生成完成）",
                                               "placeholder": "60"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {"component": "VTextField",
                                     "props": {"model": "debounce_seconds",
                                               "label": "防抖秒数（合并短时间内的多次入库）",
                                               "placeholder": "30"}}
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {"component": "VTextField",
                                     "props": {"model": "emby_url",
                                               "label": "Emby 地址（可选，留空用 MoviePilot 已配置的媒体服务器）",
                                               "placeholder": "http://10.0.0.4:8096"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {"component": "VTextField",
                                     "props": {"model": "emby_api_key",
                                               "label": "Emby API Key（可选）",
                                               "type": "password"}}
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{"component": "VDivider", "props": {}}],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {"component": "VSwitch",
                                     "props": {"model": "fn_enabled", "label": "启用飞牛影视刷新"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {"component": "VSelect",
                                     "props": {"model": "fn_scan_mode",
                                               "label": "飞牛扫描范围",
                                               "items": [
                                                   {"title": "扫描全部媒体库", "value": "all"},
                                                   {"title": "仅扫描指定媒体库", "value": "library"},
                                               ]}}
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {"component": "VTextField",
                                     "props": {"model": "fn_url",
                                               "label": "飞牛影视地址",
                                               "placeholder": "http://10.0.0.4:5666"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {"component": "VTextField",
                                     "props": {"model": "fn_library_name",
                                               "label": "指定媒体库名称（扫描范围选“仅指定”时填写）",
                                               "placeholder": "电影库"}}
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {"component": "VTextField",
                                     "props": {"model": "fn_username",
                                               "label": "飞牛用户名"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {"component": "VTextField",
                                     "props": {"model": "fn_password",
                                               "label": "飞牛密码",
                                               "type": "password"}}
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {"component": "VTextField",
                                     "props": {"model": "fn_secret",
                                               "label": "飞牛 secret（authx 签名用）",
                                               "type": "password"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {"component": "VTextField",
                                     "props": {"model": "fn_api_key",
                                               "label": "飞牛 api_key（authx 签名用）",
                                               "type": "password"}}
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {"component": "VTextField",
                                     "props": {"model": "fn_token",
                                               "label": "飞牛 token（可选，填了免登录；开了两步验证时用这个）",
                                               "type": "password"}}
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
                                    {"component": "VAlert",
                                     "props": {
                                         "type": "warning",
                                         "variant": "tonal",
                                         "text": (
                                             "飞牛影视没有官方开放 API，插件走的是其网页接口（需 authx 签名，"
                                             "依赖 secret 与 api_key），飞牛升级后可能失效。secret / api_key 获取："
                                             "浏览器登录飞牛影视后台 → 查看网页源代码 → 打开其中的 .js 文件 → "
                                             "搜索 ,s,l,c,o,e].join(\"_\") 左边引号内为 secret；"
                                             "搜索 ${Ld}/sys/progressThumb 右边为 api_key。"
                                         ),
                                     }}
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
                                    {"component": "VAlert",
                                     "props": {
                                         "type": "info",
                                         "variant": "tonal",
                                         "text": (
                                             "工作流程：MoviePilot 整理入库 → 触发 TgtoDrive 生成 STRM → "
                                             "等待设定秒数后刷新 Emby / 飞牛影视。TgtoDrive 的 STRM 同步为异步任务，"
                                             "当前无完成回调接口，故以「等待秒数」估算生成时间，请按网盘规模调整。"
                                         ),
                                     }}
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": True,
            "tgtodrive_url": "",
            "openapi_token": "",
            "source": "123云盘",
            "refresh_mode": "all",
            "wait_seconds": 60,
            "debounce_seconds": 30,
            "emby_url": "",
            "emby_api_key": "",
            "fn_enabled": False,
            "fn_scan_mode": "all",
            "fn_url": "",
            "fn_library_name": "",
            "fn_username": "",
            "fn_password": "",
            "fn_secret": "",
            "fn_api_key": "",
            "fn_token": "",
        }

    def get_page(self) -> List[dict]:
        """展示最近的执行历史。"""
        history: List[dict] = self.get_data("history") or []
        if not history:
            return [
                {
                    "component": "VAlert",
                    "props": {"type": "info", "text": "暂无执行记录。"},
                }
            ]
        rows = []
        for item in history[-20:][::-1]:
            line = (
                f"[{item.get('time')}] 来源={item.get('source')} "
                f"触发={'成功' if item.get('trigger_ok') else '失败'} "
                f"Emby={'成功' if item.get('refresh_ok') else '未执行/失败'}"
            )
            if "fn_ok" in item:
                line += f" 飞牛={'成功' if item.get('fn_ok') else '未执行/失败'}"
            line += f"\n{item.get('message', '')}"
            rows.append({
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "tonal", "density": "compact"},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "props": {},
                                        "text": line,
                                    }
                                ],
                            }
                        ],
                    }
                ],
            })
        return rows

    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event):
        """整理入库完成事件处理：防抖后触发同步流程。"""
        if not self._enabled:
            return
        event_data = event.event_data or {}
        transferinfo = event_data.get("transferinfo")
        mediainfo = event_data.get("mediainfo")
        with self._lock:
            self._pending_info = {
                "title": getattr(mediainfo, "title", "") if mediainfo else "",
                "year": getattr(mediainfo, "year", "") if mediainfo else "",
                "target_path": getattr(transferinfo, "target_path", "") if transferinfo else "",
                "tmdbid": getattr(mediainfo, "tmdb_id", "") if mediainfo else "",
                "type": getattr(mediainfo, "type", "") if mediainfo else "",
            }
            if self._timer:
                self._timer.cancel()
            delay = max(self._debounce_seconds, 1)
            self._timer = threading.Timer(delay, self._dispatch)
            self._timer.daemon = True
            self._timer.start()
        logger.info(f"收到整理完成事件，{max(self._debounce_seconds, 1)} 秒后触发 STRM 同步（防抖合并）")

    @eventmanager.register(EventType.PluginAction)
    def on_plugin_action(self, event: Event):
        """远程命令响应。"""
        if not event or not event.event_data:
            return
        action = event.event_data.get("action")
        if action != "tgtodrive_sync":
            return
        if not self._enabled:
            self.post_message(
                mtype=NotificationType.Plugin,
                title="【TgtoDrive STRM 同步】",
                text="插件未启用，命令已忽略。",
            )
            return
        with self._lock:
            self._pending_info = {"title": "手动触发"}
        self._dispatch()

    def _dispatch(self):
        """把同步流程投递到后台线程执行，避免阻塞事件总线。"""
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tgtodrive_sync")
        self._executor.submit(self._run_pipeline)

    def _run_pipeline(self):
        """执行完整流程：触发 STRM 同步 → 等待 → 刷新媒体服务器 → 通知。"""
        with self._lock:
            info = getattr(self, "_pending_info", {}) or {}
            if self._running:
                logger.info("已有 STRM 同步任务在执行，本次触发跳过")
                return
            self._running = True
        try:
            record = {
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "source": self._source,
                "trigger_ok": False,
                "refresh_ok": False,
                "message": "",
            }

            # 1. 触发 TgtoDrive 生成 STRM
            trigger_ok, trigger_msg = self._trigger_strm_sync()
            record["trigger_ok"] = trigger_ok
            record["message"] = f"STRM同步：{trigger_msg}"
            logger.info(f"TgtoDrive STRM 同步触发结果：{trigger_ok} - {trigger_msg}")

            if trigger_ok:
                # 2. 等待 STRM 生成
                if self._wait_seconds > 0:
                    logger.info(f"等待 {self._wait_seconds} 秒以便 TgtoDrive 完成 STRM 生成...")
                    time.sleep(self._wait_seconds)

                # 3. 刷新 Emby
                refresh_ok, refresh_msg = self._refresh_emby(info)
                record["refresh_ok"] = refresh_ok
                record["message"] += f"\nEmby：{refresh_msg}"
                logger.info(f"Emby 刷新结果：{refresh_ok} - {refresh_msg}")

                # 4. 刷新飞牛影视
                if self._fn_enabled:
                    fn_ok, fn_msg = self._refresh_fnos()
                    record["fn_ok"] = fn_ok
                    record["message"] += f"\n飞牛影视：{fn_msg}"
                    logger.info(f"飞牛影视刷新结果：{fn_ok} - {fn_msg}")

            # 5. 发送通知
            if self._notify:
                self._send_notice(record, info)

            self._save_history(record)
        except Exception as err:  # noqa: BLE001
            logger.error(f"TgtoDrive STRM 同步流程异常：{err}")
        finally:
            with self._lock:
                self._running = False

    def _trigger_strm_sync(self) -> Tuple[bool, str]:
        """调用 TgtoDrive OpenAPI 触发 STRM 同步。"""
        if not self._tgtodrive_url or not self._openapi_token:
            return False, "TgtoDrive 地址或 OpenAPI Token 未配置"
        url = f"{self._tgtodrive_url.rstrip('/')}/api/strm-sync/run"
        headers = {
            "Authorization": f"Bearer {self._openapi_token}",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(
                url,
                json={"source": self._source},
                headers=headers,
                timeout=(5, 30),
                allow_redirects=False,
            )
            data = {}
            try:
                data = resp.json()
            except ValueError:
                data = {}
            if resp.ok and data.get("success"):
                return True, str(data.get("message") or "已开始同步")
            err = data.get("error") or data.get("message") or f"HTTP {resp.status_code}"
            return False, str(err)
        except requests.RequestException as err:
            return False, f"请求异常：{err}"

    def _refresh_emby(self, info: dict) -> Tuple[bool, str]:
        """刷新 Emby 媒体库：优先插件自定义 Emby，否则用 MoviePilot 已配置的媒体服务器。"""
        # 方式一：插件中单独配置了 Emby 地址与 API Key，直接调 Emby REST
        if self._emby_url and self._emby_api_key:
            try:
                url = f"{self._emby_url}/emby/Library/Refresh"
                resp = requests.post(
                    url,
                    params={"api_key": self._emby_api_key, "Recursive": "true"},
                    timeout=(5, 60),
                )
                if resp.status_code in (200, 204):
                    return True, f"已请求刷新 {self._emby_url}"
                return False, f"Emby 返回 HTTP {resp.status_code}"
            except requests.RequestException as err:
                return False, f"Emby 请求异常：{err}"

        # 方式二：复用 MoviePilot 已配置的媒体服务器
        mediaserver = settings.MEDIASERVER or []
        if "emby" not in mediaserver:
            return False, "MoviePilot 未配置 Emby，且插件未单独填写 Emby 地址"
        try:
            emby = Emby()
            if self._refresh_mode == "item" and info.get("title"):
                from app.schemas import RefreshMediaItem

                items = [
                    RefreshMediaItem(
                        title=info.get("title"),
                        year=info.get("year"),
                        type=info.get("type"),
                        category=None,
                        target_path=info.get("target_path"),
                    )
                ]
                emby.refresh_library_by_items(items)
                return True, f"已按条目刷新：{info.get('title')}"
            emby.refresh_root_library()
            return True, "已请求刷新全部媒体库"
        except Exception as err:  # noqa: BLE001
            return False, f"调用 MoviePilot Emby 模块异常：{err}"

    def _refresh_fnos(self) -> Tuple[bool, str]:
        """刷新飞牛影视媒体库。"""
        if not self._fn_url:
            return False, "飞牛影视地址未配置"
        if not self._fn_secret or not self._fn_api_key:
            return False, "飞牛 secret / api_key 未配置（authx 签名必需）"

        client = FnvClient(
            base_url=self._fn_url,
            username=self._fn_username,
            password=self._fn_password,
            secret=self._fn_secret,
            api_key=self._fn_api_key,
            token=self._fn_token,
        )
        try:
            # 没有现成 token 时用用户名密码登录
            if not client.token:
                if not (self._fn_username and self._fn_password):
                    return False, "未填飞牛 token，也未填用户名/密码，无法认证"
                if not client.login():
                    return False, "飞牛登录失败，请检查用户名/密码"

            if self._fn_scan_mode == "library":
                if not self._fn_library_name:
                    return False, "扫描范围选了「仅指定媒体库」但未填写媒体库名称"
                library_id = client.find_library_id(self._fn_library_name)
                if not library_id:
                    return False, f"未找到媒体库「{self._fn_library_name}」"
                code = client.scan_library(library_id)
                label = f"媒体库「{self._fn_library_name}」"
            else:
                code = client.scan_all()
                label = "全部媒体库"

            if code == 0:
                return True, f"已触发扫描{label}"
            if code == -14:
                return True, f"{label}已有扫描任务在执行"
            return False, f"飞牛返回 code={code}"
        except requests.RequestException as err:
            return False, f"飞牛请求异常：{err}"
        except Exception as err:  # noqa: BLE001
            return False, f"飞牛刷新异常：{err}"

    def _send_notice(self, record: dict, info: dict):
        """发送结果通知。"""
        title = info.get("title") or ""
        title_part = f"（{title}）" if title else ""
        text = (
            f"来源：{record.get('source')}\n"
            f"STRM 触发：{'成功' if record.get('trigger_ok') else '失败'}\n"
            f"Emby 刷新：{'成功' if record.get('refresh_ok') else '未执行/失败'}\n"
        )
        if "fn_ok" in record:
            text += f"飞牛影视刷新：{'成功' if record.get('fn_ok') else '未执行/失败'}\n"
        text += f"{record.get('message', '')}"
        try:
            self.post_message(
                mtype=NotificationType.Plugin,
                title=f"【TgtoDrive STRM 同步】{title_part}",
                text=text,
            )
        except Exception as err:  # noqa: BLE001
            logger.error(f"发送通知失败：{err}")

    def _save_history(self, record: dict):
        """保存执行历史（最多保留 100 条）。"""
        history: List[dict] = self.get_data("history") or []
        history.append(record)
        self.save_data("history", history[-100:])

    # ---- 插件 API 实现 ----
    def api_trigger(self, apikey: str = "") -> Dict[str, Any]:
        """API：手动触发同步流程。"""
        if apikey != settings.API_TOKEN:
            return {"success": False, "message": "API Token 校验失败"}
        if not self._enabled:
            return {"success": False, "message": "插件未启用"}
        with self._lock:
            self._pending_info = {"title": "API 手动触发"}
        self._dispatch()
        return {"success": True, "message": "已提交 STRM 同步任务"}

    def api_test(self, apikey: str = "") -> Dict[str, Any]:
        """API：测试 TgtoDrive 连接。"""
        if apikey != settings.API_TOKEN:
            return {"success": False, "message": "API Token 校验失败"}
        if not self._tgtodrive_url or not self._openapi_token:
            return {"success": False, "message": "TgtoDrive 地址或 OpenAPI Token 未配置"}
        url = f"{self._tgtodrive_url.rstrip('/')}/api/openapi/spec"
        try:
            resp = requests.get(
                url,
                headers={"Authorization": f"Bearer {self._openapi_token}"},
                timeout=(5, 20),
                allow_redirects=False,
            )
            if resp.status_code == 200:
                return {"success": True, "message": "连接成功"}
            return {"success": False, "message": f"HTTP {resp.status_code}"}
        except requests.RequestException as err:
            return {"success": False, "message": f"请求异常：{err}"}

    def stop_service(self):
        """停止插件：取消定时器并关闭线程池。"""
        try:
            if self._timer:
                self._timer.cancel()
                self._timer = None
        except Exception as err:  # noqa: BLE001
            logger.warning(f"取消定时器失败：{err}")
        try:
            if self._executor:
                self._executor.shutdown(wait=False)
                self._executor = None
        except Exception as err:  # noqa: BLE001
            logger.warning(f"关闭线程池失败：{err}")

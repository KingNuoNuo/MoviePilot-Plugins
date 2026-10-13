"""
TgtoDrive STRM 同步插件

MoviePilot 整理入库（TransferComplete 事件）后，自动调用 TgtoDrive 的 OpenAPI
触发指定网盘的 STRM 同步，等待同步完成后刷新 Emby 媒体库，并发送通知。

TgtoDrive 侧依赖：
    POST {TGTO_URL}/api/strm-sync/run
    Header: Authorization: Bearer <OpenAPI Token>
    Body:   {"source": "123云盘"}

Emby 刷新侧：
    优先复用 MoviePilot 已配置的媒体服务器（settings.MEDIASERVER）；
    也可在插件中单独填写 Emby 地址与 API Key 以指向其它 Emby 实例。
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

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


class TgtoDriveSync(_PluginBase):
    """TgtoDrive STRM 同步插件。"""

    # 插件名称
    plugin_name = "TgtoDrive STRM 同步"
    # 插件描述
    plugin_desc = "整理入库后自动触发 TgtoDrive 生成网盘 STRM，完成后刷新 Emby 媒体库。"
    # 插件图标（使用官方图标库中的通用刷新图标）
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/refresh2.png"
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "靖哥哥"
    # 作者主页
    author_url = ""
    # 插件配置项ID前缀
    plugin_config_prefix = "tgtodrivesync_"
    # 加载顺序
    plugin_order = 15
    # 可使用的用户级别
    auth_level = 1

    # ---- 配置项 ----
    _enabled: bool = False
    _tgtodrive_url: str = ""
    _openapi_token: str = ""
    _source: str = "123云盘"
    _wait_seconds: int = 60
    _refresh_mode: str = "all"
    _debounce_seconds: int = 30
    _notify: bool = True
    _emby_url: str = ""
    _emby_api_key: str = ""

    # ---- 运行时状态 ----
    _executor: Optional[ThreadPoolExecutor] = None
    _lock = threading.Lock()
    _timer: Optional[threading.Timer] = None
    _running: bool = False
    _pending_info: dict = {}

    def init_plugin(self, config: dict = None):
        """加载配置并初始化运行状态。"""
        # 停止既有任务
        self.stop_service()

        self._enabled = False
        self._tgtodrive_url = ""
        self._openapi_token = ""
        self._source = "123云盘"
        self._wait_seconds = 60
        self._refresh_mode = "all"
        self._debounce_seconds = 30
        self._notify = True
        self._emby_url = ""
        self._emby_api_key = ""

        if config:
            self._enabled = bool(config.get("enabled"))
            self._tgtodrive_url = str(config.get("tgtodrive_url") or "").strip()
            self._openapi_token = str(config.get("openapi_token") or "").strip()
            self._source = str(config.get("source") or "123云盘").strip()
            self._wait_seconds = self._safe_int(config.get("wait_seconds"), 60)
            self._refresh_mode = str(config.get("refresh_mode") or "all").strip()
            self._debounce_seconds = self._safe_int(config.get("debounce_seconds"), 30)
            self._notify = bool(config.get("notify", True))
            self._emby_url = str(config.get("emby_url") or "").strip().rstrip("/")
            self._emby_api_key = str(config.get("emby_api_key") or "").strip()

        if self._source not in STRM_SOURCES:
            self._source = "123云盘"

        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tgtodrive_sync")

        if self._enabled:
            logger.info(
                f"TgtoDrive STRM 同步插件已启用：地址={self._tgtodrive_url or '(未配置)'}，"
                f"来源={self._source}，等待={self._wait_seconds}s，刷新方式={self._refresh_mode}"
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
                "description": "立即调用 TgtoDrive 生成网盘 STRM，等待后刷新 Emby 媒体库。",
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
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "notify", "label": "发送通知"},
                                    }
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
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "tgtodrive_url",
                                            "label": "TgtoDrive 地址",
                                            "placeholder": "http://10.0.0.4:12366",
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
                                            "model": "openapi_token",
                                            "label": "TgtoDrive OpenAPI Token",
                                            "placeholder": "在 TgtoDrive「实用工具 → OpenAPI」创建并复制",
                                            "type": "password",
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
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "source",
                                            "label": "STRM 同步来源",
                                            "items": source_items,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "refresh_mode",
                                            "label": "Emby 刷新方式",
                                            "items": [
                                                {"title": "刷新全部媒体库", "value": "all"},
                                                {"title": "按入库条目刷新", "value": "item"},
                                            ],
                                        },
                                    }
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
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "wait_seconds",
                                            "label": "触发后等待秒数（等待 STRM 生成完成）",
                                            "placeholder": "60",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "debounce_seconds",
                                            "label": "防抖秒数（合并短时间内的多次入库）",
                                            "placeholder": "30",
                                        },
                                    }
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
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "emby_url",
                                            "label": "Emby 地址（可选，留空用 MoviePilot 已配置的媒体服务器）",
                                            "placeholder": "http://10.0.0.4:8096",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "emby_api_key",
                                            "label": "Emby API Key（可选）",
                                            "type": "password",
                                        },
                                    }
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
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": (
                                                "工作流程：MoviePilot 整理入库 → 触发 TgtoDrive 生成 STRM → "
                                                "等待设定秒数后刷新 Emby。TgtoDrive 的 STRM 同步为异步任务，"
                                                "当前无完成回调接口，故以「等待秒数」估算生成时间，请按网盘规模调整。"
                                            ),
                                        },
                                    }
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
        rows = [
            {
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
                                        "text": (
                                            f"[{item.get('time')}] 来源={item.get('source')} "
                                            f"触发={'成功' if item.get('trigger_ok') else '失败'} "
                                            f"刷新={'成功' if item.get('refresh_ok') else '未执行/失败'}\n"
                                            f"{item.get('message', '')}"
                                        ),
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
            for item in history[-20:][::-1]
        ]
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
        """执行完整流程：触发 STRM 同步 → 等待 → 刷新 Emby → 通知。"""
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
                record["message"] += f"\n媒体库刷新：{refresh_msg}"
                logger.info(f"Emby 刷新结果：{refresh_ok} - {refresh_msg}")

            # 4. 发送通知
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

    def _send_notice(self, record: dict, info: dict):
        """发送结果通知。"""
        title = info.get("title") or ""
        title_part = f"（{title}）" if title else ""
        text = (
            f"来源：{record.get('source')}\n"
            f"STRM 触发：{'成功' if record.get('trigger_ok') else '失败'}\n"
            f"媒体库刷新：{'成功' if record.get('refresh_ok') else '未执行/失败'}\n"
            f"{record.get('message', '')}"
        )
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

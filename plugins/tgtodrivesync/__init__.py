"""
TgtoDrive STRM 同步插件

MoviePilot 整理入库（TransferComplete 事件）后，自动调用 TgtoDrive 的 OpenAPI
触发指定网盘的 STRM 同步，等待同步完成后刷新媒体服务器，并发送通知。

TgtoDrive 侧依赖：
    POST {TGTO_URL}/api/strm-sync/run
    Header: Authorization: Bearer <OpenAPI Token>
    Body:   {"source": "123云盘"}

媒体库刷新侧：
    不针对具体类型硬编码，而是通过 MoviePilot 的 MediaServerHelper 取出
    「设定 → 媒体服务器」里已配置并启用的全部媒体服务器实例，逐个调用其刷新能力。
    因此自动覆盖 Emby / Jellyfin / Plex / 飞牛影视 / 绿联影视 / Navidrome / Vyo /
    极影视以及自定义等所有类型，宿主后续新增类型也无需改插件。
    - 整库刷新：调用实例的 refresh_root_library()
    - 按条目刷新：调用实例的 refresh_library_by_items()，不支持的自动回退整库
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

import requests

from app.core.config import settings
from app.core.event import eventmanager, Event
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


def _load_mediaserver_helper():
    """按 MoviePilot 版本加载 MediaServerHelper（v2 在 app.helper，v3 在 app.application）。"""
    try:
        from app.helper.mediaserver import MediaServerHelper  # MoviePilot v2
        return MediaServerHelper
    except ImportError:
        pass
    try:
        from app.application.mediaserver import MediaServerHelper  # MoviePilot v3
        return MediaServerHelper
    except ImportError:
        return None


def _service_type_name(service_info: Any) -> str:
    """取媒体服务器的类型名（兼容字符串与枚举两种 type）。"""
    service_type = getattr(service_info, "type", None)
    if service_type is None:
        return "未知类型"
    return str(getattr(service_type, "value", service_type))


class TgtoDriveSync(_PluginBase):
    """TgtoDrive STRM 同步插件。"""

    # 插件名称
    plugin_name = "TgtoDrive STRM 同步"
    # 插件描述
    plugin_desc = "整理入库后自动触发 TgtoDrive 生成网盘 STRM，完成后刷新全部已配置的媒体服务器。"
    # 插件图标（使用官方图标库中的通用刷新图标）
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/refresh2.png"
    # 插件版本
    plugin_version = "1.4.0"
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

    # ---- 媒体库刷新 ----
    _refresh_mode: str = "all"
    _server_names: str = ""

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
        self._server_names = ""

        if config:
            self._enabled = bool(config.get("enabled"))
            self._tgtodrive_url = str(config.get("tgtodrive_url") or "").strip()
            self._openapi_token = str(config.get("openapi_token") or "").strip()
            self._source = str(config.get("source") or "123云盘").strip()
            self._wait_seconds = self._safe_int(config.get("wait_seconds"), 60)
            self._debounce_seconds = self._safe_int(config.get("debounce_seconds"), 30)
            self._notify = bool(config.get("notify", True))
            self._refresh_mode = str(config.get("refresh_mode") or "all").strip()
            self._server_names = str(config.get("server_names") or "").strip()

        if self._source not in STRM_SOURCES:
            self._source = "123云盘"
        if self._refresh_mode not in ("all", "item"):
            self._refresh_mode = "all"

        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tgtodrive_sync")

        if self._enabled:
            logger.info(
                f"TgtoDrive STRM 同步插件已启用：地址={self._tgtodrive_url or '(未配置)'}，"
                f"来源={self._source}，等待={self._wait_seconds}s，"
                f"刷新方式={self._refresh_mode}，"
                f"刷新范围={self._server_names or '全部媒体服务器'}"
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
                "description": "立即调用 TgtoDrive 生成网盘 STRM，等待后刷新全部已配置的媒体服务器。",
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
                                               "label": "媒体库刷新方式",
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
                                "props": {"cols": 12},
                                "content": [
                                    {"component": "VTextField",
                                     "props": {"model": "server_names",
                                               "label": "只刷新指定媒体服务器（可选，多个用英文逗号分隔，留空=全部）",
                                               "placeholder": "留空则刷新全部；例如：Emby,飞牛影视"}}
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
                                             "刷新范围：MoviePilot「设定 → 媒体服务器」中已配置并启用的全部服务器，"
                                             "自动覆盖 Emby / Jellyfin / Plex / 飞牛影视 / 绿联影视 / Navidrome / "
                                             "Vyo / 极影视以及自定义等所有类型，无需逐个配置。"
                                             "其中 Jellyfin、Navidrome 仅支持整库刷新，"
                                             "「按入库条目刷新」模式下会自动回退为整库刷新。"
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
                                             "等待设定秒数后刷新媒体服务器。TgtoDrive 的 STRM 同步为异步任务，"
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
            "server_names": "",
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
            targets = item.get("targets")
            if targets:
                stat = " ".join(
                    f"{t.get('name')}={'成功' if t.get('ok') else '失败'}" for t in targets
                )
            else:
                # 兼容 v1.3.x 及更早的记录结构
                stat = f"Emby={'成功' if item.get('refresh_ok') else '未执行/失败'}"
                if "fn_ok" in item:
                    stat += f" 飞牛={'成功' if item.get('fn_ok') else '未执行/失败'}"
            line = (
                f"[{item.get('time')}] 来源={item.get('source')} "
                f"触发={'成功' if item.get('trigger_ok') else '失败'} {stat}"
            )
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

                # 3. 刷新全部已配置的媒体服务器
                targets = self._refresh_media_servers(info)
                record["targets"] = [{"name": n, "ok": o, "msg": m} for n, o, m in targets]
                record["refresh_ok"] = any(o for _, o, _ in targets)
                for name, ok, msg in targets:
                    record["message"] += f"\n{name}：{msg}"
                    logger.info(f"{name} 刷新结果：{ok} - {msg}")

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

    def _refresh_media_servers(self, info: dict) -> List[Tuple[str, bool, str]]:
        """刷新 MoviePilot 中已配置并启用的全部媒体服务器，返回 [(名称, 是否成功, 说明)]。"""
        helper_cls = _load_mediaserver_helper()
        if helper_cls is None:
            return [("媒体服务器", False, "当前 MoviePilot 未提供 MediaServerHelper")]

        try:
            services = helper_cls().get_services() or {}
        except Exception as err:  # noqa: BLE001
            return [("媒体服务器", False, f"读取媒体服务器失败：{err}")]
        if not services:
            return [("媒体服务器", False, "MoviePilot 未配置任何媒体服务器")]

        wanted = [s.strip() for s in self._server_names.split(",") if s.strip()] \
            if self._server_names else []

        targets: List[Tuple[str, bool, str]] = []
        for name, service_info in services.items():
            if wanted and name not in wanted:
                continue
            label = f"{name}({_service_type_name(service_info)})"
            instance = getattr(service_info, "instance", None)
            if instance is None:
                targets.append((label, False, "实例不可用"))
                continue
            try:
                ok, msg = self._refresh_one(instance, info)
                targets.append((label, ok, msg))
            except Exception as err:  # noqa: BLE001
                targets.append((label, False, f"刷新异常：{err}"))

        if not targets:
            return [("媒体服务器", False, f"没有匹配的媒体服务器（已指定：{self._server_names}）")]
        return targets

    def _refresh_one(self, instance: Any, info: dict) -> Tuple[bool, str]:
        """刷新单个媒体服务器实例。"""
        if hasattr(instance, "is_configured"):
            try:
                if not instance.is_configured():
                    return False, "未配置或未启用"
            except Exception:  # noqa: BLE001
                pass

        # 按条目刷新：仅在实例支持且能构造出条目时使用，否则回退整库
        if self._refresh_mode == "item" and hasattr(instance, "refresh_library_by_items"):
            items = self._build_refresh_items(info)
            if items:
                try:
                    result = instance.refresh_library_by_items(items)
                    if result is not None:
                        title = info.get("title") or "-"
                        return bool(result), f"已按条目刷新：{title}"
                except Exception as err:  # noqa: BLE001
                    logger.warning(f"按条目刷新失败，回退整库刷新：{err}")

        if not hasattr(instance, "refresh_root_library"):
            return False, "该媒体服务器模块不支持刷新"
        result = instance.refresh_root_library()
        if result is None:
            return False, "刷新返回空（可能未认证或服务不可用）"
        return bool(result), "已请求刷新全部媒体库"

    @staticmethod
    def _build_refresh_items(info: dict) -> Optional[list]:
        """按入库信息构造媒体库刷新条目；无法构造时返回 None（调用方回退整库）。"""
        if not info.get("title"):
            return None
        try:
            from app.schemas import RefreshMediaItem
        except Exception:  # noqa: BLE001
            return None
        try:
            return [
                RefreshMediaItem(
                    title=info.get("title"),
                    year=info.get("year") or None,
                    type=info.get("type") or None,
                    category=None,
                    target_path=info.get("target_path") or None,
                )
            ]
        except Exception:  # noqa: BLE001
            return None

    def _send_notice(self, record: dict, info: dict):
        """发送结果通知。"""
        title = info.get("title") or ""
        title_part = f"（{title}）" if title else ""
        text = (
            f"来源：{record.get('source')}\n"
            f"STRM 触发：{'成功' if record.get('trigger_ok') else '失败'}\n"
        )
        targets = record.get("targets") or []
        if targets:
            for target in targets:
                text += f"{target.get('name')}：{'成功' if target.get('ok') else '失败'}\n"
        else:
            text += "媒体库刷新：未执行\n"
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

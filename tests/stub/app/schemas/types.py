"""宿主 app.schemas.types 垫片：只提供插件导入的事件枚举。

⚠️ 枚举成员的**值**按实机 MoviePilot v3（``/app/app/schemas/types.py``）对齐：
``EventType`` 用点分字符串（``"transfer.complete"``），``NotificationType`` 用
中文标签（``"插件"``）。插件只在 ``@eventmanager.register(...)`` 与
``NotificationType.Plugin`` 上用到它们，改值不会影响被测逻辑，但保持一致
才能在测试里复现真实的注册行为。
"""

from enum import Enum


class ChainEventType(Enum):
    """链式事件类型（仅保留插件注册的事件）。"""

    RecommendSource = "recommend.source"
    DiscoverSource = "discover.source"


class EventType(Enum):
    """宿主事件类型（仅保留被测插件用到的那几个）。"""

    PluginAction = "plugin.action"
    TransferComplete = "transfer.complete"


class NotificationType(Enum):
    """通知类型（仅保留被测插件用到的那几个）。"""

    Plugin = "插件"
    System = "系统"
    Manual = "手动处理"
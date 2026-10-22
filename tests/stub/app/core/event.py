"""宿主 v2 兼容路径 ``app.core.event``。

v3 宿主上 ``app.core.event`` 转发到 ``app.runtime.events``（已实机核实），
这里按同样的转发语义提供替身。事件注册表沿用 ``app.sdk.events`` 的实现：
按「模块名 + __qualname__」存字典，重复注册是**覆盖而非追加**。
"""

from app.sdk.events import eventmanager, Event

__all__ = ["eventmanager", "Event"]
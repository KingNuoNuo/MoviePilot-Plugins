"""宿主 **v2** 兼容导入的垫片（``app.core.*`` / ``app.log``）。

MoviePilot v3 保留了 v2 的导入路径：``app.core.config`` 实际转发到
``app.runtime.config``、``app.core.event`` 转发到 ``app.runtime.events``、
``app.log`` 转发到 ``app.sdk.logging``。所以 v2 插件（``package.json`` 里
``"v2": true`` 的 TgtoDriveSync）在 v3 宿主上**能正常加载**。

测试环境没有真实宿主，这里按同样的转发语义提供最小替身。
"""

from app.sdk.config import settings  # noqa: F401
from app.sdk.logging import logger  # noqa: F401
from app.sdk.events import eventmanager, Event  # noqa: F401

__all__ = ["settings", "logger", "eventmanager", "Event"]
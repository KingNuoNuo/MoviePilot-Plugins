"""宿主 v2 兼容路径 ``app.core.config``。

v3 宿主上 ``app.core.config`` 转发到 ``app.runtime.config``（已实机核实），
这里按同样的转发语义提供替身。
"""

from app.sdk.config import settings

__all__ = ["settings"]
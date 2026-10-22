"""宿主 app.schemas.types 垫片：只提供插件导入的事件枚举。"""

from enum import Enum


class ChainEventType(Enum):
    """链式事件类型（仅保留插件注册的事件）。"""

    RecommendSource = "recommend.source"
    DiscoverSource = "discover.source"
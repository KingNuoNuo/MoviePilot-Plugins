"""宿主 app.sdk.events 垫片：提供可注册的事件管理器。"""


class _EventManager:
    """把注册的处理函数按事件类型收集起来，便于测试直接触发。"""

    def __init__(self):
        self.handlers = {}

    def register(self, event_type):
        def decorator(func):
            self.handlers.setdefault(event_type, []).append(func)
            return func
        return decorator

    def send_event(self, event_type, event_data):
        """按注册顺序调用处理函数，返回承载结果的事件对象。"""
        from app.schemas import Event
        event = Event(event_data)
        for handler in self.handlers.get(event_type, []):
            handler(event)
        return event


eventmanager = _EventManager()


class Event:
    """重新导出宿主事件对象。"""

    def __init__(self, event_data=None):
        self.event_data = event_data


__all__ = ["Event", "eventmanager"]

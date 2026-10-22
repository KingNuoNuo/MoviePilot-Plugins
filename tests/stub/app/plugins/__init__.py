"""宿主 app.plugins 垫片：提供插件基类，并记录已注册的事件处理器。"""


class _PluginBase:
    """插件基类的替身：只提供被测代码实际调用的能力。"""

    def __init__(self):
        self._data = {}
        self.config = {}
        self.chain = None
        self.systemmessage = _SystemMessage()

    def get_data(self, key):
        return self._data.get(key)

    def save_data(self, key, value):
        self._data[key] = value

    def del_data(self, key):
        self._data.pop(key, None)

    def update_config(self, config):
        self.config = config


class _SystemMessage:
    """系统消息替身。"""

    def put(self, message):
        pass

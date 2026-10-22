"""宿主 app.plugins 垫片：提供插件基类，并记录已注册的事件处理器。"""

import copy


class _PluginBase:
    """插件基类的替身：只提供被测代码实际调用的能力。"""

    def __init__(self):
        self._data = {}
        self.config = {}
        self.chain = None
        self.systemmessage = _SystemMessage()
        # 宿主基类还挂了这两个（读「设置 → 目录」与跨插件配置）：
        # - systemconfig.get("Directories") 在宿主 v3 里是整份配置的**深拷贝**，
        #   所以按深拷贝实现，性能用例才能量出真实的重复读开销；
        # - get_config(<PluginId>) 读 plugininstance 表，插件未安装时返回 None。
        self.systemconfig = _SystemConfig()
        self._cross_config = {}

    def get_data(self, key):
        return self._data.get(key)

    def save_data(self, key, value):
        self._data[key] = value

    def del_data(self, key):
        self._data.pop(key, None)

    def update_config(self, config):
        self.config = config

    def get_config(self, plugin_id, key=None):
        """跨插件读配置（宿主语义：目标插件未安装 → None）。"""
        return copy.deepcopy(self._cross_config.get(plugin_id))


class _SystemConfig:
    """``systemconfig`` 替身。``calls`` 供性能用例断言「没有重复读配置」。"""

    def __init__(self):
        self._data = {"Directories": []}
        self.calls = 0

    def get(self, key, default=None):
        self.calls += 1
        return copy.deepcopy(self._data.get(key, default))

    def set(self, key, value):
        self._data[key] = value


class _SystemMessage:
    """系统消息替身。"""

    def put(self, message):
        pass

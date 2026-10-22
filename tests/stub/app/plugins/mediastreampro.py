"""``app.plugins.mediastreampro`` 垫片：把仓库里的插件包按**包**导入。

写法与 ``app/plugins/globalrankpro.py`` 一致：插件在宿主里是以
``app.plugins.<plugin_id>`` 加载的，包内有 ``from .app_promo import promo_card``
这类相对导入，必须给它「包」身份（单文件 spec 会让相对导入直接失败）。

插件目录按**仓库相对路径**定位，不写死绝对路径 —— 换机器、换盘符都能跑。

宿主能力：
- ``systemconfig``：宿主 v3 的 ``SystemConfigOper.get()`` 返回整份配置的**深拷贝**
  并要求配置已加载；垫身照这个语义实现，性能用例要按「深拷贝」的成本计算它。
- ``get_config(<PluginId>)``：直读 plugininstance 表，插件未安装时返回 ``None``。
"""

import copy
import importlib.util
import sys
from pathlib import Path

# 仓库根 = tests/stub/app/plugins/mediastreampro.py → 上溯五级
_REPO_ROOT = Path(__file__).resolve().parents[4]
_PLUGIN_DIR = _REPO_ROOT / "plugins.v3" / "mediastreampro"

if not _PLUGIN_DIR.is_dir():
    raise RuntimeError(f"找不到插件源码目录：{_PLUGIN_DIR}")

_spec = importlib.util.spec_from_file_location(
    __name__,
    _PLUGIN_DIR / "__init__.py",
    submodule_search_locations=[str(_PLUGIN_DIR)],
)
_module = importlib.util.module_from_spec(_spec)
sys.modules[__name__] = _module
_spec.loader.exec_module(_module)


class SystemConfigStub:
    """``systemconfig`` 替身：``get`` 返回深拷贝，与宿主一致。"""

    def __init__(self, directories=None):
        self._data = {"Directories": list(directories or [])}
        self.calls = 0  # 统计调用次数，性能用例要断言「没有重复读」

    def get(self, key, default=None):
        self.calls += 1
        return copy.deepcopy(self._data.get(key, default))


__all__ = ["SystemConfigStub"]
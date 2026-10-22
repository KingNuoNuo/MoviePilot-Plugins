"""``app.plugins.globalrankpro`` 垫片：把仓库里的插件包按**包**导入。

为什么需要这个垫片：插件在宿主里是以 ``app.plugins.<plugin_id>`` 的形式加载的，
包内还有 ``from .base import ...`` 这类相对导入。要让测试能 import 到插件，
就得给它一个「包」身份 —— 单文件模块（``spec_from_file_location`` 不给
``submodule_search_locations``）会让相对导入直接失败。

插件目录按**仓库相对路径**定位，不写死绝对路径 —— 换机器、换盘符都能跑。
"""
import importlib.util
import sys
from pathlib import Path

# 仓库根 = tests/stub/app/plugins/globalrankpro.py → 上溯五级
# （plugins → app → stub → tests → 仓库根）
_REPO_ROOT = Path(__file__).resolve().parents[4]
_PLUGIN_DIR = _REPO_ROOT / "plugins.v3" / "globalrankpro"

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

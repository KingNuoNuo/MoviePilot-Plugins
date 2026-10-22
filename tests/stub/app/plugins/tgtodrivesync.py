"""``app.plugins.tgtodrivesync`` 垫片：把仓库里的插件包按**包**导入。

与 ``app/plugins/globalrankpro.py`` 同一套写法：插件在宿主里以
``app.plugins.<plugin_id>`` 加载，包内有 ``from .app_promo import promo_card``
这类相对导入，必须给它「包」身份。

这是 **v2** 插件（``package.json`` 里 ``"v2": true``），导入 ``app.core.*``
与 ``app.log``。v3 宿主保留了这些兼容转发路径（实机核实：
``app.core.config`` → ``app/runtime/config.py``、
``app.core.event`` → ``app/runtime/events.py``、
``app.log`` → ``app/sdk/logging.py``），因此它在 v3 上能正常加载。
"""

import importlib.util
import sys
from pathlib import Path

# 仓库根 = tests/stub/app/plugins/tgtodrivesync.py → 上溯五级
_REPO_ROOT = Path(__file__).resolve().parents[4]
_PLUGIN_DIR = _REPO_ROOT / "plugins" / "tgtodrivesync"

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
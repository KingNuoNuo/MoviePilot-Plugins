"""插件仓单测的 pytest 引导。

**为什么自带宿主垫片**：插件代码在宿主的进程里运行，直接 import
``app.plugins`` / ``app.sdk`` / ``app.schemas``。没有真实 MoviePilot 后端时，
这些 import 会直接失败，测试一条都跑不了。

``tests/stub/`` 是一份**最小宿主替身**：只提供插件真正用到的符号与
方法签名（按 MoviePilot v3 宿主实现对齐，例如 ``RequestUtils.request``
返回 Response 对象、默认超时 20 秒）。有了它，``pytest tests/v3``
在任何装了 pytest 的机器上都能直接跑，不需要拉起整个后端。

若要对着**真实后端**跑（验证与宿主真实行为的差异），把后端置于本仓同级目录，
或用环境变量 ``MOVIEPILOT_BACKEND_PATH`` 指定 —— 此时用真实后端优先：

.. code-block:: bash

    MOVIEPILOT_BACKEND_PATH=/path/to/MoviePilot python -m pytest tests/v3
"""

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_STUB = _HERE / "tests" / "stub"

# 真实后端优先：指定了路径就用真后端，否则用自带垫片
_backend = os.environ.get("MOVIEPILOT_BACKEND_PATH")
if _backend and Path(_backend).is_dir():
    sys.path.insert(0, str(Path(_backend).resolve()))
elif str(_STUB) not in sys.path:
    sys.path.insert(0, str(_STUB))

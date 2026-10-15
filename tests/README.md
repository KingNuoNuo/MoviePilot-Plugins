# 插件仓单测

测试统一放在仓库根 `tests/` 下，**不放进插件目录**——MoviePilot 的插件本地同步与市场下发
按整目录拷贝，放在插件目录内的测试会被一并下发到运行时副本。

```
tests/
└─ v3/
   └─ maoyanpro/      # 猫眼榜单订阅（plugins.v3/maoyanpro）
```

## 运行

需要 MoviePilot 后端置于本仓**同级目录**（或用环境变量 `MOVIEPILOT_BACKEND_PATH` 指定），
并使用带后端依赖的解释器：

```bash
# 只跑本代插件
python -m pytest tests/v3

# 只跑猫眼插件
python -m pytest tests/v3/maoyanpro
```

测试通过生产命名空间 `app.plugins.<plugin_id>` 导入插件，不要把插件目录加入 `sys.path`
后使用顶层包名——单一模块身份可避免同一源码被重复执行，也防止事件订阅或类状态被重复创建。

## 新增 V3 插件时

新增 V3 插件应同时增加 `tests/v3/<plugin_id>/test_*.py`。
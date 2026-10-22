# 插件仓单测

测试统一放在仓库根 `tests/` 下，**不放进插件目录**——MoviePilot 的插件本地同步与市场下发
按整目录拷贝，放在插件目录内的测试会被一并下发到运行时副本。

```
tests/
└─ v3/
   └─ globalrankpro/   # 全球影视榜单（plugins.v3/globalrankpro）
```

## 运行

仓库自带一份**最小宿主垫片**（`tests/stub/`），照 MoviePilot v3 宿主的公开签名实现
插件用到的那些符号（`app.plugins` / `app.sdk` / `app.schemas` / `apscheduler`），
所以**不需要拉起真实后端**也能跑：

```bash
# 跑本代全部插件
python -m pytest tests/v3

# 只跑全球影视榜单
python -m pytest tests/v3/globalrankpro
```

要对**真实后端**跑（验证与宿主真实行为的差异），把后端置于本仓**同级目录**，
或用环境变量 `MOVIEPILOT_BACKEND_PATH` 指定 —— 此时真后端优先于垫片：

```bash
MOVIEPILOT_BACKEND_PATH=/path/to/MoviePilot python -m pytest tests/v3
```

测试通过生产命名空间 `app.plugins.<plugin_id>` 导入插件，不要把插件目录加入 `sys.path`
后使用顶层包名——单一模块身份可避免同一源码被重复执行，也防止事件订阅或类状态被重复创建。

## 垫片与全局状态

- 垫片只保证**导入与签名**正确，不实现宿主行为：涉及网络、数据库、宿主链的用例
  一律用 monkeypatch 替换，不碰真实服务。
- 插件里有些状态是**模块级全局变量**（豆瓣的熔断窗口 `_blocked_until`、
  连空计数 `_empty_streak`、403 死窗 `_search_dead_until`）——生产上刻意共享
  （限流必须跨请求生效），但测试里会让用例互相污染：某个用例触发一次熔断，
  后面本该正常查库的用例直接短路失败。
  `tests/v3/globalrankpro/conftest.py` 里的 autouse fixture 会在每个用例前后
  复位这些全局状态，**只影响测试，不改生产代码的行为**。

## 新增 V3 插件时

新增 V3 插件应同时增加 `tests/v3/<plugin_id>/test_*.py`。
"""TgtoDriveSync 的并发回归测试：重入 init_plugin 不得并发跑两条流水线。

宿主 v3 的 ``PluginManager.init_plugin`` → ``lifecycle.initialize`` 对**同一个
实例**再调一次 ``plugin.init_plugin(config)``（保存插件配置、启用/禁用开关时
都会走到）。而流水线的默认等待里就有一次 ``time.sleep(60)``，所以「保存配置」
完全可能落在流水线运行中途。

旧实现 ``init_plugin`` 开头无条件 ``stop_service()``（``shutdown(wait=False)``
**不取消**已在跑的任务）再把 ``_running`` 重置为 False，于是：

- 旧流水线（线程池 E1）继续跑它的后半段：刷新媒体库、发通知、写历史；
- 新入库事件看到 ``_running`` 是 False → 提交到新建的 E2 → **两条流水线并发**；
- 后果是重复触发 TgtoDrive 同步、重复整库刷新、重复通知，且两者的
  ``_save_history`` 读-改-写互相覆盖；
- 更糟：E1 在 ``finally`` 里再写一次 ``_running = False``，把 E2 正在运行的
  标记清掉，第三条流水线又能起来。

这里用**同一个实例**重现这个时序，并把所有外部副作用换成计数器
（不触发真实下载、不刷新真实媒体库、不发真实通知）。
"""

import threading
import time

import pytest

from app.plugins import tgtodrivesync as tg


# ---------------------------------------------------------------- 夹具


@pytest.fixture(autouse=True)
def reset_module_state():
    """复位模块级状态（防抖定时器 / 事件注册表）。"""
    saved_timer = tg.TgtoDriveSync._timer
    tg.TgtoDriveSync._timer = None
    yield
    if tg.TgtoDriveSync._timer is not None:
        tg.TgtoDriveSync._timer.cancel()
    tg.TgtoDriveSync._timer = saved_timer


class _Counter:
    """记录各类外部副作用的次数。"""

    def __init__(self):
        self.triggers = 0
        self.refreshes = 0
        self.notices = 0
        self.histories = 0
        self.concurrent_max = 0
        self._active = 0
        self._guard = threading.Lock()

    def enter(self):
        with self._guard:
            self._active += 1
            self.concurrent_max = max(self.concurrent_max, self._active)

    def leave(self):
        with self._guard:
            self._active -= 1


@pytest.fixture
def plugin():
    """一个启用、但把所有外部副作用替换成计数器的插件实例。"""
    inst = tg.TgtoDriveSync()
    inst.init_plugin({
        "enabled": True,
        "tgtodrive_url": "http://tgto.local",
        "openapi_token": "TESTTOKEN",
        "source": "123云盘",
        "wait_seconds": 0,      # 固定等待关掉，测试里不需要真的睡
        "debounce_seconds": 1,
        "max_wait_seconds": 0,
        "strm_dir": "",
        "notify": True,
    })
    counter = _Counter()
    _patch_side_effects(inst, counter)
    inst._counter = counter
    yield inst
    inst.stop_service()


def _patch_side_effects(inst, counter):
    """把触发同步 / 刷新媒体库 / 通知 / 写历史换成计数器。"""

    def trigger():
        counter.enter()
        try:
            counter.triggers += 1
            # 模拟真实耗时（真实实现里这里是 sleep(wait_seconds)）
            time.sleep(0.25)
            return "ok", "已触发"
        finally:
            counter.leave()

    def wait(items):
        time.sleep(0.1)
        return "未配置 STRM 目录，固定等待已跳过"

    def refresh(items):
        counter.enter()
        try:
            counter.refreshes += 1
            time.sleep(0.1)
            return [{"name": "Emby", "ok": True, "msg": "已刷新",
                     "libraries": ["媒体库A"]}]
        finally:
            counter.leave()

    def notice(record):
        counter.notices += 1

    def save_history(record):
        counter.histories += 1

    inst._trigger_strm_sync = trigger
    inst._wait_for_strm = wait
    inst._refresh_media_servers = refresh
    inst._send_notice = notice
    inst._save_history = save_history


# ---------------------------------------------------------------- 用例


def test_save_config_during_run_does_not_start_second_pipeline(plugin):
    """流水线运行中保存配置，不得让第二条流水线**并发**跑，且副作用不重复。

    触发条件：入库 → 防抖到点 → 流水线启动（内有 sleep）→ 用户点「保存配置」
    → 宿主对同一实例重调 ``init_plugin`` → 此时再来一个入库事件。

    期望：
    - ``concurrent_max == 1``（绝不并发跑两条）；
    - 刷新媒体库、通知、写历史各只发生**一次** —— 旧流水线发现自己已被
      新配置取代就让位给新流水线，不重复做副作用（这是修复的核心）。
      注意「同步触发」可以有两次：那是对**两批不同文件**各触发一次，属正确语义。
    """
    counter = plugin._counter

    # 1. 起第一轮流水线 —— 必须走真实入口 _dispatch（它负责置 _running=True）。
    # 直接 _executor.submit 会绕过这个标记，测的就不是线上路径了。
    plugin._pending_items = [{"title": "测试片", "year": "2026",
                              "target_path": "/media/a.mkv"}]
    plugin._manual_pending = True
    plugin._dispatch()

    # 2. 等它真的跑起来（进入 trigger 的 sleep）
    deadline = time.monotonic() + 3
    while counter.triggers == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert counter.triggers == 1, "第一轮流水线没有启动"
    assert plugin._running is True, "_dispatch 应该已把 _running 置为 True"

    # 3. 模拟「保存配置」：宿主对同一实例重调 init_plugin
    plugin.init_plugin({
        "enabled": True,
        "tgtodrive_url": "http://tgto.local",
        "openapi_token": "TESTTOKEN",
        "wait_seconds": 0,
        "debounce_seconds": 1,
        "max_wait_seconds": 0,
        "strm_dir": "",
        "notify": True,
    })
    _patch_side_effects(plugin, counter)

    # 4. 保存配置之后又来一个入库事件（另一批文件）
    plugin._pending_items = [{"title": "第二部", "year": "2026",
                              "target_path": "/media/b.mkv"}]
    plugin._manual_pending = True
    plugin._dispatch()

    # 5. 等两轮都收尾：第二条可能被 _dispatch 挡下并 arm timer 稍后重试
    #    （debounce_seconds=1），留足时间让整条链路自然走完
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if plugin._running or plugin._pending_items or plugin._manual_pending:
            time.sleep(0.05)
            continue
        if plugin._counter.refreshes >= 1 or plugin._counter.histories >= 1:
            break
        time.sleep(0.05)
    time.sleep(0.5)
    plugin.stop_service()
    time.sleep(0.3)

    assert counter.concurrent_max == 1, (
        f"出现了 {counter.concurrent_max} 条并发流水线 —— 保存配置后重复执行")
    assert counter.refreshes == 1, (
        f"媒体库被重复刷新 {counter.refreshes} 次")
    assert counter.notices == 1, f"通知被重复发送 {counter.notices} 次"
    assert counter.histories == 1, f"执行历史被重复写入 {counter.histories} 次"


def test_running_flag_not_stuck_after_supersede(plugin):
    """被取代的流水线收手后，``_running`` 必须复位。

    这是上一条的配套边界：旧流水线发现配置已被取代时会在 ``finally`` 里
    **跳过** ``_running = False``（避免清掉新流水线正在运行的标记）。如果这
    一跳之后没人复位，``_running`` 就永久停在 True —— ``_dispatch`` 会一直
    走「已有刷新任务在执行」分支，所有后续入库事件再也触发不了同步。
    """
    plugin._send_notice = lambda record: None
    items = [{"title": "测试片", "target_path": "/media/a.mkv"}]
    plugin._executor.submit(plugin._run_pipeline, items, False, plugin._generation)

    deadline = time.monotonic() + 3
    while plugin._counter.triggers == 0 and time.monotonic() < deadline:
        time.sleep(0.01)

    # 模拟运行途中保存配置
    with plugin._lock:
        plugin._generation += 1

    time.sleep(0.6)
    assert plugin._running is False, (
        "被取代的流水线让位后 _running 没复位，后续入库事件会被永久挡住")


def test_stop_service_does_not_abandon_running_pipeline(plugin):
    """停用插件时，正在跑的流水线不该把副作用做完（重复刷新 + 重复通知）。

    触发条件：流水线运行中点「停用插件」或卸载插件。
    期望：``_running`` 被正确复位，队列不丢，但不再触发新一轮外部副作用。
    """
    counter = plugin._counter
    items = [{"title": "测试片", "year": "2026", "target_path": "/media/a.mkv"}]
    plugin._executor.submit(plugin._run_pipeline, items, False)

    deadline = time.monotonic() + 3
    while counter.triggers == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert counter.triggers == 1

    plugin.stop_service()
    time.sleep(0.8)

    # 停用后 _running 必须复位，否则后续事件会永久卡在「已有任务在执行」
    assert plugin._running is False, "stop_service 之后 _running 没有复位"
    # 已启动的那一轮跑完是允许的（不重复即可）
    assert counter.triggers <= 1, (
        f"停用后又触发了同步，共 {counter.triggers} 次")


def test_enabled_gate_blocks_event_path(plugin):
    """插件未启用时，事件入口不得触发任何外部副作用。

    三个入口各自都有 ``_enabled`` 检查（``on_transfer_complete`` /
    ``on_plugin_action`` / ``api_trigger``），这里覆盖事件入口这条。
    """
    counter = plugin._counter
    plugin._enabled = False
    plugin.on_transfer_complete(None)
    time.sleep(0.3)
    assert counter.triggers == 0, "未启用时仍触发了同步"


def test_save_history_is_not_lost_under_concurrency(plugin):
    """多条记录写历史不得互相覆盖（读-改-写必须持锁）。

    触发条件：并发流水线同时写历史。旧实现 ``get_data`` → ``append`` →
    ``save_data`` 全程不持锁，两条流水线各读到旧副本，后写的丢掉先写的。
    """
    counter = plugin._counter
    store = {"history": []}
    lock = threading.Lock()

    def get_data(key, default=None):
        with lock:
            return list(store.get(key, []) or [])

    def save_data(key, value):
        with lock:
            store[key] = list(value)

    plugin.get_data = get_data
    plugin.save_data = save_data
    # fixture 把 _save_history 换成了计数器，这里换回**真实**实现 ——
    # 本用例要验的正是它的读-改-写互斥
    plugin._save_history = tg.TgtoDriveSync._save_history.__get__(plugin)

    errors = []

    def writer(tag):
        try:
            for i in range(20):
                rec = {"tag": tag, "i": i}
                # 给别的线程插入的窗口
                time.sleep(0.0005)
                plugin._save_history(rec)
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert not errors, f"写历史抛异常：{errors[:2]}"
    # history 最多保留 100 条；4×20=80 条全部写入，不该大规模丢失
    assert len(store["history"]) == 80, (
        f"并发写历史丢失严重：期望 80，实际 {len(store['history'])}")
"""GlobalRankPro 回归测试：定时刷新、并发抓取、评分缓存合并与快照读缓存。

这些用例都对应真实线上故障或实测开销，不依赖网络与宿主调度：
主插件用 ``GlobalRankPro()`` 直接构造（不注册调度），数据存取换成内存桩，
确保不会碰真实插件数据。
"""

import copy
import threading
import time

import pytest

from app.plugins.globalrankpro import GlobalRankPro, douban
from app.plugins.globalrankpro.base import RankSource


class _StubSource(RankSource):
    """最小数据源桩：fetch 结果由用例给定，并记录调用次数。"""

    source_id = "maoyan"
    source_name = "猫眼榜单"

    def __init__(self, host, result=None):
        super().__init__(host)
        self.result = result or {"board_count": 1, "item_count": 2}
        self.calls = 0

    def fetch(self):
        self.calls += 1
        return dict(self.result)


def _plugin_with(source, last_fetch=0):
    """构造只挂一个桩数据源的主插件，数据层替换成内存。"""
    plugin = GlobalRankPro()
    plugin.sources = {source.source_id: source}
    plugin.get_data = lambda key, default=None: (
        last_fetch if str(key).startswith("last_fetch_") else default)
    plugin.save_data = lambda key, value: None
    return plugin


def test_scheduled_refresh_no_longer_raises_name_error():
    """定时入口（force=False）必须能跑完：早先这里引用了未定义的类属性名。

    触发条件：任何一次定时刷新 —— ``_MIN_REFRESH_INTERVAL`` 只在类上定义，
    函数里当全局名用，读取即 NameError，APScheduler 只留一条 warning。
    """
    source = _StubSource(None)
    plugin = _plugin_with(source, last_fetch=0)
    result = plugin._GlobalRankPro__refresh_source("maoyan", force=False)
    assert result.refreshed is True
    assert source.calls == 1


def test_scheduled_refresh_still_skips_within_min_interval():
    """4 小时保护仍然生效：刚抓过就跳过，且文案带小时数。"""
    source = _StubSource(None)
    plugin = _plugin_with(source, last_fetch=time.time() - 60)
    result = plugin._GlobalRankPro__refresh_source("maoyan", force=False)
    assert result.refreshed is False
    assert source.calls == 0
    assert "跳过" in result.message


def test_concurrent_refresh_runs_single_fetch():
    """并发刷新只真正抓一次，第二路立刻返回「正在刷新」。"""
    gate = threading.Event()

    class _SlowSource(_StubSource):
        def fetch(self):
            self.calls += 1
            assert gate.wait(5), "刷新线程未被放行"
            return {"board_count": 1, "item_count": 1}

    source = _SlowSource(None)
    plugin = _plugin_with(source, last_fetch=0)
    first = {}

    def run_first():
        first["result"] = plugin._GlobalRankPro__refresh_source("maoyan")

    thread = threading.Thread(target=run_first)
    thread.start()
    try:
        deadline = time.monotonic() + 2
        while source.calls == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        second = plugin._GlobalRankPro__refresh_source("maoyan")
    finally:
        gate.set()
        thread.join(5)

    assert source.calls == 1, "第二路不应再打一次上游"
    assert second.refreshed is False
    assert "正在刷新" in second.message
    assert first["result"].refreshed is True


def test_rating_cache_merges_concurrent_writes(monkeypatch):
    """并发 /rating 的缓存写回不能覆盖别的请求刚写进去的条目。

    线上同屏三榜会并发打十几个 /rating：整份 dict 读-改-写时，后写的会用
    旧副本把别人刚查到的评分抹掉。
    """
    store = {}
    plugin = GlobalRankPro()
    plugin.get_data = lambda key, default=None: store.get(key, default)
    plugin.save_data = lambda key, value: store.__setitem__(key, value)

    def fake_lookup(title, **kwargs):
        # 模拟「另一个请求在本次读之后、写之前」也写了一条
        store.setdefault("douban_rating", {})["别的片|2020|"] = {
            "vote_average": "7.7", "rating_ts": time.time(), "rating_ver": douban.RATING_VER}
        return {"rate": "9.1"}

    monkeypatch.setattr(douban, "lookup", fake_lookup)
    response = plugin.douban_ratings(q='[{"t":"测试片","y":"2025"}]')
    assert response.success is True
    cache = store["douban_rating"]
    assert "别的片|2020|" in cache, "并发写入被覆盖了"
    assert douban.cache_key("测试片", "2025", "") in cache


def _backfill_probe(stamp_on_second_read: str):
    """跑一次 __backfill_ratings，返回「实际写了哪些键」。

    ``identity_cache`` 显式返回空 dict（不是 None）—— 返回 None 会让实现直接
    ``continue`` 跳过整个源，快照那段根本走不到，用例就变成空转。
    """
    snapshot = {
        "updated_at": "2026-10-20 10:00:00",
        "boards": [{"board": "电影票房榜单", "type": "movie",
                    "items": [{"rank": 1, "title": "测试片", "vote_average": 0}]}],
    }
    state = {"snapshot_reads": 0, "saved": []}

    def fake_get(key, default=None):
        if key.endswith("_identity_cache"):
            return {}
        if key.endswith("_snapshot"):
            state["snapshot_reads"] += 1
            if state["snapshot_reads"] > 1 and stamp_on_second_read:
                # 写前复核读到「已被抓取刷新」的新快照
                return dict(snapshot, updated_at=stamp_on_second_read)
            return copy.deepcopy(snapshot)
        return default

    plugin = GlobalRankPro()
    plugin.get_data = fake_get
    plugin.save_data = lambda key, value: state["saved"].append(key)
    plugin._GlobalRankPro__backfill_ratings({"测试片": "8.8"})
    return state


def test_backfill_skips_when_snapshot_refreshed_by_fetch():
    """回写评分时若快照已被新一轮抓取替换，必须放弃本次回写。"""
    state = _backfill_probe("2026-10-20 12:00:00")
    assert "maoyan_snapshot" not in state["saved"], "新快照被旧副本覆盖了"


def test_backfill_writes_when_snapshot_unchanged():
    """对照组：快照没被换过时必须真的写回，否则上一条断言会因「什么都不做」而假绿。"""
    state = _backfill_probe("")
    assert "maoyan_snapshot" in state["saved"]


def test_source_snapshot_read_is_memoized_then_invalidated():
    """同一轮请求里重复读快照要走缓存，写快照后必须立刻失效。"""
    reads = {"n": 0}
    data = {"maoyan_snapshot": {"updated_at": "t", "boards": [{"board": "b"}]}}

    class _Host:
        @staticmethod
        def get_data(key, default=None):
            reads["n"] += 1
            return data.get(key, default)

        @staticmethod
        def save_data(key, value):
            data[key] = value

    source = _StubSource(_Host())
    assert len(source.boards()) == 1
    assert len(source.boards()) == 1
    assert reads["n"] == 1, "同一次请求内不应重复读整份快照"

    source.save_data("snapshot", {"updated_at": "t2", "boards": []})
    assert source.boards() == []
    assert reads["n"] == 2


def test_stale_403_does_not_disable_search(monkeypatch):
    """上一次请求的 403 不能被当成「本次 /search 被封」。

    异常分支不写 status_code，旧实现会把残留的 403 当成本次结果，
    于是 /search 被误判为封禁、30 分钟不再尝试。
    """
    douban._search_dead_until = 0.0
    douban.last_status = 403

    def boom(*args, **kwargs):
        raise RuntimeError("连接被重置")

    monkeypatch.setattr(douban.RequestUtils, "request", boom)
    douban._get_json("https://m.douban.com/rexxar/api/v2/search", {"q": "x"})
    assert douban.last_status == 0
    douban._blocked_until = 0.0  # 复位熔断，避免影响其它用例


def test_prune_cache_drops_expired_entries_only():
    """评分缓存按 30 天 TTL 淘汰，新鲜条目保留。"""
    now = time.time()
    cache = {
        "fresh": {"vote_average": "8.1", "rating_ts": now - 86400, "rating_ver": douban.RATING_VER},
        "expired": {"vote_average": "7.0", "rating_ts": now - 40 * 86400, "rating_ver": douban.RATING_VER},
        "broken": {"vote_average": ""},
    }
    removed = douban.prune_cache(cache, now)
    assert removed == 2
    assert list(cache) == ["fresh"]

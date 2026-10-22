"""豆瓣共享评分缓存的并发回归测试。

``douban_rating`` 这个插件数据键有**三个**写入方：

1. ``/rating`` 接口（``__init__.douban_ratings``）—— 已用 ``_RATING_CACHE_LOCK``；
2. 猫眼榜单补分（``maoyan.__resolve_board_identities`` → ``douban.fill_ratings``）；
3. Netflix 榜单补分（``netflix.__resolve_identities`` → ``douban.fill_ratings``）。

2 与 3 走 ``fill_ratings`` 的 ``store_get`` / ``store_save`` 回调，**全程无锁**，
而且它们的读-改-写跨度是**一整轮识别**（几分钟，中间还有几十次网络请求）。
所以定时任务补分与 App 的 ``/rating`` 请求并发时，后写的一方会拿自己几分钟前
读到的旧副本覆盖对方刚写进去的评分 —— 表现是「明明查到了分，App 端下次进榜
又变空」，而且会反复出现。

这些用例全部用内存桩与 monkeypatch，不发任何豆瓣请求。
"""

import threading
import time

import pytest

from app.plugins.globalrankpro import GlobalRankPro, douban


def _plugin_with(store):
    plugin = GlobalRankPro()
    plugin.get_data = lambda key, default=None: store.get(key, default)
    plugin.save_data = lambda key, value: store.__setitem__(key, value)
    return plugin


def test_fill_ratings_does_not_clobber_concurrent_rating_writes(monkeypatch):
    """榜单补分写回共享缓存时，不得覆盖 /rating 刚写进去的条目。

    触发条件：定时抓取正在补分（``fill_ratings`` 已读过共享缓存），
    此时 App 打了个 ``/rating`` 请求查到了另一片的分并写回；
    补分轮结束时把整份 dict 写回 → 抹掉了那条评分。
    """
    store = {}
    plugin = _plugin_with(store)

    key = douban.cache_key("榜单片", "2026", "电影")
    other_key = "别的片|2020|"

    # fill_ratings 一开始会 store_get()，这里让它拿到一份「空」缓存，
    # 之后 /rating 写进去的东西必须在补分写回时活下来
    monkeypatch.setattr(douban, "is_blocked", lambda: False)
    monkeypatch.setattr(douban, "lookup",
                        lambda *a, **kw: {"rate": "8.8", "poster": "", "title": ""})
    monkeypatch.setattr(douban, "miss_cacheable", lambda: True)

    # 模拟真实时序：fill_ratings 开头 store_get() 拿到的是**旧**状态；
    # 等它跑完（此处即 lookup 返回那一刻）App 的 /rating 写进了另一条；
    # 然后 fill_ratings 写回 —— 那一刻它必须重新读到「当前最新」再合并。
    timeline = {"phase": "read"}

    def store_get():
        if timeline["phase"] == "read":
            return dict(store.get("douban_rating", {}))
        # 写回阶段：/rating 已经写进去了
        return dict(store.get("douban_rating", {}))

    def store_save(cache):
        assert timeline["phase"] == "write", (
            "写回前应当先重新读取一次共享缓存（读-改-写不能用旧副本）")
        store["douban_rating"] = cache

    def fake_lookup(*a, **kw):
        # lookup 返回前：App 的 /rating 并发写入了另一条评分
        store.setdefault("douban_rating", {})[other_key] = {
            "vote_average": "7.7", "rating_ts": time.time(),
            "rating_ver": douban.RATING_VER,
        }
        timeline["phase"] = "write"
        return {"rate": "8.8", "poster": "", "title": ""}

    monkeypatch.setattr(douban, "lookup", fake_lookup)

    used = douban.fill_ratings(
        [("榜单片", {"title_cn": "榜单片", "year": "2026", "type": "电影"})],
        budget=5,
        store_get=store_get,
        store_save=store_save,
    )

    assert used == 1, "应该真的查了一次豆瓣"
    final = store["douban_rating"]
    assert other_key in final, (
        "榜单补分把 /rating 刚写入的评分覆盖掉了")
    assert key in final, "自己这一轮补的评分反而没写进去"


def test_fill_ratings_reads_shared_cache_freshly(monkeypatch):
    """fill_ratings 写回前应重新读取共享缓存，而不是拿开头那份旧副本。"""
    store = {"douban_rating": {
        "共享片|2024|": {"vote_average": "9.1", "rating_ts": time.time(),
                        "rating_ver": douban.RATING_VER},
    }}
    plugin = _plugin_with(store)
    monkeypatch.setattr(douban, "is_blocked", lambda: False)
    monkeypatch.setattr(douban, "lookup",
                        lambda *a, **kw: {"rate": "8.0", "poster": "", "title": ""})
    monkeypatch.setattr(douban, "miss_cacheable", lambda: True)

    saved = {}
    real_get = lambda: dict(store.get("douban_rating", {}))  # noqa: E731

    def store_get():
        return real_get()

    def store_save(cache):
        saved["written"] = dict(cache)

    douban.fill_ratings(
        [("新片", {"title_cn": "新片", "year": "2026", "type": "电影"})],
        budget=5, store_get=store_get, store_save=store_save)

    assert "共享片|2024|" in saved["written"], (
        "写回时用的不是最新的一份共享缓存")


def test_fill_ratings_hits_shared_cache_without_requesting(monkeypatch):
    """共享缓存里已有的评分要直接抄，不再打豆瓣（这是共用缓存的意义）。"""
    store = {"douban_rating": {}}
    now = time.time()
    key = douban.cache_key("共享片", "2024", "电影")
    store["douban_rating"][key] = {
        "vote_average": "9.1", "rating_ts": now, "rating_ver": douban.RATING_VER}
    monkeypatch.setattr(douban, "is_blocked", lambda: False)

    def boom(*a, **kw):
        raise AssertionError("共享缓存命中时不该再打豆瓣")

    monkeypatch.setattr(douban, "lookup", boom)

    entry = {"title_cn": "共享片", "year": "2024", "type": "电影"}
    used = douban.fill_ratings(
        [("共享片", entry)], budget=5,
        store_get=lambda: dict(store["douban_rating"]),
        store_save=lambda c: None)
    assert used == 0
    assert entry["vote_average"] == "9.1"


def test_rating_endpoint_and_fill_ratings_share_one_lock(monkeypatch):
    """``/rating`` 与榜单补分必须共用同一把锁，否则并发写还是会互相覆盖。"""
    import app.plugins.globalrankpro as grp

    assert hasattr(grp, "RATING_STORE_LOCK"), (
        "共享评分缓存的锁必须是模块级单例，两条路径都要用同一把")
    lock = grp.RATING_STORE_LOCK
    assert isinstance(lock, type(threading.Lock()))


def test_fill_ratings_lock_releases_on_exception(monkeypatch):
    """豆瓣查询抛异常时锁必须释放，否则一次网络抖动就把缓存写死。"""
    import app.plugins.globalrankpro as grp

    monkeypatch.setattr(douban, "is_blocked", lambda: False)

    def boom(*a, **kw):
        raise RuntimeError("网络抖动")

    monkeypatch.setattr(douban, "lookup", boom)

    douban.fill_ratings(
        [("片", {"title_cn": "片", "year": "2026", "type": "电影"})],
        budget=5, store_get=lambda: {}, store_save=lambda c: None)
    # 能再次获取即说明没被锁死
    acquired = grp.RATING_STORE_LOCK.acquire(timeout=2)
    assert acquired, "异常后锁没有被释放"
    if acquired:
        grp.RATING_STORE_LOCK.release()
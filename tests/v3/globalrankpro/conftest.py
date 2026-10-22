"""全球影视榜单插件的测试夹具。

**为什么要重置豆瓣模块级状态**：``app.plugins.globalrankpro.douban`` 里的
熔断窗口（``_blocked_until``）、连空计数（``_empty_streak``）、403 死窗
（``_search_dead_until``）都是**模块级全局变量** —— 这是生产上刻意为之
（限流/熔断必须跨请求共享）。但在测试里，它会让用例之间互相污染：
某个用例故意触发一次失败，熔断窗口就是「未来 120 秒」，后面本该正常查库的
用例直接走 ``is_blocked()`` 短路，缓存不写回，用例莫名失败。

所以每个用例前后都把这些全局状态复位，让每个用例都从「未限流」的干净状态
开始。这只影响测试，不改生产代码的行为。
"""

import pytest

from app.plugins.globalrankpro import douban


@pytest.fixture(autouse=True)
def reset_douban_global_state():
    """复位豆瓣模块的限流 / 熔断 / 死窗状态。"""
    saved = (
        douban._blocked_until,
        douban._empty_streak,
        douban._search_dead_until,
        douban._probe_depth,
        douban.last_status,
        douban.last_raw,
    )
    douban._blocked_until = 0.0
    douban._empty_streak = 0
    douban._search_dead_until = 0.0
    douban._probe_depth = 0
    douban.last_status = 0
    douban.last_raw = None
    try:
        yield
    finally:
        (douban._blocked_until,
         douban._empty_streak,
         douban._search_dead_until,
         douban._probe_depth,
         douban.last_status,
         douban.last_raw) = saved

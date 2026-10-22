"""红果海报代理的回归测试：LRU 并发安全与落盘原子性。

``/poster`` 是 ``allow_anonymous`` 的（客户端用 ``<img>`` 加载，带不了
Authorization 头），App 一屏 20 张封面就是 20 个并发请求打进同一个接口。
因此这里对缓存的并发安全要求比其它接口更高。

覆盖两个能指到具体代码行的问题：

1. **LRU 无锁**：``OrderedDict`` 的 ``get`` → ``move_to_end`` →
   ``popitem(last=False)`` 不是一个原子操作。多线程下 A 线程刚 ``get`` 到
   某键、B 线程把该键淘汰掉、A 再 ``move_to_end`` → ``KeyError`` → 接口 500。
2. **落盘非原子**：``img`` 与 ``meta`` 分两次写。写完 img、崩在写 meta 之前
   时，下次进来两个文件都「存在」（meta 是上一次的旧值），于是把一张**写了一半
   的图**当成有效缓存返回，并按 7 天的 ``Cache-Control`` 缓存给所有客户端。

全部用临时目录 + 桩响应，不访问任何真实 CDN。
"""

import concurrent.futures
import threading

import pytest
from fastapi import HTTPException

from app.plugins.globalrankpro import hongguo


PNG = b"\x89PNG\r\n\x1a\n" + b"payload" * 16


@pytest.fixture(autouse=True)
def reset_poster_state(tmp_path, monkeypatch):
    """清空内存 LRU，并把落盘目录指到临时目录（绝不写真实 config）。"""
    saved_cache = dict(hongguo._POSTER_CACHE)
    saved_dir = hongguo._POSTER_DISK_DIR
    saved_lock = getattr(hongguo, "_POSTER_LOCK", None)
    hongguo._POSTER_CACHE.clear()
    hongguo._POSTER_DISK_DIR = tmp_path / "posters"
    yield
    hongguo._POSTER_CACHE.clear()
    hongguo._POSTER_CACHE.update(saved_cache)
    hongguo._POSTER_DISK_DIR = saved_dir
    if saved_lock is not None:
        hongguo._POSTER_LOCK = saved_lock


class _Resp:
    def __init__(self, content=PNG, media_type="image/png", status=200):
        self.content = content
        self.status_code = status
        self.headers = {"Content-Type": media_type}


@pytest.fixture
def source():
    """一个只用到海报代理的 HongguoSource（host 仅提供 __class__.__name__）。"""

    class _Host:
        pass

    inst = hongguo.HongguoSource(_Host())
    return inst


# --------------------------------------------------------- 1. LRU 并发安全


def test_poster_cache_is_thread_safe(source, monkeypatch):
    """并发请求同一批海报不得抛 KeyError（LRU 的读-移-淘汰必须原子化）。

    触发条件：App 海报墙一屏十几张 → 十几个并发 ``<img>`` 命中同一个匿名接口。
    无锁实现下「刚 get 到值、另一线程淘汰掉、再 move_to_end」会 KeyError。
    """
    downloads = {"n": 0}

    class _Req:
        def __init__(self, *a, **kw):
            pass

        def get_res(self, target, **kw):
            downloads["n"] += 1
            return _Resp()

    monkeypatch.setattr(hongguo, "RequestUtils", _Req)
    # 缓存压到极小，让淘汰与访问真正交错
    monkeypatch.setattr(hongguo, "_POSTER_CACHE_MAX", 4)

    urls = [f"https://p3-novel.byteimg.com/novel-pic/{i}.jpg" for i in range(12)]
    errors = []

    def hit(target):
        try:
            resp = source.poster_response(target)
            assert resp is not None
        except HTTPException:
            raise  # 白名单拒绝是预期的，不算失败
        except Exception as exc:  # noqa: BLE001 - 正是要抓这类
            errors.append(repr(exc))

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(hit, urls * 8))

    assert not errors, f"并发访问海报缓存抛了异常：{errors[:3]}"
    assert downloads["n"] >= 1


def test_poster_cache_repeat_hits_do_not_refetch(source, monkeypatch):
    """命中缓存（含落盘）不应再回源 —— 这是缓存存在的基本意义。"""
    downloads = {"n": 0}

    class _Req:
        def __init__(self, *a, **kw):
            pass

        def get_res(self, target, **kw):
            downloads["n"] += 1
            return _Resp()

    monkeypatch.setattr(hongguo, "RequestUtils", _Req)
    url = "https://p3-novel.byteimg.com/novel-pic/hit.jpg"

    source.poster_response(url)
    assert downloads["n"] == 1
    for _ in range(5):
        source.poster_response(url)
    assert downloads["n"] == 1, "重复请求同一张海报不该反复回源"


# ------------------------------------------------------- 2. 落盘原子性


def test_partial_disk_write_is_not_served(source, monkeypatch):
    """img 写了一半（meta 是上一次的旧值）时，不得当成有效缓存返回。

    触发条件：写盘过程中进程被杀 / 磁盘写满。旧实现分两次 ``write_bytes`` /
    ``write_text``，img 与 meta 不在同一事务里；img 已存在但内容不完整、
    meta 又是上一次留下的旧值时，两个 ``exists()`` 都成立 → 把半张图按 7 天
    ``Cache-Control`` 缓存给所有客户端。
    """
    downloads = {"n": 0}

    class _Req:
        def __init__(self, *a, **kw):
            pass

        def get_res(self, target, **kw):
            downloads["n"] += 1
            return _Resp()

    monkeypatch.setattr(hongguo, "RequestUtils", _Req)
    url = "https://p3-novel.byteimg.com/novel-pic/partial.jpg"

    # 先正常回源一次，落下完整的 img + meta
    source.poster_response(url)
    assert downloads["n"] == 1
    hongguo._POSTER_CACHE.clear()  # 只留磁盘缓存

    img_file, meta_file = hongguo._poster_disk_path(url)
    # 模拟「写到一半崩了」：img 被截断，meta 保留
    img_file.write_bytes(PNG[:6])
    assert img_file.exists() and meta_file.exists()

    resp = source.poster_response(url)
    body = resp.body if hasattr(resp, "body") else b""
    assert body == PNG, "把写了一半的图片当成有效缓存返回了"
    assert downloads["n"] == 2, "识别不出残缺文件时应回源重下"


def test_disk_and_memory_cache_are_consistent(source, monkeypatch):
    """落盘成功后再读内存缓存，内容必须与回源一致。"""
    downloads = {"n": 0}

    class _Req:
        def __init__(self, *a, **kw):
            pass

        def get_res(self, target, **kw):
            downloads["n"] += 1
            return _Resp()

    monkeypatch.setattr(hongguo, "RequestUtils", _Req)
    url = "https://p3-novel.byteimg.com/novel-pic/consistent.jpg"

    first = source.poster_response(url)
    assert first.body == PNG
    img_file, meta_file = hongguo._poster_disk_path(url)
    assert img_file.exists() and meta_file.exists(), "落盘缓存没写成功"
    assert meta_file.read_text("utf-8").strip() == "image/png"

    # 清掉内存，只走磁盘
    hongguo._POSTER_CACHE.clear()
    second = source.poster_response(url)
    assert second.body == PNG
    assert downloads["n"] == 1, "磁盘命中时不该再回源"


# ------------------------------------------------------ 3. 白名单不可绕过


@pytest.mark.parametrize("target", [
    "http://evil.example.com/novel-pic/a.jpg",
    "https://p3-novel.byteimg.com.evil.com/novel-pic/a.jpg",
    "https://p3-novel.byteimg.com/other-path/a.jpg",
    "file:///etc/passwd",
    "",
])
def test_poster_rejects_outside_whitelist(source, target):
    """白名单之外的地址必须 403 —— 这个接口是匿名可访问的。"""
    with pytest.raises(HTTPException) as exc:
        source.poster_response(target)
    assert exc.value.status_code in (400, 403)


def test_poster_accepts_whitelisted(source, monkeypatch):
    """白名单内的字节图床地址放行。"""
    monkeypatch.setattr(
        hongguo, "RequestUtils",
        lambda *a, **kw: type("R", (), {
            "__init__": lambda self, *x, **y: None,
            "get_res": lambda self, target, **y: _Resp(),
        })())
    resp = source.poster_response("https://p3-novel.byteimg.com/novel-pic/ok.jpg")
    assert resp.status_code == 200
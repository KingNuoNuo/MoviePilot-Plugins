"""MediaStreamPro 回归测试：缓存失效、并发安全、日志脱敏与热路径开销。

这些用例都对应**能指到具体代码行**的真实问题，全部用临时目录 + 内存桩，
不发任何网络请求、不碰真实媒体文件：

1. ``_strm_index`` 重建为空时沿用旧索引，但**必须同时刷新时间戳** ——
   不刷新的话 TTL 永远判定过期，每个请求都重扫一遍全树（实测 981 目录 /
   33559 文件，单次 0.05~0.17s）。
2. ``_strm_follow_cache`` 读取失败时沿用旧值，同样必须刷新时间戳 ——
   否则每次 ``/stream``、``/strm-check``、``/ping`` 都去读宿主配置库。
3. ``/strm-check`` 的放行根目录**只解析一次**（宿主 ``systemconfig.get`` 是
   整份配置的深拷贝，逐条路径重算等于 N×2 次深拷贝）。
4. 日志里不能出现网盘直链的签名/令牌。
5. 下载目录里的符号链接文件也要能被扫到（rclone 挂载）。
6. 配置开关要能正确解析字符串 / 数字形态的布尔值。
7. ``/strm-check`` 的路径条数要有上限。
"""

import asyncio
import os

import pytest

from app.plugins import mediastreampro as msp


# ---------------------------------------------------------------- 夹具


@pytest.fixture(autouse=True)
def reset_module_caches():
    """复位模块级缓存（进程内共享状态，用例之间必须隔离）。"""
    saved_index = dict(msp._strm_index_cache)
    saved_follow = dict(msp._strm_follow_cache)
    saved_probe = dict(msp._strm_probe_cache)
    msp._strm_index_cache.update({"dir": "", "ts": 0.0, "map": {}})
    msp._strm_follow_cache.update({"ts": 0.0, "dir": ""})
    msp._strm_probe_cache.clear()
    yield
    msp._strm_index_cache.clear()
    msp._strm_index_cache.update(saved_index)
    msp._strm_follow_cache.clear()
    msp._strm_follow_cache.update(saved_follow)
    msp._strm_probe_cache.clear()
    msp._strm_probe_cache.update(saved_probe)


@pytest.fixture
def plugin(tmp_path):
    """一个只放行临时目录、且不跟随任何插件的 MediaStreamPro 实例。"""
    media = tmp_path / "media"
    media.mkdir()
    dl = tmp_path / "dl"
    dl.mkdir()
    strm = tmp_path / "strm"
    strm.mkdir()
    (strm / "阿凡达.strm").write_text(
        "https://cdn.example.com/ava.mkv?sign=SECRETSIGN&token=SECRETTOKEN",
        encoding="utf-8")
    inst = msp.MediaStreamPro()
    inst.systemconfig.set("Directories", [
        {"library_path": str(media), "download_path": str(dl)},
    ])
    inst.init_plugin({"strm_dir": str(strm)})
    inst._tmp = tmp_path
    return inst


class _LogCapture:
    """替换 logger，只收集 warning/info 文本。"""

    def __init__(self):
        self.messages = []

    def warning(self, msg, *a, **kw):
        self.messages.append(str(msg))

    def info(self, msg, *a, **kw):
        self.messages.append(str(msg))

    def error(self, msg, *a, **kw):
        self.messages.append(str(msg))

    def debug(self, msg, *a, **kw):
        pass

    @property
    def joined(self):
        return "\n".join(self.messages)


# ------------------------------------------------- 1. STRM 索引的失效条件


def test_strm_index_reuses_stale_map_but_refreshes_timestamp(plugin):
    """沿用旧索引时必须刷新 ts，否则 TTL 恒过期 → 每次请求都全树重扫。

    触发条件：STRM 目录是 rclone / 网络挂载，重建时偶发返回空 —— 旧实现沿用
    上一次索引但**不更新时间戳**，于是下一次请求的 TTL 判定继续失败，又走一遍
    ``os.walk``。空表期间挂载恢复前，每个请求都付一次全树扫描。
    """
    strm_dir = plugin._strm_dir
    strm_file = os.path.join(strm_dir, "阿凡达.strm")

    # 先建立一份非空索引
    assert len(plugin._strm_index(strm_dir)) == 1

    # 模拟挂载抖动：.strm 暂时「消失」，重建必然扫出空表
    os.unlink(strm_file)
    # 同时把时间戳推到很久以前，保证本次一定会尝试重建
    msp._strm_index_cache["ts"] -= 10 * msp.STRM_INDEX_TTL
    stale_ts = msp._strm_index_cache["ts"]

    walks = {"n": 0}
    real_walk = os.walk

    def counting_walk(top, *a, **kw):
        walks["n"] += 1
        return real_walk(top, *a, **kw)

    msp.os.walk = counting_walk
    try:
        result = plugin._strm_index(strm_dir)
        assert len(result) == 1, "应该沿用上一次的索引"
        assert msp._strm_index_cache["ts"] > stale_ts, (
            "沿用旧索引后时间戳没有刷新 → 下个请求又会全树重扫")

        # 关键断言：紧接着再来一次，不该再触发全树扫描
        walks["n"] = 0
        assert len(plugin._strm_index(strm_dir)) == 1
        assert walks["n"] == 0, (
            f"TTL 内仍然又扫了一遍全树（{walks['n']} 次），缓存没起作用")
    finally:
        msp.os.walk = real_walk


def test_strm_index_empty_first_build_is_cached(plugin, tmp_path):
    """空目录首建也要写进缓存并带上时间戳，否则每次请求都重扫。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert plugin._strm_index(str(empty)) == {}
    assert msp._strm_index_cache["dir"] == str(empty)
    assert msp._strm_index_cache["ts"] > 0.0, (
        "空索引也要带时间戳，否则每次请求都判定缓存过期")

    walks = {"n": 0}
    real_walk = os.walk

    def counting_walk(top, *a, **kw):
        walks["n"] += 1
        return real_walk(top, *a, **kw)

    msp.os.walk = counting_walk
    try:
        plugin._strm_index(str(empty))
    finally:
        msp.os.walk = real_walk
    assert walks["n"] == 0, "TTL 内的第二次调用不该再扫盘"


# ------------------------------------------ 2. 跟随目录缓存的失效条件


def test_follow_cache_refreshes_timestamp_on_failure(plugin, monkeypatch):
    """读取 TgtoDriveSync 配置失败时沿用旧值，但**必须刷新 ts**。

    触发条件：TgtoDriveSync 未安装 / 被卸载 / 配置读取抛错。此时旧值一直保留
    （这点是对的），但时间戳不刷新 → TTL 判定每次都失败 → 每个 ``/stream``、
    ``/strm-check``、``/ping`` 请求都同步读一次宿主配置库（v3 下是一次 DB 查询）。
    """
    inst = plugin
    inst._strm_follow = True
    inst._strm_dir = "/fallback/strm"
    inst._cross_config = {"TgtoDriveSync": {"strm_dir": "/real/strm"}}
    assert inst._tgtodrive_strm_dir() == "/real/strm"

    # 让时间戳过期，再让读取持续失败（插件未安装 → get_config 返回 None）
    msp._strm_follow_cache["ts"] = 0.0
    reads = {"n": 0}

    def counting_get_config(plugin_id, key=None):
        reads["n"] += 1
        return None

    monkeypatch.setattr(inst, "get_config", counting_get_config)

    for _ in range(3):
        assert inst._tgtodrive_strm_dir() == "/real/strm", "失败时应沿用旧值"

    assert reads["n"] == 1, (
        f"读取失败后没有把失败结果也缓存住，配置库被读了 {reads['n']} 次")


# ------------------------------------------- 3. /strm-check 的热路径开销


def test_strm_check_resolves_roots_once_per_request(plugin):
    """一次 ``/strm-check`` 只解析一次放行根，不随路径条数线性放大。

    触发条件：客户端一次提交几十上百条媒体路径（App 列表页就是）。旧实现循环里
    逐条 ``self.resolve(path)``，而 ``resolve`` 内部每次都调 ``play_roots()``
    → ``media_roots()`` + ``download_roots()``，宿主 ``systemconfig.get(
    "Directories")`` 每次返回**整份配置的深拷贝**，于是 100 条路径 = 200 次
    深拷贝 + 每个根的 realpath/isdir 各来一遍。
    """
    inst = plugin
    inst._strm_check_probe = False  # 关掉起播测试，聚焦文件层判断
    media_root = inst.media_roots()[0]
    paths = [f"{media_root}/movie{i}.mkv" for i in range(40)]

    calls = {"n": 0}
    inner = inst.systemconfig

    class _Counter:
        def get(self, key, default=None):
            calls["n"] += 1
            return inner.get(key, default)

    inst.systemconfig = _Counter()

    result, probe = inst._check_paths_sync(paths)

    assert len(result) == 40, "40 条路径都该有结论"
    assert result[f"{media_root}/movie0.mkv"] is False
    assert calls["n"] <= 2, (
        f"40 条路径却读了 {calls['n']} 次宿主目录配置（放行根应只解析一次）")


# ------------------------------------------------------ 4. 日志脱敏


def test_resolve_strm_url_failure_does_not_log_signed_url(plugin, monkeypatch):
    """直链解析失败时日志不得包含签名/令牌。

    ``requests`` 的异常 message 形如 ``HTTPSConnectionPool(host=...):
    Max retries exceeded with url: /path?sign=xxx&token=yyy`` —— TgtoDrive
    生成的直链把签名就放在 query 里，直接把 ``{error}`` 打进日志等于把网盘
    访问凭据写进宿主日志。
    """
    inst = plugin

    def boom(*a, **kw):
        raise RuntimeError(
            "HTTPSConnectionPool(host='cdn.example.com'): "
            "Max retries exceeded with url: /ava.mkv?sign=SECRETSIGN&token=SECRETTOKEN")

    monkeypatch.setattr(msp.requests, "request", boom)
    cap = _LogCapture()
    monkeypatch.setattr(msp, "logger", cap)

    assert inst._resolve_strm_url(
        "https://cdn.example.com/ava.mkv?sign=SECRETSIGN&token=SECRETTOKEN") is None

    assert "SECRETSIGN" not in cap.joined, "日志里出现了网盘直链签名"
    assert "SECRETTOKEN" not in cap.joined, "日志里出现了网盘直链令牌"


def test_stream_strm_fallback_does_not_log_signed_url(plugin, monkeypatch):
    """退回原始直链时的 warning 也不能带上完整 query。"""

    class _Req:
        def __init__(self, *a, **kw):
            pass

        def request(self, *a, **kw):
            raise RuntimeError("network down")

    monkeypatch.setattr(msp, "requests", _Req)
    inst = plugin
    inst._strm_follow_redirect = True
    cap = _LogCapture()
    monkeypatch.setattr(msp, "logger", cap)

    strm_file = os.path.join(inst._strm_dir, "阿凡达.strm")
    inst._stream_strm(None, strm_file)

    assert "SECRETSIGN" not in cap.joined, "退回直链的日志泄露了签名"
    assert "SECRETTOKEN" not in cap.joined, "退回直链的日志泄露了令牌"


# --------------------------------------------- 5. 下载目录扫描的符号链接


def test_downloads_includes_symlinked_files(plugin, tmp_path):
    """下载目录里的符号链接文件也要能被扫到。

    rclone / 某些 NAS 的挂载会把下载目录里的文件暴露成符号链接；旧实现用
    ``is_file(follow_symlinks=False)`` 过滤，它们**根本不会出现在结果里**。
    客户端按「文件不存在 → 显示已删除」处理，于是刚下完的片子显示已删除。

    Windows 上非管理员建不了符号链接，所以这里用桩目录项直接校验判定参数：
    文件必须 ``follow_symlinks=True``，目录必须仍是 ``False``（避免挂载里的
    环路导致无限递归）。真实符号链接的用例在能建链接的系统上自动跑。
    """
    dl = tmp_path / "dl"
    real = dl / "real.mp4"
    real.write_bytes(b"data")
    try:
        os.symlink(real, dl / "link.mp4")
    except (OSError, NotImplementedError, AttributeError):
        resp = plugin.downloads()
        assert resp["success"] is True
        pytest.skip("当前环境不支持创建符号链接，改由桩目录项覆盖")

    resp = plugin.downloads()
    names = {f["name"] for f in resp["data"]["files"]}
    assert "link.mp4" in names, "符号链接的视频没被扫到，客户端会误报「已删除」"


def test_scan_videos_symlink_semantics(monkeypatch, tmp_path):
    """``_scan_videos`` 的判定口径：文件跟随符号链接、目录不跟随。"""

    class _Entry:
        def __init__(self, name, is_dir):
            self.name = name
            self._is_dir = is_dir
            self.path = str(tmp_path / name)

        def is_dir(self, follow_symlinks=True):
            assert follow_symlinks is False, (
                "目录判定必须不跟随符号链接，否则挂载里的环路会无限递归")
            return self._is_dir

        def is_file(self, follow_symlinks=True):
            assert follow_symlinks is True, (
                "文件判定必须跟随符号链接，否则 rclone 挂载里的文件全被漏掉")
            return not self._is_dir

        def stat(self, follow_symlinks=True):
            assert follow_symlinks is True
            return type("S", (), {"st_size": 100, "st_mtime": 1700000000})()

    class _Scandir:
        def __init__(self, entries):
            self._entries = entries

        def __enter__(self):
            return iter(self._entries)

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(msp.os, "scandir",
                        lambda d: _Scandir([_Entry("link.mp4", False)]))
    inst = msp.MediaStreamPro()
    out = []
    inst._scan_videos(str(tmp_path), 0, out)
    assert [o["name"] for o in out] == ["link.mp4"]


# ------------------------------------------------- 6. 配置布尔解析一致性


def test_switch_accepts_string_false(plugin):
    """配置里的 "false" / 0 必须解析成关，不能当成开。

    触发条件：配置被手工改库，或从早期版本迁移上来（这几个键是后加的）。
    ``bool("false")`` 是 True，会把「跟随 TgtoDrive」意外打开；
    ``0 is not False`` 是 True，会让「替客户端解析直链」关不掉。
    """
    inst = plugin
    inst.init_plugin({"strm_follow_tgtodrive": "false",
                      "strm_follow_redirect": "false",
                      "strm_check_probe": 0})
    assert inst._strm_follow is False
    assert inst._strm_follow_redirect is False
    assert inst._strm_check_probe is False

    inst.init_plugin({"strm_follow_tgtodrive": True,
                      "strm_follow_redirect": 1,
                      "strm_check_probe": "true"})
    assert inst._strm_follow is True
    assert inst._strm_follow_redirect is True
    assert inst._strm_check_probe is True


# ------------------------------------------------------ 7. /strm-check 上限


def test_strm_check_caps_path_count(plugin):
    """一次提交的路径条数要有上限，避免几百条把单 worker 宿主拖住。"""

    class _Req:
        def __init__(self, *a, **kw):
            pass

        async def json(self):
            return {"paths": [f"/x/movie{i}.mkv" for i in range(5000)]}

    seen = {}

    def spy(raw):
        seen["count"] = len(raw)
        return {}, {}

    plugin._check_paths_sync = spy
    asyncio.run(plugin.strm_check(_Req()))
    assert seen["count"] <= msp.STRM_CHECK_MAX_PATHS, (
        f"提交 {seen['count']} 条路径没有被截断")
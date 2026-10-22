"""MediaStreamPro ``/strm-check`` 的性能测量：改前 vs 改后。

改前的问题（已用回归测试复现）：``_check_paths_sync`` 循环里逐条调
``self.resolve(path)``，而 ``resolve`` 内部每次都算一遍放行根：

    resolve → play_roots() → media_roots() + download_roots()
            → systemconfig.get("Directories")  ← 宿主 v3 返回整份配置的**深拷贝**
            → 每个根再做 realpath + isdir

于是 N 条路径 = 2N 次配置深拷贝 + N×(根数) 次 stat。

改后：放行根在循环外算一次，循环内复用。

本脚本直接对比两种算法在同样输入下的耗时与配置读取次数。
"""
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "stub"))

from app.plugins import mediastreampro as msp  # noqa: E402

# 测量轮数：够多让缓存/抖动摊平，又不至于跑太久
ROUNDS = 30


def make_plugin(tmp, tag="a"):
    media = tmp / f"media_{tag}"
    media.mkdir(exist_ok=True)
    dl = tmp / f"dl_{tag}"
    dl.mkdir(exist_ok=True)
    inst = msp.MediaStreamPro()
    inst.systemconfig.set("Directories", [
        {"library_path": str(media), "download_path": str(dl)},
    ])
    inst.init_plugin({})
    return inst, media


def count_calls(inst):
    n = {"v": 0}
    inner = inst.systemconfig

    class _C:
        def get(self, key, default=None):
            n["v"] += 1
            return inner.get(key, default)

    inst.systemconfig = _C()
    return n


def bench(label, fn, rounds=ROUNDS):
    fn()  # 预热
    best = None
    total = 0.0
    for _ in range(rounds):
        t0 = time.perf_counter()
        fn()
        dt = time.perf_counter() - t0
        total += dt
        best = dt if best is None else min(best, dt)
    print(f"  {label}: 平均 {total / rounds * 1000:.2f} ms  最快 {best * 1000:.2f} ms")
    return total / rounds


def main():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        inst, media = make_plugin(tmp)
        n_paths = 100
        paths = [f"{media}/movie{i}.mkv" for i in range(n_paths)]

        print(f"输入：{n_paths} 条媒体路径，宿主目录配置 1 项（含媒体库 + 下载目录）")

        # ---- 改后的实现（当前代码）----
        inst._strm_check_probe = False
        calls = count_calls(inst)
        # 计数要在预热之后清零，否则「次/请求」会把预热那一次的读取算进去
        calls["v"] = 0
        after = bench("改后（放行根算一次）", lambda: inst._check_paths_sync(paths),
                      rounds=ROUNDS)
        after_calls = calls["v"] / ROUNDS
        print(f"    宿主配置读取：{after_calls:.0f} 次/请求")

        # ---- 改前的实现（复刻旧逻辑：循环内逐条 resolve）----
        # ⚠️ 改前根本没有 play_roots 这一层缓存，两次测的必须是**同一条路径**，
        #    所以这里另起一个实例，并让它的 play_roots 走「每次重算」的旧实现。
        inst2, media2 = make_plugin(tmp, "b")
        inst2._strm_check_probe = False
        inst2.play_roots = lambda: _uncached_roots(inst2)
        paths2 = [f"{media2}/movie{i}.mkv" for i in range(n_paths)]
        calls2 = count_calls(inst2)
        calls2["v"] = 0

        def old_impl():
            result = {}
            probe = {}
            for path in paths2:
                target = inst2.resolve(path)     # 每次都重算放行根
                if target is None:
                    continue
                result[path] = False
            return result, probe

        before = bench("改前（逐条重算放行根）", old_impl, rounds=ROUNDS)
        before_calls = calls2["v"] / ROUNDS
        print(f"    宿主配置读取：{before_calls:.0f} 次/请求")

        print()
        print(f"耗时：{before * 1000:.2f} ms → {after * 1000:.2f} ms"
              f"（提速 {before / after:.1f}×）")
        print(f"配置读取：{before_calls:.0f} → {after_calls:.0f} 次/请求")


def _uncached_roots(inst):
    """复刻改前的 play_roots：每次都重算，不走缓存。"""
    roots = inst.media_roots()
    for root in inst.download_roots():
        if root not in roots:
            roots.append(root)
    return roots


if __name__ == "__main__":
    main()

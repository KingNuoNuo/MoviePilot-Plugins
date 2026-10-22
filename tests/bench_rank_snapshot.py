"""GlobalRankPro /rank?source=all 的耗时基线测量（改代码前后各跑一次）。

用内存桩喂一份规模接近生产的快照，测：
  1. 一次 /rank?source=all 的端到端耗时；
  2. 其中读快照（get_data）的次数 —— 快照是 100~250KB 的 JSON，
     每次读都要反序列化，重复读就是纯浪费。

不碰网络、不碰真实插件数据。
"""
import sys
import time
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "stub"))

from app.plugins.globalrankpro import GlobalRankPro  # noqa: E402


def _mk_items(n, prefix, **extra):
    return [
        dict({
            "rank": i + 1,
            "title": f"{prefix}影片{i}",
            "title_cn": f"{prefix}影片{i}",
            "media_source": "tmdb",
            "media_id": str(1000 + i),
            "media_type": "movie",
            "poster": f"https://img.example.com/{prefix}{i}.jpg",
            "overview": "剧情简介" * 20,
            "year": "2026",
            "vote_average": 7.5,
        }, **extra)
        for i in range(n)
    ]


def build_snapshot():
    """构造接近现场规模的快照：猫眼 11 榜 + Netflix 14 榜 + 红果 4 榜。"""
    maoyan_boards = []
    for bt in ("movie", "web-movie", "web-all", "zongyi", "total", "year"):
        maoyan_boards.append({
            "board": f"猫眼{bt}", "type": bt, "type_name": bt,
            "platform": "", "platform_type": "", "show_date": "2026-10-22",
            "url": "https://piaofang.maoyan.com/", "count": 10,
            "updated_at": "2026-10-22 09:00:00",
            "items": _mk_items(10, bt),
        })
    for pt in ("3", "2", "7", "1", "20"):
        maoyan_boards.append({
            "board": f"猫眼web-all（平台{pt}）", "type": "web-all",
            "type_name": "剧集综合", "platform": f"平台{pt}",
            "platform_type": pt, "show_date": "2026-10-22",
            "url": "https://piaofang.maoyan.com/", "count": 10,
            "updated_at": "2026-10-22 09:00:00",
            "items": _mk_items(10, f"pt{pt}"),
        })
    netflix_boards = []
    for region in ("", "us", "jp", "kr", "hk", "tw", "sg"):
        for kind in ("film", "tv"):
            netflix_boards.append({
                "board": f"Netflix {region or '全球'} · {kind}",
                "type": "global" if not region else "region",
                "region": region, "region_name": region or "全球",
                "kind": kind, "category": kind, "category_name": kind,
                "week": "2026-10-20", "url": "https://www.netflix.com/",
                "count": 10, "updated_at": "2026-10-22 09:00:00",
                "items": _mk_items(10, f"nf{region}{kind}", season_title="S1"),
            })
    hongguo_boards = []
    for code in ("hot", "real", "comic", "ai"):
        hongguo_boards.append({
            "board": f"红果{code}", "type": code, "type_name": code,
            "category": code, "category_name": code,
            "url": "https://hongguoduanju.com/", "count": 20,
            "updated_at": "2026-10-22 09:00:00",
            "items": _mk_items(20, code, series_id=code, episode_cnt=80),
        })
    return {
        "maoyan": {"updated_at": "2026-10-22 09:00:00",
                   "total": 110, "boards": maoyan_boards},
        "netflix": {"updated_at": "2026-10-22 09:00:00",
                    "week": "2026-10-20", "total": 140,
                    "boards": netflix_boards},
        "hongguo": {"updated_at": "2026-10-22 09:00:00",
                    "total": 80, "boards": hongguo_boards},
    }


def main():
    snap = build_snapshot()
    sizes = {k: len(json.dumps(v, ensure_ascii=False)) for k, v in snap.items()}
    print("快照体积（字节）：", sizes, " 合计", sum(sizes.values()))

    reads = {"n": 0}

    plugin = GlobalRankPro()
    store = {f"{k}_snapshot": v for k, v in snap.items()}

    def fake_get(key, default=None):
        reads["n"] += 1
        return store.get(key, default)

    plugin.get_data = fake_get
    plugin.save_data = lambda key, value: store.__setitem__(key, value)
    plugin.config = {"maoyan_enabled": True, "netflix_enabled": True,
                     "hongguo_enabled": True}
    plugin._enabled = True
    for sid, src in plugin.sources.items():
        src.apply_config({"enabled": True})

    # 预热：把 2s 快照 memo 填上，单独测「纯组装」成本
    for src in plugin.sources.values():
        src.boards()

    rounds = 20
    reads["n"] = 0
    best, total = None, 0.0
    for _ in range(rounds):
        t0 = time.perf_counter()
        resp = plugin.get_rank_snapshot(source="all")
        dt = time.perf_counter() - t0
        total += dt
        best = dt if best is None else min(best, dt)
    boards = len(resp.data.boards)
    items = resp.data.total
    print(f"/rank?source=all：{rounds} 轮 平均 {total / rounds * 1000:.1f} ms"
          f"  最快 {best * 1000:.1f} ms  → {boards} 个榜单 / {items} 条")
    print(f"  get_data 调用 {reads['n']} 次（{rounds} 轮请求）"
          f" = {reads['n'] / rounds:.1f} 次/请求")


if __name__ == "__main__":
    main()
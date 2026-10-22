#!/usr/bin/env python3
"""重建 ``dist/`` 下的安装包。

产物两类：
- **单插件 zip**（``dist/<插件目录名>.zip``）：只含一个插件目录，解压后把目录
  放进 MoviePilot 插件目录即可手动安装；
- **总包**（``dist/MoviePilot-Plugins.zip``）：整仓一份，供一次性导入。

用法：
    python scripts/build_dist.py

⚠️ 总包的文件清单写死在 [TOTAL_ZIP_ORDER] 里：v3 插件按目录整体展开
（新增模块文件不会因为清单漏写而进不了包），v1/v2 插件与公共资源单独列出。
新增插件目录时不需要改这里 —— 只有新增**非插件目录**的资源才要补进清单。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DIST_DIR = REPO / "dist"
TOTAL_ZIP_NAME = "MoviePilot-Plugins.zip"

TOTAL_ZIP_ORDER = (
    ".gitignore",
    "package.json",
    "package.v3.json",
    "README.md",
) + tuple(
    "icons/{0}".format(p.name)
    for p in sorted((REPO / "icons").glob("*.png"))
) + tuple(
    "plugins/tgtodrivesync/{0}".format(p.name)
    for p in sorted((REPO / "plugins" / "tgtodrivesync").iterdir())
    if p.is_file()
) + tuple(
    # v3 插件全部按目录展开：新增模块（page.py、app_promo.py、music.py 这类）
    # 不会再因为清单漏写而进不了总包 —— 之前 hongguo.py、mediastreampro 都是这么漏的
    "plugins.v3/{0}/{1}".format(plugin.name, p.name)
    for plugin in sorted((REPO / "plugins.v3").iterdir())
    if plugin.is_dir() and not plugin.name.startswith("_")
    for p in sorted(plugin.iterdir())
    if p.is_file() and p.suffix in (".py", ".md")
) + (
    # 推荐卡的 logo 源文件：插件里已内嵌 data URI，放进总包只为可审计/可重生成
    "assets/moviepilot-logo.svg",
)


def add_dir(zf: zipfile.ZipFile, src_dir: Path, prefix: str) -> None:
    """把目录里的文件（跳过子目录与 ``__pycache__``）写进 zip。"""
    for p in sorted(src_dir.rglob("*")):
        if p.is_dir() or "__pycache__" in p.parts:
            continue
        zf.write(p, f"{prefix}/{p.relative_to(src_dir).as_posix()}")


def rebuild_dist() -> None:
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    # 每个 v3 插件各自打一个单插件 zip（供手动下载安装）
    for plugin_dir in sorted((REPO / "plugins.v3").iterdir()):
        if not plugin_dir.is_dir() or plugin_dir.name.startswith("_"):
            continue
        out = DIST_DIR / f"{plugin_dir.name}.zip"
        out.unlink(missing_ok=True)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            add_dir(zf, plugin_dir, plugin_dir.name)

    out = DIST_DIR / TOTAL_ZIP_NAME
    out.unlink(missing_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in TOTAL_ZIP_ORDER:
            path = REPO / rel
            if path.exists():
                zf.write(path, f"MoviePilot-Plugins/{rel}")
    print(f"dist/ 已重建：各插件 zip 与 {TOTAL_ZIP_NAME}")


if __name__ == "__main__":
    rebuild_dist()

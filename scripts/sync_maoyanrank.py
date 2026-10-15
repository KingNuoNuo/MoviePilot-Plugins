#!/usr/bin/env python3
"""把上游 MaoyanRank 插件的改动同步进本仓库的独立发行版。

上游：jxxghp/moviepilot-plugins 的 plugins.v3/maoyanrank/
基线：.upstream/maoyanrank/（上次成功同步时保存的上游原文）
本仓库：plugins.v3/maoyanrank/

合并方式：三方合并（git merge-file）
    base   = .upstream/maoyanrank/<file>   上次同步时的上游原文
    ours   = plugins.v3/maoyanrank/<file>  本仓库的独立发行版
    theirs = 上游最新 <file>

  - 无冲突：写回合并结果 → patch 版本 +1 → 同步 package.v3.json 与两处 README 的版本号
            → 重建 dist 压缩包 → 推进上游基线。
  - 有冲突：只把带冲突标记的内容写进工作区，不递增版本、不重建 dist、不推进基线，
            交由 PR 人工解决；解决并合并后，下一次运行会自动完成收尾。

用法：
    python scripts/sync_maoyanrank.py            # 正常同步
    python scripts/sync_maoyanrank.py --check    # 只检测上游是否有更新（不写任何文件）
    python scripts/sync_maoyanrank.py --force    # 跳过「无更新」判断，强制执行一次合并

供 GitHub Actions 读取的输出：changed / conflict / new_version / upstream_version
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

UPSTREAM_REPO = "jxxghp/moviepilot-plugins"
UPSTREAM_DIR = "plugins.v3/maoyanrank"
# 可用 MAOYAN_UPSTREAM_BASE 覆盖（本地验证用，例如指向 file:// 假上游目录）
UPSTREAM_RAW = os.environ.get("MAOYAN_UPSTREAM_BASE") or (
    f"https://raw.githubusercontent.com/{UPSTREAM_REPO}/main/{UPSTREAM_DIR}"
)

PLUGIN_ID = "maoyanrank"
PLUGIN_KEY = "MaoyanRank"  # 索引里的键与插件类名一致
LOCAL_DIR = REPO / "plugins.v3" / PLUGIN_ID
BASE_DIR = REPO / ".upstream" / PLUGIN_ID
META_FILE = BASE_DIR / "meta.json"

SYNC_FILES = ("__init__.py", "README.md")

INDEX_V3 = REPO / "package.v3.json"
MAIN_README = REPO / "README.md"
PLUGIN_README = LOCAL_DIR / "README.md"
DIST_DIR = REPO / "dist"

VERSION_IN_CODE = re.compile(r'(plugin_version\s*=\s*")(\d+)\.(\d+)\.(\d+)(")')
VERSION_IN_PLUGIN_README = re.compile(r"(版本 v)\d+\.\d+\.\d+")
UPSTREAM_VERSION_IN_PLUGIN_README = re.compile(r"(上游版本：\*\*v)\d+\.\d+\.\d+(\*\*)")
VERSION_CELL_IN_MAIN_README = re.compile(r"(\|\s*v)\d+\.\d+\.\d+(\s*\|)")

TOTAL_ZIP_ORDER = (
    ".gitignore",
    "package.json",
    "package.v3.json",
    "README.md",
    "icons/maoyan.jpg",
    "plugins/tgtodrivesync/__init__.py",
    "plugins/tgtodrivesync/README.md",
    "plugins.v3/maoyanrank/__init__.py",
    "plugins.v3/maoyanrank/README.md",
)

TOTAL_ZIP_NAME = "MoviePilot-Plugins.zip"


def log(msg: str) -> None:
    print(msg, flush=True)


def read_text(path: Path) -> str:
    """统一按 LF 读取，避免 CRLF/LF 差异被当成内容改动。"""
    text = path.read_text(encoding="utf-8")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def fetch(path: str) -> str:
    """读取上游文件；MAOYAN_UPSTREAM_BASE 也可指向本地目录（本地验证用）。"""
    if "://" in UPSTREAM_RAW:
        url = f"{UPSTREAM_RAW}/{path}"
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.read().decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
    return read_text(Path(UPSTREAM_RAW) / path)


def normalize_version_line(text: str, version: str) -> str:
    """把上游自报的 plugin_version 换成本仓库版本。

    上游每次发版都会改这一行，而本仓库的版本号是独立编号的——若不归一，
    每一次同步都会在这一行产生一个毫无意义的冲突。
    """
    return VERSION_IN_CODE.sub(lambda m: f"{m.group(1)}{version}{m.group(5)}", text, count=1)


def merge3(ours: Path, base: Path, theirs: Path, tmp: Path, name: str):
    """三方合并，返回 (合并文本, 冲突数)。三个输入都按 LF 参与比较。"""
    ours_lf = tmp / f"ours-{name}"
    write_text(ours_lf, read_text(ours))
    rc = subprocess.run(
        [
            "git", "merge-file", "-p",
            "-L", "本仓库（独立发行版）",
            "-L", "上游基线",
            "-L", "上游最新",
            str(ours_lf), str(base), str(theirs),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(tmp),
    )
    if rc.returncode < 0 or rc.returncode == 255:
        raise RuntimeError(f"{name} 三方合并失败：{rc.stderr.strip() or rc.returncode}")
    return rc.stdout, rc.returncode


def bump_patch(version: str) -> str:
    major, minor, patch = version.split(".")
    return f"{major}.{minor}.{int(patch) + 1}"


def find_version(text: str) -> str:
    m = VERSION_IN_CODE.search(text)
    if not m:
        raise RuntimeError("未能在插件源码中找到 plugin_version")
    return m.group(2) + "." + m.group(3) + "." + m.group(4)


def update_index(new_version: str, upstream_version: str, conflict: bool) -> None:
    data = json.loads(INDEX_V3.read_text(encoding="utf-8"))
    entry = data[PLUGIN_KEY]
    entry["version"] = new_version
    note = f"同步上游 MaoyanRank {upstream_version} 的改动（三方合并）"
    if conflict:
        note += "，存在冲突需人工解决"
    history = {f"v{new_version}": note + "。"}
    history.update(entry.get("history") or {})
    entry["history"] = history
    write_text(INDEX_V3, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    log(f"  package.v3.json → version={new_version}，history 新增 v{new_version}")


def update_readmes(new_version: str, upstream_version: str) -> None:
    text = read_text(PLUGIN_README)
    new_text, n = VERSION_IN_PLUGIN_README.subn(rf"\g<1>{new_version}", text, count=1)
    if n:
        text = new_text
        log(f"  插件 README 版本号 → v{new_version}")
    text, n = UPSTREAM_VERSION_IN_PLUGIN_README.subn(rf"\g<1>{upstream_version}\g<2>", text, count=1)
    if n:
        log(f"  插件 README 记录的上游版本 → v{upstream_version}")
    if text != read_text(PLUGIN_README):
        write_text(PLUGIN_README, text)

    lines = read_text(MAIN_README).split("\n")
    touched = 0
    for i, line in enumerate(lines):
        if PLUGIN_ID not in line.lower():
            continue
        new_line, n = VERSION_CELL_IN_MAIN_README.subn(rf"\g<1>{new_version}\g<2>", line)
        if n:
            lines[i] = new_line
            touched += n
    if touched:
        write_text(MAIN_README, "\n".join(lines))
        log(f"  主 README 插件一览表版本号 → v{new_version}（{touched} 处）")


def rebuild_dist() -> None:
    def add_dir(zf, src_dir: Path, prefix: str) -> None:
        for p in sorted(src_dir.rglob("*")):
            if p.is_dir() or "__pycache__" in p.parts:
                continue
            zf.write(p, f"{prefix}/{p.relative_to(src_dir).as_posix()}")

    out = DIST_DIR / f"{PLUGIN_ID}.zip"
    out.unlink(missing_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        add_dir(zf, LOCAL_DIR, PLUGIN_ID)

    out = DIST_DIR / TOTAL_ZIP_NAME
    out.unlink(missing_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in TOTAL_ZIP_ORDER:
            path = REPO / rel
            if path.exists():
                zf.write(path, f"MoviePilot-Plugins/{rel}")
    log(f"  dist/ 已重建：{PLUGIN_ID}.zip、{TOTAL_ZIP_NAME}")


def write_baseline(upstream: dict, upstream_version: str) -> None:
    for name, text in upstream.items():
        write_text(BASE_DIR / name, text)
    write_text(
        META_FILE,
        json.dumps(
            {
                "upstream_repo": UPSTREAM_REPO,
                "upstream_path": UPSTREAM_DIR,
                "upstream_version": upstream_version,
                "synced_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "note": "上次成功同步时的上游原文快照，用作三方合并的 base；请勿手工修改。",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    log(f"  上游基线已推进到 v{upstream_version}")


def set_output(**values) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        for key, value in values.items():
            log(f"  [output] {key}={value}")
        return
    with open(path, "a", encoding="utf-8") as fh:
        for key, value in values.items():
            fh.write(f"{key}={value}\n")


def write_report(text: str) -> None:
    path = os.environ.get("SYNC_REPORT_PATH")
    if path:
        Path(path).write_text(text, encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="同步上游 MaoyanRank 改动")
    parser.add_argument("--check", action="store_true", help="只检测上游是否有更新，不写任何文件")
    parser.add_argument("--force", action="store_true", help="跳过「无更新」判断，强制执行一次")
    args = parser.parse_args()

    missing = [str(p) for p in (LOCAL_DIR / n for n in SYNC_FILES) if not p.exists()]
    if missing:
        log(f"缺少本仓库文件：{missing}")
        return 1

    log("拉取上游原文…")
    try:
        upstream = {name: fetch(name) for name in SYNC_FILES}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log(f"拉取上游失败（跳过本次同步）：{exc}")
        set_output(changed="false", conflict="false")
        return 0

    upstream_version = find_version(upstream["__init__.py"])

    changed_files = []
    for name in SYNC_FILES:
        base_path = BASE_DIR / name
        base_text = read_text(base_path) if base_path.exists() else ""
        if upstream[name] != base_text:
            changed_files.append(name)

    log(f"上游版本 v{upstream_version}；检测到变化的文件：{changed_files or '无'}")
    if not changed_files and not args.force:
        log("上游无更新，结束。")
        set_output(changed="false", conflict="false", upstream_version=upstream_version)
        return 0

    if args.check:
        log("--check 模式，不写文件。")
        set_output(changed="true", conflict="false", upstream_version=upstream_version)
        return 0

    current_version = find_version(read_text(LOCAL_DIR / "__init__.py"))

    tmp = Path(tempfile.mkdtemp(prefix="maoyan-sync-"))
    try:
        merged = {}
        total_conflicts = 0
        conflicted = []
        for name in SYNC_FILES:
            ours = LOCAL_DIR / name
            base_path = BASE_DIR / name
            if not base_path.exists():
                raise RuntimeError(f"缺少上游基线文件：{base_path}")
            theirs_path = tmp / f"theirs-{name}"
            write_text(theirs_path, normalize_version_line(upstream[name], current_version))
            text, conflicts = merge3(ours, base_path, theirs_path, tmp, name)
            merged[name] = text
            total_conflicts += conflicts
            if conflicts:
                conflicted.append(f"{name}（{conflicts} 处）")
            log(f"  合并 {name}：" + ("无冲突" if not conflicts else f"{conflicts} 处冲突"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    conflict = total_conflicts > 0

    for name, text in merged.items():
        write_text(LOCAL_DIR / name, text)

    if conflict:
        log(f"存在冲突：{'、'.join(conflicted)}")
        log("不递增版本、不重建 dist、不推进基线，交由 PR 人工解决。")
        set_output(
            changed="true",
            conflict="true",
            upstream_version=upstream_version,
            new_version="",
        )
        write_report(
            "\n".join(
                [
                    "## 上游 MaoyanRank 同步：**存在冲突，需人工解决**",
                    "",
                    f"上游版本：`{upstream_version}`",
                    "",
                    "冲突文件：",
                    *[f"- `{item}`" for item in conflicted],
                    "",
                    "本分支已写入带冲突标记的合并结果（`<<<<<<<` / `=======` / `>>>>>>>`）。",
                    "请在 PR 中解决冲突后合并；合并后下一次定时运行会自动补齐版本号与打包产物。",
                ]
            )
        )
        return 0

    new_version = bump_patch(current_version)
    log(f"版本 {current_version} → {new_version}")

    bumped, n = VERSION_IN_CODE.subn(
        lambda m: f"{m.group(1)}{new_version}{m.group(5)}",
        read_text(LOCAL_DIR / "__init__.py"),
        count=1,
    )
    if not n:
        raise RuntimeError("版本号替换失败")
    write_text(LOCAL_DIR / "__init__.py", bumped)

    update_index(new_version, upstream_version, conflict=False)
    update_readmes(new_version, upstream_version)
    rebuild_dist()
    write_baseline(upstream, upstream_version)

    set_output(
        changed="true",
        conflict="false",
        upstream_version=upstream_version,
        new_version=new_version,
    )
    write_report(
        "\n".join(
            [
                "## 上游 MaoyanRank 同步",
                "",
                f"- 上游版本：`{upstream_version}`",
                f"- 本仓库版本：`{current_version}` → `{new_version}`",
                f"- 三方合并文件：{'、'.join(changed_files)}",
                "- 合并无冲突，已递增 patch 版本、更新索引与 README 版本号、重建 dist 打包产物",
            ]
        )
    )
    log("同步完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

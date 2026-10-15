"""scripts/sync_maoyanrank.py 的单元测试。

只测纯逻辑与受控的三方合并，不访问网络、不改动仓库文件。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "sync_maoyanrank.py"


def load_module():
    spec = importlib.util.spec_from_file_location("sync_maoyanrank", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def sync():
    return load_module()


class TestVersionHelpers:
    def test_bump_patch(self, sync):
        assert sync.bump_patch("1.0.0") == "1.0.1"
        assert sync.bump_patch("1.0.9") == "1.0.10"
        assert sync.bump_patch("2.3.4") == "2.3.5"

    def test_find_version(self, sync):
        text = '    # 插件版本\n    plugin_version = "1.2.3"\n'
        assert sync.find_version(text) == "1.2.3"

    def test_find_version_missing(self, sync):
        with pytest.raises(RuntimeError):
            sync.find_version("no version here")

    def test_normalize_version_line(self, sync):
        upstream = 'plugin_version = "4.1.4"'
        assert sync.normalize_version_line(upstream, "1.0.0") == 'plugin_version = "1.0.0"'
        # 只替换第一处，且不动其它行
        multi = 'x = "plugin_version = \\"9.9.9\\""\nplugin_version = "4.1.4"\n'
        assert 'plugin_version = "1.0.0"' in sync.normalize_version_line(multi, "1.0.0")


class TestReadmePatterns:
    def test_plugin_readme_version(self, sync):
        text = "> [← 返回插件一览](x.md) · 版本 v1.0.0 · 目录 `plugins.v3/maoyanrank`"
        assert sync.VERSION_IN_PLUGIN_README.sub(r"\g<1>1.0.1", text, count=1).endswith("`plugins.v3/maoyanrank`")
        assert "版本 v1.0.1" in sync.VERSION_IN_PLUGIN_README.sub(r"\g<1>1.0.1", text, count=1)

    def test_plugin_readme_upstream_version(self, sync):
        text = "- 上游版本：**v4.1.4**；本仓库基于该版本改造为**独立发行版**"
        out = sync.UPSTREAM_VERSION_IN_PLUGIN_README.sub(r"\g<1>4.1.5\g<2>", text, count=1)
        assert "上游版本：**v4.1.5**" in out

    def test_main_readme_version_cell_only_matches_table_cell(self, sync):
        row = "| **[猫眼榜单订阅](plugins.v3/maoyanrank/README.md)** | v1.0.0 | 简介 | → |"
        out = sync.VERSION_CELL_IN_MAIN_README.sub(r"\g<1>1.0.1\g<2>", row)
        assert "| v1.0.1 |" in out

    def test_main_readme_ignores_prose_mention(self, sync):
        # 「基于上游 v4.1.4 改造」这类叙述不该被当成版本号单元格改写
        prose = "> 的同名插件，基于上游 v4.1.4 改造为独立发行版，详见 [插件说明](plugins.v3/maoyanrank/README.md#来源)。"
        out, n = sync.VERSION_CELL_IN_MAIN_README.subn(r"\g<1>1.0.1\g<2>", prose)
        assert n == 0 and out == prose


class TestMerge3:
    """三方合并：两处改动相隔足够远时应干净合并，落在同一区域才判冲突。"""

    BASE = "".join(f"line{i}\n" for i in range(1, 13))
    OURS = BASE.replace("line3\n", "OUR FEATURE\n")
    THEIRS = BASE.replace("line10\n", "UPSTREAM FIX\n")

    def test_clean_merge_applies_upstream_change(self, sync, tmp_path):
        ours, base, theirs = (tmp_path / n for n in ("ours.py", "base.py", "theirs.py"))
        ours.write_text(self.OURS, encoding="utf-8", newline="\n")
        base.write_text(self.BASE, encoding="utf-8", newline="\n")
        theirs.write_text(self.THEIRS, encoding="utf-8", newline="\n")
        text, conflicts = sync.merge3(ours, base, theirs, tmp_path, "ours.py")
        assert conflicts == 0
        assert "OUR FEATURE" in text  # 我们的改动保留
        assert "UPSTREAM FIX" in text  # 上游改动套用进来

    def test_crlf_ours_does_not_break_merge(self, sync, tmp_path):
        """本仓库工作区是 CRLF，合并前必须归一，否则整文件会被判为冲突。"""
        ours, base, theirs = (tmp_path / n for n in ("ours.py", "base.py", "theirs.py"))
        ours.write_bytes(self.OURS.replace("\n", "\r\n").encode())
        base.write_text(self.BASE, encoding="utf-8", newline="\n")
        theirs.write_text(self.THEIRS, encoding="utf-8", newline="\n")
        text, conflicts = sync.merge3(ours, base, theirs, tmp_path, "ours.py")
        assert conflicts == 0
        assert "OUR FEATURE" in text and "UPSTREAM FIX" in text

    def test_conflict_is_reported(self, sync, tmp_path):
        ours, base, theirs = (tmp_path / n for n in ("ours.py", "base.py", "theirs.py"))
        # 双方改同一行 → 真冲突
        ours.write_text(self.BASE.replace("line3\n", "OURS\n"), encoding="utf-8", newline="\n")
        base.write_text(self.BASE, encoding="utf-8", newline="\n")
        theirs.write_text(self.BASE.replace("line3\n", "THEIRS\n"), encoding="utf-8", newline="\n")
        text, conflicts = sync.merge3(ours, base, theirs, tmp_path, "ours.py")
        assert conflicts == 1
        assert "<<<<<<<" in text and ">>>>>>>" in text


class TestRepoWiring:
    """确认工作流、脚本与基线在仓库里的接线一致。"""

    def test_workflow_exists_and_triggers(self, sync):
        import yaml

        workflow = SCRIPT.parents[1] / ".github/workflows/sync-maoyanrank.yml"
        data = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        triggers = data.get("on", data.get(True))
        assert "schedule" in triggers and "workflow_dispatch" in triggers
        assert data["permissions"]["contents"] == "write"
        assert data["permissions"]["pull-requests"] == "write"
        # 必须走 PR，不能直接推 main
        text = workflow.read_text(encoding="utf-8")
        assert "gh pr create" in text
        assert "git push origin main" not in text

    def test_baseline_matches_index_key(self, sync):
        import json

        index = json.loads((SCRIPT.parents[1] / "package.v3.json").read_text(encoding="utf-8"))
        assert sync.PLUGIN_KEY in index
        assert (SCRIPT.parents[1] / ".upstream" / sync.PLUGIN_ID / "__init__.py").exists()
        assert (SCRIPT.parents[1] / ".upstream" / sync.PLUGIN_ID / "meta.json").exists()
        assert (SCRIPT.parents[1] / sync.TOTAL_ZIP_ORDER[0]).exists()

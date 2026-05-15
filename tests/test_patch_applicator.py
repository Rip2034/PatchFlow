"""Tests for PatchApplicator — incremental snippet-level fix application."""
import pytest
from patchflow.core.fix.patch_applicator import (
    SnippetPatch, LineChange, PatchApplicator, DiffTracker, _compute_line_changes,
)


class TestSnippetPatch:
    def test_create(self):
        sp = SnippetPatch(file="a.py", old="x=1", new="x=2", reason="fix")
        assert sp.file == "a.py"
        assert sp.old == "x=1"
        assert sp.new == "x=2"


class TestLineChange:
    def test_diff_hunk(self):
        lc = LineChange(file="a.py", line_start=1, line_end=2,
                        old_lines="x=1\ny=2", new_lines="x=2\ny=3")
        hunk = lc.diff_hunk()
        assert "a.py" in hunk
        assert "-x=1" in hunk
        assert "+x=2" in hunk

    def test_empty_change(self):
        lc = LineChange(file="a.py", line_start=1, line_end=1,
                        old_lines="", new_lines="")
        assert "a.py" in lc.diff_hunk()


class TestPatchApplicatorStrategy1NewFile:
    def test_creates_new_file(self, tmp_path):
        sp = SnippetPatch(file="new.py", old="", new="print('hi')")
        ok = PatchApplicator.apply("new.py", [sp], work_dir=str(tmp_path))
        assert ok
        assert (tmp_path / "new.py").exists()

    def test_new_file_ignores_old(self, tmp_path):
        sp = SnippetPatch(file="new.py", old="some garbage", new="content")
        ok = PatchApplicator.apply("new.py", [sp], work_dir=str(tmp_path))
        assert ok
        assert (tmp_path / "new.py").read_text() == "content"


class TestPatchApplicatorStrategy2ExactReplace:
    def test_exact_snippet_in_file(self, tmp_path):
        f = tmp_path / "app.py"
        f.write_text("x = 1\ny = 2\nz = 3")
        sp = SnippetPatch(file="app.py", old="x = 1", new="x = 10")
        ok = PatchApplicator.apply("app.py", [sp], work_dir=str(tmp_path))
        assert ok
        assert "x = 10" in f.read_text()

    def test_snippet_not_found_falls_through(self, tmp_path):
        f = tmp_path / "app.py"
        original = "x" * 200
        f.write_text(original)
        sp = SnippetPatch(file="app.py", old="not found", new="replacement")
        ok = PatchApplicator.apply("app.py", [sp], work_dir=str(tmp_path))
        assert not ok  # Strategy 5: too small
        assert f.read_text() == original  # file unchanged


class TestPatchApplicatorStrategy3FullFile:
    def test_large_ratio_overwrites(self, tmp_path):
        f = tmp_path / "app.py"
        f.write_text("short")
        sp = SnippetPatch(file="app.py", old="", new="much longer content that exceeds 60 percent ratio")
        ok = PatchApplicator.apply("app.py", [sp], work_dir=str(tmp_path))
        assert ok
        assert "much longer" in f.read_text()


class TestPatchApplicatorStrategy5TooSmall:
    def test_tiny_new_refused(self, tmp_path):
        f = tmp_path / "app.py"
        original = "x" * 1000
        f.write_text(original)
        sp = SnippetPatch(file="app.py", old="not found", new="tiny")
        ok = PatchApplicator.apply("app.py", [sp], work_dir=str(tmp_path))
        assert not ok
        assert f.read_text() == original


class TestPatchApplicatorApplyAll:
    def test_multi_file_success(self, tmp_path):
        (tmp_path / "a.py").write_text("x=1")
        (tmp_path / "b.py").write_text("y=2")
        patches = [
            SnippetPatch(file="a.py", old="x=1", new="x=2"),
            SnippetPatch(file="b.py", old="y=2", new="y=3"),
        ]
        success, fail = PatchApplicator.apply_all(patches, str(tmp_path))
        assert success == 2
        assert fail == 0

    def test_duplicate_files_skipped(self, tmp_path):
        (tmp_path / "a.py").write_text("x=1")
        patches = [
            SnippetPatch(file="a.py", old="x=1", new="x=2"),
            SnippetPatch(file="a.py", old="x=1", new="x=3"),
        ]
        success, fail = PatchApplicator.apply_all(patches, str(tmp_path))
        assert success == 1  # only first patch per file


class TestDiffTracker:
    def test_record_and_context(self):
        dt = DiffTracker()
        changes = dt.record("f.py", "line1\nline2\nline3", "line1\nlineX\nline3")
        assert len(changes) >= 1
        ctx = dt.get_diff_context("f.py")
        assert "f.py" in ctx

    def test_restore_file(self, tmp_path):
        f = tmp_path / "restore.py"
        f.write_text("original content")
        dt = DiffTracker()
        dt.record(str(f), "original content", "modified content")
        assert dt.restore_file(str(f))
        assert f.read_text() == "original content"

    def test_rollback_patch(self, tmp_path):
        f = tmp_path / "rb.py"
        f.write_text("original")
        dt = DiffTracker()
        dt.record(str(f), "original", "modified")
        assert dt.rollback_patch(str(f))
        assert f.read_text() == "original"

    def test_recent_changes_summary(self):
        dt = DiffTracker()
        dt.record("f.py", "old", "new")
        summary = dt.recent_changes_summary
        assert "f.py" in summary

    def test_empty_tracker(self):
        dt = DiffTracker()
        assert dt.get_diff_context("nonexistent") == ""
        assert dt.recent_changes_summary == "no recent changes"


class TestComputeLineChanges:
    def test_single_line_change(self):
        changes = _compute_line_changes("hello\nworld", "hello\nearth", "f.py")
        assert len(changes) >= 1
        assert "world" in changes[0].old_lines or "earth" in changes[0].new_lines

    def test_no_change(self):
        changes = _compute_line_changes("hello", "hello", "f.py")
        assert len(changes) == 0

    def test_addition(self):
        changes = _compute_line_changes("line1", "line1\nline2", "f.py")
        assert len(changes) >= 1

    def test_deletion(self):
        changes = _compute_line_changes("line1\nline2", "line1", "f.py")
        assert len(changes) >= 1

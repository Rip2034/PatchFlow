import pytest

from patchflow.core.agent_sandbox import SandboxViolation
from patchflow.core.fix.fixer import apply_fix
from patchflow.core.fix.generator import write_files
from patchflow.core.fix.patch_applicator import PatchApplicator, SnippetPatch
from patchflow.core.fix.snapshot_manager import SnapshotManager
from patchflow.core.fix.validator import validate


def test_snapshot_preserves_nested_same_name_files(tmp_path):
    src = tmp_path / "src"
    tests = tmp_path / "tests"
    src.mkdir()
    tests.mkdir()
    src_file = src / "app.py"
    test_file = tests / "app.py"
    src_file.write_text("src original", encoding="utf-8")
    test_file.write_text("test original", encoding="utf-8")

    manager = SnapshotManager(str(tmp_path))
    snapshot_id = manager.save(["src/app.py", "tests/app.py"])

    src_file.write_text("src modified", encoding="utf-8")
    test_file.write_text("test modified", encoding="utf-8")

    manager.rollback(snapshot_id)

    assert src_file.read_text(encoding="utf-8") == "src original"
    assert test_file.read_text(encoding="utf-8") == "test original"


def test_snapshot_removes_file_that_did_not_exist_before(tmp_path):
    target = tmp_path / "new.py"
    manager = SnapshotManager(str(tmp_path))
    snapshot_id = manager.save(["new.py"])

    target.write_text("created", encoding="utf-8")
    manager.rollback(snapshot_id)

    assert not target.exists()


def test_patch_applicator_rejects_path_traversal(tmp_path):
    patch = SnippetPatch(file="../outside.py", old="", new="print('bad')")

    ok = PatchApplicator.apply("../outside.py", [patch], work_dir=str(tmp_path))

    assert not ok
    assert not (tmp_path.parent / "outside.py").exists()


def test_generator_rejects_path_traversal(tmp_path):
    with pytest.raises(SandboxViolation):
        write_files([{"file": "../outside.py", "content": "print('bad')"}], work_dir=str(tmp_path))


def test_apply_fix_rejects_path_traversal(tmp_path):
    ok = apply_fix({"file": "../outside.py", "content": "print('bad')"}, work_dir=str(tmp_path))

    assert not ok
    assert not (tmp_path.parent / "outside.py").exists()


def test_unknown_project_validation_is_not_success(tmp_path):
    result = validate(str(tmp_path))

    assert not result.ok
    assert result.status == "unsupported"

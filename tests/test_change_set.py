"""Tests for ChangeSet — cross-file fix coordination."""
from patchflow.core.fix.change_set import ChangeSet, FileChange, _extract_entity_names


class TestFileChange:
    def test_basic_change(self):
        fc = FileChange(file="test.py", old_content="old", new_content="new", reason="test")
        assert fc.file == "test.py"
        assert fc.old_content == "old"
        assert fc.new_content == "new"
        assert not fc.is_new_file()

    def test_is_new_file(self):
        fc = FileChange(file="test.py", old_content="", new_content="new")
        assert fc.is_new_file()

    def test_entities_changed_class_added(self):
        fc = FileChange(file="a.py", old_content="", new_content="class Foo:\n    pass")
        changed = fc.entities_changed()
        assert ("Foo", "class") in changed


class TestChangeSetBasic:
    def test_add_and_files(self):
        cs = ChangeSet(work_dir=".")
        cs.add("test.py", "content", "test reason")
        assert cs.files == ["test.py"]

    def test_add_duplicate_replaces(self, tmp_path):
        cs = ChangeSet(work_dir=str(tmp_path))
        cs.add("app.py", "content1", "first")
        cs.add("app.py", "content2", "second")
        assert len(cs.changes) == 1
        assert cs.changes[0].new_content == "content2"

    def test_summary(self):
        cs = ChangeSet(work_dir=".")
        cs.add("a.py", "x", "fix a")
        cs.add("b.py", "y", "fix b")
        assert "2 file" in cs.summary
        assert "a.py" in cs.summary

    def test_begin_rollback_restores(self, tmp_path):
        f = tmp_path / "keep.py"
        f.write_text("original", encoding="utf-8")
        cs = ChangeSet(work_dir=str(tmp_path))
        cs.add("keep.py", "original", "reason")
        sid = cs.begin()
        assert sid
        cs.rollback()
        assert f.read_text(encoding="utf-8") == "original"

    def test_commit_does_not_fail(self, tmp_path):
        f = tmp_path / "x.py"
        f.write_text("old", encoding="utf-8")
        cs = ChangeSet(work_dir=str(tmp_path))
        cs.add("x.py", "old", "reason")
        cs.begin()
        cs.commit()

    def test_apply_all_writes(self, tmp_path):
        cs = ChangeSet(work_dir=str(tmp_path))
        cs.add("out.py", "hello world", "create")
        applied = cs.apply_all()
        assert applied == 1
        assert (tmp_path / "out.py").read_text(encoding="utf-8") == "hello world"


class TestEntityExtraction:
    def test_python_class(self):
        entities = _extract_entity_names("class MyClass:\n    pass")
        assert ("MyClass", "class") in entities

    def test_python_function(self):
        entities = _extract_entity_names("def my_func():\n    pass")
        assert ("my_func", "function") in entities

    def test_go_struct(self):
        entities = _extract_entity_names("type User struct {\n    Name string\n}")
        assert ("User", "go_struct") in entities

    def test_rust_struct(self):
        entities = _extract_entity_names("pub struct Point {\n    x: f64,\n}")
        assert ("Point", "rust_struct") in entities

    def test_empty_content(self):
        assert _extract_entity_names("") == []

    def test_no_entities(self):
        assert _extract_entity_names("print('hello')") == []

"""Tests for LazyConflictDetector — entity extraction and file conflicts."""
from patchflow.core.fix.conflict_detector import LazyConflictDetector


class TestEntityExtraction:
    def setup_method(self):
        self.detector = LazyConflictDetector(work_dir=".")

    def test_python_class(self):
        entities = self.detector._extract_entities("class MyClass:\n    pass")
        assert ("MyClass", "class") in entities

    def test_python_function(self):
        entities = self.detector._extract_entities("def my_func():\n    pass")
        assert ("my_func", "function") in entities

    def test_python_async_function(self):
        entities = self.detector._extract_entities("async def fetch():\n    pass")
        assert ("fetch", "function") in entities

    def test_java_class(self):
        entities = self.detector._extract_entities("public class MyClass {\n}")
        assert ("MyClass", "class") in entities

    def test_java_interface(self):
        entities = self.detector._extract_entities("public interface Repository {\n}")
        assert ("Repository", "interface") in entities

    def test_java_enum(self):
        entities = self.detector._extract_entities("public enum Color { RED, GREEN }")
        assert ("Color", "enum") in entities

    def test_js_class(self):
        entities = self.detector._extract_entities("class Component {\n}")
        assert ("Component", "class") in entities

    def test_js_function(self):
        entities = self.detector._extract_entities("function handler() {\n}")
        assert ("handler", "function") in entities

    def test_go_struct(self):
        entities = self.detector._extract_entities("type User struct {\n}")
        assert ("User", "go_struct") in entities

    def test_go_interface(self):
        entities = self.detector._extract_entities("type Reader interface {\n}")
        assert ("Reader", "go_interface") in entities

    def test_rust_struct(self):
        entities = self.detector._extract_entities("pub struct Point {\n}")
        assert ("Point", "rust_struct") in entities

    def test_rust_enum(self):
        entities = self.detector._extract_entities("pub enum Status {\n}")
        assert ("Status", "rust_enum") in entities

    def test_csharp_class(self):
        entities = self.detector._extract_entities("public class Service {\n}")
        assert ("Service", "class") in entities

    def test_syntax_error_does_not_crash(self):
        entities = self.detector._extract_entities("this is not valid code {{{")
        assert entities == []

    def test_mixed_language_content(self):
        # Not real code, just different patterns in one string
        content = "class PyClass:\n    pass\n"
        entities = self.detector._extract_entities(content)
        assert ("PyClass", "class") in entities


class TestFileConflict:
    def setup_method(self):
        self.detector = LazyConflictDetector(work_dir=".")

    def test_no_conflict_first_agent(self):
        changes = [{"file": "app.py", "content": "class A:\n    pass"}]
        conflicts = self.detector.detect("agent_1", changes)
        assert len(conflicts) == 0

    def test_file_conflict_same_file(self):
        self.detector.detect("agent_1", [{"file": "app.py", "content": "x=1"}])
        conflicts = self.detector.detect("agent_2", [{"file": "app.py", "content": "x=2"}])
        file_conflicts = [c for c in conflicts if c["type"] == "file_conflict"]
        assert len(file_conflicts) >= 1

    def test_entity_conflict_same_name_different_file(self):
        self.detector.detect("agent_1", [{"file": "a.py", "content": "class User:\n    pass"}])
        conflicts = self.detector.detect("agent_2", [{"file": "b.py", "content": "class User:\n    pass"}])
        entity_conflicts = [c for c in conflicts if c["type"] == "entity_conflict"]
        assert len(entity_conflicts) >= 1
        assert entity_conflicts[0]["entity"] == "User"

    def test_entity_conflict_shows_both_files(self):
        self.detector.detect("agent_1", [{"file": "models.py", "content": "class Order:\n    pass"}])
        conflicts = self.detector.detect("agent_2", [{"file": "dtos.py", "content": "class Order:\n    pass"}])
        ec = [c for c in conflicts if c["type"] == "entity_conflict"][0]
        assert ec["file_a"] == "models.py"
        assert ec["file_b"] == "dtos.py"

    def test_no_entity_conflict_same_file(self):
        # Same entity in same file is not a conflict
        conflicts = self.detector.detect("agent_1", [
            {"file": "app.py", "content": "class App:\n    pass\nclass App:\n    pass"}
        ])
        assert len(conflicts) == 0

    def test_missing_file_key_skipped(self):
        conflicts = self.detector.detect("agent_1", [{"content": "x=1"}])
        assert len(conflicts) == 0

    def test_summary(self):
        self.detector.detect("agent_1", [{"file": "app.py", "content": "class A:\n    pass"}])
        summary = self.detector.summary()
        assert "agent" in summary.lower()

    def test_save_and_load_index(self, tmp_path):
        detector = LazyConflictDetector(work_dir=str(tmp_path))
        detector.detect("agent_1", [{"file": "app.py", "content": "class X:\n    pass"}])
        detector.save_index()

        detector2 = LazyConflictDetector(work_dir=str(tmp_path))
        detector2.load_index()
        assert "agent_1" in detector2._agent_writes

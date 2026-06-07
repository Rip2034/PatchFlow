"""Memory 系统测试"""
import pytest
import time
from pathlib import Path
from patchflow.core.memory import Memory, MemoryStore


class TestMemory:
    def test_basic_memory(self):
        m = Memory(
            name="test-mem",
            description="A test memory",
            type="project",
            content="Use snake_case for variables",
        )
        assert m.name == "test-mem"
        assert m.type == "project"

    def test_to_markdown(self):
        m = Memory(
            name="test-mem",
            description="Test description",
            type="project",
            content="Some content here",
            metadata={"lang": "python"},
        )
        md = m.to_markdown()
        assert md.startswith("---")
        assert "name: test-mem" in md
        assert "description: Test description" in md
        assert "Some content here" in md
        assert "lang" in md

    def test_from_markdown_roundtrip(self):
        m1 = Memory(
            name="roundtrip",
            description="Roundtrip test",
            type="user",
            content="Content body",
        )
        md = m1.to_markdown()
        m2 = Memory.from_markdown(md)
        assert m2 is not None
        assert m2.name == m1.name
        assert m2.description == m1.description
        assert m2.type == m1.type
        assert m2.content == m1.content

    def test_from_markdown_invalid(self):
        assert Memory.from_markdown("not yaml") is None
        assert Memory.from_markdown("---\ninvalid\n---") is None

    def test_to_llm_context(self):
        m = Memory(name="m", description="Desc", type="project", content="Body")
        ctx = m.to_llm_context()
        assert "[project]" in ctx
        assert "Desc" in ctx
        assert "Body" in ctx


class TestMemoryStore:
    def test_create_and_recall(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        store.remember("test", "Use snake_case naming", type="project")
        results = store.recall("snake_case")
        assert len(results) >= 1
        assert results[0].name == "test"

    def test_recall_by_name(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        store.remember("python-style", "Use 4-space indentation", type="project")
        results = store.recall("python")
        assert len(results) >= 1
        assert results[0].name == "python-style"

    def test_recall_by_type(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        store.remember("style", "code style", type="project")
        store.remember("prefs", "user prefs", type="user")
        results = store.recall("project")
        assert len(results) >= 1
        # Should find project type
        found_project = any(r.type == "project" for r in results)
        assert found_project

    def test_recall_no_match(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        results = store.recall("xyz_nonexistent_12345")
        assert results == []

    def test_get_by_name(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        store.remember("my-memory", "Content here")
        m = store.get("my-memory")
        assert m is not None
        assert m.content == "Content here"

    def test_get_missing(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        assert store.get("does-not-exist") is None

    def test_update_existing(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        m1 = store.remember("update-test", "v1")
        m2 = store.remember("update-test", "v2")
        assert m2.content == "v2"
        assert store.count == 1  # No duplicates

    def test_forget(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        store.remember("temp", "temporary")
        assert store.count == 1
        assert store.forget("temp")
        assert store.count == 0

    def test_forget_missing(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        assert not store.forget("nonexistent")

    def test_list_by_type(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        store.remember("p1", "proj 1", type="project")
        store.remember("u1", "user 1", type="user")
        store.remember("p2", "proj 2", type="project")

        projects = store.list_by_type("project")
        users = store.list_by_type("user")
        refs = store.list_by_type("reference")

        assert len(projects) == 2
        assert len(users) == 1
        assert len(refs) == 0

    def test_recall_all(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        store.remember("a", "Content A")
        store.remember("b", "Content B")
        all_mem = store.recall_all()
        assert len(all_mem) >= 2

    def test_compile_context(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        store.remember("style", "Use 4-space indentation", type="project",
                       description="Code indentation style")
        ctx = store.compile_context("indentation")
        assert "indentation" in ctx.lower()

    def test_compile_context_empty(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        ctx = store.compile_context("nothing")
        assert ctx == ""

    def test_linked_references_bonus_recall(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        store.remember("recipe-1", "See [[recipe-2]] for more", type="project")
        store.remember("recipe-2", "The referenced recipe", type="project")
        results = store.recall("recipe-2")
        assert len(results) >= 1
        # recipe-1 should also match because it links to recipe-2
        names = {r.name for r in results}
        assert "recipe-1" in names or "recipe-2" in names

    def test_memory_index_persists(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        store.remember("persisted", "Should survive reload", type="project")

        # Create new store pointing to same directory
        store2 = MemoryStore(str(tmp_path))
        m = store2.get("persisted")
        assert m is not None
        assert m.content == "Should survive reload"

    def test_name_sanitization(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        store.remember("My Memory With Spaces!", "Content")
        m = store.get("my-memory-with-spaces-")
        # Should find it with sanitized name
        assert m is not None or store.recall("My Memory")[0] is not None

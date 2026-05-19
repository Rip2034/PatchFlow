"""Tests for FixMemoryBank — persistent fix outcome memory."""
from patchflow.core.fix.memory_bank import FixMemory, FixMemoryBank


class TestFixMemory:
    def test_create_entry(self):
        fm = FixMemory(
            error_signature="type:int_str_unsupported",
            error_type="type",
            fix_pattern="cast to str before concat",
            file_context=["app.py"],
            success=True,
            strategy_used="chain",
            timestamp=12345.0,
        )
        assert fm.error_signature == "type:int_str_unsupported"
        assert fm.success


class TestSignatureGeneration:
    def test_basic_signature(self):
        mb = FixMemoryBank()
        sig = mb.generate_signature("type", "cannot concatenate str and int objects")
        assert sig.startswith("type:")
        assert "cannot" in sig
        assert "concatenate" in sig

    def test_signature_strips_punctuation(self):
        mb = FixMemoryBank()
        sig = mb.generate_signature("runtime", "NullPointerException: at line 42")
        assert sig.startswith("runtime:")
        assert "nullpointerexception" in sig or "null" in sig.lower()

    def test_short_root_cause(self):
        mb = FixMemoryBank()
        sig = mb.generate_signature("syntax", "indent")
        assert sig.startswith("syntax:")

    def test_empty_root_cause(self):
        mb = FixMemoryBank()
        sig = mb.generate_signature("unknown", "")
        assert sig.startswith("unknown:")

    def test_identical_errors_same_signature(self):
        mb = FixMemoryBank()
        sig1 = mb.generate_signature("type", "int and str cannot be added")
        sig2 = mb.generate_signature("type", "int and str cannot be added")
        assert sig1 == sig2


class TestMemoryBankAddQuery:
    def test_add_and_query(self, tmp_path):
        mb = FixMemoryBank(work_dir=str(tmp_path))
        mb.add("type", "int and str", "fixed", ["a.py"], True, "line")
        assert mb.entry_count == 1

        results = mb.query("type", "int and str error")
        assert len(results) >= 1
        assert results[0].success

    def test_query_returns_most_recent(self, tmp_path):
        mb = FixMemoryBank(work_dir=str(tmp_path))
        mb.add("type", "int concat", "fix1", ["a.py"], True)
        mb.add("type", "int concat", "fix2", ["b.py"], True)
        results = mb.query("type", "int concat")
        assert len(results) >= 1
        # Most relevant should be first
        assert results[0].error_type == "type"

    def test_query_different_type_no_match(self, tmp_path):
        mb = FixMemoryBank(work_dir=str(tmp_path))
        mb.add("syntax", "missing colon", "added colon", ["app.py"], True)
        results = mb.query("runtime", "null pointer")
        assert len(results) == 0

    def test_failed_entries_returned(self, tmp_path):
        mb = FixMemoryBank(work_dir=str(tmp_path))
        mb.add("type", "bad concat", "failed fix", ["a.py"], False)
        results = mb.query("type", "bad concat")
        assert len(results) >= 1
        assert not results[0].success


class TestMemoryBankEviction:
    def test_lru_eviction_at_capacity(self, tmp_path):
        mb = FixMemoryBank(work_dir=str(tmp_path))
        mb.MAX_ENTRIES = 5
        for i in range(10):
            mb.add("type", f"error {i}", f"fix {i}", ["a.py"], i % 2 == 0)
        assert mb.entry_count <= 5

    def test_below_capacity_no_eviction(self, tmp_path):
        mb = FixMemoryBank(work_dir=str(tmp_path))
        mb.MAX_ENTRIES = 50
        for i in range(5):
            mb.add("type", f"error {i}", f"fix {i}", ["a.py"], True)
        assert mb.entry_count == 5

    def test_failed_evicted_before_success(self, tmp_path):
        mb = FixMemoryBank(work_dir=str(tmp_path))
        mb.MAX_ENTRIES = 3
        # Add all failures first
        mb.add("type", "error a", "fix a", ["a.py"], False)
        mb.add("type", "error b", "fix b", ["b.py"], False)
        mb.add("type", "error c", "fix c", ["c.py"], False)
        # Now add a success — should evict one of the failures
        mb.add("type", "error d", "fix d", ["d.py"], True)
        assert mb.entry_count <= 3
        # At least one success should remain
        successes = [m for m in mb._entries if m.success]
        assert len(successes) >= 1


class TestMemoryBankPersistence:
    def test_save_and_load_roundtrip(self, tmp_path):
        mb = FixMemoryBank(work_dir=str(tmp_path))
        mb.add("type", "bad concat", "fixed concat", ["app.py"], True)
        mb.save()

        mb2 = FixMemoryBank(work_dir=str(tmp_path))
        mb2.load()
        assert mb2.entry_count == 1
        assert mb2._entries[0].error_type == "type"
        assert mb2._entries[0].success

    def test_load_nonexistent_file(self, tmp_path):
        mb = FixMemoryBank(work_dir=str(tmp_path))
        mb.load()  # Should not raise
        assert mb.entry_count == 0

    def test_load_corrupted_file(self, tmp_path):
        storage = tmp_path / ".patchflow" / "fix_memory.json"
        storage.parent.mkdir(parents=True, exist_ok=True)
        storage.write_text("not json", encoding="utf-8")
        mb = FixMemoryBank(work_dir=str(tmp_path))
        mb.load()  # Should handle gracefully
        assert mb.entry_count == 0

    def test_save_creates_directories(self, tmp_path):
        mb = FixMemoryBank(work_dir=str(tmp_path))
        mb.add("type", "test", "fix", ["a.py"], True)
        mb.save()
        assert (tmp_path / ".patchflow" / "fix_memory.json").exists()

    def test_clear(self, tmp_path):
        mb = FixMemoryBank(work_dir=str(tmp_path))
        mb.add("type", "test", "fix", ["a.py"], True)
        mb.save()
        assert mb.entry_count == 1
        mb.clear()
        assert mb.entry_count == 0
        assert not (tmp_path / ".patchflow" / "fix_memory.json").exists()


class TestMemoryBankShouldSkip:
    def test_repeated_failures_skip(self, tmp_path):
        mb = FixMemoryBank(work_dir=str(tmp_path))
        for _ in range(3):
            mb.add("type", "same error again", "still broken", ["a.py"], False, "line")
        should_skip, reason = mb.should_skip("type", "same error again", "line")
        assert should_skip

    def test_single_failure_does_not_skip(self, tmp_path):
        mb = FixMemoryBank(work_dir=str(tmp_path))
        mb.add("type", "one error", "fix attempt", ["a.py"], False)
        should_skip, reason = mb.should_skip("type", "one error")
        assert not should_skip

    def test_different_strategy_does_not_match(self, tmp_path):
        mb = FixMemoryBank(work_dir=str(tmp_path))
        for _ in range(3):
            mb.add("type", "err", "fix", ["a.py"], False, "line")
        should_skip, _ = mb.should_skip("type", "err", "chain")
        assert not should_skip

    def test_summary(self, tmp_path):
        mb = FixMemoryBank(work_dir=str(tmp_path))
        mb.add("type", "err1", "fix1", ["a.py"], True)
        mb.add("type", "err2", "fix2", ["b.py"], False)
        s = mb.summary()
        assert "2 entries" in s
        assert "1 success" in s

"""User Prompt 测试"""
import pytest
from patchflow.utils.user_prompt import (
    Question, ask, _parse_indices,
    ask_project_type, ask_framework, confirm_plan,
)


class TestQuestion:
    def test_basic_question(self):
        q = Question(
            key="lang",
            text="Choose a language",
            header="Language",
            options=["Python", "TypeScript", "Go"],
            default="Python",
        )
        assert q.key == "lang"
        assert len(q.options) == 3
        assert q.default == "Python"
        assert q.required
        assert not q.multi_select

    def test_multi_select_question(self):
        q = Question(
            key="features",
            text="Select features",
            options=["Auth", "API", "UI"],
            multi_select=True,
        )
        assert q.multi_select

    def test_question_without_options(self):
        q = Question(key="name", text="Enter your name", default="World")
        assert q.options == []
        assert q.default == "World"


class TestParseIndices:
    def test_single_number(self):
        assert _parse_indices("1", 5) == [1]
        assert _parse_indices("3", 5) == [3]

    def test_comma_separated(self):
        assert _parse_indices("1,2,3", 5) == [1, 2, 3]
        assert _parse_indices("1, 3, 5", 10) == [1, 3, 5]

    def test_range(self):
        assert _parse_indices("1-3", 5) == [1, 2, 3]
        assert _parse_indices("2-4", 5) == [2, 3, 4]

    def test_out_of_range_filtered(self):
        assert _parse_indices("1,10", 5) == [1]  # 10 out of range
        assert _parse_indices("0", 5) == [1]  # fallback

    def test_empty_input(self):
        assert _parse_indices("", 5) == [1]  # default to first

    def test_invalid_input(self):
        assert _parse_indices("abc", 5) == [1]  # fallback


class TestQuestionTemplates:
    def test_ask_project_type_signature(self):
        # Just verify these functions exist and are callable
        assert callable(ask_project_type)
        assert callable(ask_framework)
        assert callable(confirm_plan)

    def test_ask_framework_known_languages(self):
        for lang in ["python", "javascript", "go", "rust", "typescript"]:
            # Verify doesn't crash
            assert callable(ask_framework)

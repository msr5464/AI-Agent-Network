"""
Unit tests for src/agent/analyzer.py: failure classification and LangChain usage.
Tests FailureClassification, prompt building, response parsing, and classify_failure
with mocked LLM (no network).
"""

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

# Add repo root so src.* imports work
_repo_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_repo_root))

from src.parsers.models import TestResult, TestStatus
from src.agent.analyzer import FailureClassification, TestAnalyzer


# ---------------------------------------------------------------------------
# FailureClassification
# ---------------------------------------------------------------------------

class TestFailureClassification:
    """Test FailureClassification model and helpers."""

    def test_is_product_bug_true(self):
        fc = FailureClassification(
            test_name="MyTest.testFail",
            classification="PRODUCT_BUG",
            confidence="HIGH",
            root_cause="Expected 1 got 0",
            recommended_action="Fix app",
            root_cause_category="ASSERTION_FAILURE",
        )
        assert fc.is_product_bug() is True
        assert fc.is_automation_issue() is False

    def test_is_automation_issue_true(self):
        fc = FailureClassification(
            test_name="MyTest.testFail",
            classification="AUTOMATION_ISSUE",
            confidence="HIGH",
            root_cause="Element not found",
            recommended_action="Update locator",
            root_cause_category="ELEMENT_NOT_FOUND",
        )
        assert fc.is_automation_issue() is True
        assert fc.is_product_bug() is False

    def test_unknown_classification(self):
        fc = FailureClassification(
            test_name="MyTest.testFail",
            classification="UNKNOWN",
            confidence="LOW",
            root_cause="Parse failed",
            recommended_action="Manual review",
            root_cause_category="OTHER",
        )
        assert fc.is_product_bug() is False
        assert fc.is_automation_issue() is False

    def test_repr_product_bug(self):
        fc = FailureClassification(
            test_name="A.test", classification="PRODUCT_BUG",
            confidence="HIGH", root_cause="x", recommended_action="y",
            root_cause_category="OTHER",
        )
        assert "🐛" in repr(fc) and "PRODUCT_BUG" in repr(fc)

    def test_repr_automation_issue(self):
        fc = FailureClassification(
            test_name="A.test", classification="AUTOMATION_ISSUE",
            confidence="MEDIUM", root_cause="x", recommended_action="y",
            root_cause_category="OTHER",
        )
        assert "🔧" in repr(fc) and "AUTOMATION_ISSUE" in repr(fc)


# ---------------------------------------------------------------------------
# TestAnalyzer._parse_classification_response
# ---------------------------------------------------------------------------

class TestParseClassificationResponse:
    """Test JSON parsing and fallback behavior of classification response."""

    @pytest.fixture
    def analyzer(self):
        with mock.patch("src.agent.analyzer.Config") as mock_config:
            mock_config.LLM_PROVIDER = "openai"
            mock_config.OPENAI_MODEL = "gpt-4"
            mock_config.OPENAI_API_KEY = "test-key"
            with mock.patch("langchain_openai.ChatOpenAI") as mock_openai:
                mock_openai.return_value = mock.MagicMock()
                a = TestAnalyzer()
                return a

    def test_parse_valid_json(self, analyzer):
        response = json.dumps({
            "classification": "PRODUCT_BUG",
            "confidence": "HIGH",
            "root_cause": "POST /api/user returned 500",
            "recommended_action": "Check backend",
            "root_cause_category": "ASSERTION_FAILURE",
        })
        out = analyzer._parse_classification_response(response, "MyTest.testFail")
        assert out.test_name == "MyTest.testFail"
        assert out.classification == "PRODUCT_BUG"
        assert out.confidence == "HIGH"
        assert out.root_cause == "POST /api/user returned 500"
        assert out.recommended_action == "Check backend"
        assert out.root_cause_category == "ASSERTION_FAILURE"

    def test_parse_json_in_markdown_block(self, analyzer):
        # Parser extracts JSON from first ``` ... ``` block (no leading space on lines)
        response = """```json
{"classification": "AUTOMATION_ISSUE", "confidence": "MEDIUM", "root_cause": "NoSuchElement", "recommended_action": "Fix locator", "root_cause_category": "ELEMENT_NOT_FOUND"}
```"""
        out = analyzer._parse_classification_response(response, "A.b")
        assert out.classification == "AUTOMATION_ISSUE"
        assert out.root_cause_category == "ELEMENT_NOT_FOUND"

    def test_parse_fallback_product_bug(self, analyzer):
        response = "The failure is clearly a PRODUCT_BUG because of assertion mismatch."
        out = analyzer._parse_classification_response(response, "X.y")
        assert out.classification == "PRODUCT_BUG"
        assert out.confidence == "LOW"
        assert out.root_cause_category == "OTHER"

    def test_parse_fallback_automation_issue(self, analyzer):
        response = "This is an AUTOMATION ISSUE - element not found."
        out = analyzer._parse_classification_response(response, "X.y")
        assert out.classification == "AUTOMATION_ISSUE"
        assert out.confidence == "LOW"

    def test_parse_fallback_unknown(self, analyzer):
        response = "Something went wrong with no clear classification."
        out = analyzer._parse_classification_response(response, "X.y")
        assert out.classification == "UNKNOWN"
        assert out.confidence == "LOW"


# ---------------------------------------------------------------------------
# TestAnalyzer._build_classification_prompt
# ---------------------------------------------------------------------------

class TestBuildClassificationPrompt:
    """Test that the classification prompt includes test details and categories."""

    @pytest.fixture
    def analyzer(self):
        with mock.patch("src.agent.analyzer.Config") as mock_config:
            mock_config.LLM_PROVIDER = "openai"
            mock_config.OPENAI_MODEL = "gpt-4"
            mock_config.OPENAI_API_KEY = "test-key"
            with mock.patch("langchain_openai.ChatOpenAI") as mock_openai:
                mock_openai.return_value = mock.MagicMock()
                return TestAnalyzer()

    def test_prompt_contains_test_details(self, analyzer):
        test_result = TestResult(
            class_name="pkg.MyTest",
            method_name="testLogin",
            status=TestStatus.FAIL,
            duration_seconds=5.0,
            error_type="AssertionError",
            error_message="Expected true got false",
            stack_trace="at MyTest.testLogin(...)",
        )
        prompt = analyzer._build_classification_prompt(test_result)
        assert "pkg.MyTest" in prompt or "testLogin" in prompt
        assert "AssertionError" in prompt
        assert "Expected true got false" in prompt
        assert "PRODUCT_BUG" in prompt
        assert "AUTOMATION_ISSUE" in prompt

    def test_prompt_requests_json_with_root_cause_category(self, analyzer):
        test_result = TestResult(
            class_name="A", method_name="b", status=TestStatus.FAIL,
            duration_seconds=1.0,
        )
        prompt = analyzer._build_classification_prompt(test_result)
        assert "root_cause_category" in prompt
        assert "ELEMENT_NOT_FOUND" in prompt
        assert "TIMEOUT" in prompt
        assert "ASSERTION_FAILURE" in prompt
        assert "JSON" in prompt or "json" in prompt.lower()

    def test_prompt_uses_execution_log_when_present(self, analyzer):
        test_result = TestResult(
            class_name="A", method_name="b", status=TestStatus.FAIL,
            duration_seconds=1.0,
            execution_log="Step 1... Step 2... Failure.",
        )
        prompt = analyzer._build_classification_prompt(test_result)
        assert "Step 1" in prompt and "Failure." in prompt

    def test_prompt_truncates_long_execution_log(self, analyzer):
        long_log = "x" * 60000
        test_result = TestResult(
            class_name="A", method_name="b", status=TestStatus.FAIL,
            duration_seconds=1.0,
            execution_log=long_log,
        )
        prompt = analyzer._build_classification_prompt(test_result)
        assert len(prompt) < 60000 + 2000
        assert "..." in prompt

    def test_prompt_is_self_contained_not_from_prompts_yaml(self, analyzer):
        """Classification prompt is built in code; it does not use config/prompts.yaml placeholders."""
        test_result = TestResult(
            class_name="A", method_name="b", status=TestStatus.FAIL,
            duration_seconds=1.0,
        )
        prompt = analyzer._build_classification_prompt(test_result)
        # prompts.yaml uses {failure_details}; analyzer injects test details inline
        assert "{failure_details}" not in prompt
        assert "Test Details:" in prompt or "Test:" in prompt


# ---------------------------------------------------------------------------
# TestAnalyzer.classify_failure (with mocked LLM)
# ---------------------------------------------------------------------------

class TestClassifyFailure:
    """Test classify_failure with mocked LLM invoke."""

    @pytest.fixture
    def failure_result(self):
        return TestResult(
            class_name="pkg.TestLogin",
            method_name="testInvalidPassword",
            status=TestStatus.FAIL,
            duration_seconds=2.0,
            error_type="AssertionError",
            error_message="Expected 401 got 200",
        )

    def test_classify_failure_returns_classification(self, failure_result):
        with mock.patch("src.agent.analyzer.Config") as mock_config:
            mock_config.LLM_PROVIDER = "openai"
            mock_config.OPENAI_MODEL = "gpt-4"
            mock_config.OPENAI_API_KEY = "test-key"
            with mock.patch("langchain_openai.ChatOpenAI") as mock_openai:
                mock_llm = mock.MagicMock()
                mock_llm.invoke.return_value = json.dumps({
                    "classification": "PRODUCT_BUG",
                    "confidence": "HIGH",
                    "root_cause": "API returned 200 instead of 401",
                    "recommended_action": "Verify auth logic",
                    "root_cause_category": "ASSERTION_FAILURE",
                })
                mock_openai.return_value = mock_llm
                analyzer = TestAnalyzer()
                out = analyzer.classify_failure(failure_result)
        assert out.classification == "PRODUCT_BUG"
        assert out.confidence == "HIGH"
        assert out.is_product_bug() is True
        mock_llm.invoke.assert_called_once()

    def test_classify_failure_handles_ai_message_content(self, failure_result):
        """When LLM returns an AIMessage, content is extracted."""
        with mock.patch("src.agent.analyzer.Config") as mock_config:
            mock_config.LLM_PROVIDER = "openai"
            mock_config.OPENAI_MODEL = "gpt-4"
            mock_config.OPENAI_API_KEY = "test-key"
            with mock.patch("langchain_openai.ChatOpenAI") as mock_openai:
                mock_llm = mock.MagicMock()
                body = json.dumps({
                    "classification": "AUTOMATION_ISSUE",
                    "confidence": "MEDIUM",
                    "root_cause": "Element not found",
                    "recommended_action": "Update selector",
                    "root_cause_category": "ELEMENT_NOT_FOUND",
                })
                mock_llm.invoke.return_value = mock.MagicMock(content=body)
                mock_openai.return_value = mock_llm
                analyzer = TestAnalyzer()
                out = analyzer.classify_failure(failure_result)
        assert out.classification == "AUTOMATION_ISSUE"
        assert out.is_automation_issue() is True

    def test_classify_failure_passing_test_raises(self):
        passing = TestResult(
            class_name="A", method_name="b", status=TestStatus.PASS,
            duration_seconds=1.0,
        )
        with mock.patch("src.agent.analyzer.Config") as mock_config:
            mock_config.LLM_PROVIDER = "openai"
            mock_config.OPENAI_MODEL = "gpt-4"
            mock_config.OPENAI_API_KEY = "test-key"
            with mock.patch("langchain_openai.ChatOpenAI") as mock_openai:
                mock_openai.return_value = mock.MagicMock()
                analyzer = TestAnalyzer()
                with pytest.raises(ValueError, match="Cannot classify a passing test"):
                    analyzer.classify_failure(passing)

    def test_classify_failure_llm_error_returns_unknown(self, failure_result):
        with mock.patch("src.agent.analyzer.Config") as mock_config:
            mock_config.LLM_PROVIDER = "openai"
            mock_config.OPENAI_MODEL = "gpt-4"
            mock_config.OPENAI_API_KEY = "test-key"
            with mock.patch("langchain_openai.ChatOpenAI") as mock_openai:
                mock_llm = mock.MagicMock()
                mock_llm.invoke.side_effect = RuntimeError("API error")
                mock_openai.return_value = mock_llm
                analyzer = TestAnalyzer()
                out = analyzer.classify_failure(failure_result)
        assert out.classification == "UNKNOWN"
        assert out.confidence == "LOW"
        assert "Classification failed" in out.root_cause or "API error" in out.root_cause

    def test_classify_failure_with_gemini_provider(self, failure_result):
        """When LLM_PROVIDER is gemini, analyzer uses Gemini LLM (init and classify)."""
        mock_llm = mock.MagicMock()
        mock_llm.invoke.return_value = json.dumps({
            "classification": "AUTOMATION_ISSUE",
            "confidence": "HIGH",
            "root_cause": "Element not found",
            "recommended_action": "Update locator",
            "root_cause_category": "ELEMENT_NOT_FOUND",
        })

        def set_llm(self):
            self.llm = mock_llm
            self.model = "gemini-1.5-flash"

        with mock.patch("src.agent.analyzer.Config") as mock_config:
            mock_config.LLM_PROVIDER = "gemini"
            mock_config.GEMINI_API_KEY = "test-gemini-key"
            mock_config.GEMINI_MODEL = "gemini-1.5-flash"
            with mock.patch.object(TestAnalyzer, "_init_gemini", set_llm):
                analyzer = TestAnalyzer()
                out = analyzer.classify_failure(failure_result)
        assert out.classification == "AUTOMATION_ISSUE"
        assert out.is_automation_issue() is True
        mock_llm.invoke.assert_called_once()

    def test_gemini_init_raises_when_api_key_missing(self):
        """When LLM_PROVIDER is gemini and GEMINI_API_KEY is empty, init raises ValueError."""
        fake_genai = mock.MagicMock()
        fake_genai.ChatGoogleGenerativeAI = mock.MagicMock()
        with mock.patch("src.agent.analyzer.Config") as mock_config:
            mock_config.LLM_PROVIDER = "gemini"
            mock_config.GEMINI_API_KEY = ""
            mock_config.GEMINI_MODEL = "gemini-1.5-flash"
            with mock.patch.dict("sys.modules", {"langchain_google_genai": fake_genai}):
                with pytest.raises(ValueError, match="GEMINI_API_KEY not found"):
                    TestAnalyzer()


# ---------------------------------------------------------------------------
# TestAnalyzer.classify_multiple_failures
# ---------------------------------------------------------------------------

class TestClassifyMultipleFailures:
    """Test batch classification with mocked LLM."""

    def test_empty_list_returns_empty(self):
        with mock.patch("src.agent.analyzer.Config") as mock_config:
            mock_config.LLM_PROVIDER = "openai"
            mock_config.OPENAI_MODEL = "gpt-4"
            mock_config.OPENAI_API_KEY = "test-key"
            with mock.patch("langchain_openai.ChatOpenAI") as mock_openai:
                mock_openai.return_value = mock.MagicMock()
                analyzer = TestAnalyzer()
                out = analyzer.classify_multiple_failures([])
        assert out == []

    def test_only_passed_tests_returns_empty(self):
        passed = TestResult(
            class_name="A", method_name="b", status=TestStatus.PASS,
            duration_seconds=1.0,
        )
        with mock.patch("src.agent.analyzer.Config") as mock_config:
            mock_config.LLM_PROVIDER = "openai"
            mock_config.OPENAI_MODEL = "gpt-4"
            mock_config.OPENAI_API_KEY = "test-key"
            with mock.patch("langchain_openai.ChatOpenAI") as mock_openai:
                mock_openai.return_value = mock.MagicMock()
                analyzer = TestAnalyzer()
                out = analyzer.classify_multiple_failures([passed])
        assert out == []

    def test_classifies_each_failure(self):
        failures = [
            TestResult("A", "f1", TestStatus.FAIL, 1.0),
            TestResult("B", "f2", TestStatus.FAIL, 1.0),
        ]
        with mock.patch("src.agent.analyzer.Config") as mock_config:
            mock_config.LLM_PROVIDER = "openai"
            mock_config.OPENAI_MODEL = "gpt-4"
            mock_config.OPENAI_API_KEY = "test-key"
            with mock.patch("langchain_openai.ChatOpenAI") as mock_openai:
                mock_llm = mock.MagicMock()
                mock_llm.invoke.side_effect = [
                    json.dumps({"classification": "PRODUCT_BUG", "confidence": "HIGH", "root_cause": "r1", "recommended_action": "a1", "root_cause_category": "OTHER"}),
                    json.dumps({"classification": "AUTOMATION_ISSUE", "confidence": "MEDIUM", "root_cause": "r2", "recommended_action": "a2", "root_cause_category": "ELEMENT_NOT_FOUND"}),
                ]
                mock_openai.return_value = mock_llm
                analyzer = TestAnalyzer()
                out = analyzer.classify_multiple_failures(failures)
        assert len(out) == 2
        assert out[0].classification == "PRODUCT_BUG"
        assert out[1].classification == "AUTOMATION_ISSUE"
        assert mock_llm.invoke.call_count == 2

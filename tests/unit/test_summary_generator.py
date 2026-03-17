"""
Unit tests for src/agent/summary_generator.py.
Tests high-level summary synthesis and executive summary generation with mocked LLM.
"""

import sys
from pathlib import Path
from unittest import mock

import pytest

_repo_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_repo_root))

from src.parsers.models import TestSummary
from src.agent.summary_generator import SummaryGenerator


def _summary_generator_with_mocked_llm():
    """Create SummaryGenerator with LLM mocked (default provider is ollama)."""
    with mock.patch("src.agent.summary_generator.Config") as mock_config:
        mock_config.LLM_PROVIDER = "ollama"
        mock_config.OLLAMA_MODEL = "llama3.2:3b"
        mock_config.OLLAMA_BASE_URL = "http://localhost:11434"
        with mock.patch("langchain_ollama.OllamaLLM") as mock_ollama:
            mock_ollama.return_value = mock.MagicMock()
            gen = SummaryGenerator()
            return gen


class TestHighLevelSummary:
    """Test _generate_high_level_summary_html output structure and content."""

    def test_contains_project_overview(self):
        gen = _summary_generator_with_mocked_llm()
        summary = TestSummary(total=100, passed=90, failed=10, skipped=0, errors=0, duration_seconds=300.0)
        html = gen._generate_high_level_summary_html("MyReport-123", summary)
        assert "Project Overview" in html
        assert "QA AI Agent" in html
        assert "database-first" in html or "database" in html.lower()
        assert "actionable insights" in html or "insights" in html

    def test_contains_key_features(self):
        gen = _summary_generator_with_mocked_llm()
        summary = TestSummary(total=50, passed=45, failed=5, skipped=0, errors=0, duration_seconds=120.0)
        html = gen._generate_high_level_summary_html("Suite-A", summary)
        assert "Key Features" in html
        assert "Intelligent Classification" in html
        assert "Auto-Fix" in html
        assert "Historical Analysis" in html

    def test_contains_architecture(self):
        gen = _summary_generator_with_mocked_llm()
        summary = TestSummary(total=10, passed=10, failed=0, skipped=0, errors=0, duration_seconds=60.0)
        html = gen._generate_high_level_summary_html("Smoke", summary)
        assert "Architecture" in html
        assert "Database-first" in html or "database" in html.lower()
        assert "AI-driven" in html

    def test_contains_technology_stack(self):
        gen = _summary_generator_with_mocked_llm()
        summary = TestSummary(total=20, passed=18, failed=2, skipped=0, errors=0, duration_seconds=90.0)
        html = gen._generate_high_level_summary_html("Regression", summary)
        assert "Technology Stack" in html
        assert "Python" in html
        assert "LangChain" in html

    def test_run_context_report_name_and_counts(self):
        gen = _summary_generator_with_mocked_llm()
        summary = TestSummary(total=42, passed=35, failed=7, skipped=0, errors=0, duration_seconds=200.0)
        html = gen._generate_high_level_summary_html("ProdSanity-All-Tests-541", summary)
        assert "ProdSanity-All-Tests-541" in html
        assert "42" in html
        assert "83.3" in html  # pass_rate 35/42

    def test_high_level_summary_has_high_level_summary_heading(self):
        gen = _summary_generator_with_mocked_llm()
        summary = TestSummary(total=1, passed=1, failed=0, skipped=0, errors=0, duration_seconds=1.0)
        html = gen._generate_high_level_summary_html("Report", summary)
        assert "High-Level Summary" in html


class TestGenerateExecutiveSummaryIncludesHighLevel:
    """Test that generate_executive_summary output starts with high-level summary."""

    def test_executive_summary_includes_high_level_block(self):
        gen = _summary_generator_with_mocked_llm()
        summary = TestSummary(total=5, passed=4, failed=1, skipped=0, errors=0, duration_seconds=10.0)
        with mock.patch.object(gen, "_generate_html_executive_summary", return_value="<div>Insights</div>"):
            out = gen.generate_executive_summary(
                summary=summary,
                classifications=[],
                report_name="UnitRun",
            )
        assert "High-Level Summary" in out
        assert "Project Overview" in out
        assert "Key Features" in out
        assert "Architecture" in out
        assert "Technology Stack" in out
        assert "UnitRun" in out
        assert "5" in out
        assert "<div>Insights</div>" in out

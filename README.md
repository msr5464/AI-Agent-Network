<div>
  <img src="https://raw.githubusercontent.com/msr5464/Basic-Automation-Framework/refs/heads/master/ThanosLogo.png" title="Powered by Thanos and created by Mukesh Rajput" height="50">

  # QA AI Agent

  **🤖 Automated Test Report Analyzer | Database-First Intelligence | Two-Level Failure Classification | Flaky Test Detection | Screenshots & Known Issues**

  [![Python](https://img.shields.io/badge/Python-3.9+-blue.svg)](https://www.python.org/)
  [![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
</div>

---

## 📋 Table of Contents
- [Overview](#-overview)
- [Sample Report](#-sample-report)
- [Features](#-features)
- [Quick Start](#-quick-start)
- [Architecture & Components](#-architecture--components)
- [Project Structure](#-project-structure)
- [Troubleshooting](#-troubleshooting)
- [Creator](#-creator)

---

## 🎯 Overview
**QA AI Agent** is an intelligent, automated analysis system that transforms raw test execution data into actionable insights. Unlike traditional report generators, it uses a **database-first approach** combined with **HTML log parsing** to provide a complete picture of test health.

It leverages **Generative AI (OpenAI, Ollama, or Google Gemini)** to classify failures with human-like understanding, distinguishing between genuine product bugs and automation issues.

### What It Does
- **🤖 Intelligent Classification**: Automatically analyzes failure logs to determine if a failure is a `🐛 Product Bug` or `🔧 Automation Issue`.
- **📊 Historical Trending**: Tracks test stability over time using a MySQL database to identify flaky tests and recurring patterns.
- **📝 Root Cause Analysis**: Extracts precise root causes (e.g., "API 500 Error", "Element Not Found", "Assertion Mismatch") from verbose logs, plus a **Likely location** (file:line) for faster triage.
- **🧠 Context-Aware Recommendations**: Provides specific, actionable steps to resolve failures based on the error context.
- **📸 Failure Screenshots**: Embeds failure screenshots from test runs in the report (thumbnails with click-to-zoom) when available in the report directory.
- **🔖 Known Issues**: Highlights tests marked as known failures in the database (with execution history and optional Jira links) in a dedicated section.
- **📈 Interactive Reporting**: Generates a modern, single-file HTML report with executive summaries, trend charts, and detailed drill-downs.

---

## 📸 Sample Report

![Sample Report](sample_report.png)

---

## ✨ Features

### Core Capabilities
- **📊 Database-First Data Retrieval**
  - Queries MySQL database for reliable historical test results.
  - Merges database records with detailed execution logs parsed from HTML artifacts.
  
- **🤖 Two-Level AI Classification System**
  - **Level 1 (High-Level)**: Classifies as `Product Bug` vs. `Automation Issue`.
  - **Level 2 (Root Cause)**: Categorizes into `ELEMENT_NOT_FOUND`, `TIMEOUT`, `ASSERTION_FAILURE`, `ENVIRONMENT_ISSUE`, or `OTHER`.
  - powered by **GPT-4** (OpenAI), **Gemini** (Google), or **Llama 3** (Ollama, Local/Private).

- **📉 Smart Flaky Test Detection**
  - Identifies tests that flip-flop between Pass/Fail.
  - Configurable thresholds (e.g., "Failed 4 times in the last 10 runs").
  - Visualizes execution history with colored status dots.

### Report Features
- **Executive Summary**: High-level health metrics and AI-generated insights.
- **Failures by Category**: Grouped failures for efficient triage (e.g., see all "Timeouts" together).
- **Screenshots**: Failure screenshots from the test run are shown in test details (thumbnail; click to view full size). When the report is opened from disk, images load from the local testdata path.
- **Likely location**: For each failure, the report shows a **Likely location** (e.g. `LoginPage.java:115`) derived from stack traces so you can jump straight to the code.
- **Known Failures**: A dedicated section lists tests marked as known failures in the database, with last 10 execution history dots and optional Jira links for tracking.
- **Interactive UI**: Search, sort, expand details, copy-to-clipboard, and screenshot zoom.

*Auto-fix (self-healing locator fixes) is planned as a future enhancement and is not yet fully supported.*

---

## 🚀 Quick Start

### Prerequisites
- **Python 3.9+** (3.11+ recommended)
- **MySQL Database** (for storage of test results)
- **LLM Provider**:
  - **Ollama** (Local, Private, Free) - *Recommended*
  - **OpenAI API Key** (Cloud, Powerful)
  - **Google Gemini API Key** (Cloud; set `LLM_PROVIDER=gemini` and `GEMINI_API_KEY` from [Google AI Studio](https://aistudio.google.com/apikey))

### Installation

```bash
# Clone the repository
git clone <repository-url>
cd QA-AI-Agent

# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### Configuration
1. **Create environment file**:
   ```bash
   cp config/.env.example config/.env
   ```

2. **Edit `config/.env`**:
   - Set `LLM_PROVIDER=ollama` (default), `openai`, or `gemini`.
   - For **OpenAI**: set `OPENAI_API_KEY` and optionally `OPENAI_MODEL`.
   - For **Gemini**: set `GEMINI_API_KEY` (get one at [Google AI Studio](https://aistudio.google.com/apikey)) and optionally `GEMINI_MODEL` (default: `gemini-1.5-flash`).
   - Configure `DB_HOST`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`.
   - Set `INPUT_DIR` (where your raw reports live) and `OUTPUT_DIR`.

### Run the Application

**macOS / Linux:**
```bash
./scripts/run.sh --input-dir testdata/Regression-Suite --output-dir reports
```

**Windows (PowerShell):**
```powershell
.\scripts\run.ps1 --input-dir testdata/Regression-Suite --output-dir reports
```

> **Note**: If no arguments are passed, it defaults to the paths in your `.env` file.

---

## 🏗 Architecture & Components

The agent is designed with modularity in mind, separating data parsing, intelligence, and reporting.

### 🔄 Workflow (high level)
1. **Ingest** → 2. **Query DB** → 3. **Parse HTML** → 4. **Merge** → 5. **AI + Rules** → 6. **Summary** → 7. **Generate Report**

### 📊 Data Flow Diagram (how the report is created, step by step)

Each step shows **inputs** → **component** → **outputs**. Follow the flow top to bottom to see how data moves until the final HTML report is produced.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  INPUTS                                                                     │
│  • Report Directory (path to test run: HTML, logs, Screenshots/)            │
│  • report_name / build_tag (from directory name, e.g. Regression-Frs-266)    │
│  • --table-name (optional; else derived from report_name)                    │
└─────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 1 — Load data from database                                            │
│  Component: AgentMemory  (reads from MySQL)                                  │
│  • get_test_results_by_buildtag(...)  → db_results                          │
│  • detect_recurring_failures(...)      → recurring (flaky tests)             │
│  • get_trend_analysis(...)             → trends (pass rate, etc.)             │
│  Outputs: db_results, recurring, trends                                       │
└─────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 2 — Parse HTML from report directory                                   │
│  Component: HTML Parsers / DataBuilder helpers                               │
│  • get_execution_logs_from_html(report_dir)  → execution_logs, html_links   │
│  • get_test_durations_from_html(report_dir)  → durations                      │
│  Outputs: execution_logs, durations, html_links                               │
└─────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 3 — Merge DB + HTML into full test data                                │
│  Component: DataBuilder (get_full_report_data_from_db)                       │
│  Inputs: report_dir, db_results, execution_logs, durations, html_links      │
│  Outputs: data = { summary, test_results }                                    │
│           (each TestResult: status, execution_log, knownFailure from DB…)   │
└─────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 4 — Classify failures with AI                                           │
│  Component: TestAnalyzer (LLM: OpenAI / Ollama / Gemini)                     │
│  Inputs: failures = test_results where status = FAIL/ERROR                   │
│  Outputs: classifications (Product Bug vs Automation, root cause,            │
│           recommended_action, root_cause_category)                            │
└─────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 5 — Refine categories with rules                                       │
│  Component: CategoryRuleEngine                                               │
│  Inputs: classifications, test_data_cache (execution logs)                   │
│  Outputs: category_counts, category_failures (ELEMENT_NOT_FOUND, TIMEOUT,   │
│           ASSERTION_FAILURE, ENVIRONMENT_ISSUE, OTP→product, etc.)           │
└─────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 6 — Generate executive summary text                                    │
│  Component: SummaryGenerator (LLM)                                           │
│  Inputs: summary, classifications, category_*, recurring, test_html_links,  │
│          test_results                                                         │
│  Outputs: ai_summary (narrative for the report)                               │
└─────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 7 — Build and save HTML report                                          │
│  Component: ReportGenerator                                                   │
│  Inputs: summary, classifications, ai_summary, recurring, trends,            │
│          report_dir (screenshots + likely location from logs),               │
│          test_results, test_html_links, environment, output_dir               │
│  • Resolves screenshot paths under report_dir (e.g. …/Screenshots/*.png)     │
│  • Extracts "Likely location" (file:line) from stack traces                   │
│  • Renders: categories, known failures, flaky table, trend, summary, links  │
│  Outputs: HTML file (e.g. AI-Generated-Report_Regression-Frs-266.html)        │
└─────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
                              ┌─────────────────────┐
                              │   HTML Report       │
                              │   (single file)     │
                              └─────────────────────┘
```

---

## 📁 Project Structure

```
QA-AI-Agent/
├── config/               # Configuration (.env.example, prompts.yaml)
├── docs/                 # Guides (report usage; auto-fix docs for future enhancement)
├── scripts/              # Run scripts
│   ├── run.sh, run.ps1           # Report generation (recommended)
│   ├── trigger_auto_fix.*        # Planned: auto-fix flow (coming soon)
│   └── windows/setup.ps1         # Windows venv setup
├── src/
│   ├── agent/            # AI and analysis
│   │   ├── analyzer.py           # LLM classification (Product Bug vs Automation)
│   │   ├── memory.py             # DB access (results, history, flaky, trends)
│   │   └── summary_generator.py  # Executive summary (LLM)
│   ├── auto_fix/         # Planned: self-healing locator fixes (enhancement, coming later)
│   ├── parsers/          # HTML & data
│   │   ├── data_builder.py       # Merge DB + HTML into TestResults
│   │   ├── html_parser.py        # Parse suite index and test result HTML
│   │   └── models.py            # TestResult, TestSummary
│   ├── reporters/       # Report generation
│   │   ├── report_generator.py   # HTML report (screenshots, likely location, known failures)
│   │   ├── category_rules.py     # Refine root-cause categories (OTP, timeout, etc.)
│   │   ├── html_styles.py        # Report CSS
│   │   └── html_scripts.py      # Report JS
│   ├── database.py      # MySQL connection & table name resolution
│   ├── main.py          # Entry point (orchestrator)
│   ├── settings.py      # Config loader
│   └── utils.py         # Helpers (TestDataCache, ReportUrlBuilder, etc.)
├── testdata/            # Sample / input report directories
├── reports/              # Generated HTML reports (output)
├── tests/                # Unit tests (e.g. unit/test_memory.py, test_html_parser.py)
├── requirements.txt
└── README.md
```

---

## 🐛 Troubleshooting

### Common Issues

**Q: No test results found in database?**
A: Ensure your test runner inserts results into MySQL *before* running this agent. The agent queries by `buildTag` (directory name).

**Q: AI analysis is failing or slow?**
A: If using **Ollama**, ensure the model is pulled (`ollama pull llama3.2`). If using **OpenAI**, check your API key quota. If using **Gemini**, set `GEMINI_API_KEY` in `config/.env` (create one at [Google AI Studio](https://aistudio.google.com/apikey)).

**Q: "Table not found" error?**
A: The agent attempts to derive the table name from the report name. You can override this with `--table-name`.

---

## 📝 License
This project is open source. See [LICENSE](LICENSE) file for details.

---

## 👤 Creator
**Mukesh Rajput**

For any further help or queries, contact [@mukesh.rajput](https://www.linkedin.com/in/mukesh-rajput/)

---
<div align="center">
  <strong>Made with ❤️ for Engineering Team!</strong>
</div>
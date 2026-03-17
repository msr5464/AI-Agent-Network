"""
Main Orchestrator for the QA AI Agent.
Ties together Parser, Analyzer, Memory, and Reporters.
"""

import os
import sys
import logging
import argparse
import warnings
from pathlib import Path

# Suppress ALL warnings from urllib3 BEFORE importing anything
import urllib3
urllib3.disable_warnings(urllib3.exceptions.NotOpenSSLWarning)
warnings.filterwarnings("ignore", message=".*urllib3.*")
warnings.filterwarnings("ignore", message=".*OpenSSL.*")
warnings.filterwarnings("ignore", category=UserWarning, module="urllib3")

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import config to ensure environment variables are loaded
from src.settings import Config

from src.parsers.data_builder import (
    find_latest_report,
    get_full_report_data_from_db,
    get_execution_logs_from_html,
    get_test_durations_from_html
)
from src.agent.analyzer import TestAnalyzer
from src.agent.summary_generator import SummaryGenerator
from src.agent.memory import AgentMemory
from src.reporters.report_generator import ReportGenerator

# Configure logging
# Configure logging with UTF-8 encoding for cross-platform compatibility
import sys

# Set UTF-8 encoding for stdout/stderr to handle emoji characters
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

logging.basicConfig(
    level=logging.INFO,
    format=Config.LOG_FORMAT,
    handlers=[
        logging.FileHandler(Config.LOG_FILE_NAME, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger("Orchestrator")

def main():
    """Run the QA AI Agent workflow"""
    
    parser = argparse.ArgumentParser(description="QA AI Agent")
    parser.add_argument("--input-dir", help="Path to input directory containing test reports")
    parser.add_argument("--output-dir", help="Path to output directory for generated reports")
    parser.add_argument("--table-name", help="Explicit database table name to query (overrides auto-detection)")
    parser.add_argument("--environment", help="Environment label to display/use (e.g., qa-1, qa-2, production)")
    parser.add_argument("--skip-report", action="store_true", help="Skip HTML/report generation (useful for auto-fix only runs)")
    parser.add_argument("--skip-autofix", action="store_true", help="Skip auto-fix flow (report-only runs)")
    parser.add_argument("--autofix-tests", help="Comma-separated list of test names to auto-fix (e.g., 'TestClass.testMethod1,TestClass.testMethod2')")
    parser.add_argument("--autofix-tests-file", help="Path to file containing test names (one per line) to auto-fix")
    args = parser.parse_args()
    
    logger.info("🚀 Starting QA AI Agent...")
    if args.skip_report:
        logger.info("Report generation will be skipped (skip-report flag).")
    if args.skip_autofix:
        logger.info("Auto-fix flow will be skipped (skip-autofix flag).")
    
    def _guess_environment(report_path: str) -> str:
        name = Path(report_path).name.lower() if report_path else ""
        if Config.AUTO_FIX_ENV_OVERRIDE:
            return Config.AUTO_FIX_ENV_OVERRIDE
        for token, env in [
            ("prod", "production"),
            ("production", "production"),
            ("qa-2", "qa-2"),
            ("qa2", "qa-2"),
            ("qa-1", "qa-1"),
            ("qa1", "qa-1"),
            ("staging", "staging"),
        ]:
            if token in name:
                return env
        return ""

    # Determine report directory to process
    report_dir = args.input_dir
    if report_dir and not Path(report_dir).exists():
        candidate = Path(Config.INPUT_DIR) / report_dir
        if candidate.exists():
            report_dir = str(candidate)
    
    # Determine output directory for reports
    output_dir = args.output_dir if args.output_dir else Config.OUTPUT_DIR
    
    # If no input-dir provided, use default INPUT_DIR
    if not report_dir:
        logger.info(f"Looking for reports in {Config.INPUT_DIR}...")
        report_dir = find_latest_report(Config.INPUT_DIR)
    
    if not report_dir:
        logger.error("❌ No reports found! Exiting.")
        return
        
    logger.info(f"📂 Processing report: {report_dir}")
    # Normalize report name to handle both POSIX and Windows/UNC style paths
    from src.utils import ReportUrlBuilder
    normalized_report_dir = ReportUrlBuilder.normalize_path(report_dir)
    report_name = Path(normalized_report_dir).name
    
    # Extract buildTag from report name (folder name is the buildTag)
    build_tag = report_name
    
    # 2. Query Database for Test Results
    logger.info("💾 Querying database for test results...")
    memory = AgentMemory()
    
    try:
        # Use explicit table name if provided, otherwise it will be derived from report_name inside the method
        db_results = memory.get_test_results_by_buildtag(report_name, build_tag, table_name=args.table_name)
        
        if not db_results:
            logger.error(f"❌ No test results found in database for buildTag: {build_tag}")
            logger.error("   Make sure the test results have been inserted into the database first.")
            logger.error(f"   Check: DB_HOST={Config.DB_HOST}, DB_NAME={Config.DB_NAME}")
            return
        
        logger.info(f"📊 Found {len(db_results)} test results in database")
        
    except ValueError as e:
        logger.error(f"❌ {e}")
        return
    except Exception as e:
        logger.error(f"Failed to query database: {e}")
        logger.error("Please check your database configuration in config/.env")
        import traceback
        traceback.print_exc()
        return
    
    # 3. Calculate Flaky Tests (using database)
    logger.info("🔍 Calculating flaky tests from database...")
    all_test_names_from_db = [row.get('testcaseName', '') for row in db_results if row.get('testcaseName')]
    current_failure_names = [
        row.get('testcaseName', '') for row in db_results 
        if row.get('testStatus', '').upper() in ['FAIL', 'FAILED', 'ERROR', 'ERRORED']
    ]
    
    recurring = memory.detect_recurring_failures(
        current_failure_names,
        days=Config.FLAKY_TESTS_LAST_RUNS,
        min_occurrences=Config.FLAKY_TESTS_MIN_FAILURES,
        report_name=report_name,
        all_test_names=all_test_names_from_db,
        table_name=args.table_name
    )
    
    # 4. Trend Analysis
    trends = {
        'trend': 'UNKNOWN',
        'average_pass_rate': 0.0
    }
    try:
        trends = memory.get_trend_analysis(days=10, report_name=report_name, table_name=args.table_name)
    except Exception as e:
        logger.error(f"Error calculating trends: {e}")
    
    if recurring:
        logger.info(f"⚠️ Detected {len(recurring)} recurring failures")
    trend_value = trends.get('trend', 'UNKNOWN')
    avg_pass_rate = trends.get('average_pass_rate', 0.0) or 0.0
    logger.info(f"📈 Trend: {trend_value} (Avg Pass Rate: {avg_pass_rate:.1f}%)")
    
    # 4. Parse HTML for Execution Logs Only
    logger.info("📄 Extracting execution logs from HTML...")
    execution_logs, html_links = get_execution_logs_from_html(report_dir)
    durations = get_test_durations_from_html(report_dir)
    logger.info(f"📝 Extracted execution logs for {len(execution_logs)} tests")
    
    # 5. Merge DB Data + HTML Logs
    logger.info("🔄 Merging database results with HTML execution logs...")
    try:
        data = get_full_report_data_from_db(report_dir, db_results, execution_logs, durations, html_links)
        summary = data['summary']
        failures = [r for r in data['test_results'] if r.is_failure]
        
        logger.info(f"📊 Total tests: {summary.total}. Pass Rate: {summary.pass_rate:.1f}%")
        logger.info(f"❌ Found {len(failures)} failures")
        
    except Exception as e:
        logger.error(f"Failed to merge data: {e}")
        return

    # 3. AI Analysis
    classifications = []
    if failures:
        # Deduplicate failures by full_name before classification
        # A test might appear in multiple test suites
        seen_failures = {}
        deduplicated_failures = []
        for failure in failures:
            test_key = failure.full_name
            if test_key not in seen_failures:
                seen_failures[test_key] = failure
                deduplicated_failures.append(failure)
            else:
                # Prefer the one with execution_log or FAIL status
                existing = seen_failures[test_key]
                if failure.execution_log and not existing.execution_log:
                    deduplicated_failures.remove(existing)
                    deduplicated_failures.append(failure)
                    seen_failures[test_key] = failure
                    logger.debug(f"Replaced duplicate {test_key} with version that has execution_log")
                elif failure.status.value in ['FAIL', 'ERROR'] and existing.status.value not in ['FAIL', 'ERROR']:
                    deduplicated_failures.remove(existing)
                    deduplicated_failures.append(failure)
                    seen_failures[test_key] = failure
                    logger.debug(f"Replaced duplicate {test_key} with FAIL status")
        
        if len(failures) != len(deduplicated_failures):
            logger.info(f"⚠️ Deduplicated failures: {len(failures)} -> {len(deduplicated_failures)} (removed {len(failures) - len(deduplicated_failures)} duplicates)")
        
        logger.info("🤖 Starting AI Analysis...")
        analyzer = TestAnalyzer()
        classifications = analyzer.classify_multiple_failures(deduplicated_failures)
    else:
        logger.info("🎉 No failures to analyze!")

    # Filter recurring failures to only show those that match current test structure
    if recurring and data.get('test_results'):
        all_current_test_names = {t.full_name for t in data['test_results']}
        current_test_patterns = set()
        for t in data['test_results']:
            parts = t.full_name.split('.')
            if len(parts) >= 2:
                current_test_patterns.add('.'.join(parts[-2:]))  # ClassName.methodName
        
        filtered_recurring = []
        for r in recurring:
            test_name = r['test_name']
            parts = test_name.split('.')
            test_pattern = '.'.join(parts[-2:]) if len(parts) >= 2 else test_name
            
            if (r['in_current_run'] or 
                test_name in all_current_test_names or 
                test_pattern in current_test_patterns):
                filtered_recurring.append(r)
            else:
                logger.debug(f"Filtered out recurring failure (no match): {test_name}")
        
        recurring = filtered_recurring
        logger.info(f"After filtering: {len(recurring)} recurring failures match current tests")

    # 5. Extract API endpoints map BEFORE generating summary (same method as tables)
    logger.info("🔍 Extracting API endpoints map...")
    report_gen = ReportGenerator()
    # Parse group/branch from overview (used for header and for aligning auto-fix branch if available)
    automation_group = automation_branch = automation_env = None
    try:
        automation_group, automation_branch, automation_env = report_gen._parse_automation_group_and_branch(report_dir)
    except Exception:
        automation_group = automation_branch = automation_env = None
    if automation_env is None:
        automation_env = ""
    environment_label = (
        (args.environment or "").strip().lower()
        or automation_env
        or _guess_environment(report_dir)
        or "qa-1"
    )
    # Create cache for consistent data access
    from src.utils import TestDataCache
    test_data_cache = TestDataCache(data['test_results'], data.get('html_links', {}))
    test_api_map = report_gen.extract_test_api_map(classifications, test_data_cache)
    logger.info(f"📊 Found API endpoints for {len(test_api_map)} tests")
    
    # 5.5. Calculate category breakdown for Executive Summary (same logic as report generator)
    from src.reporters.category_rules import CategoryRuleEngine
    from src.utils import FailureClassificationUtils
    rule_engine = CategoryRuleEngine()
    category_counts = {}
    category_failures = {}
    
    # Deduplicate classifications using centralized utility (highest confidence wins)
    deduplicated_classifications = FailureClassificationUtils.deduplicate(classifications)
    
    for failure in deduplicated_classifications:
        category = rule_engine.classify(failure, test_data_cache)
        if category not in category_counts:
            category_counts[category] = 0
            category_failures[category] = []
        category_counts[category] += 1
        category_failures[category].append(failure)
    
    logger.info(f"📊 Category breakdown: {category_counts}")
    
    if args.skip_report:
        logger.info("📝 Skipping summary and HTML report generation (skip-report flag).")
        ai_summary = ""
    else:
        # 6. Generate Summary
        logger.info("📝 Generating Executive Summary...")
        generator = SummaryGenerator()
        ai_summary = generator.generate_executive_summary(
            summary=summary,
            classifications=deduplicated_classifications,
            report_name=report_name,
            category_counts=category_counts,
            category_failures=category_failures,
            recurring_failures=recurring,
            test_html_links=data.get('html_links', {}),
            test_results=data.get('test_results')
        )

        # 7. Generate HTML Report
        logger.info("🎨 Generating HTML Report...")
        html_content, _ = report_gen.generate_html_report(
            summary=summary,
            classifications=deduplicated_classifications,
            report_name=report_name,
            ai_summary=ai_summary,
            recurring_failures=recurring,
            trend=trends['trend'],
            report_dir=report_dir,
            test_results=data['test_results'],
            test_html_links=data.get('html_links', {}),
            environment=environment_label,
            output_dir=output_dir,
        )
        
        # Save HTML report with dynamic name based on report_name
        # Sanitize report_name for filename (remove invalid characters)
        safe_report_name = "".join(c for c in report_name if c.isalnum() or c in ('-', '_', ' ')).strip().replace(' ', '-')
        html_report_path = Path(output_dir) / f"AI-Generated-Report_{safe_report_name}.html"
        saved_path = report_gen.save_report(html_content, str(html_report_path))
        logger.info(f"📄 HTML report saved to: {saved_path}")
        
        # Generate auto-fix tests file (contains all auto-fixable test names)
        autofix_tests_file = report_gen.save_autofix_tests_file(
            classifications=classifications,
            output_dir=output_dir,
            report_name=report_name
        )
        if autofix_tests_file:
            logger.info(f"📋 Auto-fix tests file: {autofix_tests_file}")
            logger.info(f"   This file will be auto-detected when running auto-fix (no need to specify manually)")

    # 8. Optional Auto-Fix Flow (experimental)
    if Config.AUTO_FIX_ENABLED and not args.skip_autofix:
        autofix_results = []
        try:
            from src.auto_fix import AutoFixManager

            # Filter classifications to auto-fixable ones (Automation Issues only for now)
            def _to_autofixable(items):
                allowed = []
                for item in items:
                    if item.classification == "AUTOMATION_ISSUE" and item.confidence in ["HIGH", "MEDIUM"]:
                        allowed.append(item)
                return allowed
            
            auto_classifications = _to_autofixable(classifications)
            
            # Auto-detect autofix tests file if not explicitly provided
            autofix_tests_file = args.autofix_tests_file
            if not autofix_tests_file and not args.autofix_tests:
                # Try to find auto-generated file in output directory
                report_name_for_file = Path(report_dir).name if report_dir else None
                if report_name_for_file:
                    safe_report_name = "".join(c for c in report_name_for_file if c.isalnum() or c in ('-', '_', ' ')).strip().replace(' ', '-')
                    auto_generated_file = Path(output_dir) / f"autofix_tests_{safe_report_name}.txt"
                    if auto_generated_file.exists():
                        autofix_tests_file = str(auto_generated_file)
                        logger.info(f"📋 Auto-detected autofix tests file: {autofix_tests_file}")
            
            # Apply test selection filter if provided
            if args.autofix_tests or autofix_tests_file:
                selected_tests = set()
                
                # Read from comma-separated string
                if args.autofix_tests:
                    selected_tests.update([t.strip() for t in args.autofix_tests.split(',') if t.strip()])
                
                # Read from file
                if autofix_tests_file:
                    try:
                        with open(autofix_tests_file, 'r', encoding='utf-8') as f:
                            for line in f:
                                test_name = line.strip()
                                if test_name and not test_name.startswith('#'):
                                    selected_tests.add(test_name)
                        logger.info(f"📋 Loaded {len(selected_tests)} test names from file: {autofix_tests_file}")
                    except Exception as e:
                        logger.error(f"Failed to read test file {autofix_tests_file}: {e}")
                
                if selected_tests:
                    # Normalize test names for matching (handle various formats)
                    def normalize_test_name(name):
                        # Remove package prefix if present, keep class.method
                        parts = name.split('.')
                        if len(parts) >= 2:
                            return '.'.join(parts[-2:])  # ClassName.methodName
                        return name
                    
                    normalized_selected = {normalize_test_name(t) for t in selected_tests}
                    
                    # Filter classifications to only selected tests
                    filtered = []
                    for item in auto_classifications:
                        normalized_item = normalize_test_name(item.test_name)
                        # Match by full name or class.method
                        if (item.test_name in selected_tests or 
                            normalized_item in normalized_selected or
                            any(item.test_name.endswith(t) for t in selected_tests if '.' in t)):
                            filtered.append(item)
                    
                    logger.info(f"🔧 Test selection: {len(filtered)} tests selected from {len(auto_classifications)} auto-fixable tests")
                    auto_classifications = filtered
            
            if not auto_classifications:
                logger.info("🔧 Auto-fix enabled but no auto-fixable classifications found (need AUTOMATION_ISSUE with MEDIUM/HIGH confidence)")
            elif not Config.GITHUB_TOKEN or not Config.GITHUB_REPO_AUTOMATION:
                logger.error("🔧 Auto-fix enabled but GitHub configuration is missing (GITHUB_TOKEN or GITHUB_REPO_AUTOMATION)")
            else:
                logger.info(f"🔧 Auto-fix: attempting up to {Config.AUTO_FIX_MAX_FIXES_PER_RUN} fixes (dry_run={Config.AUTO_FIX_DRY_RUN})")
                # Create session file path for tracking passed tests
                report_name = Path(report_dir).name if report_dir else "unknown"
                session_file = Path(output_dir) / f".autofix_session_{report_name}.json"
                
                manager = AutoFixManager(
                    github_token=Config.GITHUB_TOKEN,
                    github_org=Config.GITHUB_ORG,
                    github_repo_automation=Config.GITHUB_REPO_AUTOMATION,
                    github_default_branch=automation_branch or Config.GITHUB_DEFAULT_BRANCH,
                    github_pr_reviewers=Config.GITHUB_PR_REVIEWERS,
                    llm_provider=Config.LLM_PROVIDER,
                    openai_api_key=Config.OPENAI_API_KEY,
                    openai_model=Config.OPENAI_MODEL,
                    ollama_model=Config.OLLAMA_MODEL,
                    ollama_base_url=Config.OLLAMA_BASE_URL,
                    gemini_api_key=Config.GEMINI_API_KEY,
                    gemini_model=Config.GEMINI_MODEL,
                    max_fixes_per_run=Config.AUTO_FIX_MAX_FIXES_PER_RUN,
                    dry_run=Config.AUTO_FIX_DRY_RUN,
                    run_tests_locally=True,
                    target_environment=environment_label,
                    session_file=str(session_file)  # Pass session file for tracking
                )
                autofix_results = manager.process_classifications(auto_classifications)
                logger.info(f"🔧 Auto-fix completed: {len([r for r in autofix_results if r.success])} succeeded, {len([r for r in autofix_results if r.skipped])} skipped, {len([r for r in autofix_results if not r.success and not r.skipped])} failed")
        except Exception as e:
            logger.error(f"Auto-fix flow failed: {e}")
    elif args.skip_autofix:
        logger.info("🔧 Auto-fix skipped (skip-autofix flag).")



    logger.info("🎉 QA AI Agent finished successfully!")

if __name__ == "__main__":
    main()

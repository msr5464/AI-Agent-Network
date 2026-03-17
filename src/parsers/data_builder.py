"""
Data builder - combines database results with HTML execution logs.
Database-first approach: Get test results from DB, enhance with HTML execution logs.
"""

from pathlib import Path
from typing import List, Optional, Dict
import logging
import re

from .html_parser import HTMLReportParser
from .models import TestResult, TestStatus, TestSummary
from ..utils import remove_duplicate_class_name

logger = logging.getLogger(__name__)




def find_latest_report(reports_dir: str, pattern: str = "Regression-*") -> Optional[str]:
    """
    Find the most recent report directory.
    
    Args:
        reports_dir: Path to directory containing report folders
        pattern: Glob pattern for report directories
        
    Returns:
        Path to latest report directory, or None if not found
    """
    reports_path = Path(reports_dir)
    
    if not reports_path.exists():
        logger.error(f"Reports directory not found: {reports_dir}")
        return None
    
    # Find all matching directories
    report_dirs = [d for d in reports_path.glob(pattern) if d.is_dir()]
    
    if not report_dirs:
        logger.warning(f"No report directories found matching pattern: {pattern}")
        return None
    
    # Sort by modification time (most recent first)
    latest = max(report_dirs, key=lambda d: d.stat().st_mtime)
    
    logger.info(f"Latest report: {latest.name}")
    return str(latest)


def db_row_to_test_result(db_row: Dict, execution_log: Optional[str] = None, duration: Optional[float] = None) -> TestResult:
    """
    Convert database row to TestResult object.
    
    Args:
        db_row: Dictionary from database query result
        execution_log: Optional execution log from HTML
        duration: Optional duration in seconds from HTML
        
    Returns:
        TestResult object
    """
    testcase_name = db_row.get('testcaseName', '')
    if not testcase_name:
        raise ValueError("testcaseName is required in database row")
    
    # Extract class name and method name from testcaseName
    # Format: "ClassName.methodName" or "package.ClassName.methodName"
    parts = testcase_name.split('.')
    if len(parts) >= 2:
        class_name = '.'.join(parts[:-1])  # Everything except last part
        method_name = parts[-1]  # Last part is method
    else:
        # Fallback: use testcaseName as method, empty class
        class_name = ''
        method_name = testcase_name
    
    # Parse status
    status_str = db_row.get('testStatus', '').upper().strip()
    if status_str in ['PASS', 'PASSED', 'SUCCESS', 'OK']:
        status = TestStatus.PASS
    elif status_str in ['FAIL', 'FAILED', 'FAILURE']:
        status = TestStatus.FAIL
    elif status_str in ['ERROR', 'ERRORED']:
        status = TestStatus.ERROR
    elif status_str in ['SKIP', 'SKIPPED']:
        status = TestStatus.SKIP
    else:
        # Default to PASS if unknown
        status = TestStatus.PASS
        logger.warning(f"Unknown status '{status_str}' for test {testcase_name}, defaulting to PASS")
    
    # Extract known failure (Jira ticket ID)
    known_failure = db_row.get('knownFailure', '') or None
    if known_failure:
        known_failure = str(known_failure).strip()
        if not known_failure or known_failure.upper() in ['NULL', 'NONE', '']:
            known_failure = None
    
    # Extract error information
    failure_reason = db_row.get('failureReason', '')
    error_message = None
    stack_trace = None
    error_type = None
    
    if failure_reason:
        # Clean failure reason (remove "Results Url:" and "Testcase Name:" lines)
        lines = failure_reason.split('\n')
        cleaned_lines = []
        for line in lines:
            if re.match(r'^\s*Results\s*Url\s*:', line, re.IGNORECASE):
                continue
            if re.match(r'^\s*Testcase\s*Name\s*:', line, re.IGNORECASE):
                continue
            cleaned_lines.append(line)
        failure_reason = '\n'.join(cleaned_lines).strip()
        
        if failure_reason:
            # Try to extract error type from first line
            first_line = failure_reason.split('\n')[0]
            error_match = re.search(r'(\w+Exception|\w+Error)', first_line)
            if error_match:
                error_type = error_match.group(1)
            
            # If it looks like a stack trace (has multiple lines and "at" patterns), use as stack_trace
            if '\n' in failure_reason and len(failure_reason) > 500:
                stack_trace = failure_reason
                error_message = first_line[:500]  # First line as error message
            else:
                error_message = failure_reason
    
    # Extract platform from class name (heuristic)
    platform = None
    class_name_lower = class_name.lower()
    if 'api' in class_name_lower or '.api.' in class_name_lower:
        platform = 'API'
    elif 'web' in class_name_lower or '.web.' in class_name_lower:
        platform = 'WEB'
    elif 'mobile' in class_name_lower or '.mobile.' in class_name_lower:
        platform = 'MOBILE'
    
    # Use duration from HTML if available, otherwise default to 0
    duration_seconds = duration if duration is not None else 0.0
    
    return TestResult(
        class_name=class_name,
        method_name=method_name,
        status=status,
        duration_seconds=duration_seconds,
        error_type=error_type,
        error_message=error_message,
        stack_trace=stack_trace,
        platform=platform,
        execution_log=execution_log,
        description=None,  # Will be filled from HTML if available
        known_failure=known_failure  # Jira ticket ID if test passed due to known failure
    )


def get_execution_logs_from_html(report_dir: str) -> tuple[Dict[str, str], Dict[str, str]]:
    """
    Extract execution logs from HTML files, indexed by test full_name.
    Also builds a mapping of test names to their HTML result file URLs.
    
    Args:
        report_dir: Path to report directory containing html/ subdirectory
        
    Returns:
        Tuple of (execution_logs_dict, html_links_dict)
        - execution_logs_dict: Dictionary mapping test full_name to execution_log string
        - html_links_dict: Dictionary mapping test full_name to HTML file URL
    """
    report_path = Path(report_dir)
    html_dir = report_path / 'html'
    
    if not html_dir.exists():
        logger.warning(f"No html/ directory found in {report_dir}, skipping execution log extraction")
        return {}, {}
    
    html_parser = HTMLReportParser()
    execution_logs = {}
    html_links = {}
    
    # Parse overview.html to get list of test suites
    overview_path = html_dir / 'overview.html'
    if not overview_path.exists():
        logger.warning(f"overview.html not found in {html_dir}, skipping execution log extraction")
        return {}, {}
    
    try:
        test_suites = html_parser.parse_overview(str(overview_path))
        
        # Base URL for constructing links (using Config if available)
        try:
            from ..settings import Config
            base_url = Config.DASHBOARD_BASE_URL
            report_name = Path(report_dir).name
            
            # Extract project name and job name using centralized builder
            from ..utils import ReportUrlBuilder
            
            project_name, job_name = ReportUrlBuilder.extract_project_job_from_path(report_dir)
            
            # Construct URL
            html_base_url = ReportUrlBuilder.build_dashboard_url(
                base_url, 
                report_name, 
                "html/", 
                project_name, 
                job_name
            )
        except:
            # Fallback to relative path
            html_base_url = "html/"
        
        # Parse each test suite's results file to extract execution logs and build links
        for suite in test_suites:
            results_file = html_dir / suite['results_file']
            if results_file.exists():
                try:
                    suite_results = html_parser.parse_test_results(str(results_file))
                    for result in suite_results:
                        # Store execution log
                        if result.execution_log:
                            execution_logs[result.full_name] = result.execution_log
                        
                        # Build HTML link for this test
                        # The link is to the suite's results file
                        html_link = f"{html_base_url}{suite['results_file']}"
                        html_links[result.full_name] = html_link
                        
                except Exception as e:
                    logger.error(f"Failed to parse {suite['name']} for execution logs: {e}")
        
        logger.info(f"Extracted execution logs for {len(execution_logs)} tests from HTML")
        logger.info(f"Built HTML links for {len(html_links)} tests")
        return execution_logs, html_links
        
    except Exception as e:
        logger.error(f"Failed to extract execution logs from HTML: {e}")
        return {}, {}


def get_test_durations_from_html(report_dir: str) -> Dict[str, float]:
    """
    Extract test durations from HTML files, indexed by test full_name.
    
    Args:
        report_dir: Path to report directory containing html/ subdirectory
        
    Returns:
        Dictionary mapping test full_name to duration in seconds
    """
    report_path = Path(report_dir)
    html_dir = report_path / 'html'
    
    if not html_dir.exists():
        return {}
    
    html_parser = HTMLReportParser()
    durations = {}
    
    overview_path = html_dir / 'overview.html'
    if not overview_path.exists():
        return {}
    
    try:
        test_suites = html_parser.parse_overview(str(overview_path))
        
        for suite in test_suites:
            results_file = html_dir / suite['results_file']
            if results_file.exists():
                try:
                    suite_results = html_parser.parse_test_results(str(results_file))
                    for result in suite_results:
                        if result.duration_seconds > 0:
                            durations[result.full_name] = result.duration_seconds
                except Exception as e:
                    logger.debug(f"Failed to parse {suite['name']} for durations: {e}")
        
        return durations
        
    except Exception as e:
        logger.debug(f"Failed to extract durations from HTML: {e}")
        return {}


def _find_matching_execution_log(testcase_name: str, execution_logs: Dict[str, str]) -> Optional[str]:
    """
    Find matching execution log for a testcase name using multiple matching strategies.
    
    Args:
        testcase_name: Test case name from database (e.g., "ClassName.methodName" or "package.ClassName.methodName")
        execution_logs: Dictionary mapping HTML full_name to execution_log
        
    Returns:
        Execution log string if found, None otherwise
    """
    if not execution_logs:
        return None
    
    # Strategy 1: Exact match on testcaseName
    if testcase_name in execution_logs:
        return execution_logs[testcase_name]
    
    # Strategy 2: Extract class.method and try exact match
    parts = testcase_name.split('.')
    if len(parts) >= 2:
        class_method = '.'.join(parts[-2:])  # Last two parts
        if class_method in execution_logs:
            return execution_logs[class_method]
        
        # Strategy 3: Try with cleaned class name (remove duplicates)
        class_name = parts[-2]
        method_name = parts[-1]
        cleaned_class = remove_duplicate_class_name(class_name)
        cleaned_class_method = f"{cleaned_class}.{method_name}"
        if cleaned_class_method in execution_logs:
            return execution_logs[cleaned_class_method]
        
        # Strategy 4: Try matching by method name only (case-insensitive)
        method_lower = method_name.lower()
        for html_full_name, log in execution_logs.items():
            html_parts = html_full_name.split('.')
            if len(html_parts) >= 1 and html_parts[-1].lower() == method_lower:
                return log
    
    return None


def _find_matching_duration(testcase_name: str, durations: Dict[str, float]) -> Optional[float]:
    """
    Find matching duration for a testcase name using multiple matching strategies.
    
    Args:
        testcase_name: Test case name from database
        durations: Dictionary mapping HTML full_name to duration
        
    Returns:
        Duration in seconds if found, None otherwise
    """
    if not durations:
        return None
    
    # Same matching strategies as execution logs
    if testcase_name in durations:
        return durations[testcase_name]
    
    parts = testcase_name.split('.')
    if len(parts) >= 2:
        class_method = '.'.join(parts[-2:])
        if class_method in durations:
            return durations[class_method]
        
        class_name = parts[-2]
        method_name = parts[-1]
        cleaned_class = remove_duplicate_class_name(class_name)
        cleaned_class_method = f"{cleaned_class}.{method_name}"
        if cleaned_class_method in durations:
            return durations[cleaned_class_method]
        
        method_lower = method_name.lower()
        for html_full_name, duration in durations.items():
            html_parts = html_full_name.split('.')
            if len(html_parts) >= 1 and html_parts[-1].lower() == method_lower:
                return duration
    
    return None


def get_full_report_data_from_db(report_dir: str, db_results: List[Dict], execution_logs: Dict[str, str], durations: Dict[str, float], html_links: Optional[Dict[str, str]] = None) -> dict:
    """
    Combine database results with HTML execution logs and create TestResult objects.
    Deduplicates test results by testcaseName (keeps one entry per unique test).
    
    Args:
        report_dir: Path to report directory
        db_results: List of database rows from query by buildTag
        execution_logs: Dictionary mapping test full_name to execution_log
        durations: Dictionary mapping test full_name to duration
        html_links: Optional dictionary mapping test full_name to HTML file URL
        
    Returns:
        Dictionary with 'test_results', 'summary', 'report_dir', and 'html_links'
    """
    # First pass: collect all test results, grouped by testcaseName
    test_results_by_name = {}
    matched_logs = 0
    matched_durations = 0
    
    for db_row in db_results:
        testcase_name = db_row.get('testcaseName', '')
        if not testcase_name:
            continue
        
        # Find matching execution log and duration using flexible matching
        execution_log = _find_matching_execution_log(testcase_name, execution_logs)
        duration = _find_matching_duration(testcase_name, durations)
        
        try:
            test_result = db_row_to_test_result(db_row, execution_log=execution_log, duration=duration)
            
            # Deduplicate: keep only one entry per testcaseName
            if testcase_name not in test_results_by_name:
                test_results_by_name[testcase_name] = test_result
                if execution_log:
                    matched_logs += 1
                if duration:
                    matched_durations += 1
            else:
                # If duplicate found, prefer the one with:
                # 1. Execution log (if current doesn't have one)
                # 2. FAIL/ERROR status (if current is PASS)
                existing = test_results_by_name[testcase_name]
                should_replace = False
                
                if execution_log and not existing.execution_log:
                    should_replace = True
                elif test_result.status in [TestStatus.FAIL, TestStatus.ERROR] and existing.status == TestStatus.PASS:
                    should_replace = True
                elif execution_log and existing.execution_log:
                    # Both have logs, prefer FAIL over PASS
                    if test_result.status in [TestStatus.FAIL, TestStatus.ERROR] and existing.status == TestStatus.PASS:
                        should_replace = True
                
                if should_replace:
                    test_results_by_name[testcase_name] = test_result
                    if execution_log and not existing.execution_log:
                        matched_logs += 1
                    if duration and not existing.duration_seconds:
                        matched_durations += 1
        
        except Exception as e:
            logger.warning(f"Failed to convert DB row to TestResult for {testcase_name}: {e}")
            continue
    
    # Convert dict values to list
    test_results = list(test_results_by_name.values())
    
    logger.info(f"Deduplicated: {len(db_results)} DB rows -> {len(test_results)} unique tests")
    logger.info(f"Matched execution logs: {matched_logs}/{len(test_results)} tests")
    logger.info(f"Matched durations: {matched_durations}/{len(test_results)} tests")
    
    # Calculate summary from deduplicated results
    html_parser = HTMLReportParser()
    summary = html_parser.get_summary_stats(test_results)
    
    logger.info(f"Created {len(test_results)} unique TestResult objects from database (with HTML logs merged)")
    
    return {
        'test_results': test_results,
        'summary': summary,
        'report_dir': report_dir,
        'html_links': html_links or {}
    }



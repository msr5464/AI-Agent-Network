"""
HTML Report Parser for TestNG/ReportNG HTML reports.
Extracts complete execution logs including API calls, responses, and detailed error messages.
"""

import re
import logging
import warnings
from pathlib import Path
from typing import List, Dict, Optional
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from .models import TestResult, TestStatus, TestSummary
from ..utils import remove_duplicate_class_name

# Suppress XMLParsedAsHTMLWarning - we're intentionally parsing HTML/XML hybrid files
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

logger = logging.getLogger(__name__)


class HTMLReportParser:
    """Parser for TestNG HTML reports with detailed execution logs"""
    
    def parse_overview(self, html_path: str) -> List[Dict]:
        """
        Parse overview.html to get list of test suites with their result files.
        
        Args:
            html_path: Path to overview.html file
            
        Returns:
            List of dictionaries with test suite information
        """
        path = Path(html_path)
        if not path.exists():
            raise FileNotFoundError(f"HTML file not found: {html_path}")
        
        with open(html_path, 'r', encoding='utf-8') as f:
            soup = BeautifulSoup(f.read(), 'lxml')
        
        test_suites = []
        
        # Find all test rows in the overview table
        for row in soup.find_all('tr', class_='test'):
            cells = row.find_all('td')
            if len(cells) >= 6:
                # Extract test suite link
                link_elem = cells[0].find('a')
                if link_elem:
                    suite_name = link_elem.text.strip()
                    results_file = link_elem.get('href', '')
                    
                    # Extract pass/fail counts
                    duration = cells[1].text.strip()
                    passed = int(cells[2].text.strip())
                    skipped = int(cells[3].text.strip())
                    failed = int(cells[4].text.strip())
                    
                    test_suites.append({
                        'name': suite_name,
                        'results_file': results_file,
                        'duration': duration,
                        'passed': passed,
                        'skipped': skipped,
                        'failed': failed
                    })
        
        logger.info(f"Found {len(test_suites)} test suites in overview")
        return test_suites
    
    def parse_test_results(self, html_path: str) -> List[TestResult]:
        """
        Parse individual test result HTML file to extract detailed test results.
        
        Args:
            html_path: Path to test results HTML file (e.g., suite1_test67_results.html)
            
        Returns:
            List of TestResult objects with complete execution logs
        """
        path = Path(html_path)
        if not path.exists():
            raise FileNotFoundError(f"HTML file not found: {html_path}")
        
        with open(html_path, 'r', encoding='latin-1') as f:
            content = f.read()
            soup = BeautifulSoup(content, 'lxml')
        
        results = []
        
        # Parse failed tests
        failed_tests = self._parse_test_section(soup, content, 'Failed Tests', TestStatus.FAIL)
        results.extend(failed_tests)
        
        # Parse passed tests
        passed_tests = self._parse_test_section(soup, content, 'Passed Tests', TestStatus.PASS)
        results.extend(passed_tests)
        
        # Parse skipped tests
        skipped_tests = self._parse_test_section(soup, content, 'Skipped Tests', TestStatus.SKIP)
        results.extend(skipped_tests)
        
        return results
    
    def _parse_test_section(self, soup: BeautifulSoup, raw_html: str, section_name: str, status: TestStatus) -> List[TestResult]:
        """Parse a specific section (Failed/Passed/Skipped) of test results"""
        results = []
        
        # Find the section header
        section_header = soup.find('th', string=section_name)
        if not section_header:
            return results
        
        # Find the table containing this section
        table = section_header.find_parent('table')
        if not table:
            return results
        
        # Find all test method rows
        current_class = None
        for row in table.find_all('tr'):
            # Check if this is a class name row
            class_cell = row.find('td', class_='group')
            if class_cell:
                current_class = class_cell.text.strip()
                
                # CRITICAL: Remove duplicate class name if present in class_cell text
                # Example: "Automation.Access.AccountOpening.api.dash.TestDashBusinessesApis.TestDashBusinessesApis"
                # Should become: "Automation.Access.AccountOpening.api.dash.TestDashBusinessesApis"
                current_class = remove_duplicate_class_name(current_class)
                
                continue
            
            # Check if this is a test method row
            method_cell = row.find('td', class_='method')
            if method_cell and current_class:
                # Extract test method name - need to get the actual Java method name, not the description
                # In TestNG HTML reports, the method name is usually in the anchor tag's href
                # Format: href="#ClassName.methodName" or href="html/ClassName.html#methodName"
                
                method_name = None
                
                # First, try to find an anchor tag (link) - this usually contains the actual method name
                # Also check if href contains full qualified class name
                method_link = method_cell.find('a')
                if method_link:
                    # Extract from href (e.g., href="#TestDashBusinessesApis.testComplianceCanApproveHighRiskKYB")
                    # or href="#Automation.Access.AccountOpening.api.dash.TestDashBusinessesApis.testComplianceCanApproveHighRiskKYB")
                    href = method_link.get('href', '')
                    if href:
                        # Extract method name from anchor (format: #ClassName.methodName or html/file.html#methodName)
                        if '#' in href:
                            anchor_part = href.split('#')[-1]
                            if '.' in anchor_part:
                                # Format: ClassName.methodName or Full.Qualified.ClassName.methodName
                                parts = anchor_part.split('.')
                                method_name = parts[-1]  # Last part is method name
                                
                                # Check if href contains full qualified class name (more than 2 parts before method)
                                if len(parts) >= 2:
                                    # If we have multiple parts, the class name might be in the href
                                    # Update current_class if href has full qualified name
                                    potential_class = '.'.join(parts[:-1])
                                    
                                    # CRITICAL: Remove duplicate class name if present
                                    potential_class = remove_duplicate_class_name(potential_class)
                                    
                                    # If potential_class has more segments than current_class, use it
                                    if potential_class.count('.') > current_class.count('.'):
                                        current_class = potential_class
                                        logger.debug(f"Updated class name from href: {current_class}")
                            else:
                                # Format: #methodName (just the method name)
                                method_name = anchor_part
                        # If no #, check if it's a direct method name in the href
                        elif '.' in href:
                            # href might contain ClassName.methodName or Full.Qualified.ClassName.methodName
                            parts = href.split('.')
                            if len(parts) > 1:
                                method_name = parts[-1]
                                # Check if href has full qualified class name
                                potential_class = '.'.join(parts[:-1])
                                
                                # CRITICAL: Remove duplicate class name if present
                                potential_class = remove_duplicate_class_name(potential_class)
                                
                                if potential_class.count('.') > current_class.count('.'):
                                    current_class = potential_class
                                    logger.debug(f"Updated class name from href (no #): {current_class}")
                
                # If no link or couldn't extract, try span with description class
                # Sometimes the title attribute has the method name
                if not method_name:
                    method_span = method_cell.find('span', class_='description')
                    if method_span:
                        # Check title attribute first
                        title = method_span.get('title', '').strip()
                        if title:
                            # If title looks like a method name (no spaces, camelCase), use it
                            if ' ' not in title and len(title) < 100 and (title[0].islower() or title.startswith('test')):
                                method_name = title
                            # If title contains ClassName.methodName format, extract method name
                            elif '.' in title:
                                parts = title.split('.')
                                if len(parts) > 1:
                                    potential_method = parts[-1]
                                    # Verify it looks like a method name
                                    if ' ' not in potential_method and len(potential_method) < 100:
                                        method_name = potential_method
                
                # Fallback: try to extract from row id or cell id
                if not method_name:
                    row_id = row.get('id', '')
                    if row_id and '.' in row_id:
                        method_name = row_id.split('.')[-1]
                    else:
                        cell_id = method_cell.get('id', '')
                        if cell_id and '.' in cell_id:
                            method_name = cell_id.split('.')[-1]
                
                # Last resort: use cell text, but only if it looks like a method name
                if not method_name:
                    raw_text = method_cell.text.strip()
                    # If it looks like a Java method name (no spaces, reasonable length, camelCase)
                    if raw_text and ' ' not in raw_text and len(raw_text) < 100 and (raw_text[0].islower() or raw_text.startswith('test')):
                        method_name = raw_text
                
                # Final fallback - try to extract from execution log if available
                # The execution log usually starts with "Execution started for testcase - [description]"
                # But we can also look for the method name in the log later
                if not method_name:
                    # Last resort: use description, but log a debug message
                    method_name = method_cell.text.strip() or 'UnknownMethod'
                    logger.debug(f"⚠️ Could not extract actual method name for {current_class}, using description: {method_name[:50]}")
                    logger.debug(f"   This may cause deduplication issues. Please check HTML structure.")
                
                # Extract duration
                duration_cell = row.find('td', class_='duration')
                duration_str = duration_cell.text.strip() if duration_cell else '0s'
                duration = self._parse_duration(duration_str)
                
                # Extract execution log and failure details
                result_cell = row.find('td', class_='result')
                execution_log = ''
                error_message = None
                stack_trace = None
                error_type = None
                
                if result_cell:
                    # Extract execution log from testOutput div
                    test_output = result_cell.find('div', class_='testOutput')
                    if test_output:
                        execution_log = self._extract_execution_log(test_output, raw_html, method_name)
                        
                        # If method_name looks like a description (has spaces, long), try to extract actual method name from execution log
                        if execution_log and (' ' in method_name or len(method_name) > 50):
                            # Look for method name patterns in execution log
                            # Pattern: "Failure occurred in test 'MethodName' of Class"
                            method_match = re.search(r"Failure occurred in test '([^']+)' of Class", execution_log)
                            if method_match:
                                potential_method = method_match.group(1)
                                # Verify it looks like a method name
                                if ' ' not in potential_method and len(potential_method) < 100:
                                    logger.debug(f"Extracted actual method name '{potential_method}' from execution log for {current_class}")
                                    method_name = potential_method
                            else:
                                # Try another pattern: "Test 'MethodName' of Class"
                                method_match = re.search(r"Test '([^']+)' of Class", execution_log)
                                if method_match:
                                    potential_method = method_match.group(1)
                                    if ' ' not in potential_method and len(potential_method) < 100:
                                        logger.debug(f"Extracted actual method name '{potential_method}' from execution log for {current_class}")
                                        method_name = potential_method
                    
                    # Extract failure details if this is a failed test
                    if status == TestStatus.FAIL:
                        failure_details = self._extract_failure_details(result_cell, raw_html)
                        error_message = failure_details.get('error_message')
                        stack_trace = failure_details.get('stack_trace')
                        error_type = failure_details.get('error_type', 'AssertionError')
                
                # Extract description (English description of what the test does)
                # The description is typically the visible text content of the method cell
                # In TestNG HTML reports, the cell usually contains:
                # - A link/anchor with href pointing to method (method name is in href, not visible text)
                # - Visible text that is the English description
                description = None
                
                # Get the visible text content of the method cell
                # This excludes the link text (which might be method name) and gets the actual description
                # IMPORTANT: We want to get the text that is NOT part of the link/anchor
                # The description is usually the visible text, while the method name is in the href
                cell_text = method_cell.get_text(separator=' ', strip=True)
                
                # Remove any link text from cell_text to get pure description
                # If there's a link, its text might be the method name, so we want the rest
                if method_link:
                    link_text = method_link.get_text(strip=True)
                    # Remove link text from cell_text if it appears
                    if link_text and link_text in cell_text:
                        # Replace link text with empty string to get description
                        description_text = cell_text.replace(link_text, '', 1).strip()
                        if description_text:
                            cell_text = description_text
                
                # If cell text exists and is different from method name, use it as description
                # Descriptions typically have spaces (readable English) or are longer than method names
                # Examples: "Verify that admin can do Aml search for person"
                #           "Verify that AML is NOT triggered if update address of business"
                if cell_text and cell_text != method_name:
                    # If it has spaces (readable English) or is significantly longer, it's likely a description
                    if ' ' in cell_text or len(cell_text) > len(method_name) + 5:
                        description = cell_text
                
                # Also check for span with class 'description' - sometimes the description is explicitly there
                method_span = method_cell.find('span', class_='description')
                if method_span:
                    span_text = method_span.get_text(strip=True)
                    # Use span text if it looks like a description and is different from method name
                    if span_text and span_text != method_name:
                        if ' ' in span_text or len(span_text) > len(method_name):
                            description = span_text
                
                # Fallback: if method_name itself looks like a description (has spaces), use it
                # This handles edge cases where we couldn't extract the actual method name
                if not description and method_name and ' ' in method_name:
                    description = method_name
                
                # CRITICAL: Ensure no duplicate class name before creating TestResult
                # Remove any duplicate that might have been introduced during parsing
                current_class = remove_duplicate_class_name(current_class)
                
                # Determine platform from class name
                platform = self._extract_platform(current_class)
                
                result = TestResult(
                    class_name=current_class,
                    method_name=method_name,
                    status=status,
                    duration_seconds=duration,
                    error_type=error_type,
                    error_message=error_message,
                    stack_trace=stack_trace,
                    platform=platform,
                    execution_log=execution_log,  # Full execution log
                    description=description  # English description
                )
                
                results.append(result)
        
        return results
    
    def _extract_execution_log(self, test_output_div, raw_html: str, method_name: str = None) -> str:
        """
        Extract the complete execution log from the testOutput div for a specific test case.
        Captures everything from start to end, including:
        - Start: "Method arguments:" or "Execution started for testcase"
        - Middle: All test steps, API calls, verifications
        - End: "EXECUTION OF TESTCASE ENDS HERE" + failure details + exception
        This ensures we only analyze logs from the specific test case, not mixing with other tests.
        """
        # Get all text content, preserving structure
        log_lines = []
        
        # Find all font tags with timestamps
        for font_tag in test_output_div.find_all('font', style=re.compile('font-size:110%')):
            text = font_tag.get_text(separator=' ', strip=True)
            # Clean up HTML entities
            text = text.replace('&nbsp', ' ').replace('&nbsp;', ' ')
            if text:
                log_lines.append(text)
        
        full_log = '\n'.join(log_lines)
        
        # Isolate logs for this specific test case by finding start and end markers
        # Start marker: "Method arguments:" (first) or "Execution started for testcase"
        start_marker_patterns = [
            r'Method arguments:',  # This appears first, before execution starts
            r'Execution started for testcase',
            r'Execution started for test',
            r'Test Case Details'
        ]
        
        # Find the start of this test case's execution
        start_idx = 0
        for pattern in start_marker_patterns:
            match = re.search(pattern, full_log, re.IGNORECASE)
            if match:
                start_idx = match.start()
                break
        
        # Find the end of this test case's execution
        # We need to capture everything including failure details after "EXECUTION OF TESTCASE ENDS HERE"
        end_idx = len(full_log)
        
        # First, find where "EXECUTION OF TESTCASE ENDS HERE" appears
        execution_end_match = re.search(r'EXECUTION OF TESTCASE ENDS HERE', full_log[start_idx:], re.IGNORECASE)
        if execution_end_match:
            execution_end_pos = start_idx + execution_end_match.end()
            
            # After "EXECUTION OF TESTCASE ENDS HERE", we need to capture:
            # - Screenshot info
            # - Console logs
            # - "Failure occurred in test" message
            # - "Total time taken by Test" message
            # - The actual exception/stack trace
            
            # Look for the exception/stack trace - this is the real end
            # Common exception patterns
            exception_patterns = [
                r'org\.openqa\.selenium\.[\w]+Exception:',  # Selenium exceptions
                r'java\.lang\.[\w]+Exception:',  # Java exceptions
                r'java\.lang\.[\w]+Error:',  # Java errors
                r'AssertionError:',  # Assertion errors
                r'at [\w\.]+\([\w\.]+\.java:\d+\)',  # Stack trace line
            ]
            
            # Search for exception after execution ends
            remaining_after_end = full_log[execution_end_pos:]
            exception_found = False
            
            for pattern in exception_patterns:
                exception_match = re.search(pattern, remaining_after_end)
                if exception_match:
                    # Found exception, capture everything up to and including it
                    # Also capture some lines after the exception for full context
                    exception_pos = execution_end_pos + exception_match.end()
                    # Look for end of exception (usually blank line or next test case start)
                    # Capture at least 2000 chars after exception start to get full stack trace
                    end_idx = min(exception_pos + 2000, len(full_log))
                    
                    # But also check if there's a next test case starting
                    next_test_match = re.search(r'Method arguments:|Execution started for testcase', remaining_after_end[exception_match.end():], re.IGNORECASE)
                    if next_test_match:
                        end_idx = execution_end_pos + exception_match.end() + next_test_match.start()
                    
                    exception_found = True
                    break
            
            if not exception_found:
                # No exception found, but we have execution end marker
                # Look for "Failure occurred in test" or "Total time taken by Test" as end markers
                failure_markers = [
                    r'Failure occurred in test',
                    r'Total time taken by Test'
                ]
                
                for pattern in failure_markers:
                    failure_match = re.search(pattern, remaining_after_end, re.IGNORECASE)
                    if failure_match:
                        # Capture everything including failure message
                        end_idx = execution_end_pos + failure_match.end() + 1000  # Add extra for failure details
                        break
                else:
                    # If no failure markers found, capture everything after execution end
                    # But limit to reasonable size (5000 chars after execution end)
                    end_idx = min(execution_end_pos + 5000, len(full_log))
        else:
            # No "EXECUTION OF TESTCASE ENDS HERE" marker found
            # Try to find other end markers
            end_marker_patterns = [
                r'Total time taken by Test',
                r'Failure occurred in test',
                r'org\.openqa\.selenium\.[\w]+Exception:',  # Exception as end marker
            ]
            
            for pattern in end_marker_patterns:
                remaining_log = full_log[start_idx:]
                match = re.search(pattern, remaining_log, re.IGNORECASE)
                if match:
                    end_idx = start_idx + match.end() + 2000  # Include exception/stack trace
                    break
        
        # Extract only the logs for this specific test case
        isolated_log = full_log[start_idx:end_idx]
        
        # If we couldn't find markers, use the full log but log a warning
        if start_idx == 0 and end_idx == len(full_log):
            logger.debug(f"Could not find test case boundaries for {method_name}, using full log")
            isolated_log = full_log
        
        # CRITICAL: Do NOT truncate logs - we need the full execution log for accurate API extraction
        # The API extraction logic needs to see all API calls to find the one immediately before the failure
        # Truncation was causing API calls in the middle section to be lost, leading to incorrect API identification
        # If memory becomes an issue, we can increase this limit, but for now, keep full logs
        
        return isolated_log
    
    def _extract_failure_details(self, result_cell, raw_html: str) -> Dict:
        """Extract assertion error and stack trace from failed test"""
        details = {
            'error_type': 'AssertionError',
            'error_message': '',
            'stack_trace': ''
        }
        
        # Look for exception divs
        exception_div = result_cell.find('div', id=re.compile('exception-'))
        if exception_div:
            # Get the full stack trace
            stack_trace_text = exception_div.get_text(separator='\n', strip=True)
            details['stack_trace'] = stack_trace_text
            
            # Extract error message (first line usually)
            lines = stack_trace_text.split('\n')
            if lines:
                details['error_message'] = lines[0][:500]  # First line, max 500 chars
        
        # Also look for the assertion error link
        error_link = result_cell.find('a', href=re.compile('toggleElement'))
        if error_link:
            error_text = error_link.get_text(separator=' ', strip=True)
            if error_text and not details['error_message']:
                details['error_message'] = error_text[:500]
        
        return details
    
    def _remove_duplicate_class_name(self, class_name: str) -> str:
        """
        Remove duplicate class name segments from a class name string.
        Checks for consecutive duplicate segments anywhere in the string.
        
        Examples:
        - "Automation.Access.AccountOpening.api.dash.TestDashBusinessesApis.TestDashBusinessesApis.testMethod"
          -> "Automation.Access.AccountOpening.api.dash.TestDashBusinessesApis.testMethod"
        - "Automation.Access.AccountOpening.api.dash.TestDashBusinessesApis.TestDashBusinessesApis"
          -> "Automation.Access.AccountOpening.api.dash.TestDashBusinessesApis"
        - "TestDashBusinessesApis.TestDashBusinessesApis"
          -> "TestDashBusinessesApis"
        """
        if not class_name or '.' not in class_name:
            return class_name
        
        parts = class_name.split('.')
        if len(parts) < 2:
            return class_name
        
        # Remove consecutive duplicates anywhere in the string
        # Check for consecutive duplicate segments
        cleaned_parts = []
        i = 0
        while i < len(parts):
            # Add current part
            cleaned_parts.append(parts[i])
            # Skip next part if it's the same as current (duplicate)
            if i + 1 < len(parts) and parts[i] == parts[i + 1]:
                logger.debug(f"Removed duplicate class name '{parts[i]}' from '{class_name}'")
                i += 2  # Skip both the current and duplicate
            else:
                i += 1
        
        cleaned = '.'.join(cleaned_parts)
        return cleaned
    
    def _extract_platform(self, class_name: str) -> str:
        """Extract platform (WEB/API/MOBILE) from class name"""
        class_lower = class_name.lower()
        
        if '.api.' in class_lower:
            return 'API'
        elif '.mobile.' in class_lower:
            return 'MOBILE'
        elif '.web.' in class_lower:
            return 'WEB'
        else:
            return 'UNKNOWN'
    
    def _parse_duration(self, duration_str: str) -> float:
        """Parse duration string (e.g., '83.182s') to float seconds"""
        try:
            return float(duration_str.replace('s', '').strip())
        except ValueError:
            return 0.0
    
    def get_summary_stats(self, results: List[TestResult]) -> TestSummary:
        """Calculate summary statistics from test results"""
        total = len(results)
        passed = sum(1 for r in results if r.status == TestStatus.PASS)
        failed = sum(1 for r in results if r.status == TestStatus.FAIL)
        errors = sum(1 for r in results if r.status == TestStatus.ERROR)
        skipped = sum(1 for r in results if r.status == TestStatus.SKIP)
        duration = sum(r.duration_seconds for r in results)
        
        return TestSummary(
            total=total,
            passed=passed,
            failed=failed,
            skipped=skipped,
            errors=errors,
            duration_seconds=duration
        )

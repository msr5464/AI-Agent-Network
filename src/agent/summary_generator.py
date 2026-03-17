"""
Summary generator for test results.
Creates executive summaries and detailed reports.
"""

import logging
import html as html_escape
import re
from typing import List, Dict, Optional

from ..parsers.models import TestSummary
from .analyzer import FailureClassification
from ..utils import remove_duplicate_class_name, normalize_root_cause
from ..settings import Config
# Constants are now in Config class

logger = logging.getLogger(__name__)


class SummaryGenerator:
    """Generates executive summaries of test results"""

    def __init__(self):
        """Initialize the summary generator with configured LLM provider (openai, ollama, or gemini)."""
        self.llm_provider = Config.LLM_PROVIDER
        logger.info(f"Initializing SummaryGenerator with provider: {self.llm_provider}")

        if self.llm_provider == 'openai':
            self._init_openai()
        elif self.llm_provider == 'gemini':
            self._init_gemini()
        else:
            self._init_ollama()


    def _init_ollama(self):
        """Initialize Ollama LLM for summary generation."""
        try:
            from langchain_ollama import OllamaLLM
            self.model = Config.OLLAMA_MODEL
            self.base_url = Config.OLLAMA_BASE_URL
            self.llm = OllamaLLM(
                model=self.model,
                base_url=self.base_url,
                temperature=0.5
            )
            logger.info(f"✅ Ollama LLM initialized for summary generation: {self.model}")
        except ImportError:
            logger.error("langchain-ollama not installed.")
            raise
        except Exception as e:
            logger.error(f"Failed to initialize Ollama: {e}")
            raise

    def _init_openai(self):
        """Initialize OpenAI LLM for summary generation."""
        try:
            from langchain_openai import ChatOpenAI
            api_key = Config.OPENAI_API_KEY
            if not api_key:
                raise ValueError("OPENAI_API_KEY not found in environment variables")
            self.model = Config.OPENAI_MODEL
            self.llm = ChatOpenAI(
                model=self.model,
                api_key=api_key,
                temperature=0.5
            )
            logger.info(f"✅ OpenAI LLM initialized for summary generation: {self.model}")
        except ImportError:
            logger.error("langchain-openai not installed.")
            raise
        except Exception as e:
            logger.error(f"Failed to initialize OpenAI: {e}")
            raise

    def _init_gemini(self):
        """Initialize Google Gemini LLM for summary generation."""
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
        except ImportError:
            logger.warning("Gemini provider selected but langchain-google-genai is not installed.")
            raise ImportError("langchain-google-genai not installed")

        try:
            api_key = Config.GEMINI_API_KEY
            if not api_key:
                raise ValueError("GEMINI_API_KEY not found in environment variables")
            self.model = Config.GEMINI_MODEL
            self.llm = ChatGoogleGenerativeAI(
                model=self.model,
                google_api_key=api_key,
                temperature=0.5
            )
            logger.info(f"✅ Gemini LLM initialized for summary generation: {self.model}")
        except Exception as e:
            logger.error(f"Failed to initialize Gemini: {e}")
            raise
    
    def generate_executive_summary(
        self,
        summary: TestSummary,
        classifications: List[FailureClassification],
        report_name: str = "Test Report",
        category_counts: Optional[Dict[str, int]] = None,
        category_failures: Optional[Dict[str, List[FailureClassification]]] = None,
        recurring_failures: Optional[List[Dict]] = None,
        test_html_links: Optional[Dict[str, str]] = None,
        test_results: Optional[List] = None
    ) -> str:
        """
        Generate an HTML-formatted executive summary of test results with insights.
        
        Args:
            summary: TestSummary with overall statistics
            classifications: List of failure classifications
            report_name: Name of the report
            category_counts: Dictionary mapping category names to failure counts
            category_failures: Dictionary mapping category names to failure lists
            recurring_failures: List of recurring/flaky test failures
            
        Returns:
            HTML-formatted executive summary string
        """
        logger.info("Generating executive summary...")
        
        # Execution insights (failure breakdown, flaky tests, quick wins, etc.)
        html_summary = self._generate_html_executive_summary(
            summary,
            category_counts=category_counts,
            category_failures=category_failures,
            recurring_failures=recurring_failures,
            test_html_links=test_html_links,
            test_results=test_results
        )
        
        logger.info("✅ Executive summary generated")
        return html_summary
    
    def _identify_common_failures_by_signature(self, category_failures: Dict[str, List[FailureClassification]]) -> Dict[str, Dict]:
        """
        Identify failures that share the same AI-generated failure_signature.
        Groups failures by signature to find common issues affecting multiple tests.
        
        This is simpler and more accurate than normalizing root_cause text,
        as the AI already generates concise, groupable signatures.
        
        Args:
            category_failures: Dictionary mapping category to list of FailureClassification objects
            
        Returns:
            Dictionary mapping failure_signature to:
            {
                'signature': failure signature text,
                'tests': list of test names affected,
                'category': root cause category,
                'root_causes': list of {test_name, root_cause} dicts for details
            }
        """
        signature_groups = {}
        from ..utils import normalize_failure_signature
        
        # Iterate through all failures across all categories
        for category, failures in category_failures.items():
            for failure in failures:
                # Use AI-generated signature (same field used by Representative Signals)
                if hasattr(failure, 'failure_signature') and failure.failure_signature:
                    original_sig = failure.failure_signature.strip()
                else:
                    # Fallback for legacy data without failure_signature
                    original_sig = "Unclassified Failure"
                
                # Normalize for grouping key (case insensitive, ignore trailing dots)
                group_key = normalize_failure_signature(original_sig)
                
                # Group by normalized signature
                if group_key not in signature_groups:
                    signature_groups[group_key] = {
                        'signature': original_sig,  # Keep the first encountered original signature for display
                        'tests': [],
                        'category': failure.root_cause_category,
                        'root_causes': []  # Store root causes for tooltip/detail view
                    }
                
                # Add test if not already present
                if failure.test_name not in signature_groups[group_key]['tests']:
                    signature_groups[group_key]['tests'].append(failure.test_name)
                    # Store root cause for this specific test (for detail view)
                    signature_groups[group_key]['root_causes'].append({
                        'test_name': failure.test_name,
                        'root_cause': failure.root_cause
                    })
        
        # Filter to only include signatures affecting 2+ tests (Quick Wins!)
        filtered = {data['signature']: data for key, data in signature_groups.items() if len(data['tests']) >= 2}
        
        # Debug logging
        logger.info(f"Quick Wins: Found {len(signature_groups)} unique normalized signatures across all failures")
        logger.info(f"Quick Wins: After filtering (2+ tests), {len(filtered)} signatures qualify")
        if len(signature_groups) > 0 and len(filtered) == 0:
            logger.warning("⚠️ Quick Wins: All failures have unique signatures - no common patterns found")
            for key, data in list(signature_groups.items())[:5]:  # Show first 5
                logger.info(f"  - '{data['signature']}': {len(data['tests'])} test(s)")
        
        return filtered
    
    
    def _generate_html_executive_summary(
        self,
        summary: TestSummary,
        category_counts: Optional[Dict[str, int]] = None,
        category_failures: Optional[Dict[str, List[FailureClassification]]] = None,
        recurring_failures: Optional[List[Dict]] = None,
        test_html_links: Optional[Dict[str, str]] = None,
        test_results: Optional[List] = None
    ) -> str:
        """Generate HTML-formatted executive summary aligned with Root Cause Categories and Flaky Tests sections"""
        
        html = []
        
        # Category styles matching Root Cause Categories section (used across multiple sections)
        category_styles = {
            'ELEMENT_NOT_FOUND': {'icon': '🔍', 'label': 'Element Locator Issues', 'color': '#f97316'},
            'TIMEOUT': {'icon': '⏱️', 'label': 'Page Load Timeout Issues', 'color': '#facc15'},
            'ASSERTION_FAILURE': {'icon': '❌', 'label': 'Assertion Mismatch Issues', 'color': '#dc2626'},
            'ENVIRONMENT_ISSUE': {'icon': '🏗️', 'label': 'Environment Issues', 'color': '#8b5cf6'},
            'OTHER': {'icon': '❓', 'label': 'Miscellaneous Issues', 'color': '#475569'}
        }
        
        # Note: Test Execution Overview removed - Dashboard at top already shows this information
        
        # 0. No Data Message
        if summary.total == 0:
            html.append('<div style="text-align: center; padding: 24px; background: #fff3cd; border-radius: 8px; border: 1px solid #ffeeba; margin-bottom: 20px; font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, Helvetica, Arial, sans-serif;">')
            html.append('<h3 style="color: #856404; margin-top: 0; margin-bottom: 8px; font-size: 20px; font-weight: 600;">⚠️ No Test Results Found</h3>')
            html.append('<p style="color: #856404; margin-bottom: 0; font-size: 15px;">No test execution data was found for this report. Please check if the data has been uploaded to the database.</p>')
            html.append('</div>')
            return ''.join(html)

        # 1. All Tests Passed Message
        if summary.failed == 0 and summary.errors == 0:
            html.append('<div style="text-align: center; padding: 24px; background: #f0fdf4; border-radius: 8px; border: 1px solid #bbf7d0; margin-bottom: 20px; font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, Helvetica, Arial, sans-serif;">')
            html.append('<div style="font-size: 48px; margin-bottom: 16px;">🎉</div>')
            html.append('<h3 style="color: #166534; margin-top: 0; margin-bottom: 8px; font-size: 20px; font-weight: 600;">Excellent! All tests passed.</h3>')
            html.append('<p style="color: #15803d; margin-bottom: 0; font-size: 15px;">No failures were detected in this execution. The system appears stable.</p>')
            html.append('</div>')
        
        # 2. Failure Breakdown by Category (aligned with Root Cause Categories section)
        if category_counts and category_failures:
            html.append('<div style="margin-bottom: 15px;">')
            html.append('<h3 style="color: #2c3e50; margin-bottom: 8px; font-size: 16px; border-bottom: 2px solid #6610f2; padding-bottom: 6px; font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, Helvetica, Arial, sans-serif;">🧩 Failure Breakdown by Category</h3>')
            html.append('<p style="margin-bottom: 8px; color: #666; font-size: 15px; font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, Helvetica, Arial, sans-serif;">Failures grouped by root cause type. <a href="#root-cause-categories" style="color: #6366f1; text-decoration: none;">View detailed breakdown →</a></p>')
            
            total_failures = sum(category_counts.values())
            if total_failures > 0:
                # Sort categories by count (descending)
                sorted_categories = sorted(category_counts.items(), key=lambda x: x[1], reverse=True)
                
                # Calculate dynamic chart size based on number of categories
                num_categories = len(sorted_categories)
                # Scale chart size based on number of categories to accommodate all items
                if num_categories <= 2:
                    chart_size = 100
                elif num_categories <= 3:
                    chart_size = 120
                elif num_categories <= 5:
                    chart_size = 140  # Increased for 4-5 categories
                elif num_categories <= 8:
                    chart_size = 160
                elif num_categories <= 12:
                    chart_size = 180
                else:
                    chart_size = 200
                
                # Calculate radii maintaining proportions
                center_x = chart_size / 2
                center_y = chart_size / 2
                outer_radius = chart_size / 2
                inner_radius = outer_radius * 0.75  # Maintain 75% ratio for donut thickness
                
                # Calculate font sizes proportionally
                center_font_size = int(chart_size * 0.167)  # ~20px for 120px chart
                center_label_font_size = int(chart_size * 0.083)  # ~10px for 120px chart
                
                # Create a donut chart visualization with category details
                html.append('<div style="background: #fff; padding: 12px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, Helvetica, Arial, sans-serif;">')
                html.append('<div style="display: grid; grid-template-columns: auto 1fr; gap: 18px; align-items: center;">')
                
                # Left side: Full circle donut chart using SVG paths
                
                html.append(f'<div style="position: relative; width: {chart_size}px; height: {chart_size}px; flex-shrink: 0;">')
                html.append(f'<svg width="{chart_size}" height="{chart_size}" viewBox="0 0 {chart_size} {chart_size}">')
                
                # Calculate segments for full circle donut chart using path arcs
                import math
                chart_segments = []
                current_angle = -90  # Start from top (12 o'clock)
                
                for category_key, count in sorted_categories:  # Use all categories for full circle
                    percentage = (count / total_failures) * 100
                    style = category_styles.get(category_key, {
                        'icon': '❓',
                        'label': category_key.replace('_', ' ').title(),
                        'color': '#6c757d'
                    })
                    
                    # Calculate angle for this segment
                    angle = (percentage / 100) * 360
                    start_angle = current_angle
                    end_angle = current_angle + angle
                    
                    # Convert angles to radians
                    start_rad = math.radians(start_angle)
                    end_rad = math.radians(end_angle)
                    
                    # Calculate arc coordinates
                    x1 = center_x + outer_radius * math.cos(start_rad)
                    y1 = center_y + outer_radius * math.sin(start_rad)
                    x2 = center_x + outer_radius * math.cos(end_rad)
                    y2 = center_y + outer_radius * math.sin(end_rad)
                    
                    x3 = center_x + inner_radius * math.cos(end_rad)
                    y3 = center_y + inner_radius * math.sin(end_rad)
                    x4 = center_x + inner_radius * math.cos(start_rad)
                    y4 = center_y + inner_radius * math.sin(start_rad)
                    
                    # Large arc flag (1 if angle > 180, 0 otherwise)
                    large_arc = 1 if angle > 180 else 0
                    
                    # Create path for donut segment
                    path_d = f"M {x1} {y1} A {outer_radius} {outer_radius} 0 {large_arc} 1 {x2} {y2} L {x3} {y3} A {inner_radius} {inner_radius} 0 {large_arc} 0 {x4} {y4} Z"
                    
                    segment_id = f"donut-segment-{len(chart_segments)}"
                    chart_segments.append({
                        'category_key': category_key,
                        'count': count,
                        'percentage': percentage,
                        'style': style,
                        'path_d': path_d,
                        'segment_id': segment_id
                    })
                    
                    # Escape HTML for tooltip content
                    label_escaped = html_escape.escape(style['label'])
                    icon_escaped = html_escape.escape(style['icon'])
                    
                    html.append(f'''
                        <path
                            id="{segment_id}"
                            d="{path_d}"
                            fill="{style['color']}"
                            stroke="none"
                            style="cursor: pointer; transition: opacity 0.2s;"
                            onmouseover="showDonutTooltip(event, '{segment_id}', '{label_escaped}', '{icon_escaped}', {count}, {percentage:.1f}, '{style['color']}')"
                            onmouseout="hideDonutTooltip('{segment_id}')"
                            onmousemove="updateDonutTooltipPosition(event)"
                        />
                    ''')
                    
                    # Move to next segment
                    current_angle = end_angle
                
                html.append('</svg>')
                
                # Center text showing total (with dynamic font sizes)
                html.append(f'''
                    <div style="position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); text-align: center;">
                        <div style="font-size: {center_font_size}px; font-weight: 700; color: #111827;">{total_failures}</div>
                        <div style="font-size: {center_label_font_size}px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.5px;">Total</div>
                    </div>
                ''')
                
                # Tooltip will be created dynamically in JavaScript
                html.append('</div>')
                
                # Right side: Legend with details
                html.append('<div style="display: flex; flex-direction: column; gap: 8px; min-width: 0;">')
                
                for segment in chart_segments:  # Show all categories in detail
                    category_key = segment['category_key']
                    count = segment['count']
                    percentage = segment['percentage']
                    style = segment['style']
                    
                    html.append(f'''
                        <div style="display: flex; align-items: center; gap: 10px; padding: 8px; border-radius: 6px; transition: background 0.2s;" onmouseover="this.style.background='#f9fafb'" onmouseout="this.style.background='transparent'">
                            <div style="display: flex; align-items: center; gap: 8px; flex: 1; min-width: 0;">
                                <div style="width: 14px; height: 14px; border-radius: 4px; background: {style['color']}; flex-shrink: 0;"></div>
                                <div style="flex: 1; min-width: 0;">
                                    <div style="display: flex; align-items: center; gap: 5px;">
                                        <span style="font-size: 13px;">{style['icon']}</span>
                                        <span style="font-size: 12px; color: #374151; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">{style['label']}</span>
                                    </div>
                                </div>
                            </div>
                            <div style="display: flex; align-items: baseline; gap: 6px; flex-shrink: 0;">
                                <span style="font-size: 14px; font-weight: 600; color: #111827;">{count} tests</span>
                                <span style="font-size: 11px; color: #6b7280;">({percentage:.1f}% of all failures)</span>
                            </div>
                        </div>
                    ''')
                
                html.append('</div>')
                html.append('</div>')
                html.append('</div>')
            
            html.append('</div>')
        
        # 2. Flaky Tests Summary (statistical overview)
        if recurring_failures:
            html.append('<div style="margin-bottom: 15px;">')
            html.append('<h3 style="color: #2c3e50; margin-bottom: 8px; font-size: 16px; border-bottom: 2px solid #6c757d; padding-bottom: 6px; font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, Helvetica, Arial, sans-serif;">⚠️ Flaky Tests</h3>')
            
            total_flaky = len(recurring_failures)
            html.append(f'<p style="margin-bottom: 8px; color: #666; font-size: 15px; font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, Helvetica, Arial, sans-serif;">{total_flaky} flaky test{"" if total_flaky == 1 else "s"} detected (failed {Config.FLAKY_TESTS_MIN_FAILURES}+ times in last {Config.FLAKY_TESTS_LAST_RUNS} runs). <a href="#flaky-tests" style="color: #6366f1; text-decoration: none;">View all →</a></p>')
            
            # Group by failure count (occurrences)
            failure_count_groups = {}
            for failure in recurring_failures:
                occurrences = failure.get('occurrences', 0)
                if occurrences not in failure_count_groups:
                    failure_count_groups[occurrences] = []
                failure_count_groups[occurrences].append(failure)
            
            # Show failure count breakdown (sorted by count descending)
            sorted_counts = sorted(failure_count_groups.items(), key=lambda x: x[0], reverse=True)
            
            for occurrences, tests in sorted_counts[:5]:  # Show top 5 failure count groups
                count = len(tests)
                percentage = (occurrences / Config.FLAKY_TESTS_LAST_RUNS) * 100
                
                # Determine severity color based on failure count
                if occurrences >= 9:
                    count_color = "#dc3545"  # Red - critical
                elif occurrences >= 7:
                    count_color = "#e67e22"  # Orange - high
                elif occurrences >= 5:
                    count_color = "#f39c12"  # Yellow - medium
                else:
                    count_color = "#3498db"  # Blue - low
                
                html.append(f'''
                    <div style="background: #fff; padding: 10px 12px; border-radius: 6px; margin-bottom: 5px; border-left: 3px solid {count_color}; box-shadow: 0 1px 2px rgba(0,0,0,0.05); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;">
                        <div style="display: flex; align-items: center; gap: 8px;">
                            <span style="font-size: 12px; font-weight: 600; color: #111827; background: rgba(16, 185, 129, 0.08); padding: 2px 6px; border-radius: 3px; white-space: nowrap;">
                                {count} test{"" if count == 1 else "s"}
                            </span>
                            <span style="font-size: 12px; color: #374151; flex: 1;">
                                failed <strong style="color: {count_color};">{occurrences} out of {Config.FLAKY_TESTS_LAST_RUNS}</strong> times ({percentage:.0f}% failure rate)
                            </span>
                        </div>
                    </div>
                ''')
            
            html.append('</div>')
        
        # 2.5. Known Failures Summary
        if test_results:
            known_failures_list = [t for t in test_results if t.known_failure]
            if known_failures_list:
                html.append('<div style="margin-bottom: 15px;">')
                html.append('<h3 style="color: #2c3e50; margin-bottom: 8px; font-size: 16px; border-bottom: 2px solid #ff9800; padding-bottom: 6px; font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, Helvetica, Arial, sans-serif;">🔖 Known Failures</h3>')
                
                total_known = len(known_failures_list)
                html.append(f'<p style="margin-bottom: 8px; color: #666; font-size: 15px; font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, Helvetica, Arial, sans-serif;">{total_known} test{"" if total_known == 1 else "s"} marked as PASSED due to known issues (Jira tickets linked). <a href="#known-failures" style="color: #6366f1; text-decoration: none;">View all →</a></p>')
                
                # Group by Jira ticket ID
                ticket_groups = {}
                for test in known_failures_list:
                    ticket_id = test.known_failure.strip()
                    if ticket_id not in ticket_groups:
                        ticket_groups[ticket_id] = []
                    ticket_groups[ticket_id].append(test)
                
                # Show ticket breakdown (sorted by count descending)
                sorted_tickets = sorted(ticket_groups.items(), key=lambda x: len(x[1]), reverse=True)
                
                for ticket_id, tests in sorted_tickets[:5]:  # Show top 5 tickets
                    count = len(tests)
                    jira_url = f"{Config.JIRA_BASE_URL.rstrip('/')}/browse/{ticket_id}"
                    
                    html.append(f'''
                        <div style="background: #fff; padding: 10px 12px; border-radius: 6px; margin-bottom: 5px; border-left: 3px solid #ff9800; box-shadow: 0 1px 2px rgba(0,0,0,0.05); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;">
                            <div style="display: flex; align-items: center; gap: 8px;">
                                <span style="font-size: 12px; font-weight: 600; color: #111827; background: rgba(255, 152, 0, 0.08); padding: 2px 6px; border-radius: 3px; white-space: nowrap;">
                                    {count} test{"" if count == 1 else "s"}
                                </span>
                                <span style="font-size: 12px; color: #374151; flex: 1;">
                                    failed but marked as passed due to known issue <a href="{jira_url}" target="_blank" style="color: #ff9800; text-decoration: none; font-weight: 600;">{ticket_id}</a>
                                </span>
                            </div>
                        </div>
                    ''')
                
                html.append('</div>')
        
        # 3. Quick Wins (Common Failures by Signature - Fix Once, Resolve Multiple)
        if category_failures:
            common_failures = self._identify_common_failures_by_signature(category_failures)
            if common_failures:
                html.append('<div style="margin-bottom: 15px;">')
                html.append('<h3 style="color: #2c3e50; margin-bottom: 8px; font-size: 16px; border-bottom: 2px solid #10b981; padding-bottom: 6px; font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, Helvetica, Arial, sans-serif;">⚡ Quick Wins</h3>')
                html.append('<p style="margin-bottom: 8px; color: #666; font-size: 15px; font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, Helvetica, Arial, sans-serif;">Fix once, resolve multiple test failures.</p>')
                
                # Calculate total failures for impact percentage
                total_failures = sum(len(failures) for failures in category_failures.values())
                
                # Sort by number of affected tests (descending)
                sorted_common_failures = sorted(common_failures.items(), key=lambda x: len(x[1]['tests']), reverse=True)
                
                for idx, (signature, data) in enumerate(sorted_common_failures[:5]):  # Show top 5 quick wins
                    affected_tests = data['tests']
                    num_tests = len(affected_tests)
                    failure_signature = data['signature']
                    category = data['category']
                    
                    # Calculate impact percentage
                    impact_pct = (num_tests / total_failures * 100) if total_failures > 0 else 0
                    
                    # Get category style
                    category_style = category_styles.get(category, {
                        'icon': '❓',
                        'label': category.replace('_', ' ').title(),
                        'color': '#475569'
                    })
                    
                    # Generate collapsible section ID
                    details_id = f"quick-win-{idx}"
                    
                    # Build test names list with links and copy buttons
                    test_names_html = []
                    for test_name in affected_tests:
                        from ..utils import extract_class_and_method
                        class_name, method_name = extract_class_and_method(test_name)
                        display_name = f"{class_name}.{method_name}"
                        display_name_escaped = html_escape.escape(display_name)
                        test_name_js = html_escape.escape(test_name).replace("'", "\\'")
                        
                        # Get HTML link if available - try multiple name formats
                        html_link = None
                        if test_html_links:
                            # Try exact match first
                            html_link = test_html_links.get(test_name)
                            # Try with class.method format
                            if not html_link:
                                html_link = test_html_links.get(display_name)
                            # Try normalized versions (remove duplicate class names)
                            if not html_link:
                                normalized_test_name = remove_duplicate_class_name(test_name)
                                html_link = test_html_links.get(normalized_test_name)
                            # Try with full class path (package.class.method)
                            if not html_link:
                                # Extract full class name from test_name if it contains package info
                                if '::' in test_name or '.' in test_name:
                                    parts = test_name.split('::') if '::' in test_name else test_name.split('.')
                                    if len(parts) >= 2:
                                        # Try with just class.method
                                        simple_name = f"{parts[-2]}.{parts[-1]}"
                                        html_link = test_html_links.get(simple_name)
                            # Try partial matches (check if any key contains the test name or vice versa)
                            if not html_link:
                                for key, link in test_html_links.items():
                                    # Normalize both for comparison
                                    key_normalized = key.lower().replace(' ', '').replace('_', '')
                                    test_normalized = test_name.lower().replace(' ', '').replace('_', '')
                                    display_normalized = display_name.lower().replace(' ', '').replace('_', '')
                                    
                                    if (test_normalized in key_normalized or key_normalized in test_normalized or 
                                        display_normalized in key_normalized or key_normalized in display_normalized):
                                        html_link = link
                                        break
                        
                        html_link_escaped = html_escape.escape(html_link) if html_link else None
                        
                        # Build test name HTML with link and copy button
                        if html_link_escaped:
                            test_name_html = f'''
                                <div style="display: flex; align-items: center; gap: 6px; padding: 4px 6px; margin: 2px 0; background: #f9fafb; border-radius: 4px; font-size: 12px;">
                                    <a href="{html_link_escaped}" target="_blank" onclick="event.stopPropagation();" style="flex: 1; color: #6366f1; text-decoration: none; font-weight: 500; cursor: pointer;" onmouseover="this.style.textDecoration='underline'; this.style.color='#4f46e5';" onmouseout="this.style.textDecoration='none'; this.style.color='#6366f1';" title="Open test logs: {html_escape.escape(html_link)}">
                                        {display_name_escaped}
                                    </a>
                                    <button onclick="copyTestName('{test_name_js}', this, event)" style="background: none; border: none; color: #6b7280; cursor: pointer; padding: 2px 4px; display: flex; align-items: center;" title="Copy test name" class="quick-win-copy-btn">
                                        <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>
                                    </button>
                                </div>
                            '''
                        else:
                            test_name_html = f'''
                                <div style="display: flex; align-items: center; gap: 6px; padding: 4px 6px; margin: 2px 0; background: #f9fafb; border-radius: 4px; font-size: 12px;">
                                    <span style="flex: 1; color: #374151;">{display_name_escaped}</span>
                                    <button onclick="copyTestName('{test_name_js}', this, event)" style="background: none; border: none; color: #6b7280; cursor: pointer; padding: 2px 4px; display: flex; align-items: center;" title="Copy test name" class="quick-win-copy-btn">
                                        <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>
                                    </button>
                                </div>
                            '''
                        test_names_html.append(test_name_html)
                    
                    # Create compact preview (first 2 tests)
                    preview_tests = []
                    for test_name in affected_tests[:2]:
                        from ..utils import extract_class_and_method
                        class_name, method_name = extract_class_and_method(test_name)
                        preview_tests.append(f"{class_name}.{method_name}")
                    
                    preview_text = ', '.join([html_escape.escape(name) for name in preview_tests])
                    if num_tests > 2:
                        preview_text += f" +{num_tests - 2} more"
                    
                    # Determine impact level for visual styling
                    if impact_pct >= 20:
                        impact_label = "High Impact"
                        impact_color = "#dc2626"
                    elif impact_pct >= 10:
                        impact_label = "Medium Impact"
                        impact_color = "#f59e0b"
                    else:
                        impact_label = "Low Impact"
                        impact_color = "#6b7280"
                    
                    html.append(f'''
                        <div style="background: #fff; padding: 10px 12px; border-radius: 6px; margin-bottom: 6px; border-left: 3px solid {category_style['color']}; box-shadow: 0 1px 2px rgba(0,0,0,0.05); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;">
                            <div style="display: flex; align-items: flex-start; gap: 8px;">
                                <div style="flex: 1; min-width: 0;">
                                    <div style="display: flex; align-items: center; gap: 6px; margin-bottom: 4px; flex-wrap: wrap;">
                                        <span style="font-size: 14px;">{category_style['icon']}</span>
                                        <span style="font-size: 14px; font-weight: 600; color: #111827; line-height: 1.4; flex: 1; min-width: 0;">
                                            {html_escape.escape(failure_signature)}
                                        </span>
                                        <span style="font-size: 13px; font-weight: 600; color: #111827; background: rgba(16, 185, 129, 0.1); padding: 2px 8px; border-radius: 3px; white-space: nowrap;">
                                            {num_tests} test{"s" if num_tests != 1 else ""}
                                        </span>
                                    </div>
                                    <div style="font-size: 12px; color: #6b7280; margin-bottom: 4px; line-height: 1.3;">
                                        Fix this {category_style['label'].lower()} to resolve {num_tests} test failure{"s" if num_tests != 1 else ""} • <span style="color: {impact_color}; font-weight: 500;">{impact_label}</span> ({impact_pct:.1f}% of all failures)
                                    </div>
                                    <div style="font-size: 11px; color: #9ca3af; margin-top: 2px; margin-bottom: 0; line-height: 1.2;">
                                        {preview_text}
                                    </div>
                                    <details id="{details_id}" style="margin: 0; padding: 0; margin-top: 4px;">
                                        <summary style="font-size: 11px; color: #6366f1; cursor: pointer; user-select: none; list-style: none; display: inline-flex; align-items: center; gap: 3px; text-decoration: underline; text-decoration-color: rgba(99, 102, 241, 0.4); margin: 0; padding: 0; line-height: 1;" onmouseover="this.style.textDecorationColor='rgba(99, 102, 241, 0.7)'" onmouseout="this.style.textDecorationColor='rgba(99, 102, 241, 0.4)'">
                                            <span style="line-height: 1;">Show all {num_tests} test{"s" if num_tests != 1 else ""}</span>
                                            <svg id="{details_id}-icon" xmlns="http://www.w3.org/2000/svg" width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="transition: transform 0.2s; display: inline-block; vertical-align: middle; line-height: 1;">
                                                <polyline points="6 9 12 15 18 9"></polyline>
                                            </svg>
                                        </summary>
                                        <div style="margin-top: 4px; padding-top: 4px; border-top: 1px solid #e5e7eb; max-height: 200px; overflow-y: auto;">
                                            {''.join(test_names_html)}
                                        </div>
                                    </details>
                                    <script>
                                        (function() {{
                                            const details = document.getElementById('{details_id}');
                                            const icon = document.getElementById('{details_id}-icon');
                                            if (details && icon) {{
                                                details.addEventListener('toggle', function() {{
                                                    icon.style.transform = details.open ? 'rotate(180deg)' : 'rotate(0deg)';
                                                }});
                                            }}
                                        }})();
                                    </script>
                                    <style>
                                        .quick-win-copy-btn:hover {{
                                            color: #111827;
                                        }}
                                        .quick-win-copy-btn:active {{
                                            transform: scale(0.9);
                                        }}
                                    </style>
                                </div>
                            </div>
                        </div>
                    ''')
                
                html.append('</div>')
        
        
        return ''.join(html)

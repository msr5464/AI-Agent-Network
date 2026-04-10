"""
Memory system for storing and retrieving historical test analysis data.
Tracks failures over time and detects patterns.
"""

import os
import re
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import logging

from ..parsers.models import TestResult, TestSummary
from .analyzer import FailureClassification
from ..utils import normalize_root_cause
from ..settings import Config
from ..database import Database, MYSQL_AVAILABLE, Error

# Import pymysql for DictCursor
try:
    import pymysql
except ImportError:
    pymysql = None

# MySQL error: table doesn't exist
MYSQL_ER_NO_SUCH_TABLE = 1146

logger = logging.getLogger(__name__)


class AgentMemory:
    """Stores and retrieves historical test analysis data using MySQL database"""
    
    def __init__(self):
        """Initialize memory storage with MySQL database."""
        self.db = Database()
        logger.info("AgentMemory initialized with MySQL database")
    
    def _get_table_name_from_report_name(self, report_name: str) -> Optional[str]:
        """Map report name to database table name (delegates to Database class)"""
        return Database.get_table_name_from_report_name(report_name)
    
    def _get_db_connection(self):
        """Get MySQL database connection (delegates to Database class)"""
        return self.db.get_connection()
    
    def get_test_results_by_buildtag(self, report_name: str, build_tag: str, table_name: Optional[str] = None) -> List[Dict]:
        """
        Query database for all test results for a specific buildTag.
        
        Args:
            report_name: Report name (used to determine table name if table_name not provided)
            build_tag: Build tag to query (e.g., "Regression-AccountOpening-Tests-424")
            table_name: Optional explicit table name to query
            
        Returns:
            List of dictionaries with test result data from database
        """
        if not table_name:
            table_name = self._get_table_name_from_report_name(report_name)
        if not table_name:
            raise ValueError(f"Cannot determine table name for report: {report_name}")
        
        connection = None
        try:
            connection = self._get_db_connection()
            # PyMySQL uses DictCursor instead of dictionary=True
            cursor = connection.cursor(pymysql.cursors.DictCursor)
            
            # Check which columns exist in the table
            cursor.execute(f"SHOW COLUMNS FROM {table_name}")
            available_columns = [row['Field'] for row in cursor.fetchall()]
            logger.debug(f"Available columns in {table_name}: {available_columns}")
            
            # Build SELECT clause with only existing columns
            select_columns = []
            if 'testcaseName' in available_columns:
                select_columns.append('testcaseName')
            if 'testStatus' in available_columns:
                select_columns.append('testStatus')
            elif 'status' in available_columns:
                select_columns.append('status as testStatus')
            if 'failureReason' in available_columns:
                select_columns.append('failureReason')
            if 'buildTag' in available_columns:
                select_columns.append('buildTag')
            
            # Try to find date column
            date_col = None
            date_columns = ['createdAt', 'created_at', 'executionDate', 'timestamp', 'date', 'execution_date', 'runDate', 'executedAt']
            for col in date_columns:
                if col in available_columns:
                    date_col = col
                    select_columns.append(f'{col} as executionDate')
                    break
            
            # Add id if available
            if 'id' in available_columns:
                select_columns.append('id')
            
            # Add knownFailure if available
            if 'knownFailure' in available_columns:
                select_columns.append('knownFailure')
            
            if not select_columns or 'testcaseName' not in available_columns:
                raise ValueError(f"Required columns (testcaseName) not found in table {table_name}")
            
            # Query by buildTag
            select_clause = ', '.join(select_columns)
            query = f"""
                SELECT {select_clause}
                FROM {table_name}
                WHERE buildTag = %s
                ORDER BY id ASC
            """
            
            logger.info(f"Querying test results for buildTag: {build_tag} from table: {table_name}")
            cursor.execute(query, (build_tag,))
            raw_results = cursor.fetchall()
            # One row per (testcaseName, buildTag): table can have duplicates per test due to multiple testrailCaseIds
            seen_testcase = set()
            results = []
            for row in raw_results:
                key = (row.get('testcaseName') or '').strip()
                if key in seen_testcase:
                    continue
                seen_testcase.add(key)
                results.append(row)
            if len(raw_results) != len(results):
                logger.debug(f"Deduplicated build results: {len(raw_results)} rows -> {len(results)} unique tests")
            logger.info(f"Found {len(results)} test results for buildTag: {build_tag}")
            return results
            
        except Error as e:
            errno = getattr(e, 'args', [None])[0] if getattr(e, 'args', None) else None
            if errno == MYSQL_ER_NO_SUCH_TABLE:
                msg = (
                    f"Table '{table_name}' does not exist in the database. "
                    "Ensure test results have been uploaded to the results table first (e.g. from your test run/CI), "
                    "or verify --table-name and DB_NAME in config/.env."
                )
                logger.error(msg)
                raise ValueError(msg) from e
            logger.error(f"Error querying database for buildTag {build_tag}: {e}")
            raise
        finally:
            if connection:
                try:
                    cursor.close()
                except:
                    pass
                if connection.open:
                    connection.close()
    
    def _get_test_execution_history_from_db(
        self,
        report_name: str,
        test_names: List[str],
        limit_per_test: int = 10,
        table_name: Optional[str] = None
    ) -> Dict[str, List[Dict]]:
        """
        Get test execution history from MySQL database for specific test cases.
        Queries by testcaseName (not buildTag) to get last N executions across all builds.
        
        Args:
            report_name: Report name (used to determine table name if table_name not provided)
            test_names: List of test case names to query
            limit_per_test: Number of recent executions to fetch per test (default: 10)
            table_name: Optional explicit table name to query
            
        Returns:
            Dictionary mapping testcaseName to list of execution records (ordered by id desc)
        """
        if not table_name:
            table_name = self._get_table_name_from_report_name(report_name)
        if not table_name:
            logger.warning(f"Cannot determine table name for report: {report_name}")
            return {}
        
        if not test_names:
            logger.warning("No test names provided for database query")
            return {}
        
        connection = None
        test_history = {}
        
        try:
            connection = self._get_db_connection()
            cursor = connection.cursor(pymysql.cursors.DictCursor)
            
            # First, check which columns exist in the table
            cursor.execute(f"SHOW COLUMNS FROM {table_name}")
            available_columns = [row['Field'] for row in cursor.fetchall()]
            logger.debug(f"Available columns in {table_name}: {available_columns}")
            
            # Check if 'id' column exists for ordering
            has_id_column = 'id' in available_columns
            
            # Build SELECT clause with only existing columns
            select_columns = []
            if 'testcaseName' in available_columns:
                select_columns.append('testcaseName')
            if 'buildTag' in available_columns:
                select_columns.append('buildTag')
            
            # Use testStatus column (actual column name in database)
            status_col = None
            if 'testStatus' in available_columns:
                status_col = 'testStatus'
                select_columns.append('testStatus')
            else:
                # Fallback only if testStatus doesn't exist
                for col_name in ['status', 'result', 'executionStatus', 'outcome']:
                    if col_name in available_columns:
                        status_col = col_name
                        select_columns.append(col_name)
                        break
            
            # Use failureReason column (actual column name in database)
            error_col = None
            if 'failureReason' in available_columns:
                error_col = 'failureReason'
                select_columns.append('failureReason')
            else:
                # Fallback only if failureReason doesn't exist
                for col_name in ['errorMessage', 'error', 'errorMsg', 'message']:
                    if col_name in available_columns:
                        error_col = col_name
                        select_columns.append(col_name)
                        break
            if error_col:
                logger.debug(f"Using '{error_col}' column for error messages in table {table_name}")
            
            if not error_col:
                logger.warning(f"No error message column found in table {table_name}. Available columns: {available_columns}")
            
            # Add knownFailure column if available
            if 'knownFailure' in available_columns:
                select_columns.append('knownFailure')
            
            # Try to find date column - prioritize createdAt (actual column name)
            date_col = None
            date_columns = ['createdAt', 'created_at', 'executionDate', 'timestamp', 'date', 'execution_date', 'runDate', 'executedAt']
            for col in date_columns:
                if col in available_columns:
                    date_col = col
                    select_columns.append(f'{col} as executionDate')
                    break
            
            if not select_columns or 'testcaseName' not in available_columns:
                logger.error(f"Required columns (testcaseName) not found in table {table_name}")
                return {}
            
            # Build base SELECT clause
            select_clause = ', '.join(select_columns)
            
            # Determine ORDER BY clause
            if has_id_column:
                order_by = 'id DESC'
            elif date_col:
                order_by = f'{date_col} DESC'
            else:
                order_by = 'testcaseName DESC'
                logger.warning(f"No id or date column found, using testcaseName for ordering")
            
            # Query each test case individually to get last N executions
            logger.info(f"Querying {len(test_names)} test cases from {table_name}")
            
            # First, let's check what test names exist in the database (sample)
            try:
                sample_query = f"SELECT DISTINCT testcaseName FROM {table_name} LIMIT 10"
                cursor.execute(sample_query)
                sample_names = [row['testcaseName'] for row in cursor.fetchall()]
                if sample_names:
                    logger.info(f"Sample test names in database ({len(sample_names)}): {sample_names[:5]}")
                    logger.info(f"Sample test names from report ({len(test_names)}): {test_names[:5] if test_names else []}")
                    
                    # Extract class.method from report names for comparison
                    def extract_class_method(full_name: str) -> str:
                        parts = full_name.split('.')
                        return '.'.join(parts[-2:]) if len(parts) >= 2 else full_name
                    
                    report_class_methods = [extract_class_method(name) for name in test_names[:10]]
                    db_names_lower = {name.lower() for name in sample_names}
                    report_names_lower = {name.lower() for name in report_class_methods}
                    matches = db_names_lower.intersection(report_names_lower)
                    if matches:
                        logger.info(f"Found {len(matches)} matching test names (case-insensitive): {list(matches)[:3]}")
                    else:
                        logger.warning(f"No matching test names found! Will extract class.method format for queries.")
            except Exception as e:
                logger.warning(f"Could not fetch sample names: {e}")
            
            # Helper function to extract class.method from full package path
            def extract_class_method(full_name: str) -> str:
                """Extract ClassName.methodName from full package path.
                Example: 'Automation.Access.AccountOpening.web.customer.TestActivation.testSecondActivationMissionCards'
                -> 'TestActivation.testSecondActivationMissionCards'
                """
                parts = full_name.split('.')
                if len(parts) >= 2:
                    # Get last two parts (class and method)
                    return '.'.join(parts[-2:])
                return full_name
            
            # OPTIMIZED: Batch query all test names at once using IN clause
            # Extract class.method format for all test names
            query_names_map = {}  # Maps query_name -> original test_name
            query_names_list = []
            
            for test_name in test_names:
                query_name = extract_class_method(test_name)
                if query_name not in query_names_map:
                    query_names_map[query_name] = []
                query_names_map[query_name].append(test_name)
                if query_name not in query_names_list:
                    query_names_list.append(query_name)
            
            if not query_names_list:
                logger.warning("No valid test names to query after extraction")
                return {}
            
            # Single batch query to fetch all results
            # Note: We fetch more than limit_per_test initially, then limit per test in Python
            # This is more efficient than N separate queries
            
            # CHUNKING LOGIC: Split query_names_list into chunks of 200 to avoid huge queries
            CHUNK_SIZE = 200
            chunks = [query_names_list[i:i + CHUNK_SIZE] for i in range(0, len(query_names_list), CHUNK_SIZE)]
            all_results = []
            
            # Calculate date threshold to prevent fetching ancient data
            # Use 90 days instead of 14 because CI runs may be infrequent;
            # we need enough executions per test to cross the min_occurrences threshold
            # (e.g., FLAKY_TESTS_MIN_FAILURES=5). The actual per-test cap is
            # handled by limit_per_test in Python code below.
            date_threshold = (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%d %H:%M:%S')
            date_filter = f"AND {date_col} >= %s" if date_col else ""
            
            import time
            for chunk_idx, name_chunk in enumerate(chunks):
                placeholders = ','.join(['%s'] * len(name_chunk))
                batch_query = f"""
                    SELECT {select_clause}
                    FROM {table_name}
                    WHERE testcaseName IN ({placeholders})
                    {date_filter}
                    ORDER BY {order_by}
                """
                
                try:
                    start_time = time.time()
                    logger.info(f"⏳ Executing batch query chunk {chunk_idx+1}/{len(chunks)} for {len(name_chunk)} tests (last 90 days)...")
                    
                    params = tuple(name_chunk)
                    if date_col:
                        params = params + (date_threshold,)
                    
                    cursor.execute(batch_query, params)
                    chunk_results = cursor.fetchall()
                    
                    query_duration = time.time() - start_time
                    logger.info(f"✅ Chunk {chunk_idx+1} completed in {query_duration:.2f}s. Found {len(chunk_results)} rows.")
                    
                    # If no exact matches for this chunk, try optimized case-insensitive search
                    if not chunk_results:
                        logger.info(f"⚠️ No exact matches for chunk {chunk_idx+1}. Attempting optimized case-insensitive search...")
                        lower_names = [n.lower() for n in name_chunk]
                        start_time_ci = time.time()
                        
                        params_ci = tuple(lower_names)
                        if date_col:
                            params_ci = params_ci + (date_threshold,)
                            
                        cursor.execute(batch_query, params_ci)
                        chunk_results = cursor.fetchall()
                        ci_duration = time.time() - start_time_ci
                        logger.info(f"✅ Case-insensitive search for chunk {chunk_idx+1} completed in {ci_duration:.2f}s. Found {len(chunk_results)} rows.")
                    
                    all_results.extend(chunk_results)
                    
                except Error as e:
                    logger.error(f"Error executing batch query chunk {chunk_idx+1}: {e}")
                    # If this chunk fails, we continue with others or let it fallback to individual queries later if no results
                    continue
            
            if not all_results:
                logger.warning(f"No results found across all {len(chunks)} chunks.")
            else:
                logger.info(f"Total results across all chunks: {len(all_results)} rows")

            try:
                # Group results by testcaseName and limit per test
                results_by_test = {}
                for row in all_results:
                    if not isinstance(row, dict):
                        logger.warning("Skipping malformed execution row (not a dict)")
                        continue
                    db_test_name = row.get('testcaseName')
                    if not db_test_name:
                        continue
                    
                    # Find matching original test names (handle case-insensitive matching)
                    matched_original_names = []
                    db_test_lower = db_test_name.lower().strip()
                    
                    # Try to match db_test_name with query_names_map
                    for query_name, original_names in query_names_map.items():
                        query_name_lower = query_name.lower().strip()
                        # Match if query_name matches db_test_name (case-insensitive)
                        if query_name_lower == db_test_lower:
                            matched_original_names.extend(original_names)
                            logger.debug(f"Matched DB test '{db_test_name}' with query name '{query_name}' -> {len(original_names)} original names")
                    
                    # If still no match, try matching db_test_name directly with original test names
                    # (in case database has full names instead of Class.method)
                    if not matched_original_names:
                        for original_name in test_names:
                            # Extract class.method from original name for comparison
                            original_query_name = extract_class_method(original_name)
                            if original_query_name.lower().strip() == db_test_lower:
                                matched_original_names.append(original_name)
                                logger.debug(f"Matched DB test '{db_test_name}' directly with original name '{original_name}'")
                    
                    # Add to results for all matching original test names
                    if matched_original_names:
                        for original_name in matched_original_names:
                            if original_name not in results_by_test:
                                results_by_test[original_name] = []
                            results_by_test[original_name].append(row)
                    else:
                        logger.debug(f"No match found for DB test name: '{db_test_name}' (query names: {list(query_names_map.keys())[:5]})")
                
                # Process results: one row per (test, buildTag) then limit per test
                import re
                for test_name, rows in results_by_test.items():
                    # Table can have multiple rows per (testcaseName, buildTag) due to testrailCaseIds; keep one per build
                    seen_build = set()
                    unique_rows = []
                    for row in rows:
                        bt = row.get('buildTag')
                        if bt in seen_build:
                            continue
                        seen_build.add(bt)
                        unique_rows.append(row)
                    # Limit to last N executions per test (already ordered by id DESC or date DESC)
                    limited_rows = unique_rows[:limit_per_test]
                    
                    test_history[test_name] = []
                    
                    for row in limited_rows:
                        if not isinstance(row, dict):
                            logger.warning("Skipping malformed execution row (not a dict)")
                            continue
                        try:
                            execution_record = {
                                'buildTag': row.get('buildTag')
                            }
                            
                            # Add testStatus if available
                            if status_col:
                                exec_status = row.get(status_col)
                                if exec_status is not None:
                                    execution_record['testStatus'] = exec_status
                            
                            # Add failureReason if available (filter redundant lines)
                            if error_col:
                                exec_error = row.get(error_col)
                                if exec_error is not None:
                                    if isinstance(exec_error, str):
                                        # Split by newlines and filter out redundant lines
                                        lines = exec_error.split('\n')
                                        filtered_lines = []
                                        for line in lines:
                                            # Remove "Results Url:" lines
                                            if re.match(r'^\s*Results\s*Url\s*:', line, re.IGNORECASE):
                                                continue
                                            # Remove "Testcase Name:" lines
                                            if re.match(r'^\s*Testcase\s*Name\s*:', line, re.IGNORECASE):
                                                continue
                                            filtered_lines.append(line)
                                        exec_error = '\n'.join(filtered_lines).strip()
                                    execution_record['failureReason'] = exec_error
                            
                            # Add date if available
                            if date_col:
                                exec_date = row.get('executionDate')
                                if not exec_date:
                                    exec_date = row.get(date_col)
                                if exec_date:
                                    execution_record['date'] = exec_date
                                    execution_record['executionDate'] = exec_date
                            
                            # Add id if available
                            if has_id_column and 'id' in row:
                                execution_record['id'] = row.get('id')
                            
                            # Add knownFailure if available (for Known Failures section history dots)
                            if 'knownFailure' in row:
                                execution_record['knownFailure'] = row.get('knownFailure')
                            
                            test_history[test_name].append(execution_record)
                        except Exception as e:
                            logger.warning("Skipping malformed execution record: %s", e, exc_info=False)
                            continue
                    
                    logger.debug(f"Retrieved {len(test_history[test_name])} executions for {test_name}")
                
                # Log summary for tests with no results
                tests_with_results = set(test_history.keys())
                tests_without_results = set(test_names) - tests_with_results
                if tests_without_results and len(tests_without_results) <= 10:
                    logger.debug(f"No results found for {len(tests_without_results)} test(s): {list(tests_without_results)[:5]}")
                elif tests_without_results:
                    logger.debug(f"No results found for {len(tests_without_results)} test(s)")
                
            except Error as e:
                logger.error(f"Error processing batch results: {e}")
                # Fallback to individual queries ONLY if there are few tests
                if len(test_names) > 20:
                    logger.warning(f"Too many tests ({len(test_names)}) to fallback to individual queries. Skipping history for some.")
                    return test_history
                
                logger.warning("Falling back to individual queries for few tests")
                for test_name in test_names:
                    query_name = extract_class_method(test_name)
                    try:
                        query = f"""
                            SELECT {select_clause}
                            FROM {table_name}
                            WHERE testcaseName = %s
                            ORDER BY {order_by}
                            LIMIT %s
                        """
                        cursor.execute(query, (query_name, max(limit_per_test * 15, 100)))
                        raw_results = cursor.fetchall()
                        # One row per buildTag (table can have multiple rows per test per build)
                        seen_build = set()
                        results = []
                        for row in raw_results:
                            if not isinstance(row, dict):
                                continue
                            bt = row.get('buildTag')
                            if bt in seen_build:
                                continue
                            seen_build.add(bt)
                            results.append(row)
                        results = results[:limit_per_test]
                        if results:
                            test_history[test_name] = []
                            for row in results:
                                if not isinstance(row, dict):
                                    continue
                                try:
                                    execution_record = {'buildTag': row.get('buildTag')}
                                    if status_col:
                                        exec_status = row.get(status_col)
                                        if exec_status is not None:
                                            execution_record['testStatus'] = exec_status
                                    if error_col:
                                        exec_error = row.get(error_col)
                                        if exec_error and isinstance(exec_error, str):
                                            lines = exec_error.split('\n')
                                            filtered_lines = [l for l in lines 
                                                             if not re.match(r'^\s*Results\s*Url\s*:', l, re.IGNORECASE)
                                                             and not re.match(r'^\s*Testcase\s*Name\s*:', l, re.IGNORECASE)]
                                            exec_error = '\n'.join(filtered_lines).strip()
                                            execution_record['failureReason'] = exec_error
                                    if date_col:
                                        exec_date = row.get('executionDate') or row.get(date_col)
                                        if exec_date:
                                            execution_record['date'] = exec_date
                                            execution_record['executionDate'] = exec_date
                                    if has_id_column and 'id' in row:
                                        execution_record['id'] = row.get('id')
                                    if 'knownFailure' in row:
                                        execution_record['knownFailure'] = row.get('knownFailure')
                                    test_history[test_name].append(execution_record)
                                except Exception as e:
                                    logger.warning("Skipping malformed execution record (fallback): %s", e, exc_info=False)
                                    continue
                    except Error as e2:
                        logger.warning(f"Error querying test {test_name}: {e2}")
                        continue
            
            total_executions = sum(len(execs) for execs in test_history.values())
            logger.info(f"Retrieved {total_executions} total executions for {len(test_history)} test cases from {table_name}")
            
        except Error as e:
            logger.error(f"Error querying database: {e}")
            return {}
        finally:
            if connection:
                try:
                    cursor.close()
                except:
                    pass
                if connection.open:
                    connection.close()
        
        return test_history
    
    def detect_recurring_failures(
        self,
        current_failures: List[str],
        days: int = 10,
        min_occurrences: int = 3,
        report_name: str = None,
        all_test_names: Optional[List[str]] = None,
        table_name: Optional[str] = None
    ) -> List[Dict]:
        """
        Detect tests that fail repeatedly using MySQL database.
        
        Args:
            current_failures: List of current failing test names
            days: Number of days to look back
            min_occurrences: Minimum occurrences to be considered recurring
            report_name: Report name (required) to determine MySQL table name
            all_test_names: Optional list of all test names (to query history for all tests, not just failures)
            table_name: Optional explicit table name to query
            
        Returns:
            List of dictionaries with recurring failure info
            
        Raises:
            ValueError: If report_name is not provided
        """
        if not report_name:
            raise ValueError("report_name is required for recurring failures detection")
        
        if not MYSQL_AVAILABLE:
            raise ImportError("pymysql is required. Install with: pip install pymysql")
        
        # Use all_test_names if provided, otherwise just current_failures
        test_names_to_query = all_test_names if all_test_names else current_failures
        logger.info(f"📊 Using MySQL database for recurring failures (report: {report_name}, querying {len(test_names_to_query)} tests)")
        return self._detect_recurring_failures_from_db(
            current_failures, days, min_occurrences, report_name, test_names_to_query, table_name
        )
    
    def _detect_recurring_failures_from_db(
        self,
        current_failures: List[str],
        days: int = 10,
        min_occurrences: int = 3,
        report_name: str = None,
        test_names_to_query: List[str] = None,
        table_name: Optional[str] = None
    ) -> List[Dict]:
        """
        Detect recurring failures from MySQL database.
        Queries by testcaseName (not buildTag) to get historical data across multiple builds.
        """
        # Use test_names_to_query if provided, otherwise fall back to current_failures
        if not test_names_to_query:
            test_names_to_query = current_failures
        
        # Get test execution history from database
        # Query by testcaseName to get last X executions across all builds
        # Normalize report path for cross-platform inputs (UNC/Windows)
        from lib.utils import ReportUrlBuilder
        normalized_report_name = ReportUrlBuilder.normalize_path(report_name)
        test_history = self._get_test_execution_history_from_db(
            normalized_report_name, 
            test_names=test_names_to_query,
            limit_per_test=Config.FLAKY_TESTS_LAST_RUNS,
            table_name=table_name
        )
        
        if not test_history:
            logger.warning(f"No test history found in database for report: {report_name}")
            return []
        
        # Count failures and build failure details
        failure_counts = {}
        failure_details = {}
        
        # Get unique dates from all test executions
        all_dates = set()
        for test_executions in test_history.values():
            for exec_record in test_executions:
                if not isinstance(exec_record, dict):
                    logger.warning("Skipping malformed exec_record when collecting dates (not a dict)")
                    continue
                try:
                    exec_date = exec_record.get('date') or exec_record.get('executionDate')
                    if exec_date:
                        # Handle datetime objects
                        if hasattr(exec_date, 'date'):
                            all_dates.add(exec_date.date())
                        elif isinstance(exec_date, str):
                            # Try to parse string date
                            try:
                                from datetime import datetime
                                parsed_date = datetime.strptime(exec_date.split()[0], '%Y-%m-%d').date()
                                all_dates.add(parsed_date)
                            except (ValueError, IndexError, TypeError):
                                pass
                        else:
                            all_dates.add(exec_date)
                except Exception as e:
                    logger.warning("Skipping malformed exec_record when collecting dates: %s", e, exc_info=False)
                    continue
        
        sorted_dates = sorted(all_dates, reverse=True)[:days] if all_dates else []  # Last N days
        logger.info(f"Found {len(sorted_dates)} unique dates from execution records")
        if sorted_dates:
            logger.info(f"Date range: {sorted_dates[-1]} to {sorted_dates[0]}")
        else:
            logger.warning("No dates found in execution records - will use execution order for history")
        
        for test_name, executions in test_history.items():
            # Count failures in the last X executions
            # Use testStatus column directly from DB
            if executions:
                failure_count = 0
                for e in executions:
                    if not isinstance(e, dict):
                        continue
                    try:
                        exec_status = e.get('testStatus')
                        if exec_status:
                            status = str(exec_status).upper().strip()
                            if status in ['FAIL', 'FAILED', 'ERROR', 'FAILURE']:
                                failure_count += 1
                    except Exception:
                        pass
            else:
                failure_count = 0
            
            if failure_count >= min_occurrences:
                failure_counts[test_name] = failure_count
                failure_details[test_name] = {
                    'dates': [],
                    'classifications': [],
                    'root_causes': [],
                    'executions': executions
                }
                
                # Extract details from failed executions
                for exec_record in executions:
                    if not isinstance(exec_record, dict):
                        continue
                    try:
                        # Check if this is a failure using testStatus
                        exec_status = exec_record.get('testStatus')
                        is_failure = False
                        if exec_status:
                            is_failure = str(exec_status).upper() in ['FAIL', 'FAILED', 'ERROR', 'FAILURE']
                        elif exec_record.get('failureReason'):
                            # If no status but has error message, it's a failure
                            is_failure = True
                        
                        if is_failure:
                            exec_date = exec_record.get('date') or exec_record.get('executionDate')
                            if exec_date:
                                if hasattr(exec_date, 'date'):
                                    date_str = str(exec_date.date())
                                else:
                                    date_str = str(exec_date).split()[0] if ' ' in str(exec_date) else str(exec_date)
                                failure_details[test_name]['dates'].append(date_str)
                            
                            error_msg = exec_record.get('failureReason') or ''
                            failure_details[test_name]['root_causes'].append(error_msg)
                            
                            # Try to classify based on error message
                            classification = self._classify_from_error_message(error_msg)
                            failure_details[test_name]['classifications'].append(classification)
                    except Exception as e:
                        logger.warning("Skipping malformed exec_record in failure_details for %s: %s", test_name, e, exc_info=False)
                        continue
        
        # Build history vector for each test (1=Pass, 0=Fail) using last N executions (newest first from DB)
        recurring = []
        for test_name, count in failure_counts.items():
            try:
                details = failure_details[test_name]
                
                # Always use execution order (not date buckets) to build history
                executions = details['executions'][:Config.FLAKY_TESTS_LAST_RUNS]  # newest first
                
                # Build history from executions (oldest -> newest for display)
                history = []
                for idx, exec_record in enumerate(reversed(executions)):  # oldest first
                    if not isinstance(exec_record, dict):
                        logger.warning("Skipping malformed exec_record when building history for %s; treating as pass", test_name)
                        history.append(1)
                        continue
                    try:
                        exec_status = exec_record.get('testStatus')
                        is_failure = False
                        
                        if exec_status:
                            status = str(exec_status).upper().strip()
                            if status in ['FAIL', 'FAILED', 'ERROR', 'FAILURE']:
                                is_failure = True
                                logger.debug(f"Execution {idx+1} for {test_name}: testStatus='{exec_status}' -> FAILED")
                            elif status in ['PASS', 'PASSED', 'SUCCESS', 'OK']:
                                is_failure = False
                                logger.debug(f"Execution {idx+1} for {test_name}: testStatus='{exec_status}' -> PASSED")
                            else:
                                logger.warning(f"Unknown testStatus '{exec_status}' for {test_name} execution {idx+1}; treating as pass")
                                is_failure = False
                        else:
                            # No status available; assume pass
                            logger.warning(f"No testStatus found for {test_name} execution {idx+1}; treating as pass")
                            is_failure = False
                        
                        history.append(0 if is_failure else 1)
                    except Exception as e:
                        logger.warning("Skipping malformed exec_record when building history for %s: %s; treating as pass", test_name, e, exc_info=False)
                        history.append(1)
                
                logger.info(f"Generated history for {test_name}: {history} (from {len(executions)} executions)")
                
                # Ensure history has exactly 10 items (pad or truncate if needed)
                if len(history) > 10:
                    # Truncate to last 10 items (newest)
                    history = history[-10:]
                    logger.warning(f"History for {test_name} had {len(history)} items, truncated to 10")
                elif len(history) < 10:
                    # Pad with passes at the beginning (older runs) to always show 10 dots
                    padding_needed = 10 - len(history)
                    while len(history) < 10:
                        history.insert(0, 1)  # Insert passes at the beginning (older runs)
                    logger.debug(f"History for {test_name} had {len(history) - padding_needed} items, padded {padding_needed} items to 10")
                
                # Ensure history is not empty - if still empty, create a default history
                if not history:
                    logger.warning(f"No history generated for {test_name}, creating default history")
                    # Create history based on failure count (10 days)
                    # If it failed multiple times, show mostly failures
                    if count >= 7:
                        history = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0]  # All failures
                    elif count >= 5:
                        history = [0, 0, 0, 0, 0, 1, 0, 1, 0, 1]  # Mostly failures
                    elif count >= 3:
                        history = [0, 0, 1, 0, 1, 0, 1, 0, 1, 0]  # Intermittent failures
                    else:
                        history = [1, 0, 1, 0, 1, 0, 1, 0, 1, 0]  # Mostly passes
                
                # Recalculate occurrences from history vector to ensure consistency
                # Count failures (0 = fail, 1 = pass) in the last 10 executions
                # This ensures occurrences matches what's actually displayed in the UI
                failures_in_history = sum(1 for status in history if status == 0)
                # Update count to match the actual failures shown in history
                if failures_in_history != count:
                    logger.debug(f"Recalculating occurrences for {test_name}: was {count}, now {failures_in_history} (from history vector)")
                    count = failures_in_history
                
                # Log history generation for first few tests
                if len(recurring) < 3:
                    logger.info(f"History for {test_name}: {history} (length: {len(history)}, failures: {failures_in_history})")
                
                # Recalculate occurrences from history vector to ensure consistency
                # Count failures (0 = fail, 1 = pass) in the last 10 executions
                # This ensures occurrences matches what's actually displayed in the UI
                failures_in_history = sum(1 for status in history if status == 0)
                # Update count to match the actual failures shown in history
                if failures_in_history != count:
                    logger.debug(f"Recalculating occurrences for {test_name}: was {count}, now {failures_in_history} (from history vector)")
                    count = failures_in_history
                
                # Determine if it's consistently the same type
                classifications = details['classifications']
                if classifications:
                    most_common = max(set(classifications), key=classifications.count)
                    consistency = classifications.count(most_common) / len(classifications)
                else:
                    most_common = 'UNKNOWN'
                    consistency = 0.0
                
                # Determine if it's flaky
                has_passes = 1 in history
                has_failures = 0 in history
                is_intermittent = has_passes and has_failures
                is_inconsistent = consistency < 0.8
                
                # Analyze root causes - normalize to handle dynamic values like URLs, IDs
                root_causes = details['root_causes']
                normalized_root_causes = [normalize_root_cause(rc) for rc in root_causes if rc]
                unique_root_causes = len(set(normalized_root_causes))
                same_reason = unique_root_causes == 1
                different_reasons = unique_root_causes > 1
                
                # Categorize failure pattern
                failure_pattern = self._categorize_failure_pattern(
                    is_intermittent=is_intermittent,
                    same_reason=same_reason,
                    different_reasons=different_reasons
                )
                
                # Prepare execution details for UI expansion
                # CRITICAL: Use the CORRECTED history vector (after all corrections have been applied)
                # Executions are ordered by id DESC (newest first), but history is oldest to newest
                # We need to align them: history[0] corresponds to oldest execution, history[9] to newest
                execution_details = []
                executions_list = details.get('executions', [])
                
                # Reverse executions to match history order (oldest to newest)
                reversed_executions = list(reversed(executions_list[:10])) if executions_list else []
                
                # Calculate padding offset: if history was padded, the first N items are padded passes
                # We need to find where actual execution data starts in the history vector
                actual_execution_count = len(reversed_executions)
                history_length = len(history)
                padding_count = max(0, history_length - actual_execution_count)
                
                # Create execution details aligned with CORRECTED history vector
                # CRITICAL: The history vector is the source of truth - it's built from testStatus column
                # and may have been corrected for current failures. Use hist_status (from history vector)
                # to determine pass/fail, NOT the original testStatus from database.
                # NOTE: If history was padded, history[0] through history[padding_count-1] are padded,
                # and actual execution data starts at history[padding_count]
                for idx, hist_status in enumerate(history):
                    exec_detail = {
                        'index': idx,
                        'status': 'pass' if hist_status == 1 else 'fail',  # Use corrected history status
                        'history_index': idx
                    }
                    
                    # Check if this is a padded entry (before actual execution data starts)
                    if idx < padding_count:
                        # This is a padded entry (older run, assumed pass)
                        exec_detail['padded'] = True
                        exec_detail['testStatus'] = 'PASSED'
                    elif idx - padding_count < len(reversed_executions):
                        # This corresponds to actual execution data
                        exec_record = reversed_executions[idx - padding_count]
                        
                        # Get original testStatus from database for reference
                        original_testStatus = exec_record.get('testStatus', '') if isinstance(exec_record, dict) else ''
                        
                        # CRITICAL: Use hist_status (from corrected history vector) as the source of truth
                        # The history vector was built from testStatus, but may have been corrected
                        # Store both: original from DB and the corrected status from history vector
                        corrected_testStatus = 'PASSED' if hist_status == 1 else 'FAILED'
                        
                        # CRITICAL: If this is the last execution (newest) and test is in current failures,
                        # ensure error message is preserved even if database doesn't have it
                        error_msg = exec_record.get('failureReason', '') if isinstance(exec_record, dict) else ''
                        if idx == len(history) - 1 and test_name in current_failures and hist_status == 0:
                            # This is the newest execution and it's marked as failed in corrected history
                            # If no error message, add a placeholder
                            if not error_msg or not str(error_msg).strip():
                                error_msg = 'Test failed in current build'
                        
                        exec_detail.update({
                            'id': exec_record.get('id') if isinstance(exec_record, dict) else None,
                            'buildTag': exec_record.get('buildTag', '') if isinstance(exec_record, dict) else '',
                            'date': str(exec_record.get('date') or exec_record.get('executionDate', '')) if isinstance(exec_record, dict) else '',
                            'failureReason': error_msg,
                            'testStatus': corrected_testStatus  # Use corrected status from history vector, not original from DB
                        })
                    else:
                        # No execution data (padded entry)
                        exec_detail['padded'] = True
                    
                    execution_details.append(exec_detail)
                
                recurring.append({
                    'test_name': test_name,
                    'occurrences': count,  # This is now recalculated from history vector to match what's displayed
                    'dates': details['dates'],
                    'most_common_classification': most_common,
                    'consistency': consistency,
                    'is_flaky': is_intermittent or is_inconsistent,
                    'in_current_run': test_name in current_failures,
                    'history': history,  # Always exactly 10 items
                    'execution_details': execution_details,  # Details for each execution, aligned with history
                    'failure_pattern': failure_pattern,
                    'same_reason': same_reason,
                    'different_reasons': different_reasons,
                    'unique_root_causes': unique_root_causes
                })
            except Exception as e:
                logger.warning("Skipping test %s due to malformed execution data: %s", test_name, e, exc_info=False)
                continue
        
        # Filter to only show tests that meet the criteria:
        # occurrences >= Y failures in last X runs
        filtered_recurring = []
        excluded_tests = []
        
        for failure in recurring:
            occurrences = failure.get('occurrences', 0)
            test_name = failure.get('test_name', 'unknown')
            
            # Simple filtering: include if occurrences >= min_occurrences
            if occurrences >= min_occurrences:
                filtered_recurring.append(failure)
            else:
                excluded_tests.append((test_name, occurrences))
        
        # Log excluded tests for debugging
        if excluded_tests:
            logger.info(f"Excluded {len(excluded_tests)} tests that didn't meet criteria:")
            for test_name, occ in excluded_tests[:10]:  # Log first 10
                logger.info(f"  - {test_name}: {occ} occurrences (need >={min_occurrences} failures in last {Config.FLAKY_TESTS_LAST_RUNS} runs)")
        
        logger.info(f"Found {len(recurring)} recurring failures from database, filtered to {len(filtered_recurring)} based on criteria (>={min_occurrences} failures in last {Config.FLAKY_TESTS_LAST_RUNS} runs)")
        return filtered_recurring
    
    def _classify_from_error_message(self, error_message: str) -> str:
        """
        Classify failure type based on error message.
        This is a simple heuristic - can be enhanced with AI analysis.
        """
        if not error_message:
            return 'UNKNOWN'
        
        error_upper = error_message.upper()
        
        # Product bug indicators
        if any(keyword in error_upper for keyword in ['ASSERTION', 'EXPECTED', 'ACTUAL', 'MISMATCH', 'VALIDATION']):
            return 'PRODUCT_BUG'
        
        # Automation issue indicators
        if any(keyword in error_upper for keyword in ['NOSUCHELEMENT', 'TIMEOUT', 'STALE', 'WEBDRIVER', 'LOCATOR']):
            return 'AUTOMATION_ISSUE'
        
        # API errors
        if any(keyword in error_upper for keyword in ['API', 'HTTP', 'STATUS CODE', '500', '404', '403']):
            return 'PRODUCT_BUG'  # API errors are usually product bugs
        
        return 'UNKNOWN'
    
    def get_trend_analysis(self, days: int = 10, report_name: str = None, table_name: Optional[str] = None) -> Dict:
        """
        Analyze trends over time using MySQL database.
        
        Args:
            days: Number of days to analyze
            report_name: Report name (required) to determine MySQL table name
            table_name: Optional explicit table name to query
            
        Returns:
            Dictionary with trend analysis
            
        Raises:
            ValueError: If report_name is not provided
        """
        if not report_name:
            raise ValueError("report_name is required for trend analysis")
        
        if not MYSQL_AVAILABLE:
            raise ImportError("pymysql is required. Install with: pip install pymysql")
        
        logger.info(f"📊 Using MySQL database for trend analysis (report: {report_name})")
        return self._get_trend_analysis_from_db(report_name, days, table_name)
    
    def _get_trend_analysis_from_db(self, report_name: str, days: int = 10, table_name: Optional[str] = None) -> Dict:
        """
        Get trend analysis from MySQL database by grouping test results by buildTag.
        
        Args:
            report_name: Report name (used to determine table name)
            days: Number of days to analyze
            table_name: Optional explicit table name to query
            
        Returns:
            Dictionary with trend analysis
        """
        if not table_name:
            table_name = self._get_table_name_from_report_name(report_name)
        if not table_name:
            raise ValueError(f"Cannot determine table name for report: {report_name}")
        
        connection = None
        try:
            connection = self._get_db_connection()
            cursor = connection.cursor(pymysql.cursors.DictCursor)
            
            # Check which columns exist
            cursor.execute(f"SHOW COLUMNS FROM {table_name}")
            available_columns = [row['Field'] for row in cursor.fetchall()]
            
            # Check for status column
            status_col = None
            if 'testStatus' in available_columns:
                status_col = 'testStatus'
            else:
                for col_name in ['status', 'result', 'executionStatus', 'outcome']:
                    if col_name in available_columns:
                        status_col = col_name
                        break
            
            if not status_col:
                raise ValueError(f"No status column found in {table_name}")
        
            # Check for date column
            date_col = None
            date_columns = ['createdAt', 'created_at', 'executionDate', 'timestamp', 'date', 'execution_date', 'runDate', 'executedAt']
            for col in date_columns:
                if col in available_columns:
                    date_col = col
                    break
            
            # Get distinct buildTags with their execution dates
            if date_col:
                query = f"""
                    SELECT DISTINCT buildTag, {date_col} as executionDate
                    FROM {table_name}
                    WHERE buildTag IS NOT NULL AND buildTag != ''
                    ORDER BY {date_col} DESC
                    LIMIT 100
                """
            else:
                # If no date column, use id for ordering (assuming newer builds have higher ids)
                query = f"""
                    SELECT DISTINCT buildTag, MAX(id) as max_id
                    FROM {table_name}
                    WHERE buildTag IS NOT NULL AND buildTag != ''
                    GROUP BY buildTag
                    ORDER BY max_id DESC
                    LIMIT 100
                """
            
            cursor.execute(query)
            builds = cursor.fetchall()
            
            if not builds:
                logger.warning(f"No builds found in {table_name}")
                return {
                    'days_analyzed': 0,
                    'trend': 'NO_DATA'
                }
            
            # Calculate pass rates for each build
            dates = []
            pass_rates = []
            total_tests = []
            
            for build in builds[:20]:  # Limit to last 20 builds
                build_tag = build['buildTag']
                
                # Count distinct tests (one row per test per build; table may have duplicates per test due to testrailCaseIds)
                count_query = f"""
                    SELECT 
                        COUNT(DISTINCT testcaseName) as total,
                        COUNT(DISTINCT CASE WHEN {status_col} IN ('PASS', 'PASSED', 'SUCCESS', 'OK') THEN testcaseName END) as passed
                    FROM {table_name}
                    WHERE buildTag = %s
                """
                cursor.execute(count_query, (build_tag,))
                result = cursor.fetchone()
                
                if result and result['total'] > 0:
                    total_raw = result.get('total')
                    passed_raw = result.get('passed') or 0
                    
                    # Normalize numeric types to avoid mixing Decimal and float
                    try:
                        total = int(total_raw)
                    except (TypeError, ValueError):
                        total = int(float(total_raw))
                    
                    try:
                        passed = int(passed_raw)
                    except (TypeError, ValueError):
                        passed = int(float(passed_raw))
                    
                    if total <= 0:
                        continue
                    
                    pass_rate = (passed / total) * 100.0
                    
                    dates.append(build_tag)  # Use buildTag as identifier
                    pass_rates.append(float(pass_rate))
                    total_tests.append(total)
            
            # Reverse arrays so they're chronological (oldest → newest)
            dates = list(reversed(dates))
            pass_rates = list(reversed(pass_rates))
            total_tests = list(reversed(total_tests))
            
            # Calculate trends
            if len(pass_rates) >= 2:
                # Compare older half vs newer half
                mid = len(pass_rates) // 2
                older_half_avg = sum(pass_rates[:mid]) / mid
                newer_half_avg = sum(pass_rates[mid:]) / (len(pass_rates) - mid)
                
                if newer_half_avg > older_half_avg + 5:
                    trend = 'IMPROVING'
                elif newer_half_avg < older_half_avg - 5:
                    trend = 'DECLINING'
                else:
                    trend = 'STABLE'
            else:
                trend = 'INSUFFICIENT_DATA'
            
            return {
                'days_analyzed': len(pass_rates),
                'dates': dates,
                'pass_rates': pass_rates,
                'average_pass_rate': sum(pass_rates) / len(pass_rates) if pass_rates else 0,
                'latest_pass_rate': pass_rates[-1] if pass_rates else 0,
                'trend': trend,
                'total_product_bugs': 0,  # Not available from DB without classifications
                'total_automation_issues': 0,  # Not available from DB without classifications
                'average_product_bugs': 0,
                'average_automation_issues': 0
            }
    
        except Error as e:
            logger.error(f"Error querying database for trend analysis: {e}")
            raise
        finally:
            if connection:
                try:
                    cursor.close()
                except:
                    pass
                if connection.open:
                    connection.close()
    
    
    
    def _categorize_failure_pattern(
        self,
        is_intermittent: bool,
        same_reason: bool,
        different_reasons: bool
    ) -> str:
        """
        Categorize failure pattern based on characteristics.
        
        Args:
            is_intermittent: Whether failures are intermittent (pass/fail pattern)
            same_reason: Whether all failures have the same root cause
            different_reasons: Whether failures have different root causes
            
        Returns:
            Pattern category string
        """
        if is_intermittent:
            if same_reason:
                return "Intermittently failing due to same reason"
            else:
                return "Intermittently failing but different reasons"
        else:
            if same_reason:
                return "Continuously failing due to same reason"
            else:
                return "Continuously failing but different reasons"

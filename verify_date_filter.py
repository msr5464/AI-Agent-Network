
import sys
import os
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta

# Add current directory to path
sys.path.append(os.getcwd())

# Mock pymysql
mock_pymysql = MagicMock()
sys.modules['pymysql'] = mock_pymysql
sys.modules['pymysql.cursors'] = MagicMock()

# Prepare other mocks
import types
parsers = types.ModuleType('parsers')
parsers.models = types.ModuleType('models')
parsers.models.TestResult = MagicMock()
parsers.models.TestSummary = MagicMock()
sys.modules['src.parsers'] = parsers
sys.modules['src.parsers.models'] = parsers.models
sys.modules['src.settings'] = MagicMock()

# Now we can import memory
from src.agent.memory import AgentMemory

def test_date_filtering():
    print("Testing date-based filtering...")
    memory = AgentMemory()
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    
    # Mock column check (with createdAt)
    mock_cursor.fetchall.side_effect = [
        [{'Field': 'testcaseName'}, {'Field': 'testStatus'}, {'Field': 'buildTag'}, {'Field': 'id'}, {'Field': 'createdAt'}], # Columns
        [], # Sample names
        [], # Chunk 1 exact
        [], # Chunk 1 CI
    ]
    
    test_names = ["Test1.testMethod"]
    
    with patch.object(AgentMemory, '_get_db_connection', return_value=mock_conn):
        with patch.object(AgentMemory, '_get_table_name_from_report_name', return_value="results_prodsanity"):
            memory._get_test_execution_history_from_db("report", test_names)
    
    # Check execute calls
    execute_calls = mock_cursor.execute.call_args_list
    
    # The batch query should be one of the calls
    batch_call = None
    for call in execute_calls:
        query = call[0][0]
        if "WHERE testcaseName IN (" in query:
            batch_call = call
            break
            
    assert batch_call is not None, "Batch query not found"
    
    query, params = batch_call[0]
    print(f"Query: {query}")
    print(f"Params: {params}")
    
    assert "AND createdAt >= %s" in query
    assert len(params) == 2 # 1 test name + 1 date threadhold
    
    # Verify date is roughly 14 days ago
    date_passed = datetime.strptime(params[1], '%Y-%m-%d %H:%M:%S')
    expected_date = datetime.now() - timedelta(days=14)
    # Check within 1 minute tolerance
    diff = abs((expected_date - date_passed).total_seconds())
    print(f"Date difference: {diff}s")
    assert diff < 60
    
    print("✅ Date-based filtering verified.")

if __name__ == "__main__":
    try:
        test_date_filtering()
    except Exception as e:
        print(f"❌ Verification failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

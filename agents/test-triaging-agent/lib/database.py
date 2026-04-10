"""
Database connection and helper functions for MySQL operations.
Centralizes all database-related code.
"""

import re
import logging
from typing import Optional, Dict

try:
    import pymysql
    from pymysql import Error
    MYSQL_AVAILABLE = True
    # Make pymysql compatible with mysql-connector-python style
    pymysql.install_as_MySQLdb()
except ImportError:
    MYSQL_AVAILABLE = False
    Error = Exception  # Fallback for type hints

from .settings import Config

logger = logging.getLogger(__name__)

if not MYSQL_AVAILABLE:
    logger.warning("pymysql not installed. MySQL features will be disabled.")


class Database:
    """Database connection and helper functions"""
    
    def __init__(self):
        """Initialize database connection with configuration from Config"""
        if not MYSQL_AVAILABLE:
            raise ImportError("pymysql is required. Install with: pip install pymysql")
        
        self.db_config = Config.get_db_config()
    
    def get_connection(self):
        """Get MySQL database connection."""
        if not MYSQL_AVAILABLE:
            raise ImportError("pymysql is not installed. Install with: pip install pymysql")
        
        try:
            # Use pymysql with proper parameter names
            config_with_timeout = self.db_config.copy()
            # pymysql uses 'connect_timeout' not 'connection_timeout'
            config_with_timeout['connect_timeout'] = 10
            config_with_timeout['read_timeout'] = 15
            config_with_timeout['write_timeout'] = 15
            if 'connection_timeout' in config_with_timeout:
                del config_with_timeout['connection_timeout']
            
            connection = pymysql.connect(**config_with_timeout)
            return connection
        except Error as e:
            logger.error(f"Error connecting to MySQL database: {e}")
            raise
    
    @staticmethod
    def get_table_name_from_report_name(report_name: str) -> Optional[str]:
        """
        Map report name to database table name.
        Example: "Regression-AccountOpening-Tests-420" -> "results_accountopening"
        Example: "ProdSanity-All-Tests-524" -> "results_prodsanity"
        
        Args:
            report_name: Report name like "Regression-AccountOpening-Tests-420" or "ProdSanity-All-Tests-524"
            
        Returns:
            Table name like "results_accountopening" or "results_prodsanity" or None if pattern doesn't match
        """
        if not report_name:
            return None
        
        # Pattern 1: "Regression-AccountOpening-Tests-420" -> "results_accountopening"
        match = re.search(r'Regression-([A-Za-z]+)-', report_name)
        if match:
            project_name = match.group(1).lower()
            return f"results_{project_name}"
        
        # Pattern 2: "ProdSanity-All-Tests-524" -> "results_prodsanity"
        match = re.search(r'ProdSanity-', report_name)
        if match:
            return "results_prodsanity"
        
        # Fallback: try to extract any word after "Regression-"
        match = re.search(r'Regression-([A-Za-z]+)', report_name)
        if match:
            project_name = match.group(1).lower()
            return f"results_{project_name}"
        
        logger.warning(f"Could not extract table name from report_name: {report_name}")
        return None


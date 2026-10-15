"""
Configuration management for the QA Agent Network.
Centralizes environment variable loading, configuration, and constants.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables once at module level
_config_dir = Path(__file__).parent.parent / 'config'
_config_env = _config_dir / '.env'
_config_example = _config_dir / '.env.example'

# Preferred: config/.env
if _config_env.exists():
    load_dotenv(_config_env)
# Fallback: config/.env.example (if user chose to store secrets there)
elif _config_example.exists():
    load_dotenv(_config_example)
else:
    # Final fallback to root .env if nothing in config/
    load_dotenv(Path(__file__).parent.parent / '.env')


class Config:
    """Centralized configuration class"""
    
    # Database Configuration
    TRIAGING_DB_HOST = os.getenv('TRIAGING_DB_HOST', 'localhost')
    TRIAGING_DB_PORT = int(os.getenv('TRIAGING_DB_PORT', '3306'))
    TRIAGING_DB_USER = os.getenv('TRIAGING_DB_USER', 'root')
    TRIAGING_DB_PASSWORD = os.getenv('TRIAGING_DB_PASSWORD', '')
    TRIAGING_DB_NAME = os.getenv('TRIAGING_DB_NAME', 'qa_results')
    
    # LLM Configuration
    LLM_PROVIDER = os.getenv('LLM_PROVIDER', 'ollama').lower()
    OLLAMA_MODEL = os.getenv('OLLAMA_MODEL', 'llama3.2:3b')
    OLLAMA_BASE_URL = os.getenv('OLLAMA_BASE_URL', 'http://localhost:11434')
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')
    OPENAI_MODEL = os.getenv('OPENAI_MODEL', 'gpt-4o-mini')
    GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '')
    GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-1.5-flash')
    
    # Report Configuration
    TRIAGING_INPUT_DIR = os.getenv('TRIAGING_INPUT_DIR', 'testdata')
    TRIAGING_OUTPUT_DIR = os.getenv('TRIAGING_OUTPUT_DIR', 'reports')
    # Bump this when report HTML/CSS/JS template or behavior changes so viewers refresh and support can identify format
    REPORT_FORMAT_VERSION = os.getenv('REPORT_FORMAT_VERSION', '2')
    
    # Dashboard URL Configuration (for linking to test reports)
    TRIAGING_DASHBOARD_BASE_URL = os.getenv('TRIAGING_DASHBOARD_BASE_URL', 'https://qa.dashboard.example.com')
    
    # Jira URL Configuration (for linking to known failure tickets)
    TRIAGING_JIRA_BASE_URL = os.getenv('TRIAGING_JIRA_BASE_URL', 'https://jira.example.com')
    
    # Logging Configuration
    LOG_FILE_NAME = 'agent.log'
    LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    
    # Flaky Tests Detection Constants
    TRIAGING_FLAKY_TESTS_LAST_RUNS = int(os.getenv('TRIAGING_FLAKY_TESTS_LAST_RUNS', '10'))  # X: Number of last runs to check
    TRIAGING_FLAKY_TESTS_MIN_FAILURES = int(os.getenv('TRIAGING_FLAKY_TESTS_MIN_FAILURES', '5'))  # Y: Minimum failures required

    # GitHub Configuration (read-only — test-triaging-agent does not push code)
    GITHUB_TOKEN = os.getenv('GITHUB_TOKEN', '')
    GITHUB_ORG = os.getenv('GITHUB_ORG', '')
    GITHUB_REPO_AUTOMATION = os.getenv('GITHUB_REPO_AUTOMATION', '')
    GITHUB_DEFAULT_BRANCH = os.getenv('GITHUB_DEFAULT_BRANCH', 'main')
    GITHUB_PR_REVIEWERS = [r.strip() for r in os.getenv('GITHUB_PR_REVIEWERS', '').split(',') if r.strip()]
    
    @classmethod
    def get_db_config(cls) -> dict:
        """Get database configuration dictionary"""
        return {
            'host': cls.TRIAGING_DB_HOST,
            'port': cls.TRIAGING_DB_PORT,
            'user': cls.TRIAGING_DB_USER,
            'password': cls.TRIAGING_DB_PASSWORD,
            'database': cls.TRIAGING_DB_NAME
        }


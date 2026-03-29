"""
Configuration management for the QA AI Agent.
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
    DB_HOST = os.getenv('DB_HOST', 'localhost')
    DB_PORT = int(os.getenv('DB_PORT', '3306'))
    DB_USER = os.getenv('DB_USER', 'root')
    DB_PASSWORD = os.getenv('DB_PASSWORD', '')
    DB_NAME = os.getenv('DB_NAME', 'qa_results')
    
    # LLM Configuration
    LLM_PROVIDER = os.getenv('LLM_PROVIDER', 'ollama').lower()
    OLLAMA_MODEL = os.getenv('OLLAMA_MODEL', 'llama3.2:3b')
    OLLAMA_BASE_URL = os.getenv('OLLAMA_BASE_URL', 'http://localhost:11434')
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')
    OPENAI_MODEL = os.getenv('OPENAI_MODEL', 'gpt-4o-mini')
    GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '')
    GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-1.5-flash')
    
    # Report Configuration
    INPUT_DIR = os.getenv('INPUT_DIR', 'testdata')
    OUTPUT_DIR = os.getenv('OUTPUT_DIR', 'reports')
    # Bump this when report HTML/CSS/JS template or behavior changes so viewers refresh and support can identify format
    REPORT_FORMAT_VERSION = os.getenv('REPORT_FORMAT_VERSION', '2')
    
    # Dashboard URL Configuration (for linking to test reports)
    DASHBOARD_BASE_URL = os.getenv('DASHBOARD_BASE_URL', 'https://qa.dashboard.example.com')
    
    # Jira URL Configuration (for linking to known failure tickets)
    JIRA_BASE_URL = os.getenv('JIRA_BASE_URL', 'https://jira.example.com')
    
    # Logging Configuration
    LOG_FILE_NAME = 'agent.log'
    LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    
    # Flaky Tests Detection Constants
    FLAKY_TESTS_LAST_RUNS = int(os.getenv('FLAKY_TESTS_LAST_RUNS', '10'))  # X: Number of last runs to check
    FLAKY_TESTS_MIN_FAILURES = int(os.getenv('FLAKY_TESTS_MIN_FAILURES', '5'))  # Y: Minimum failures required

    # Auto-fix / GitHub Configuration
    AUTO_FIX_ENABLED = os.getenv('AUTO_FIX_ENABLED', 'false').lower() == 'true'
    AUTO_FIX_DRY_RUN = os.getenv('AUTO_FIX_DRY_RUN', 'false').lower() == 'true'
    AUTO_FIX_MAX_FIXES_PER_RUN = int(os.getenv('AUTO_FIX_MAX_FIXES_PER_RUN', '5'))
    AUTO_FIX_ENV_OVERRIDE = os.getenv('AUTO_FIX_ENV_OVERRIDE', '').strip()
    GITHUB_TOKEN = os.getenv('GITHUB_TOKEN', '')
    GITHUB_ORG = os.getenv('GITHUB_ORG', '')
    GITHUB_REPO_AUTOMATION = os.getenv('GITHUB_REPO_AUTOMATION', '')
    GITHUB_DEFAULT_BRANCH = os.getenv('GITHUB_DEFAULT_BRANCH', 'main')
    GITHUB_PR_REVIEWERS = [r.strip() for r in os.getenv('GITHUB_PR_REVIEWERS', '').split(',') if r.strip()]
    
    @classmethod
    def get_db_config(cls) -> dict:
        """Get database configuration dictionary"""
        return {
            'host': cls.DB_HOST,
            'port': cls.DB_PORT,
            'user': cls.DB_USER,
            'password': cls.DB_PASSWORD,
            'database': cls.DB_NAME
        }


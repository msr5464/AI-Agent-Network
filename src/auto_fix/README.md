# Auto-Fix Feature

This directory contains the auto-fix feature for automatically fixing test failures and creating Pull Requests.

## 📁 Structure

```
auto_fix/
├── __init__.py          # Package initialization and exports
├── models.py            # Data models (FixProposal, PRResult, etc.)
├── manager.py           # Main orchestrator
├── code_analyzer.py     # Locates and analyzes test code
├── fix_generator.py     # AI-powered fix generation
└── github/              # GitHub integration
    ├── __init__.py
    ├── client.py        # GitHub API client
    └── pr_creator.py    # PR title/body generation
```

## 🎯 Purpose

Automatically fixes test failures classified as:
- **Product Changes**: Test code needs updates due to product changes
- **Automation Issues**: Test framework problems (locators, timeouts, etc.)

## 🚀 Usage

### Standalone Usage (Before Integration)

```python
from src.auto_fix import AutoFixManager
from src.agent.analyzer import FailureClassification

# Initialize manager
manager = AutoFixManager(
    github_token="ghp_xxxxx",
    github_org="your-org",
    github_repo_automation="automation-tests",
    llm_provider="openai",
    openai_api_key="sk-xxxxx",
    dry_run=True  # Test mode
)

# Process classifications
classifications = [...]  # List of FailureClassification objects
results = manager.process_classifications(classifications)

# Check results
for result in results:
    if result.success:
        print(f"✅ {result.test_name}: {result.pr_url}")
    elif result.skipped:
        print(f"⏭️ {result.test_name}: {result.skip_reason}")
    else:
        print(f"❌ {result.test_name}: {result.error}")
```

### After Integration with Main

```python
# In main.py (after integration)
from src.settings import Config

if Config.AUTO_FIX_ENABLED:
    from src.auto_fix import AutoFixManager
    
    manager = AutoFixManager(
        github_token=Config.GITHUB_TOKEN,
        github_org=Config.GITHUB_ORG,
        # ... other config
    )
    results = manager.process_classifications(classifications)
```

## 🔧 Configuration

Required environment variables (will be added to `config/.env` during integration):

```bash
# GitHub Configuration
GITHUB_TOKEN=ghp_xxxxxxxxxxxxx
GITHUB_ORG=your-org-name
GITHUB_REPO_AUTOMATION=automation-tests-repo
GITHUB_DEFAULT_BRANCH=main
GITHUB_PR_REVIEWERS=user1,user2

# Auto-Fix Configuration
AUTO_FIX_ENABLED=true
AUTO_FIX_DRY_RUN=false
AUTO_FIX_MAX_FIXES_PER_RUN=5
```

## 🔒 Safety Features

1. **Dry Run Mode**: Test without creating actual PRs
2. **Confidence Filtering**: Only HIGH/MEDIUM confidence fixes
3. **Max Fixes Limit**: Prevent overwhelming the team
4. **Syntax Validation**: Basic checks before committing
5. **PR Labels**: All PRs tagged as "automated-fix"

## 📝 Components

### AutoFixManager
Main orchestrator that coordinates the entire workflow.

### CodeAnalyzer
Locates test files and extracts method code from Java files.

### FixGenerator
Uses AI (OpenAI/Ollama) to generate code fixes.

### GitHubClient
Manages git operations and GitHub API interactions.

### PRCreator
Generates PR titles, descriptions, and labels.

## 🧪 Testing

```bash
# Test with dry run
python -c "
from src.auto_fix import AutoFixManager
manager = AutoFixManager(..., dry_run=True)
# ... test code
"
```

## 📚 Dependencies

Required packages (will be added to requirements.txt during integration):
- `PyGithub>=2.1.1`
- `GitPython>=3.1.40`

## 🔄 Status

**Current**: ✅ Core components implemented  
**Next**: Integration with main.py (pending user approval)

## 📖 Documentation

See [`docs/auto-fix-implementation-plan.md`](../../docs/auto-fix-implementation-plan.md) for complete implementation details.

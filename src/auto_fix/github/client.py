"""
GitHub API integration for repository management and PR creation.
"""

import os
import logging
from pathlib import Path
from typing import List, Dict, Optional, Tuple

try:
    from github import Github, GithubException
    GITHUB_AVAILABLE = True
except ImportError:
    GITHUB_AVAILABLE = False
    
try:
    from git import Repo, GitCommandError
    GIT_AVAILABLE = True
except ImportError:
    GIT_AVAILABLE = False

from ..models import FileChange, PRResult

logger = logging.getLogger(__name__)


class GitHubClient:
    """Manages GitHub API interactions and git operations"""
    
    def __init__(self, github_token: str, org: str, default_branch: str = "main"):
        """
        Initialize GitHub client with token.
        
        Args:
            github_token: GitHub personal access token
            org: GitHub organization name
            default_branch: Default branch name (default: "main")
        """
        if not GITHUB_AVAILABLE:
            raise ImportError("PyGithub not installed. Run: pip install PyGithub")
        if not GIT_AVAILABLE:
            raise ImportError("GitPython not installed. Run: pip install GitPython")
        
        if not github_token:
            raise ValueError("GitHub token is required")
        
        self.github = Github(github_token)
        self.org = org
        self.default_branch = default_branch
        self.workspace_dir = Path("workspace")
        self.workspace_dir.mkdir(exist_ok=True)
        
        logger.info(f"GitHub client initialized for org: {self.org}")
    
    def clone_repository(self, repo_name: str, local_path: Optional[str] = None) -> str:
        """
        Clone a GitHub repository.
        
        Args:
            repo_name: Repository name (e.g., "automation-tests-repo")
            local_path: Optional local path, defaults to workspace/repo_name
            
        Returns:
            Absolute path to cloned repository
        """
        if local_path is None:
            local_path = self.workspace_dir / repo_name
        
        # Remove existing directory if it exists
        if Path(local_path).exists():
            logger.info(f"Removing existing directory: {local_path}")
            import shutil
            shutil.rmtree(local_path)
        
        # Clone repository
        repo_url = f"https://{self.github._Github__requester._Requester__auth.token}@github.com/{self.org}/{repo_name}.git"
        logger.info(f"Cloning repository: {self.org}/{repo_name}")
        
        try:
            Repo.clone_from(repo_url, local_path)
            logger.info(f"Repository cloned to: {local_path}")
            return str(local_path)
        except GitCommandError as e:
            logger.error(f"Failed to clone repository: {e}")
            raise
    
    def create_branch(self, repo_path: str, branch_name: str) -> bool:
        """
        Create a new branch in the local repository.
        
        Args:
            repo_path: Path to local repository
            branch_name: Name of the new branch
            
        Returns:
            True if successful
        """
        try:
            repo = Repo(repo_path)
            # Ensure we're on the default branch and up to date
            repo.git.checkout(self.default_branch)
            repo.git.pull()

            # If branch already exists locally, reuse it
            if branch_name in repo.heads:
                repo.git.checkout(branch_name)
                logger.info(f"Checked out existing branch: {branch_name}")
                return True

            # If branch exists remotely but not locally, check it out tracking origin
            try:
                repo.git.fetch("origin", branch_name)
                repo.git.checkout("-B", branch_name, f"origin/{branch_name}")
                logger.info(f"Checked out remote branch: {branch_name}")
                return True
            except GitCommandError:
                # Create new branch from default
                repo.git.checkout('-b', branch_name)
                logger.info(f"Created branch: {branch_name}")
                return True

        except GitCommandError as e:
            logger.error(f"Failed to create branch: {e}")
            return False
    
    def apply_changes(self, repo_path: str, file_changes: List[FileChange]) -> bool:
        """
        Apply file changes to the repository.
        
        Args:
            repo_path: Path to local repository
            file_changes: List of FileChange objects
            
        Returns:
            True if all changes applied successfully
        """
        try:
            for change in file_changes:
                file_path = Path(repo_path) / change.file_path
                
                if change.change_type == "modify" or change.change_type == "create":
                    # Ensure parent directory exists
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    # Write new content
                    file_path.write_text(change.new_content)
                    logger.info(f"Applied change to: {change.file_path}")
                elif change.change_type == "delete":
                    if file_path.exists():
                        file_path.unlink()
                        logger.info(f"Deleted file: {change.file_path}")
            
            return True
        except Exception as e:
            logger.error(f"Failed to apply changes: {e}")
            return False
    
    def commit_changes(self, repo_path: str, message: str) -> bool:
        """
        Commit changes in the repository.
        
        Args:
            repo_path: Path to local repository
            message: Commit message
            
        Returns:
            True if successful
        """
        try:
            repo = Repo(repo_path)
            # Add all changes
            repo.git.add(A=True)
            # Commit
            repo.index.commit(message)
            logger.info(f"Committed changes: {message}")
            return True
        except GitCommandError as e:
            logger.error(f"Failed to commit changes: {e}")
            return False
    
    def push_branch(self, repo_path: str, branch_name: str, force: bool = False) -> bool:
        """
        Push branch to remote repository.
        
        Args:
            repo_path: Path to local repository
            branch_name: Name of the branch to push
            force: Force push when branch already exists remotely
            
        Returns:
            True if successful
        """
        try:
            repo = Repo(repo_path)
            origin = repo.remote(name='origin')
            if force:
                origin.push(branch_name, force=True)
            else:
                origin.push(branch_name)
            logger.info(f"Pushed branch: {branch_name}")
            return True
        except GitCommandError as e:
            logger.error(f"Failed to push branch: {e}")
            return False
    
    def get_open_pr_by_branch(self, repo_name: str, branch_name: str):
        """Return an open PR for the given branch if it exists."""
        repo = self.github.get_repo(f"{self.org}/{repo_name}")
        try:
            pulls = repo.get_pulls(state='open', head=f"{self.org}:{branch_name}")
            for pr in pulls:
                return pr
            return None
        except GithubException as e:
            logger.warning(f"Unable to read existing PRs for {branch_name}: {e}")
            return None
    
    def create_pull_request(
        self,
        repo_name: str,
        branch_name: str,
        title: str,
        body: str,
        labels: Optional[List[str]] = None,
        reviewers: Optional[List[str]] = None,
        reuse_existing: bool = True
    ) -> PRResult:
        """
        Create a pull request on GitHub.
        
        Args:
            repo_name: Repository name
            branch_name: Source branch name
            title: PR title
            body: PR description
            labels: Optional list of labels
            reviewers: Optional list of reviewer usernames
            
        Returns:
            PRResult object
        """
        try:
            repo = self.github.get_repo(f"{self.org}/{repo_name}")
            
            pr = None
            if reuse_existing:
                pr = self.get_open_pr_by_branch(repo_name, branch_name)
                if pr:
                    logger.info(f"Reusing existing PR: {pr.html_url}")
            
            if pr is None:
                pr = repo.create_pull(
                    title=title,
                    body=body,
                    head=branch_name,
                    base=self.default_branch
                )
                logger.info(f"Created PR: {pr.html_url}")
            
            # Add labels if provided
            if labels:
                pr.add_to_labels(*labels)
                logger.info(f"Added labels: {labels}")
            
            # Request reviewers if provided
            if reviewers:
                pr.create_review_request(reviewers=reviewers)
                logger.info(f"Requested reviewers: {reviewers}")
            
            return PRResult(success=True, pr_url=pr.html_url)
            
        except GithubException as e:
            logger.error(f"Failed to create PR: {e}")
            return PRResult(success=False, error=str(e))

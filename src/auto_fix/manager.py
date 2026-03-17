"""
Orchestrates the auto-fix workflow for Product Changes and Automation Issues.
"""

import logging
import re
import json
import time
from typing import List, Optional, Tuple, Dict, Any, Set
from datetime import datetime
from pathlib import Path

# Import from parent package
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.agent.analyzer import FailureClassification
from .code_analyzer import CodeAnalyzer
from .context_builder import ContextBuilder
from .fix_generator import FixGenerator, FixProposal
from .test_runner import TestRunner
from .github.client import GitHubClient
from .github.pr_creator import PRCreator
from .cursor_client import CursorClient
from .browser_inspector import BrowserInspector
from .models import FileChange, PRResult, AutoFixResult

logger = logging.getLogger(__name__)


class AutoFixManager:
    """Manages the auto-fix workflow"""
    
    def __init__(
        self,
        github_token: str,
        github_org: str,
        github_repo_automation: str,
        github_default_branch: str = "main",
        github_pr_reviewers: Optional[List[str]] = None,
        llm_provider: str = "openai",
        openai_api_key: Optional[str] = None,
        openai_model: str = "gpt-4o-mini",
        ollama_model: str = "llama3.2:3b",
        ollama_base_url: str = "http://localhost:11434",
        gemini_api_key: Optional[str] = None,
        gemini_model: str = "gemini-1.5-flash",
        max_fixes_per_run: int = 5,
        dry_run: bool = False,
        run_tests_locally: bool = True,  # New flag
        target_environment: str = "",
        session_file: Optional[str] = None  # File to track passed tests
    ):
        """
        Initialize auto-fix manager.
        ...
        """
        self.code_analyzer = CodeAnalyzer()
        self.context_builder = ContextBuilder(self.code_analyzer)
        self.fix_generator = FixGenerator(
            llm_provider=llm_provider,
            openai_api_key=openai_api_key,
            openai_model=openai_model,
            ollama_model=ollama_model,
            ollama_base_url=ollama_base_url,
            gemini_api_key=gemini_api_key,
            gemini_model=gemini_model
        )
        self.github_client = GitHubClient(
            github_token=github_token,
            org=github_org,
            default_branch=github_default_branch
        )
        self.pr_creator = PRCreator()
        self.cursor_client = CursorClient()
        
        self.github_repo_automation = github_repo_automation
        self.github_pr_reviewers = github_pr_reviewers or []
        self.max_fixes_per_run = max_fixes_per_run
        self.dry_run = dry_run
        self.run_tests_locally = run_tests_locally
        self.target_environment = target_environment.strip()
        self.context_cache = {}
        self.session_file = session_file
        
        # Load session data (passed tests from previous runs)
        self.passed_tests: Set[str] = self._load_session_data()
        
        # Initialize browser inspector for locator discovery (lazy initialization)
        self.browser_inspector = None
        
        logger.info("Auto-fix manager initialized")
        if dry_run:
            logger.warning("DRY RUN MODE: No actual PRs will be created")
        if self.passed_tests:
            logger.info(f"📋 Loaded {len(self.passed_tests)} passed tests from session (will be skipped)")
    
    def process_classifications(
        self,
        classifications: List[FailureClassification]
    ) -> List[AutoFixResult]:
        """
        Process classifications and create fixes/PRs for auto-fixable issues.
        
        Args:
            classifications: List of failure classifications
            
        Returns:
            List of AutoFixResult objects
        """
        results = []
        auto_fixable = [c for c in classifications if self.is_auto_fixable(c)]
        
        logger.info(f"Found {len(auto_fixable)} auto-fixable failures out of {len(classifications)} total")
        
        # Limit number of fixes per run
        if len(auto_fixable) > self.max_fixes_per_run:
            logger.warning(f"Limiting to {self.max_fixes_per_run} fixes per run")
            auto_fixable = auto_fixable[:self.max_fixes_per_run]
        
        if not auto_fixable:
            return results
        
        # Clone repository once for all fixes (optimization)
        logger.info(f"Cloning repository: {self.github_repo_automation}")
        try:
            repo_path = self.github_client.clone_repository(self.github_repo_automation)
        except Exception as e:
            logger.error(f"Failed to clone repository: {e}")
            # Return all as failed
            for classification in auto_fixable:
                results.append(AutoFixResult(
                    test_name=classification.test_name,
                    success=False,
                    error=f"Failed to clone repository: {e}"
                ))
            return results
        
        # Process each failure using the same cloned repo
        for classification in auto_fixable:
            if self.target_environment:
                self._ensure_environment(repo_path, self.target_environment)
            result = self._process_single_failure(classification, repo_path)
            results.append(result)
            
            # Track tests that passed locally
            if result.skipped and "passed locally" in (result.skip_reason or ""):
                self.passed_tests.add(classification.test_name)
        
        # Save session data (passed tests)
        self._save_session_data()
        
        # Summary
        successful = sum(1 for r in results if r.success)
        failed = sum(1 for r in results if not r.success and not r.skipped)
        skipped = sum(1 for r in results if r.skipped)
        
        logger.info(f"Auto-fix summary: {successful} successful, {failed} failed, {skipped} skipped")
        
        return results
    
    def is_auto_fixable(self, classification: FailureClassification) -> bool:
        """
        Determine if a failure is auto-fixable.
        
        Args:
            classification: Failure classification
            
        Returns:
            True if auto-fixable
        """
        # Only fix Product Changes and Automation Issues
        if classification.classification not in ['PRODUCT_CHANGE', 'AUTOMATION_ISSUE']:
            return False
        
        # Require HIGH or MEDIUM confidence
        if classification.confidence not in ['HIGH', 'MEDIUM']:
            logger.debug(f"Skipping {classification.test_name}: Low confidence")
            return False
        
        return True
    
    def _process_single_failure(
        self,
        classification: FailureClassification,
        repo_path: str
    ) -> AutoFixResult:
        """Process a single failure and create PR"""
        test_name = classification.test_name
        start_time = time.time()
        logger.info(f"🔧 Processing auto-fix for: {test_name}")
        logger.info(f"   ⏱️  Estimated time: 15-25 minutes (test runs + LLM + browser inspection + verification)")
        
        try:
            # Step 1: Find test file (repo already cloned)
            test_file = self.code_analyzer.find_test_file(test_name, repo_path)
            if not test_file:
                return AutoFixResult(
                    test_name=test_name,
                    success=False,
                    skipped=True,
                    skip_reason="Test file not found in repository"
                )
            
            # Step 2: Extract test method and context
            method_name = test_name.split('.')[-1]
            full_file_path = Path(repo_path) / test_file
            test_code = self.code_analyzer.extract_test_method(str(full_file_path), method_name)
            
            if not test_code:
                return AutoFixResult(
                    test_name=test_name,
                    success=False,
                    skipped=True,
                    skip_reason="Test method not found in file"
                )
            
            # Build initial context (no stack frames yet, but include classification for locator lookup)
            cache_key = (test_file, method_name, classification.root_cause_category)
            base_context = self.context_cache.get(cache_key)
            if not base_context:
                base_context = self.context_builder.collect(
                    repo_path,
                    test_file,
                    method_name,
                    stack_frames=None,
                    root_cause=classification.root_cause,
                    execution_log="",  # Will be updated with fresh_logs later
                    root_cause_category=classification.root_cause_category
                )
                self.context_cache[cache_key] = base_context
            
            # Attempt loop: initial + one retry with stack trace from verification
            max_attempts = 2
            last_error = None
            for attempt in range(max_attempts):
                # Step 2.5: Run test locally to get fresh logs (Active Analysis)
                fresh_logs = ""
                stack_frames = []
                test_to_run = test_name
                if self.run_tests_locally:
                    class_name = test_name.split('.')[-2]
                    fqcn = self.code_analyzer.get_fully_qualified_name(str(full_file_path), class_name, method_name)
                    test_to_run = fqcn if fqcn else test_name
                    
                    logger.info(f"🏃 [Step 1/6] Running test locally to capture fresh logs: {test_to_run}")
                    logger.info(f"   ⏱️  This may take up to 10 minutes (test timeout: 600s)...")
                    test_start = time.time()
                    test_runner = TestRunner(repo_path)
                    success, stdout, stderr = test_runner.run_test(test_to_run)
                    test_duration = time.time() - test_start
                    logger.info(f"   ✅ Test run completed in {test_duration:.1f}s")
                    
                    if success:
                        logger.warning(
                            f"Test {test_to_run} passed locally! "
                            "Skipping fix generation to avoid over-writing existing logic."
                        )
                        return AutoFixResult(
                            test_name=test_name,
                            success=False,
                            skipped=True,
                            skip_reason="Test passed locally; no fix generated"
                        )
                    else:
                        logger.info(f"Test failed locally as expected. Capturing logs.")
                        fresh_logs = test_runner.extract_failure_log(stdout + "\n" + stderr)
                        stack_frames = self._parse_stack_frames(fresh_logs, repo_path)
                
                # Build context with stack frames and fresh logs for locator lookup
                context = dict(base_context)
                context['file_path'] = test_file
                if stack_frames:
                    context['stack_frames'] = stack_frames
                    context['stack_file_snippets'] = self._collect_stack_file_snippets(stack_frames, repo_path)
                
                # Update context with page objects and discovered locators (for ELEMENT_NOT_FOUND/TIMEOUT)
                if fresh_logs and classification.root_cause_category in ['ELEMENT_NOT_FOUND', 'TIMEOUT']:
                    # Re-collect context with fresh logs to find page objects
                    updated_context = self.context_builder.collect(
                        repo_path,
                        test_file,
                        method_name,
                        stack_frames=stack_frames,
                        root_cause=classification.root_cause,
                        execution_log=fresh_logs,
                        root_cause_category=classification.root_cause_category
                    )
                    # Merge page objects if found
                    if 'page_objects' in updated_context:
                        context['page_objects'] = updated_context['page_objects']
                    
                    # Browser-based locator discovery
                    logger.info(f"🔍 [Step 2/6] Browser inspection for locator discovery (if ELEMENT_NOT_FOUND/TIMEOUT)...")
                    browser_start = time.time()
                    discovered_locators = self._discover_locators_from_browser(
                        classification, fresh_logs
                    )
                    browser_duration = time.time() - browser_start
                    if discovered_locators:
                        context['discovered_locators'] = discovered_locators
                        logger.info(f"   ✅ Discovered {len(discovered_locators)} locator candidates in {browser_duration:.1f}s")
                    elif classification.root_cause_category in ['ELEMENT_NOT_FOUND', 'TIMEOUT']:
                        logger.info(f"   ⚠️  Browser inspection skipped (no page URL found or other issue)")
                
                classification_with_logs = classification
                if fresh_logs:
                    classification_with_logs = FailureClassification(
                        test_name=classification.test_name,
                        classification=classification.classification,
                        confidence=classification.confidence,
                        root_cause=f"{classification.root_cause}\n\n--- FRESH EXECUTION LOGS ---\n{fresh_logs}",
                        recommended_action=classification.recommended_action,
                        root_cause_category=classification.root_cause_category
                    )
                
                # Step 3: Generate fix
                logger.info(f"🤖 [Step 3/6] Generating fix using LLM...")
                llm_start = time.time()
                fix_proposal = self.fix_generator.generate_fix(classification_with_logs, test_code, context)
                llm_duration = time.time() - llm_start
                if fix_proposal:
                    logger.info(f"   ✅ Fix generated in {llm_duration:.1f}s")
                
                if not fix_proposal:
                    last_error = "Failed to generate fix"
                    if attempt == max_attempts - 1:
                        return AutoFixResult(test_name=test_name, success=False, error=last_error)
                    continue
                
                # Step 4: Validate fix
                if not self.fix_generator.validate_fix_syntax(fix_proposal.fixed_code):
                    last_error = "Generated fix has invalid syntax"
                    if attempt == max_attempts - 1:
                        return AutoFixResult(test_name=test_name, success=False, error=last_error)
                    continue
                
                # Step 5: Apply fix and create PR
                if self.dry_run:
                    logger.info("DRY RUN: Would create PR with fix")
                    return AutoFixResult(
                        test_name=test_name,
                        success=True,
                        pr_url="DRY_RUN_MODE"
                    )
                
                logger.info(f"💾 [Step 4/6] Applying fix via Cursor CLI...")
                logger.info(f"🔁 [Step 5/6] Verifying fix (running test again, may take up to 10 minutes)...")
                logger.info(f"📤 [Step 6/6] Creating PR on GitHub...")
                pr_start = time.time()
                pr_result = self._create_pr_with_fix(
                    repo_path,
                    classification_with_logs,
                    fix_proposal,
                    test_file,
                    test_to_run
                )
                pr_duration = time.time() - pr_start
                total_duration = time.time() - start_time
                logger.info(f"   ✅ PR creation completed in {pr_duration:.1f}s")
                logger.info(f"   ⏱️  Total time for this fix: {total_duration:.1f}s ({total_duration/60:.1f} minutes)")
                
                if pr_result.success:
                    return AutoFixResult(
                        test_name=test_name,
                        success=True,
                        pr_url=pr_result.pr_url
                    )
                else:
                    last_error = pr_result.error
                    if attempt == max_attempts - 1:
                        return AutoFixResult(
                            test_name=test_name,
                            success=False,
                            error=pr_result.error
                        )
                    logger.info("Retrying after failed apply/verify with additional logs")
            
            return AutoFixResult(test_name=test_name, success=False, error=last_error or "Unknown failure")
                
        except Exception as e:
            logger.error(f"Error processing {test_name}: {e}")
            return AutoFixResult(
                test_name=test_name,
                success=False,
                error=str(e)
            )
    
    def _create_pr_with_fix(
        self,
        repo_path: str,
        classification: FailureClassification,
        fix_proposal: FixProposal,
        test_file: str,
        test_to_run: str
    ) -> PRResult:
        """Create a PR with the fix"""
        
        branch_name = self._build_branch_name(classification.test_name)
        existing_pr = self.github_client.get_open_pr_by_branch(
            self.github_repo_automation,
            branch_name
        )
        if existing_pr:
            logger.info(f"Existing PR found for {classification.test_name}: {existing_pr.html_url}. Reusing branch.")
        

        if not self.github_client.create_branch(repo_path, branch_name):
            return PRResult(success=False, error="Failed to create branch")
        
        # Apply fix to file (via Cursor client adapter)
        file_changes: List[FileChange] = []
        original_contents = {}
        full_file_path = Path(repo_path) / test_file
        current_content = full_file_path.read_text()
        original_contents[test_file] = current_content
        
        updated_content, apply_error = self._apply_fix_to_method(current_content, fix_proposal)
        if apply_error:
            return PRResult(success=False, error=apply_error)
        
        file_changes.append(
            FileChange(
                file_path=test_file,
                new_content=updated_content,
                change_type="modify"
            )
        )
        
        for extra_change in fix_proposal.additional_changes:
            extra_path = Path(repo_path) / extra_change.file_path
            if not extra_path.exists():
                return PRResult(
                    success=False,
                    error=f"Additional file not found: {extra_change.file_path}"
                )
            extra_content = extra_path.read_text()
            original_contents[extra_change.file_path] = extra_content
            updated_extra_content, replaced = self._flex_replace(
                extra_content,
                extra_change.original_snippet,
                extra_change.updated_snippet
            )
            if not replaced:
                self.cursor_client.restore_files(repo_path, original_contents)
                logger.error("Snippet not found in %s. Snippet preview: %s", extra_change.file_path, extra_change.original_snippet[:500])
                return PRResult(
                    success=False,
                    error=f"Original snippet not found in {extra_change.file_path}"
                )
            file_changes.append(
                FileChange(
                    file_path=extra_change.file_path,
                    new_content=updated_extra_content,
                    change_type="modify"
                )
            )
        
        logger.info(f"   Applying changes via Cursor CLI...")
        apply_start = time.time()
        applied, apply_err = self.cursor_client.apply_changes(repo_path, file_changes)
        apply_duration = time.time() - apply_start
        if not applied:
            self.cursor_client.restore_files(repo_path, original_contents)
            return PRResult(success=False, error=f"Failed to apply changes via Cursor adapter: {apply_err}")
        logger.info(f"   ✅ Changes applied in {apply_duration:.1f}s")
        
        logger.info(f"   Verifying fix by re-running test (this may take up to 10 minutes)...")
        verify_start = time.time()
        verification_error = self._verify_changes(repo_path, test_to_run)
        verify_duration = time.time() - verify_start
        logger.info(f"   ✅ Verification completed in {verify_duration:.1f}s")
        if verification_error:
            self.cursor_client.restore_files(repo_path, original_contents)
            return PRResult(success=False, error=verification_error)
        
        # Commit changes
        commit_message = f"Auto-fix: {classification.test_name}\n\n{fix_proposal.explanation}"
        if not self.github_client.commit_changes(repo_path, commit_message):
            return PRResult(success=False, error="Failed to commit changes")
        
        # Push branch
        force_push = existing_pr is not None
        if not self.github_client.push_branch(repo_path, branch_name, force=force_push):
            return PRResult(success=False, error="Failed to push branch")
        
        # Create PR
        pr_title = self.pr_creator.generate_pr_title(classification)
        pr_body = self.pr_creator.generate_pr_body(classification, fix_proposal)
        labels = self.pr_creator.determine_labels(classification)
        reviewers = self.github_pr_reviewers if self.github_pr_reviewers else None
        
        return self.github_client.create_pull_request(
            repo_name=self.github_repo_automation,
            branch_name=branch_name,
            title=pr_title,
            body=pr_body,
            labels=labels,
            reviewers=reviewers,
            reuse_existing=True
        )

    def _build_branch_name(self, test_name: str) -> str:
        """Deterministic branch name per test."""
        parts = test_name.split('.')
        method = parts[-1] if parts else test_name
        slug = re.sub(r'[^a-zA-Z0-9_-]', '', method) or "autoFix"
        return f"auto-fix/{slug[:40]}"

    def _ensure_environment(self, repo_path: str, target_env: str):
        """
        Ensure Parameters/config.properties has the desired environment value.
        """
        config_path = Path(repo_path) / "Parameters" / "config.properties"
        if not config_path.exists():
            logger.warning("config.properties not found at %s", config_path)
            return

        try:
            content = config_path.read_text(encoding="utf-8")
        except Exception as exc:
            logger.warning("Unable to read config.properties: %s", exc)
            return

        lines = content.splitlines()
        changed = False
        new_lines = []
        for line in lines:
            if line.strip().startswith("environment="):
                current = line.split("=", 1)[1].strip()
                if current != target_env:
                    new_lines.append(f"environment={target_env}")
                    changed = True
                else:
                    new_lines.append(line)
            else:
                new_lines.append(line)

        if changed:
            try:
                config_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
                logger.info("Updated environment to '%s' in %s", target_env, config_path)
            except Exception as exc:
                logger.warning("Unable to write config.properties: %s", exc)

    def _parse_stack_frames(self, failure_log: str, repo_path: str):
        """
        Parse stack frames of the form 'at package.Class.method(File.java:123)'
        and return list of dicts with file_path, line_no, snippet.
        """
        frames = []
        if not failure_log:
            return frames
        import re
        pattern = re.compile(r'at\s+[\w.$]+\([\w./-]+\.java:\d+\)')
        for line in failure_log.splitlines():
            match = pattern.search(line)
            if not match:
                continue
            entry = match.group(0)
            file_match = re.search(r'\(([^:]+):(\d+)\)', entry)
            if not file_match:
                continue
            file_name = file_match.group(1)
            line_no = int(file_match.group(2))
            # Find file under repo_path
            candidate = self._find_file_in_repo(repo_path, file_name)
            snippet = ""
            if candidate and candidate.exists():
                snippet = self._read_snippet(candidate, line_no)
            frames.append({
                "file_path": str(candidate.relative_to(repo_path)) if candidate else file_name,
                "line_no": line_no,
                "snippet": snippet
            })
        return frames

    def _find_file_in_repo(self, repo_path: str, file_name: str):
        repo = Path(repo_path)
        try:
            for path in repo.rglob(file_name):
                return path
        except Exception:
            return None
        return None

    def _read_snippet(self, path: Path, line_no: int, context: int = 5) -> str:
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            start = max(0, line_no - context - 1)
            end = min(len(lines), line_no + context)
            snippet_lines = lines[start:end]
            return "\n".join(snippet_lines)
        except Exception:
            return ""

    def _collect_stack_file_snippets(self, frames: List[Dict[str, Any]], repo_path: str, context_lines: int = 40) -> List[Dict[str, str]]:
        """
        Given parsed stack frames, load the referenced files and extract surrounding code.
        """
        snippets = []
        repo = Path(repo_path)
        for frame in frames or []:
            rel = frame.get("file_path")
            line_no = frame.get("line_no")
            if not rel or not line_no:
                continue
            try:
                file_path = repo / rel
                if not file_path.exists():
                    continue
                lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
                start = max(0, int(line_no) - context_lines // 2 - 1)
                end = min(len(lines), int(line_no) + context_lines // 2)
                snippet = "\n".join(lines[start:end])
                snippets.append({
                    "path": str(file_path.relative_to(repo)),
                    "line": int(line_no),
                    "snippet": snippet
                })
            except Exception as exc:
                logger.debug(f"Unable to load stack frame file snippet for {rel}:{line_no}: {exc}")
                continue
        return snippets
    
    def _discover_locators_from_browser(
        self,
        classification: FailureClassification,
        execution_log: str
    ) -> List[Dict[str, str]]:
        """
        Use browser inspector to discover new locators for ELEMENT_NOT_FOUND/TIMEOUT failures.
        
        Args:
            classification: Failure classification with root cause
            execution_log: Full execution log
            
        Returns:
            List of discovered locator candidates as dicts
        """
        try:
            # Lazy initialization of browser inspector
            if not self.browser_inspector:
                try:
                    self.browser_inspector = BrowserInspector(headless=True, timeout=10)
                except Exception as e:
                    logger.warning(f"Failed to initialize browser inspector: {e}. Locator discovery will be skipped.")
                    return []
            
            # Extract page URL from logs
            page_url = self.browser_inspector.extract_page_url(execution_log, classification.root_cause)
            if not page_url:
                logger.debug("No page URL found in execution logs for browser inspection")
                return []
            
            # Extract element name from root cause
            element_name = self._extract_element_name_from_root_cause(classification.root_cause)
            if not element_name:
                logger.debug("Could not extract element name from root cause for browser inspection")
                return []
            
            logger.info(f"🔍 Discovering locators for element '{element_name}' on page: {page_url}")
            
            # Discover locators using browser inspector
            # Use context manager to ensure browser is properly closed
            with self.browser_inspector:
                candidates = self.browser_inspector.discover_element_locators(
                    page_url=page_url,
                    element_name=element_name,
                    element_text_hint=element_name
                )
            
            # Convert LocatorCandidate objects to dicts for JSON serialization
            discovered = []
            for candidate in candidates:
                discovered.append({
                    'type': candidate.locator_type,
                    'value': candidate.locator_value,
                    'confidence': candidate.confidence,
                    'element_text': candidate.element_text
                })
            
            return discovered
            
        except Exception as e:
            logger.warning(f"Error during browser-based locator discovery: {e}")
            return []
    
    def _extract_element_name_from_root_cause(self, root_cause: str) -> Optional[str]:
        """
        Extract element name from root cause text.
        Examples:
        - "Element 'DashPeopleDetailsPage:Block Reason PopUp Header' is NOT visible"
        -> "Block Reason PopUp Header"
        - "Element 'Submit Button' is NOT clickable"
        -> "Submit Button"
        """
        # Pattern 1: "PageName:ElementName" format
        pattern1 = re.compile(r"['\"]([A-Za-z][\w]*Page):([A-Za-z][\w\s]+)['\"]", re.IGNORECASE)
        match = pattern1.search(root_cause)
        if match:
            return match.group(2).strip()
        
        # Pattern 2: "Element 'elementName' is NOT"
        pattern2 = re.compile(r"Element\s+['\"]([A-Za-z][\w\s]+)['\"]\s+is\s+NOT", re.IGNORECASE)
        match = pattern2.search(root_cause)
        if match:
            return match.group(1).strip()
        
        # Pattern 3: Extract from exception messages
        pattern3 = re.compile(r"(?:NoSuchElementException|TimeoutException).*?['\"]([^'\"]+)['\"]", re.IGNORECASE)
        match = pattern3.search(root_cause)
        if match:
            locator = match.group(1).strip()
            # Extract meaningful part (remove common prefixes)
            if ':' in locator:
                return locator.split(':')[-1].strip()
            return locator
        
        return None

    def _verify_changes(self, repo_path: str, test_to_run: str) -> Optional[str]:
        """Run the target test to ensure edits compile and execute."""
        logger.info("🔁 Verifying changes by re-running: %s", test_to_run)
        test_runner = TestRunner(repo_path)
        success, stdout, stderr = test_runner.run_test(test_to_run)
        if success:
            logger.info("✅ Verification passed for %s", test_to_run)
            return None
        preview = stderr or stdout[-1000:]
        logger.error("❌ Verification failed for %s", test_to_run)
        return f"Post-change verification failed:\n{preview}"

    def _apply_fix_to_method(self, file_content: str, fix_proposal: FixProposal) -> Tuple[str, Optional[str]]:
        """
        Replace the original method with the generated method while ensuring the
        signature/annotations are preserved and the structure is valid.
        """
        original_code = (fix_proposal.original_code or "").strip()
        fixed_code = (fix_proposal.fixed_code or "").strip()

        if not original_code or not fixed_code:
            return file_content, "Generated fix missing original or fixed code fragments"

        signature_block = self._extract_signature_block(original_code)
        if not signature_block:
            return file_content, "Unable to determine method signature for validation"

        if not self._has_matching_signature(fixed_code, signature_block):
            preview = signature_block.strip().replace("\n", "\\n")[:120]
            return file_content, f"Generated fix must include the same method signature (@Test + method). Expected: {preview}"

        if not self._has_balanced_braces(fixed_code):
            return file_content, "Generated fix has unbalanced braces"

        updated_content, replaced = self._flex_replace(file_content, original_code, fixed_code)
        if not replaced:
            return file_content, "Original test method not found in file during replacement"
        return updated_content, None

    def _extract_signature_block(self, code: str) -> Optional[str]:
        """Return the annotation + signature block up to the opening brace."""
        lines = (code or "").strip().splitlines()
        block = []
        for line in lines:
            stripped = line.rstrip()
            if not stripped and not block:
                continue
            block.append(stripped)
            if stripped.endswith('{'):
                break
        return "\n".join(block).strip() if block else None

    def _has_matching_signature(self, fixed_code: str, signature_block: str) -> bool:
        """Ensure the generated code starts with the same signature block."""
        candidate = self._extract_signature_block(fixed_code)
        if not candidate:
            return False
        return self._normalize_signature_text(candidate) == self._normalize_signature_text(signature_block)

    def _has_balanced_braces(self, code: str) -> bool:
        """Basic brace balance check to avoid truncated methods."""
        trimmed = code.strip()
        return trimmed.endswith('}') and trimmed.count('{') == trimmed.count('}')

    def _normalize_signature_text(self, text: str) -> str:
        """Collapse whitespace for resilient signature comparisons."""
        return re.sub(r'\s+', ' ', text.strip())

    def _flex_replace(self, content: str, target: str, replacement: str) -> Tuple[str, bool]:
        """Replace target snippet with tolerance for whitespace differences."""
        if target in content:
            return content.replace(target, replacement, 1), True

        # Fallback: use regex to match allowing whitespace variance
        pattern = re.sub(r'\s+', r'\\s+', re.escape(target.strip()))
        match = re.search(pattern, content, re.MULTILINE)
        if not match:
            return content, False

        return content[:match.start()] + replacement + content[match.end():], True
    
    def _load_session_data(self) -> Set[str]:
        """Load passed tests from session file"""
        if not self.session_file:
            return set()
        
        session_path = Path(self.session_file)
        if not session_path.exists():
            return set()
        
        try:
            with open(session_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                passed_tests = set(data.get('passed_tests', []))
                logger.debug(f"Loaded {len(passed_tests)} passed tests from session file")
                return passed_tests
        except Exception as e:
            logger.warning(f"Failed to load session data from {self.session_file}: {e}")
            return set()
    
    def _save_session_data(self):
        """Save passed tests to session file"""
        if not self.session_file:
            return
        
        session_path = Path(self.session_file)
        try:
            # Create directory if needed
            session_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Load existing data and merge
            existing_data = {}
            if session_path.exists():
                try:
                    with open(session_path, 'r', encoding='utf-8') as f:
                        existing_data = json.load(f)
                except:
                    pass
            
            # Update with current passed tests
            existing_data['passed_tests'] = sorted(list(self.passed_tests))
            existing_data['last_updated'] = datetime.now().isoformat()
            
            # Save
            with open(session_path, 'w', encoding='utf-8') as f:
                json.dump(existing_data, f, indent=2)
            
            logger.debug(f"Saved {len(self.passed_tests)} passed tests to session file")
        except Exception as e:
            logger.warning(f"Failed to save session data to {self.session_file}: {e}")
import logging
import os
import subprocess
from pathlib import Path
from typing import Tuple

logger = logging.getLogger(__name__)

class TestRunner:
    """
    Executes tests locally using the repository's build tool (Gradle).
    """
    
    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path).resolve()
        self.gradle_wrapper = self.repo_path / "gradlew"
        
    def run_test(self, test_name: str) -> Tuple[bool, str, str]:
        """
        Run a specific test method.
        
        Args:
            test_name: Full test name (e.g., "com.example.TestClass.testMethod")
            
        Returns:
            Tuple containing:
            - success (bool): True if test passed, False otherwise
            - stdout (str): Standard output
            - stderr (str): Standard error
        """
        if not self.gradle_wrapper.exists():
            return False, "", "Gradle wrapper not found"

        filter_pattern = self._build_primary_filter(test_name)
        success, stdout, stderr, _ = self._run_gradle_command(filter_pattern)
        return success, stdout, stderr

    def extract_failure_log(self, stdout: str) -> str:
        """
        Extract relevant failure information from Gradle output.
        """
        # Simple extraction strategy: capture stack-ish lines and failure markers
        lines = stdout.splitlines()
        relevant_lines = []
        capture = False
        
        for line in lines:
            if "FAILED" in line or "Exception" in line or "Error" in line or line.strip().startswith("at "):
                capture = True
            
            if capture:
                relevant_lines.append(line)
                
        return "\n".join(relevant_lines[-500:]) if relevant_lines else stdout[-2000:]

    def _build_primary_filter(self, test_name: str) -> str:
        """
        Build the primary Gradle --tests pattern. We stick to the simple
        `ClassName.testMethod` style because that is what works locally.
        """
        test_name = (test_name or "").strip()
        if not test_name:
            return test_name

        if "." not in test_name:
            return test_name

        parts = test_name.split(".")
        method_name = parts[-1]
        class_name = parts[-2]

        if method_name:
            return f"{class_name}.{method_name}"
        return class_name

    def _run_gradle_command(self, test_filter: str) -> Tuple[bool, str, str, int]:
        """
        Execute gradlew with a single --tests filter and return raw results.
        """
        cmd = [
            str(self.gradle_wrapper),
            "test",
            "--tests",
            test_filter,
            "--info",
        ]

        logger.info("Running test: %s", " ".join(cmd))

        try:
            os.chmod(self.gradle_wrapper, 0o755)

            process = subprocess.run(
                cmd,
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=600,
            )

            success = process.returncode == 0
            if not success:
                logger.warning("Test execution failed with return code %s", process.returncode)
                logger.warning("Stdout length: %s", len(process.stdout))
                logger.warning("Stderr length: %s", len(process.stderr))
                if process.stderr:
                    logger.warning("Full Stderr: %s", process.stderr)
                if len(process.stdout) < 1000:
                    logger.warning("Full Stdout (short): %s", process.stdout)

            return success, process.stdout, process.stderr, process.returncode

        except subprocess.TimeoutExpired:
            return False, "", "Test execution timed out", 124
        except Exception as exc:
            return False, "", f"Failed to execute test: {exc}", 1


"""
AI-powered test failure analyzer.
Supports both Ollama (local) and OpenAI (cloud) providers.
"""

import os
import json
import logging
from typing import List, Dict, Optional
from ..parsers.models import TestResult
from ..settings import Config

logger = logging.getLogger(__name__)


class FailureClassification:
    """Result of AI failure classification"""
    
    def __init__(self, test_name: str, classification: str, confidence: str, 
                 root_cause: str, recommended_action: str, root_cause_category: str = "OTHER",
                 failure_signature: str = None):
        self.test_name = test_name
        self.classification = classification  # PRODUCT_BUG or AUTOMATION_ISSUE
        self.confidence = confidence  # HIGH, MEDIUM, LOW
        self.root_cause = root_cause
        self.recommended_action = recommended_action
        self.root_cause_category = root_cause_category  # ELEMENT_NOT_FOUND, TIMEOUT, etc.
        self.failure_signature = failure_signature or "Unspecified Failure"
    
    def is_product_bug(self) -> bool:
        """Check if classified as product bug"""
        if self.root_cause_category == "CODE_ISSUE":
            return False
        return self.classification == "PRODUCT_BUG"
    
    def is_automation_issue(self) -> bool:
        """Check if classified as automation issue"""
        if self.root_cause_category == "CODE_ISSUE":
            return True
        return self.classification == "AUTOMATION_ISSUE"
    
    def __repr__(self) -> str:
        if self.is_product_bug():
            icon = "🐛"
        else:
            icon = "🔧"
        return f"{icon} {self.test_name}: {self.classification} ({self.confidence})"


class TestAnalyzer:
    """AI-powered test failure analyzer supporting Ollama and OpenAI"""
    
    
    def __init__(self):
        """Initialize the analyzer with configured LLM provider"""
        self.llm_provider = Config.LLM_PROVIDER
        
        logger.info(f"Initializing TestAnalyzer with provider: {self.llm_provider}")
        
        if self.llm_provider == 'openai':
            self._init_openai()
        elif self.llm_provider == 'gemini':
            self._init_gemini()
        else:
            self._init_ollama()
    
    def _init_ollama(self):
        """Initialize Ollama LLM"""
        try:
            from langchain_ollama import OllamaLLM
            
            self.model = Config.OLLAMA_MODEL
            self.base_url = Config.OLLAMA_BASE_URL
            
            self.llm = OllamaLLM(
                model=self.model,
                base_url=self.base_url,
                temperature=0.3
            )
            logger.info(f"✅ Ollama LLM initialized: {self.model}")
        except Exception as e:
            logger.error(f"Failed to initialize Ollama: {e}")
            raise
    
    def _init_openai(self):
        """Initialize OpenAI LLM"""
        try:
            from langchain_openai import ChatOpenAI
            
            api_key = Config.OPENAI_API_KEY
            if not api_key:
                raise ValueError("OPENAI_API_KEY not found in environment variables")
            
            self.model = Config.OPENAI_MODEL
            
            self.llm = ChatOpenAI(
                model=self.model,
                api_key=api_key,
                temperature=0.3
            )
            logger.info(f"✅ OpenAI LLM initialized: {self.model}")
        except Exception as e:
            logger.error(f"Failed to initialize OpenAI: {e}")
            raise

    def _init_gemini(self):
        """Initialize Google Gemini LLM"""
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
        except ImportError:
            logger.error("Gemini provider selected but langchain-google-genai is not installed.")
            raise
        try:
            api_key = Config.GEMINI_API_KEY
            if not api_key:
                raise ValueError("GEMINI_API_KEY not found in environment variables")

            self.model = Config.GEMINI_MODEL

            self.llm = ChatGoogleGenerativeAI(
                model=self.model,
                google_api_key=api_key,
                temperature=0.3
            )
            logger.info(f"✅ Gemini LLM initialized: {self.model}")
        except Exception as e:
            logger.error(f"Failed to initialize Gemini: {e}")
            raise

    def classify_failure(self, test_result: TestResult) -> FailureClassification:
        """
        Classify a single test failure using AI.
        
        Args:
            test_result: TestResult object with failure details
            
        Returns:
            FailureClassification object
        """
        if not test_result.is_failure:
            raise ValueError("Cannot classify a passing test")
        
        # Build the prompt
        prompt = self._build_classification_prompt(test_result)
        
        logger.debug(f"Classifying failure: {test_result.full_name}")
        
        try:
            # Get AI response
            response = self.llm.invoke(prompt)
            
            # Convert AIMessage to string if needed (OpenAI returns AIMessage object)
            if hasattr(response, 'content'):
                response = response.content
            
            # Parse the response
            # CRITICAL: Use full_name property which already removes duplicates automatically
            # No need to call _remove_duplicate_class_name since full_name property handles it
            cleaned_full_name = test_result.full_name  # Property already removes duplicates
            classification = self._parse_classification_response(response, cleaned_full_name)
            
            logger.debug(f"✅ Classified {cleaned_full_name} as {classification.classification}")
            return classification
            
        except Exception as e:
            logger.error(f"Failed to classify {test_result.full_name}: {e}")
            # Return a fallback classification
            # CRITICAL: Use full_name property which already removes duplicates automatically
            cleaned_full_name = test_result.full_name  # Property already removes duplicates
            return FailureClassification(
                test_name=cleaned_full_name,
                classification="UNKNOWN",
                confidence="LOW",
                root_cause=f"AI Classification Error: {str(e)}\nOriginal Error: {test_result.error_message[:500] if test_result.error_message else 'No error message'}",
                recommended_action="Manual review required",
                root_cause_category="OTHER"
            )
    
    def _build_classification_prompt(self, test_result: TestResult) -> str:
        """Build the classification prompt for AI"""
        
        # Use execution log if available (from HTML parser), otherwise use stack trace
        # The execution_log should already be isolated to this specific test case
        execution_context = test_result.execution_log if test_result.execution_log else test_result.stack_trace
        
        # Truncate execution log if too long for LLM context limit
        # Keep last 50000 chars (approximately 12500 tokens) to stay within 128K token limit
        # Full logs are still available in test_result.execution_log for API extraction
        if execution_context and len(execution_context) > 50000:
            execution_context = '...' + execution_context[-50000:]
        
        prompt = f"""You are an expert QA engineer analyzing test automation failures.

Classify this test failure as PRODUCT_BUG or AUTOMATION_ISSUE.

PRODUCT_BUG indicators:
- Assertion failures on business logic (ALWAYS classify as PRODUCT_BUG with root_cause_category ASSERTION_FAILURE):
  * Expected vs Actual mismatches (e.g., "Expected 'Frozen state' was :-'true'. But actual is 'false'")
  * Missing keys in API responses (e.g., "Missing Key: debitAccountUuid")
  * Class type mismatches (e.g., "Classes of actual and expected key 'remark' are different")
  * Null values where non-null expected (e.g., "Key/Value is null while putting run time property")
  * OTP-related failures (ALWAYS PRODUCT_BUG with root_cause_category ASSERTION_FAILURE): "matching OTP not found", "Matching OTP Not Found", OTP not received, OTP delivery/validation failure—these are application or test-flow issues, not environment/infrastructure.
- Unexpected application behavior
- API returning wrong data or status codes
- UI displaying incorrect values
- Functional defects in the application

AUTOMATION_ISSUE indicators:
- ElementClickInterceptedException (element not clickable - ALWAYS classify as AUTOMATION_ISSUE with root_cause_category ELEMENT_NOT_FOUND)
  Example: "element click intercepted: Element <button data-cy='login-step-start-submit-button'> is not clickable"
- NoSuchElementException (element locator not found)
- StaleElementReferenceException (DOM changed)
- NullPointerException related to WebElement operations (getText(), getPageElement returning null)
- IndexOutOfBoundsException when accessing empty element lists (e.g., "Index 0 out of bounds for length 0")
- IllegalArgumentException related to element operations (e.g., "IllegalArgumentException: bound must be positive" - ALWAYS classify as AUTOMATION_ISSUE with root_cause_category ELEMENT_NOT_FOUND)
- Page load timeout (e.g., "'DashReviewPage' NOT loaded even after :- 40.071 seconds" or "'TransferPage' NOT loaded even after :- 40.395 seconds" - ALWAYS classify as AUTOMATION_ISSUE with root_cause_category TIMEOUT)
- TimeoutException (element didn't appear in time)
- WebDriver connection/session issues
- Test data setup/cleanup failures
- Synchronization problems
- Code-level logic issues or NullPointerExceptions (e.g., Cannot invoke "String.split(String)" because "otp" is null - ALWAYS classify as AUTOMATION_ISSUE with root_cause_category CODE_ISSUE)

Test Details:
- Test: {test_result.full_name}
- Platform: {test_result.platform}
- Error Type: {test_result.error_type}
- Error Message: {test_result.error_message}
- Execution Log (isolated for this test case only, from start to end): {execution_context or 'N/A'}

        Respond in JSON format:
        {{
          "classification": "PRODUCT_BUG" or "AUTOMATION_ISSUE",
          "confidence": "HIGH" or "MEDIUM" or "LOW",
          "root_cause": "Detailed explanation of the failure. Must include: API Name, Status Code, Missing Key, Locator Name, or Exception Message.",
          "failure_signature": "Concise (3-6 words) grouping key. Format: [Type]: [Short Details]. NO IDs/Timestamps/Narratives. Examples: 'API Error: POST /login 500', 'Element Missing: #submit-btn', 'Assertion: Status Code 404', 'Timeout: TransferPage load'",
          "root_cause_category": "ELEMENT_NOT_FOUND" or "TIMEOUT" or "ASSERTION_FAILURE" or "ENVIRONMENT_ISSUE" or "CODE_ISSUE" or "OTHER",
          "recommended_action": "Specific next step to take"
        }}

        IMPORTANT RULES FOR ROOT CAUSE:
        1. NO NARRATIVES: Do NOT start with "The test...", "This test...", "Verified that...", "It appears that...".
        2. NO VALIDATION DESCRIPTIONS: Do NOT explain what the test does. Only state what went wrong.
        3. BE CONCISE: Use less than 15 words if possible.
        4. SPECIFICITY: Include the exact key name, status code, locator, or exception.
        5. BAD EXAMPLES (DO NOT USE):
           - "The test validates that after approving a pending transfer, it should not be present..." (Too verbose)
           - "The test failed because the API returned an error" (Too generic)
           - "Element was not found on the page" (Missing locator)
        6. GOOD EXAMPLES:
           - "POST /debit/transfer returned 500 Internal Server Error"
           - "Missing Key: 'transferId' in response"
           - "Locator: 'button.submit-transfer' not found"
           - "Value mismatch: Expected 'APPROVED' but got 'PENDING'"

Root Cause Category Guidelines (with examples):

IMPORTANT: Prefer a specific category (ELEMENT_NOT_FOUND, TIMEOUT, ASSERTION_FAILURE, ENVIRONMENT_ISSUE, CODE_ISSUE) whenever the failure message or stack trace has a matching pattern. Use OTHER only when the failure clearly does not fit any of the categories.

ELEMENT_NOT_FOUND - Use this for element locator and WebElement access issues:
- ElementClickInterceptedException (e.g., "element click intercepted: Element <button> is not clickable")
- StaleElementReferenceException (DOM element became stale)
- NoSuchElementException (element locator not found)
- NullPointerException related to WebElement operations:
  * "Cannot invoke \"org.openqa.selenium.WebElement.getText()\" because the return value of \"Automation.Utils.Element.getPageElement(...)\" is null"
  * NullPointerException when calling getText(), getPageElement, or similar WebElement methods
- IndexOutOfBoundsException when accessing empty element lists:
  * "Index 0 out of bounds for length 0"
  * Accessing element lists that are empty
- IllegalArgumentException related to element operations:
  * "IllegalArgumentException: bound must be positive"
  * IllegalArgumentException when working with element lists or locators

TIMEOUT - Use this ONLY for page load timeouts (not general timeouts):
- Page load timeout patterns:
  * "'TransferPage' NOT loaded even after :- 40.395 seconds"
  * "'DashHomePage' NOT loaded even after :- 40.849 seconds"
  * "'PageName' NOT loaded even after :- X seconds" (where X is a number)
- Do NOT use TIMEOUT for: Element visibility timeouts, general TimeoutException, or element not appearing in time

ASSERTION_FAILURE - Use this for actual assertion/validation failures:
- Expected vs Actual mismatches:
  * "Expected 'Frozen state' was :-'true'. But actual is 'false'"
  * "Expected 'action' was :-'unfrozen'. But actual is 'frozen'"
  * "Expected 'Business State' was :-'BUNE'. But actual is 'BUPE'"
- Class type mismatches:
  * "Classes of actual and expected key 'remark' are different. Expected is: 'class java.lang.String' but Actual is: 'class org.json.JSONObject$Null'"
  * "Classes of actual and expected key 'risk_level' are different. Expected is: 'class java.lang.Integer' but Actual is: 'class org.json.JSONObject$Null'"
- Missing keys/fields:
  * "Missing Key: debitAccountUuid"
  * "Actual JSON doesn't contain all expected keys. Expected has: '[key1, key2]' but Actual has: '[key1]'"
- Null value assertions:
  * "Key/Value is null while putting run time property. Key: debitAccountUuid Value: null"
- Assertion failure messages:
  * "The following asserts failed: ..."
  * Patterns containing both "expected" and "actual" keywords together

ENVIRONMENT_ISSUE - Use this for environment, infrastructure, and network-related issues:
- Database connection failures
- Service unavailable (503 errors)
- Infrastructure issues
- Connection refused errors
- DNS errors
- Network timeout
- 502 Bad Gateway
- Any environment or network-related failures
- Do NOT use ENVIRONMENT_ISSUE for OTP-related failures. OTP issues (e.g. "matching OTP not found", OTP not received, OTP delivery/validation failure) are PRODUCT_BUG with root_cause_category ASSERTION_FAILURE—they indicate application/test-flow behavior (OTP generation, delivery, or validation), not infrastructure.
- Do NOT use ENVIRONMENT_ISSUE for Java NullPointerExceptions that look like logic errors (e.g., Cannot invoke "String.split(String)" because "otp" is null). Use CODE_ISSUE instead.

CODE_ISSUE - Use this for Java logic errors and NullPointerExceptions that are not related to Selenium elements:
- General NullPointerExceptions (e.g., "Cannot invoke \"String.split(String)\" because \"otp\" is null")
- NullPointerExceptions on business objects or data (e.g., "Cannot invoke \"java.lang.Integer.intValue()\" because \"user.id\" is null")
- Any NullPointerException that is clearly a coding or logic flaw in the automation or application code, but not a simple "element not found".
- ALWAYS classify as AUTOMATION_ISSUE.

OTHER: Use ONLY when the failure clearly does not match any of the categories above (e.g., compilation error, unknown error). If you see exception names (NoSuchElementException, TimeoutException, etc.), expected/actual text, or HTTP/connection errors, choose the matching specific category instead of OTHER.

Confidence Guidelines:
- HIGH: Clear, unambiguous indicators (e.g., NoSuchElementException, API 500 error, missing required keys, clear assertion mismatch with specific values)
- MEDIUM: Mostly clear but some ambiguity (e.g., timeout could be product slowness or automation sync issue, generic errors with partial context)
- LOW: Ambiguous or insufficient information (e.g., generic errors without clear context, missing execution logs, unclear failure pattern)

Respond ONLY with valid JSON, no additional text."""

        return prompt
    
    def _parse_classification_response(self, response: str, test_name: str) -> FailureClassification:
        """Parse AI response into FailureClassification"""
        
        try:
            # Try to extract JSON from response
            response = response.strip()
            
            # Sometimes LLM adds markdown code blocks
            if response.startswith('```'):
                # Extract JSON from code block
                lines = response.split('\n')
                json_lines = []
                in_json = False
                for line in lines:
                    if line.strip().startswith('```'):
                        if in_json:
                            break
                        in_json = True
                        continue
                    if in_json:
                        json_lines.append(line)
                response = '\n'.join(json_lines)
            
            # Parse JSON
            data = json.loads(response)
            
            return FailureClassification(
                test_name=test_name,
                classification=data.get('classification', 'UNKNOWN'),
                confidence=data.get('confidence', 'LOW'),
                root_cause=data.get('root_cause', 'No explanation provided'),
                recommended_action=data.get('recommended_action', 'Manual review required'),
                root_cause_category=data.get('root_cause_category', 'OTHER'),
                failure_signature=data.get('failure_signature', 'Unspecified Failure')
            )
            
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse JSON response: {e}")
            logger.debug(f"Response was: {response}")
            
            # Fallback: try to extract classification from text
            response_upper = response.upper()
            if 'PRODUCT_BUG' in response_upper or 'PRODUCT BUG' in response_upper:
                classification = 'PRODUCT_BUG'
            elif 'AUTOMATION_ISSUE' in response_upper or 'AUTOMATION ISSUE' in response_upper:
                classification = 'AUTOMATION_ISSUE'
            else:
                classification = 'UNKNOWN'
            
            return FailureClassification(
                test_name=test_name,
                classification=classification,
                confidence='LOW',
                root_cause=response[:200] if response else 'No explanation',
                recommended_action='Manual review required',
                root_cause_category='OTHER',
                failure_signature='Parse Error'
            )
    
    def classify_multiple_failures(self, test_results: List[TestResult]) -> List[FailureClassification]:
        """
        Classify multiple test failures.
        
        Args:
            test_results: List of TestResult objects
            
        Returns:
            List of FailureClassification objects
        """
        failures = [r for r in test_results if r.is_failure]
        
        if not failures:
            logger.info("No failures to classify")
            return []
        
        logger.info(f"🤖 Starting AI Analysis on {len(failures)} failures...")
        logger.info("=" * 70)
        
        classifications = []
        for i, failure in enumerate(failures, 1):
            # CRITICAL: Use full_name property which automatically removes duplicates
            # full_name is a property that constructs name from class_name.method_name
            # and the property now removes duplicates automatically
            cleaned_full_name = failure.full_name  # This property now removes duplicates
            # Extract class and method name for display
            parts = cleaned_full_name.split('.')
            if len(parts) >= 2:
                class_name = parts[-2]  # Second to last is class name
                method_name = parts[-1]  # Last is method name
            else:
                class_name = cleaned_full_name
                method_name = ""
            method_name = method_name[:50] + "..." if len(method_name) > 50 else method_name
            
            logger.info(f"[{i}/{len(failures)}] Analyzing: {class_name}.{method_name}")
            logger.info(f"         Platform: {failure.platform or 'UNKNOWN'} | Error: {failure.error_type or 'Unknown'}")
            
            try:
                classification = self.classify_failure(failure)
                if classification.is_product_bug():
                    icon = "🐛"
                else:
                    icon = "🔧"
                logger.info(f"         {icon} Classified as: {classification.classification} ({classification.confidence} confidence)")
                classifications.append(classification)
            except Exception as e:
                logger.error(f"         ❌ Failed to classify: {e}")
                # Add fallback classification
                classifications.append(FailureClassification(
                    test_name=failure.full_name,
                    classification="UNKNOWN",
                    confidence="LOW",
                    root_cause=f"AI Classification Error: {str(e)}\nOriginal Error: {failure.error_message[:500] if failure.error_message else 'No error message'}",
                    recommended_action="Manual review required",
                    root_cause_category="OTHER",
                    failure_signature="Classification Failed"
                ))
            
            logger.info("")  # Empty line for readability
        
        logger.info("=" * 70)
        logger.info(f"✅ Completed AI Analysis: {len(classifications)} failures classified")
        logger.info(f"   🐛 Potential Bugs: {sum(1 for c in classifications if c.is_product_bug())}")
        logger.info(f"   🔧 Automation Issues: {sum(1 for c in classifications if c.is_automation_issue())}")
        
        return classifications

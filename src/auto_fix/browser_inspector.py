"""
Browser-based locator discovery for self-healing locator issues.
Uses Selenium to inspect the browser DOM and discover new locators when elements are not found.
"""

import logging
import re
from typing import List, Dict, Optional, Tuple
from pathlib import Path

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    from selenium.common.exceptions import TimeoutException, WebDriverException
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

logger = logging.getLogger(__name__)


class LocatorCandidate:
    """Represents a discovered locator candidate"""
    
    def __init__(self, locator_type: str, locator_value: str, confidence: str, element_text: str = ""):
        self.locator_type = locator_type  # 'css', 'xpath', 'id', 'name', 'data-cy', etc.
        self.locator_value = locator_value
        self.confidence = confidence  # 'HIGH', 'MEDIUM', 'LOW'
        self.element_text = element_text
    
    def to_selenium_by(self) -> Tuple[By, str]:
        """Convert to Selenium By tuple"""
        by_map = {
            'id': By.ID,
            'name': By.NAME,
            'class': By.CLASS_NAME,
            'tag': By.TAG_NAME,
            'css': By.CSS_SELECTOR,
            'xpath': By.XPATH,
            'link_text': By.LINK_TEXT,
            'partial_link_text': By.PARTIAL_LINK_TEXT,
        }
        by = by_map.get(self.locator_type, By.CSS_SELECTOR)
        return by, self.locator_value
    
    def __repr__(self):
        return f"LocatorCandidate(type={self.locator_type}, value={self.locator_value[:50]}..., confidence={self.confidence})"


class BrowserInspector:
    """
    Inspects browser DOM to discover new locators when elements are not found.
    """
    
    def __init__(self, headless: bool = True, timeout: int = 10):
        """
        Initialize browser inspector.
        
        Args:
            headless: Run browser in headless mode
            timeout: Timeout for element discovery operations
        """
        if not SELENIUM_AVAILABLE:
            raise ImportError("selenium not installed. Run: pip install selenium")
        
        self.headless = headless
        self.timeout = timeout
        self.driver: Optional[webdriver.Chrome] = None
    
    def __enter__(self):
        """Context manager entry"""
        self.start_browser()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.close_browser()
    
    def start_browser(self):
        """Start a Chrome browser session"""
        try:
            chrome_options = Options()
            if self.headless:
                chrome_options.add_argument('--headless')
            chrome_options.add_argument('--no-sandbox')
            chrome_options.add_argument('--disable-dev-shm-usage')
            chrome_options.add_argument('--disable-gpu')
            chrome_options.add_argument('--window-size=1920,1080')
            
            self.driver = webdriver.Chrome(options=chrome_options)
            self.driver.implicitly_wait(self.timeout)
            logger.info("Browser started successfully")
        except Exception as e:
            logger.error(f"Failed to start browser: {e}")
            raise
    
    def close_browser(self):
        """Close the browser session"""
        if self.driver:
            try:
                self.driver.quit()
                logger.info("Browser closed")
            except Exception as e:
                logger.warning(f"Error closing browser: {e}")
            finally:
                self.driver = None
    
    def extract_page_url(self, execution_log: str, root_cause: str = "") -> Optional[str]:
        """
        Extract page URL from execution logs or root cause.
        
        Args:
            execution_log: Full execution log text
            root_cause: Root cause text
            
        Returns:
            Page URL or None if not found
        """
        combined_text = f"{execution_log}\n{root_cause}"
        
        # Pattern 1: "Page URL:- https://..."
        pattern1 = r'Page URL[:\s-]+([^\s\n]+)'
        match = re.search(pattern1, combined_text, re.IGNORECASE)
        if match:
            url = match.group(1).strip()
            if url.startswith('http'):
                return url
        
        # Pattern 2: Direct URL in text
        pattern2 = r'(https?://[^\s\n]+)'
        matches = re.findall(pattern2, combined_text)
        for url in matches:
            # Prefer URLs that look like application URLs (not API endpoints)
            if any(domain in url for domain in ['app.', 'dashboard.', 'qa-', 'staging']):
                return url.strip()
        
        # Return first HTTP URL if found
        if matches:
            return matches[0].strip()
        
        return None
    
    def discover_element_locators(
        self,
        page_url: str,
        element_name: str,
        element_text_hint: str = ""
    ) -> List[LocatorCandidate]:
        """
        Discover locator candidates for a missing element.
        
        Args:
            page_url: URL of the page where element should be
            element_name: Name of the element (e.g., "Block Reason PopUp Header")
            element_text_hint: Expected text content of the element
            
        Returns:
            List of LocatorCandidate objects, sorted by confidence
        """
        # Browser should be started via context manager, but start if not
        if not self.driver:
            try:
                self.start_browser()
            except Exception as e:
                logger.error(f"Failed to start browser: {e}")
                return []
        
        candidates = []
        
        try:
            logger.info(f"Navigating to: {page_url}")
            self.driver.get(page_url)
            
            # Wait for page to load
            WebDriverWait(self.driver, self.timeout).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            
            # Try multiple discovery strategies
            candidates.extend(self._discover_by_text(element_name, element_text_hint))
            candidates.extend(self._discover_by_attributes(element_name))
            candidates.extend(self._discover_by_data_attributes(element_name))
            candidates.extend(self._discover_by_role_and_aria(element_name))
            
            # Sort by confidence (HIGH > MEDIUM > LOW)
            confidence_order = {'HIGH': 3, 'MEDIUM': 2, 'LOW': 1}
            candidates.sort(key=lambda x: confidence_order.get(x.confidence, 0), reverse=True)
            
            logger.info(f"Discovered {len(candidates)} locator candidates for '{element_name}'")
            
        except TimeoutException:
            logger.warning(f"Timeout while loading page: {page_url}")
        except WebDriverException as e:
            logger.error(f"Browser error while discovering locators: {e}")
        except Exception as e:
            logger.error(f"Unexpected error during locator discovery: {e}")
        
        return candidates[:10]  # Return top 10 candidates
    
    def _discover_by_text(self, element_name: str, text_hint: str = "") -> List[LocatorCandidate]:
        """Discover elements by matching text content"""
        candidates = []
        
        try:
            # Normalize element name for matching
            search_terms = [element_name]
            if text_hint:
                search_terms.append(text_hint)
            
            # Extract key words from element name (remove common words)
            words = re.findall(r'\b[A-Z][a-z]+\b', element_name)
            if words:
                search_terms.extend(words)
            
            for term in search_terms:
                if len(term) < 3:
                    continue
                
                # Try exact text match
                try:
                    elements = self.driver.find_elements(By.XPATH, f"//*[text()='{term}']")
                    for elem in elements[:3]:  # Limit to first 3 matches
                        if elem.is_displayed():
                            locator = self._generate_locator_for_element(elem)
                            if locator:
                                candidates.append(LocatorCandidate(
                                    locator_type=locator[0],
                                    locator_value=locator[1],
                                    confidence='HIGH',
                                    element_text=term
                                ))
                except:
                    pass
                
                # Try partial text match
                try:
                    elements = self.driver.find_elements(By.XPATH, f"//*[contains(text(), '{term}')]")
                    for elem in elements[:3]:
                        if elem.is_displayed():
                            locator = self._generate_locator_for_element(elem)
                            if locator:
                                candidates.append(LocatorCandidate(
                                    locator_type=locator[0],
                                    locator_value=locator[1],
                                    confidence='MEDIUM',
                                    element_text=term
                                ))
                except:
                    pass
        
        except Exception as e:
            logger.debug(f"Error in _discover_by_text: {e}")
        
        return candidates
    
    def _discover_by_attributes(self, element_name: str) -> List[LocatorCandidate]:
        """Discover elements by ID, name, class attributes"""
        candidates = []
        
        try:
            # Try to find elements with ID/name matching element name (normalized)
            normalized = element_name.lower().replace(' ', '').replace('-', '').replace('_', '')
            
            # Try ID
            try:
                elem = self.driver.find_element(By.ID, normalized)
                if elem.is_displayed():
                    candidates.append(LocatorCandidate(
                        locator_type='id',
                        locator_value=normalized,
                        confidence='HIGH',
                        element_text=element_name
                    ))
            except:
                pass
            
            # Try name attribute
            try:
                elem = self.driver.find_element(By.NAME, normalized)
                if elem.is_displayed():
                    candidates.append(LocatorCandidate(
                        locator_type='name',
                        locator_value=normalized,
                        confidence='MEDIUM',
                        element_text=element_name
                    ))
            except:
                pass
        
        except Exception as e:
            logger.debug(f"Error in _discover_by_attributes: {e}")
        
        return candidates
    
    def _discover_by_data_attributes(self, element_name: str) -> List[LocatorCandidate]:
        """Discover elements by data-cy, data-testid, etc."""
        candidates = []
        
        try:
            normalized = element_name.lower().replace(' ', '-').replace('_', '-')
            
            # Try data-cy
            try:
                elem = self.driver.find_element(By.CSS_SELECTOR, f"[data-cy='{normalized}']")
                if elem.is_displayed():
                    candidates.append(LocatorCandidate(
                        locator_type='css',
                        locator_value=f"[data-cy='{normalized}']",
                        confidence='HIGH',
                        element_text=element_name
                    ))
            except:
                pass
            
            # Try data-testid
            try:
                elem = self.driver.find_element(By.CSS_SELECTOR, f"[data-testid='{normalized}']")
                if elem.is_displayed():
                    candidates.append(LocatorCandidate(
                        locator_type='css',
                        locator_value=f"[data-testid='{normalized}']",
                        confidence='HIGH',
                        element_text=element_name
                    ))
            except:
                pass
        
        except Exception as e:
            logger.debug(f"Error in _discover_by_data_attributes: {e}")
        
        return candidates
    
    def _discover_by_role_and_aria(self, element_name: str) -> List[LocatorCandidate]:
        """Discover elements by ARIA roles and labels"""
        candidates = []
        
        try:
            # Try aria-label
            try:
                elements = self.driver.find_elements(By.CSS_SELECTOR, f"[aria-label*='{element_name}']")
                for elem in elements[:2]:
                    if elem.is_displayed():
                        aria_label = elem.get_attribute('aria-label')
                        candidates.append(LocatorCandidate(
                            locator_type='css',
                            locator_value=f"[aria-label='{aria_label}']",
                            confidence='MEDIUM',
                            element_text=element_name
                        ))
            except:
                pass
        
        except Exception as e:
            logger.debug(f"Error in _discover_by_role_and_aria: {e}")
        
        return candidates
    
    def _generate_locator_for_element(self, element) -> Optional[Tuple[str, str]]:
        """
        Generate the best locator for a given element.
        Priority: ID > data-cy > data-testid > CSS selector > XPath
        """
        try:
            # Try ID first
            elem_id = element.get_attribute('id')
            if elem_id:
                return ('id', elem_id)
            
            # Try data-cy
            data_cy = element.get_attribute('data-cy')
            if data_cy:
                return ('css', f"[data-cy='{data_cy}']")
            
            # Try data-testid
            data_testid = element.get_attribute('data-testid')
            if data_testid:
                return ('css', f"[data-testid='{data_testid}']")
            
            # Try name
            name = element.get_attribute('name')
            if name:
                return ('name', name)
            
            # Generate CSS selector
            tag = element.tag_name
            classes = element.get_attribute('class')
            if classes:
                class_list = classes.split()
                if class_list:
                    # Use first class
                    return ('css', f"{tag}.{class_list[0]}")
            
            # Fallback to XPath (less stable but works)
            xpath = self._generate_xpath(element)
            if xpath:
                return ('xpath', xpath)
        
        except Exception as e:
            logger.debug(f"Error generating locator: {e}")
        
        return None
    
    def _generate_xpath(self, element) -> Optional[str]:
        """Generate a simple XPath for an element"""
        try:
            # Simple XPath based on tag and text
            tag = element.tag_name
            text = element.text.strip()
            if text:
                # Escape quotes in text
                text_escaped = text.replace("'", "\\'")
                return f"//{tag}[text()='{text_escaped}']"
            
            # Fallback to tag only
            return f"//{tag}"
        except:
            return None

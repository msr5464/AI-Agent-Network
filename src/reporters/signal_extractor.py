import re
import logging
from typing import List, Dict, Optional
from ..agent.analyzer import FailureClassification
from ..utils import TestDataCache

logger = logging.getLogger(__name__)

class FailureSignalExtractor:
    """Extracts concise signals from test failures using AI-generated signatures."""

    def __init__(self, test_data_cache: TestDataCache):
        self.cache = test_data_cache

    def get_signals(self, category: str, failures: List[FailureClassification], limit: int = 5) -> str:
        """Get summarized signals for a list of failures in a category."""
        counts = {}
        
        for failure in failures:
            # PURE AI APPROACH: Use the signature directly from the AI analysis
            # The AI has already done the heavy lifting of understanding the error
            if hasattr(failure, 'failure_signature') and failure.failure_signature:
                signal = failure.failure_signature
            else:
                # Backwards compatibility / Fallback
                signal = "Legacy/Unprocessed Signal"

            # Use normalize_failure_signature for grouping
            from ..utils import normalize_failure_signature
            
            # 1. Normalize for grouping (matches Quick Wins logic)
            group_key = normalize_failure_signature(signal)
            
            # 2. Store original signal for display (first one wins, or most common later)
            if group_key not in counts:
                 # Minimal display cleanup for the representative string
                 display_signal = signal.strip().strip('.')
                 if display_signal:
                     display_signal = display_signal[0].upper() + display_signal[1:]
                 if len(display_signal) > 50:
                      display_signal = display_signal[:47] + "..."
                 
                 counts[group_key] = {
                     'count': 0,
                     'display': display_signal
                 }
            
            counts[group_key]['count'] += 1
            
        # Sort and format
        # Sort by count descending
        sorted_signals = sorted(counts.items(), key=lambda x: x[1]['count'], reverse=True)
        top_signals = sorted_signals[:limit]
        
        result = ", ".join([f"{data['display']} ({data['count']})" for key, data in top_signals])
        if len(sorted_signals) > limit:
            result += f", and {len(sorted_signals) - limit} other pattern(s)"
            
        return result

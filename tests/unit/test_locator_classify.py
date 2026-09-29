"""Locate's verdict when the text around a missing element is missing too.

The run this pins: Locate called Naukri's login button FEATURE_REMOVED ("33% of
neighbouring text survives") while the failure capture of the same page still
showed all three neighbours. Locate never searched, and the fix fell to the model.
"""
from shared import locator_classify as lc

CFG = {"classify": {"neighbour_survival_min": 0.5}}
BASELINE = {"element": {"neighbor_texts": ["Use OTP to Login", "Forgot Password?", "Or"]},
            "context": {}}
LOGIN_FORM = [{"text": t, "is_visible": True, "attrs": {}}
              for t in ("Login", "Use OTP to Login", "Forgot Password?", "Or")]
NO_FORM = {"url": "https://example.test/nlogin/login", "title": "Login",
           "elements": [{"text": "Forgot", "is_visible": True, "attrs": {}}]}


def test_a_short_neighbour_must_match_a_whole_word():
    """As a substring, 'Or' was found inside 'Forgot' on any page."""
    assert lc._neighbour_survival(["Or"], NO_FORM) == (0.0, ["Or"])


def test_context_still_in_the_failure_capture_is_not_a_removed_feature():
    verdict = lc.classify(NO_FORM, BASELINE, 0, None, CFG, failure_elements=LOGIN_FORM)
    assert verdict.kind == "WRONG_STATE"
    assert "failure capture still has it" in verdict.reason
    assert "nlogin/login" in verdict.reason          # what Locate actually examined


def test_context_gone_from_both_is_a_removed_feature():
    verdict = lc.classify(NO_FORM, BASELINE, 0, None, CFG, failure_elements=[])
    assert verdict.kind == "FEATURE_REMOVED"
    assert "'Use OTP to Login'" in verdict.reason    # names what went missing

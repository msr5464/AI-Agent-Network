"""One switch for whether the browsers this network starts are visible.

`PLAYWRIGHT_HEADLESS` was read by whichever step happened to want it — the
authoring agent's web validation and Maven run, the healing agent's DOM
inspection, the adaptation explorer — each with its own copy of
`os.environ.get(...).lower() != "false"`. Every other browser ignored it. So
`PLAYWRIGHT_HEADLESS=false` opened a window for one step of one agent while the
reproduce run, the verification runs, the confirmation probes, the locate step's
live replay and the session mint all stayed invisible, which is precisely when
someone is watching: you set it because you want to see what the browser sees.

Every browser now resolves its mode here, and there is deliberately no second
knob to resolve against it first — a per-step override reintroduces exactly the
"I set it false and one step stayed headless" surprise this module exists to
remove. Two ranks, no exceptions:

  1. `PLAYWRIGHT_HEADLESS`
  2. the caller's own default

Rank 2 is why `maven_properties()` returns nothing when the switch is unset,
rather than forcing `true`: a Maven run that is handed no `-Dheadless` follows
the automation framework's own `parameters/config.properties`, and silently
overriding that file is not what "unset" should mean.

Accepted spellings: true/false, 1/0, yes/no, on/off, in any case. Anything else
is treated as unset — a typo must not quietly flip the mode.
"""

from __future__ import annotations

import os
from typing import Dict, Optional

_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}

#: The switch every agent honours. Named for the library, not for one step.
ENV_VAR = "PLAYWRIGHT_HEADLESS"


def parse(raw: Optional[str]) -> Optional[bool]:
    """True / False for a recognised spelling, None for unset or unreadable."""
    value = (raw or "").strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return None


def configured() -> Optional[bool]:
    """The mode the environment asks for, or None if it asks for nothing.

    Callers that must distinguish "asked for headless" from "did not ask" — the
    Maven path, and the locate step's fall-back to the framework's own config —
    use this rather than `headless()`.
    """
    return parse(os.environ.get(ENV_VAR))


def headless(default: bool = True) -> bool:
    """Should a browser launched right now be headless?

    `default` applies only when nothing is configured. It stays True for direct
    Playwright and MCP launches: there is no framework config behind those to
    fall back to, and headless is the right default for an unattended run.
    """
    decided = configured()
    return default if decided is None else decided


def maven_properties() -> Dict[str, str]:
    """`{"headless": ...}` for a JVM runner, or `{}` when the switch is unset.

    The framework reads this key through `Config.getRunTimeProperty`, which
    checks system properties before `parameters/config.properties` — so passing
    it as `-Dheadless=` overrides the file for that run only, and passing
    nothing leaves the file in charge.
    """
    decided = configured()
    if decided is None:
        return {}
    return {"headless": "true" if decided else "false"}


def label(is_headless: bool) -> str:
    """How to say it in a log line, consistently across agents."""
    return "headless" if is_headless else "headed (browser window visible)"

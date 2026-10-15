"""How people received the agents' work, and whether lessons are helping.

The other half of Analytics. Analytics counts what the agents did (runs, cost,
outcomes); this counts what people then said about it, from the feedback store
(shared/feedback.py), over the same window, users and runs:

  * per agent, of the runs that reached a verdict: accepted as is, accepted after
    edits, rejected, or not reviewed yet;
  * per lesson (shared/lessons.py), the same for the runs it was given;
  * for test design, the tests it offered against the tests a person pushed, as
    written or edited;
  * explicit feedback by where it was given.

It never changes how a run counts: a run's status stays analytics.run_status's.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Optional

from qa_agents_server import analytics
from shared import feedback, lessons

# The runs a person can judge are the ones Analytics counts as Runs (analytics.JUDGED),
# so the two views always cover the same runs.


def _latest_verdicts(events) -> Dict[tuple, str]:
    found: Dict[tuple, str] = {}
    for e in events:
        if e.get("kind") == "verdict" and e.get("verdict") and e.get("session"):
            found[(e.get("agent"), e["session"])] = e["verdict"]
    return found


def _within(event: Dict, since: Optional[float], until: float, user: Optional[str]) -> bool:
    import calendar
    import time
    stamp = (event.get("recorded_at") or "")[:19]
    try:
        when = calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return False
    if (since is not None and when < since) or when > until:
        return False
    return not user or analytics.canonical_user_id(event.get("who") or "") == user


def summary(window: str = "7d", since: Optional[float] = None, until: Optional[float] = None,
            user_id: Optional[str] = None) -> Dict[str, Any]:
    rows, since, until = analytics.select_rows(window, None, since, until, user_id)
    user = analytics.canonical_user_id(user_id) if user_id else None
    events = feedback.read()
    verdicts = _latest_verdicts(events)

    by_agent: Dict[str, Dict[str, int]] = {}
    per_lesson: Dict[str, Counter] = {}
    for row in rows:
        if row.get("status") not in analytics.JUDGED:
            continue
        agent = row.get("agent") or "unknown"
        verdict = verdicts.get((agent, row.get("session_id")), "not_reviewed")
        counts = by_agent.setdefault(agent, Counter())
        counts["runs"] += 1
        counts[verdict] += 1
        for lesson_id in row.get("lessons") or []:
            tally = per_lesson.setdefault(lesson_id, Counter())
            tally["runs"] += 1
            tally[verdict] += 1

    statements: Dict[str, Dict[str, str]] = {}
    for agent in lessons.INJECTION_STEPS:
        usable, _refused = lessons.load(agent)
        if agent in by_agent or usable:
            by_agent.setdefault(agent, Counter())["active_lessons"] = len(usable)
        for lesson in lessons.read_file(agent):
            statements[str(lesson.get("id"))] = {"agent": agent, "statement": lesson.get("statement", ""),
                                                 "status": lesson.get("status", "")}

    windowed = [e for e in events if _within(e, since, until, user)]
    # Test design's lessons: the runs given each (its generation events) and what a
    # person pushed from those runs. A test offered and never pushed is neither.
    pushed_by_session: Dict[str, Counter] = {}
    for e in windowed:
        if e.get("kind") == "test_push":
            pushed_by_session.setdefault(e.get("session") or "", Counter())[e.get("verdict")] += 1
    for e in windowed:
        if e.get("kind") != "generation":
            continue
        pushed = pushed_by_session.get(e.get("session") or "", Counter())
        for lesson_id in (e.get("artifact") or {}).get("lessons") or []:
            tally = per_lesson.setdefault(lesson_id, Counter())
            tally["runs"] += 1
            tally["accept"] += pushed.get("accept", 0)
            tally["edited"] += pushed.get("edited", 0)
    offered = sum(len((e.get("artifact") or {}).get("tests") or [])
                  for e in windowed if e.get("kind") == "generation")
    pushes = Counter(e.get("verdict") for e in windowed if e.get("kind") == "test_push")
    if offered or pushes:
        design = by_agent.setdefault(feedback.DESIGN_AGENT, Counter())
        design.update({"tests_offered": offered, "pushed_as_is": pushes.get("accept", 0),
                       "pushed_after_edits": pushes.get("edited", 0)})
    sources = Counter(e.get("source") for e in windowed if e.get("explicit"))

    return {
        "window": {"from": since, "to": until, "label": window},
        "by_agent": {agent: dict(counts) for agent, counts in sorted(by_agent.items())},
        "lessons": [{"id": lesson_id, **statements.get(lesson_id, {}), **dict(tally)}
                    for lesson_id, tally in sorted(per_lesson.items())]
                   + [{"id": lesson_id, **info, "runs": 0}
                      for lesson_id, info in sorted(statements.items())
                      if lesson_id not in per_lesson and info.get("status") == "active"],
        "explicit_by_source": dict(sources),
    }

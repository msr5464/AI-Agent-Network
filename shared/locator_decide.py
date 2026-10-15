"""Ranking and the accept/reject decision.

Three numbers, not one. Published tools accept "closest wins above a threshold";
that is what produces wrong-element matches in dense UIs full of near-identical
rows. We additionally require the winner to be *clearly* ahead of the runner-up.
"""
from __future__ import annotations
from dataclasses import dataclass, field

from shared.locator_candidates import Candidate, TIERS
from shared.locator_score import Volatility, score as score_pair

AUTO, VERIFY, ESCALATE, NONE = "AUTO", "VERIFY", "ESCALATE", "NO_CANDIDATE"
# Tiers whose evidence is a unique match on a strong identity signal. These may
# skip the ambiguity margin: "the testid still exists and matches one element"
# is not made less true by a structurally similar runner-up.
IDENTITY_TIERS = {"T0_literal", "T1_identity"}


@dataclass
class Decision:
    outcome: str
    reason: str
    top: Candidate | None = None
    runners: list[Candidate] = field(default_factory=list)
    margin: float = 0.0

    @property
    def proceed(self) -> bool:
        return self.outcome in (AUTO, VERIFY)


def rank(cands: list[Candidate], baseline: dict, cfg: dict, vol: Volatility) -> list[Candidate]:
    for c in cands:
        c.score, c.breakdown = score_pair(baseline["element"], c.el, cfg, vol)
    return sorted(cands, key=lambda c: (-c.score, TIERS.index(c.best_tier)))


def decide(ranked: list[Candidate], cfg: dict) -> Decision:
    th = cfg["thresholds"]
    if not ranked:
        return Decision(NONE, "no candidate elements on the page")

    top = ranked[0]
    runners = ranked[1:4]
    margin = top.score - ranked[1].score if len(ranked) > 1 else 1.0
    identity = bool(top.tiers & IDENTITY_TIERS)

    if top.score < th["accept"]:
        return Decision(ESCALATE,
                        f"best score {top.score:.2f} < accept {th['accept']}",
                        top, runners, margin)

    if margin < th["margin"] and not identity:
        return Decision(ESCALATE,
                        f"ambiguous: top two within {margin:.2f} "
                        f"(need {th['margin']}) and no unique identity match",
                        top, runners, margin)

    why = "unique identity match" if identity else f"margin {margin:.2f}"
    if top.score >= th["auto"]:
        return Decision(AUTO, f"score {top.score:.2f}, {why}", top, runners, margin)
    return Decision(VERIFY, f"score {top.score:.2f}, {why}", top, runners, margin)

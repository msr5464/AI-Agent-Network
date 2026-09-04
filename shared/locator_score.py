"""Weighted similarity scoring (Similo / Similo++ with modern-web additions).

score = sum(w_i * op_i) / sum(w_i present in baseline)

The denominator rule matters: a property the baseline never recorded, or that
carries no information (a machine-generated id, a hashed class), is dropped from
BOTH sides. Scoring such a property as 0 would punish every candidate equally
and drag good matches under the accept threshold for no reason.
"""
from __future__ import annotations
import math, re
from typing import Any

# ------------------------------------------------------------- string metrics

def levenshtein(a: str, b: str) -> int:
    if a == b: return 0
    if not a: return len(b)
    if not b: return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def lev_sim(a: str, b: str) -> float:
    a, b = (a or ""), (b or "")
    if not a and not b: return 1.0
    m = max(len(a), len(b))
    return 1.0 - levenshtein(a, b) / m if m else 1.0


def jaro(a: str, b: str) -> float:
    if a == b: return 1.0
    if not a or not b: return 0.0
    window = max(max(len(a), len(b)) // 2 - 1, 0)
    a_flags, b_flags = [False] * len(a), [False] * len(b)
    matches = 0
    for i, ca in enumerate(a):
        for j in range(max(0, i - window), min(len(b), i + window + 1)):
            if not b_flags[j] and b[j] == ca:
                a_flags[i] = b_flags[j] = True
                matches += 1
                break
    if not matches: return 0.0
    trans, k = 0, 0
    for i, ca in enumerate(a):
        if a_flags[i]:
            while not b_flags[k]: k += 1
            if ca != b[k]: trans += 1
            k += 1
    trans //= 2
    return (matches / len(a) + matches / len(b) + (matches - trans) / matches) / 3


def jaro_winkler(a: str, b: str, p: float = 0.1) -> float:
    a, b = (a or "").lower(), (b or "").lower()
    j = jaro(a, b)
    if j < 0.7: return j
    prefix = 0
    for ca, cb in zip(a[:4], b[:4]):
        if ca != cb: break
        prefix += 1
    return j + prefix * p * (1 - j)


def jaccard(a: set, b: set) -> float:
    if not a and not b: return 1.0
    if not a or not b: return 0.0
    return len(a & b) / len(a | b)


def word_set_sim(a: list[str], b: list[str]) -> float:
    wa = {w.lower() for s in (a or []) for w in re.findall(r"\w+", s)}
    wb = {w.lower() for s in (b or []) for w in re.findall(r"\w+", s)}
    return jaccard(wa, wb)


def exp_decay(distance: float, scale: float) -> float:
    """1.0 at zero distance, decaying smoothly. Softer than a hard cutoff so a
    small layout shift costs a little rather than everything."""
    return math.exp(-distance / scale)


def ratio_sim(a: float, b: float) -> float:
    if a <= 0 or b <= 0:
        return 1.0 if a == b else 0.0
    return exp_decay(abs(math.log(a / b)), 0.7)


# ------------------------------------------------------------------ volatility

class Volatility:
    """Decides which recorded values are machine-generated noise."""

    def __init__(self, cfg: dict):
        self.id_pats = [re.compile(p, re.I) for p in cfg["volatile"]["id"]]
        self.class_pats = [re.compile(p, re.I) for p in cfg["volatile"]["class"]]

    def id_is_generated(self, v: str | None) -> bool:
        return bool(v) and any(p.match(v) for p in self.id_pats)

    def stable_classes(self, classes: list[str] | None) -> list[str]:
        return [c for c in (classes or []) if not any(p.match(c) for p in self.class_pats)]


# ----------------------------------------------------------------- the score

def _eq(a, b) -> float:
    if a is None or b is None:
        return 0.0
    return 1.0 if str(a).strip().lower() == str(b).strip().lower() else 0.0


def score(base: dict, cand: dict, cfg: dict, vol: Volatility) -> tuple[float, list[dict]]:
    """Return (normalised score 0..1, per-property breakdown).

    The breakdown is not decoration: a heal we cannot explain is a heal we
    cannot defend in a PR.
    """
    W = cfg["weights"]
    SC = cfg["scoring"]
    rows: list[dict] = []

    def add(prop: str, present: bool, value: float, detail: str = "", scale: float = 1.0):
        if not present:
            return                       # dropped from numerator AND denominator
        w = round(W[prop] * scale, 3)
        rows.append({"prop": prop, "weight": w, "op": round(value, 3),
                     "contrib": round(w * value, 3), "detail": detail})

    def absent(cand_value) -> float:
        """The candidate simply does not have this property.

        Absence of evidence is not evidence of difference. The app dropping a
        data-testid is not the same signal as the candidate carrying a *different*
        testid, so it costs a fraction of the weight rather than all of it.
        """
        return SC["absent_penalty"] if cand_value in (None, "", [], {}) else 1.0

    b_testid, c_testid = base.get("testid"), cand.get("testid")
    add("testid", bool(b_testid), _eq(b_testid, c_testid), f"{b_testid} vs {c_testid}",
        absent(c_testid))

    b_an, c_an = base.get("accessible_name"), cand.get("accessible_name")
    add("accessible_name", bool(b_an), jaro_winkler(b_an, c_an), f"{b_an!r} vs {c_an!r}")

    # Tag equality, but role-aware. <button> -> <a role="button"> -> <div role="button">
    # is routine component-library churn; when the computed role still matches, a
    # changed tag is presentation, not identity.
    same_tag = _eq(base["tag"], cand["tag"])
    if not same_tag and base.get("role") and base["role"] not in ("generic", "presentation") \
            and base["role"] == cand.get("role"):
        tag_op, tag_note = SC["tag_role_match"], f"{base['tag']}->{cand['tag']} (role held)"
    else:
        tag_op, tag_note = same_tag, f"{base['tag']} vs {cand['tag']}"
    add("tag", True, tag_op, tag_note)

    b_id = base.get("id")
    add("id", bool(b_id) and not vol.id_is_generated(b_id),
        _eq(b_id, cand.get("id")), f"{b_id} vs {cand.get('id')}", absent(cand.get("id")))

    add("name", bool(base.get("name")), _eq(base.get("name"), cand.get("name")),
        "", absent(cand.get("name")))
    add("role", bool(base.get("role")), _eq(base.get("role"), cand.get("role")),
        f"{base.get('role')} vs {cand.get('role')}")

    b_text = (base.get("text") or "").strip()
    add("text", bool(b_text), lev_sim(b_text, (cand.get("text") or "").strip()),
        f"{b_text[:30]!r} vs {(cand.get('text') or '')[:30]!r}")

    add("neighbor_texts", bool(base.get("neighbor_texts")),
        word_set_sim(base.get("neighbor_texts"), cand.get("neighbor_texts")))

    add("type", bool(base.get("type")), _eq(base.get("type"), cand.get("type")),
        "", absent(cand.get("type")))
    add("aria_label", bool(base.get("aria_label")),
        lev_sim(base.get("aria_label") or "", cand.get("aria_label") or ""),
        "", absent(cand.get("aria_label")))

    # All attributes as a k=v set (Similo++). Class/id are scored separately,
    # and volatile values are excluded so hashes cannot dominate the overlap.
    def attr_set(d: dict) -> set:
        out = set()
        for k, v in (d.get("attrs") or {}).items():
            if k in ("class", "style"):
                continue
            if k == "id" and vol.id_is_generated(v):
                continue
            out.add(f"{k}={v}")
        return out

    ba, ca = attr_set(base), attr_set(cand)
    add("attrs", bool(ba), jaccard(ba, ca))

    bc = vol.stable_classes(base.get("class_list"))
    cc = vol.stable_classes(cand.get("class_list"))
    add("class_list", bool(bc), jaccard(set(bc), set(cc)), f"{bc} vs {cc}")

    for prop in ("href", "alt"):
        add(prop, bool(base.get(prop)), lev_sim(base.get(prop) or "", cand.get(prop) or ""),
            "", absent(cand.get(prop)))

    add("abs_xpath", True, lev_sim(base.get("abs_xpath"), cand.get("abs_xpath")))
    add("id_xpath", True, lev_sim(base.get("id_xpath"), cand.get("id_xpath")))
    add("is_interactive", True, 1.0 if base["is_interactive"] == cand["is_interactive"] else 0.0)

    bb, cb = base["bbox_norm"], cand["bbox_norm"]
    dist = math.hypot(bb["x"] - cb["x"], bb["y"] - cb["y"])
    add("location", True, exp_decay(dist, 0.25), f"d={dist:.3f}")
    add("area", True, ratio_sim(base["area_norm"], cand["area_norm"]))
    add("shape", True, ratio_sim(max(base["aspect"], 1e-6), max(cand["aspect"], 1e-6)))

    total_w = sum(r["weight"] for r in rows)
    got = sum(r["contrib"] for r in rows)
    return (got / total_w if total_w else 0.0), rows


def format_breakdown(rows: list[dict], limit: int = 12) -> str:
    rows = sorted(rows, key=lambda r: -r["contrib"])[:limit]
    w = max((len(r["prop"]) for r in rows), default=4)
    return "\n".join(
        f"    {r['prop']:<{w}}  w={r['weight']:<4} op={r['op']:<5} +{r['contrib']:<6}"
        f"{r.get('detail', '')}".rstrip()
        for r in rows)

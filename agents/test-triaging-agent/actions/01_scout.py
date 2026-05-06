#!/usr/bin/env python3
"""
Step 01 — Scout
Query DB for recent build tags, score by failure count and recency,
skip already-analyzed build tags from feedback, select best candidate.
Outputs: audit/<session>/01-scout.json + 01-scout.md + .selected-buildtag

No AI calls. No DB writes. Read-only.
"""

import os, sys, json, re
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root → platform.*
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # agent dir → lib.*

from shared.log import log as _log
def log(msg): _log("scout", msg)

# ── Config ────────────────────────────────────────────────────────────────────

AUDIT_DIR = Path(os.environ["AUDIT_DIR"])
AGENT_DIR = Path(os.environ.get("AGENT_DIR", Path(__file__).resolve().parents[1]))
REPO_ROOT = Path(os.environ.get("REPO_ROOT", Path(__file__).resolve().parents[3]))
LOOKBACK_DAYS = int(os.environ.get("SCOUT_LOOKBACK_DAYS", "7"))

SKIP_FILE = AGENT_DIR / "feedback" / "skip-buildtags.json"

# ── Load skip list ────────────────────────────────────────────────────────────

def load_skip_buildtags():
    """Load already-analyzed build tags to avoid re-processing."""
    if not SKIP_FILE.exists():
        return set()
    try:
        entries = json.loads(SKIP_FILE.read_text())
        return {e["build_tag"] for e in entries if "build_tag" in e}
    except (json.JSONDecodeError, IOError):
        return set()

# ── DB query ──────────────────────────────────────────────────────────────────

def get_db_connection():
    """Create a MySQL connection using config settings."""
    from lib.settings import Config
    import pymysql
    return pymysql.connect(
        host=Config.DB_HOST,
        port=Config.DB_PORT,
        user=Config.DB_USER,
        password=Config.DB_PASSWORD,
        database=Config.DB_NAME,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
    )


def get_table_name_for_candidate(build_tag: str) -> str | None:
    """Derive table name from build tag using same logic as Database class."""
    from lib.database import Database
    return Database.get_table_name_from_report_name(build_tag)


def fetch_recent_build_tags():
    """
    Discover recent buildTags across known tables.
    Returns list of (build_tag, table_name, failure_count, total_count, latest_date) tuples.
    """
    from lib.settings import Config
    from lib.database import Database
    import pymysql

    cutoff = datetime.now() - timedelta(days=LOOKBACK_DAYS)
    candidates = []

    # Known table patterns to scan
    known_tables = [
        "results_prodsanity",
        "results_accountopening",
        "results_regression",
        "results_sanity",
        "results_smoke",
    ]

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Dynamically discover tables matching results_* pattern
        cursor.execute("SHOW TABLES LIKE 'results\\_%'")
        rows = cursor.fetchall()
        discovered = []
        for row in rows:
            tname = list(row.values())[0]
            if tname not in discovered:
                discovered.append(tname)
        tables_to_scan = list(set(known_tables + discovered))

        for table in tables_to_scan:
            try:
                # Check columns
                cursor.execute(f"SHOW COLUMNS FROM {table}")
                cols = {row["Field"] for row in cursor.fetchall()}

                if "buildTag" not in cols or "testStatus" not in cols:
                    continue

                # Find date column
                date_col = None
                for dc in ["createdAt", "created_at", "executionDate", "timestamp", "date"]:
                    if dc in cols:
                        date_col = dc
                        break

                if date_col:
                    cursor.execute(
                        f"""
                        SELECT
                            buildTag,
                            COUNT(*) AS total,
                            SUM(CASE WHEN UPPER(testStatus) IN ('FAIL','FAILED','ERROR','ERRORED') THEN 1 ELSE 0 END) AS failures,
                            MAX({date_col}) AS latest_date
                        FROM {table}
                        WHERE {date_col} >= %s
                        GROUP BY buildTag
                        ORDER BY latest_date DESC
                        LIMIT 50
                        """,
                        (cutoff.strftime("%Y-%m-%d %H:%M:%S"),),
                    )
                else:
                    cursor.execute(
                        f"""
                        SELECT
                            buildTag,
                            COUNT(*) AS total,
                            SUM(CASE WHEN UPPER(testStatus) IN ('FAIL','FAILED','ERROR','ERRORED') THEN 1 ELSE 0 END) AS failures,
                            NULL AS latest_date
                        FROM {table}
                        GROUP BY buildTag
                        ORDER BY id DESC
                        LIMIT 50
                        """,
                    )

                for row in cursor.fetchall():
                    bt = row.get("buildTag") or ""
                    if not bt:
                        continue
                    candidates.append({
                        "build_tag": bt,
                        "table": table,
                        "total": int(row.get("total") or 0),
                        "failures": int(row.get("failures") or 0),
                        "latest_date": str(row.get("latest_date") or ""),
                    })

            except Exception as e:
                log(f"Skipping table {table}: {e}")
                continue

    finally:
        if conn:
            conn.close()

    return candidates

# ── Scoring ───────────────────────────────────────────────────────────────────

# Fixability weights: error patterns that are likely ELEMENT_NOT_FOUND (auto-fixable)
# score higher; environment/infra errors score 0 (not worth analyzing for auto-fix).
# Pattern → (category_label, fixability_bonus)
_FIXABILITY_RULES = [
    # High fixability — locator issues that auto-fix can address (+15 bonus)
    (r"NoSuchElement|ElementNotFound|ElementClickIntercepted|ElementNotInteractable|StaleElement", "element-not-found", 15),
    (r"NOT loaded even after|waitForPageLoad|TimeoutException|timeout.*element|element.*timeout", "timeout-locator", 12),
    # Medium fixability — code errors in test code (+8 bonus)
    (r"NullPointerException.*[Tt]est|NPE.*[Ss]pec|null.*page.*object", "null-pointer-test", 8),
    # Low/no fixability — environment or product issues (0 bonus, may also skip)
    (r"ConnectionRefused|ConnectException|SocketTimeout|HTTP 5[0-9]{2}|API.*500|server.*error", "infra", 0),
    (r"OTP|one.time.password|authentication.*failed|login.*failed", "product-bug", 0),
]


def fixability_bonus(build_tag: str) -> tuple[int, str]:
    """
    Estimate fixability bonus (0–15) based on the build tag name / table name pattern.
    Returns (bonus_pts, category_label).
    The build_tag is a proxy — real error patterns come from 02_collect.
    This is a lightweight heuristic for scout prioritisation only.
    """
    # Sanity-check-style builds are usually locator-heavy → fixable
    tag_lower = build_tag.lower()
    if any(k in tag_lower for k in ("prodsanity", "sanity", "smoke", "regression")):
        return 10, "likely-locator"
    return 5, "unknown"


def score_candidate(c: dict) -> float:
    """
    Score a build tag candidate 0–115.
    Factors:
      1. Failure count       (50 pts) — more failures = higher priority
      2. Recency             (30 pts) — newer = higher priority
      3. Failure rate        (20 pts) — higher fail% = higher priority
      4. Fixability estimate (15 pts) — error category proxy for auto-fix potential
    """
    score = 0.0

    # Factor 1: Failure count (50 pts)
    failures = c["failures"]
    if failures >= 20:
        score += 50
    elif failures >= 10:
        score += 40
    elif failures >= 5:
        score += 30
    elif failures >= 2:
        score += 20
    elif failures >= 1:
        score += 10
    else:
        return 0.0  # No failures → not worth analyzing

    # Factor 2: Recency (30 pts)
    ld = c.get("latest_date", "")
    if ld:
        try:
            dt = datetime.fromisoformat(str(ld).replace("Z", "+00:00"))
            age_hours = (datetime.now(timezone.utc) - dt.replace(tzinfo=timezone.utc)).total_seconds() / 3600
            if age_hours < 6:
                score += 30
            elif age_hours < 24:
                score += 25
            elif age_hours < 48:
                score += 18
            elif age_hours < 72:
                score += 10
            else:
                score += 5
        except (ValueError, TypeError):
            score += 5

    # Factor 3: Failure rate (20 pts)
    total = c["total"]
    if total > 0:
        rate = failures / total
        if rate >= 0.30:
            score += 20
        elif rate >= 0.15:
            score += 15
        elif rate >= 0.05:
            score += 10
        else:
            score += 3

    # Factor 4: Fixability estimate (up to 15 pts)
    # Uses build_tag as a lightweight proxy (real patterns resolved in 02_collect)
    bonus, category = fixability_bonus(c["build_tag"])
    score += bonus
    c["fixability_category"] = category

    return round(score, 1)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log(f"Scanning DB for build tags in last {LOOKBACK_DAYS} days...")

    skip_tags = load_skip_buildtags()
    if skip_tags:
        log(f"Loaded {len(skip_tags)} skip tags from feedback")

    candidates = fetch_recent_build_tags()
    log(f"Found {len(candidates)} build tag records across all tables")

    skipped = []
    scored = []

    # Deduplicate: if same buildTag appears in multiple tables, keep highest failures
    seen: dict[str, dict] = {}
    for c in candidates:
        bt = c["build_tag"]
        if bt not in seen or c["failures"] > seen[bt]["failures"]:
            seen[bt] = c
    unique_candidates = list(seen.values())

    for c in unique_candidates:
        bt = c["build_tag"]
        if bt in skip_tags:
            skipped.append({"build_tag": bt, "reason": "already analyzed (in skip-buildtags.json)"})
            continue

        c["score"] = score_candidate(c)
        if c["score"] == 0:
            skipped.append({"build_tag": bt, "reason": "no failures"})
            continue

        scored.append(c)

    scored.sort(key=lambda x: x["score"], reverse=True)

    if not scored:
        log("ERROR: No eligible build tags found after filtering")
        sys.exit(1)

    selected = scored[0]
    log(f"Selected: {selected['build_tag']} (score={selected['score']}, failures={selected['failures']}/{selected['total']})")

    # ── Write JSON ─────────────────────────────────────────────────────────────
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    result = {
        "timestamp": ts,
        "lookback_days": LOOKBACK_DAYS,
        "total_records": len(candidates),
        "unique_candidates": len(unique_candidates),
        "skipped": len(skipped),
        "scored": len(scored),
        "selected_build_tag": selected["build_tag"],
        "selected": selected,
        "top10": scored[:10],
        "skip_summary": {},
    }
    for s in skipped:
        reason = s["reason"]
        result["skip_summary"][reason] = result["skip_summary"].get(reason, 0) + 1

    json_path = AUDIT_DIR / "01-scout.json"
    json_path.write_text(json.dumps(result, indent=2, default=str))

    # ── Write Markdown ─────────────────────────────────────────────────────────
    md_lines = [
        "# Scout Results",
        "",
        f"**Lookback:** {LOOKBACK_DAYS} days  ",
        f"**Candidates found:** {len(unique_candidates)} | **Skipped:** {len(skipped)} | **Scored:** {len(scored)}  ",
        f"**Timestamp:** {ts}",
        "",
        "## Top 10 Build Tags (Ranked by Priority)",
        "",
        "| Rank | Build Tag | Table | Failures | Total | Score |",
        "|------|-----------|-------|----------|-------|-------|",
    ]
    for i, c in enumerate(scored[:10], 1):
        md_lines.append(
            f"| {i} | {c['build_tag']} | {c['table']} | {c['failures']} | {c['total']} | {c['score']} |"
        )

    md_lines += [
        "",
        "## Selected",
        "",
        f"**Build Tag:** {selected['build_tag']}  ",
        f"**Table:** {selected['table']}  ",
        f"**Failures:** {selected['failures']} / {selected['total']}  ",
        f"**Score:** {selected['score']}/100  ",
    ]

    if skipped:
        md_lines += ["", "## Skipped", ""]
        for reason, count in sorted(result["skip_summary"].items()):
            md_lines.append(f"- {reason}: {count}")

    (AUDIT_DIR / "01-scout.md").write_text("\n".join(md_lines) + "\n")

    # ── Write gate file ────────────────────────────────────────────────────────
    (AUDIT_DIR / ".selected-buildtag").write_text(selected["build_tag"])
    log(f"Wrote .selected-buildtag: {selected['build_tag']}")


if __name__ == "__main__":
    main()

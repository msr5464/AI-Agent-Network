# learning-agent — Master Context

Read this file first. Every time. Before doing anything else.

## What This Agent Does

Turns what people said about and did to the other agents' work into **lessons**:
one sentence each, scoped to an agent and a step, put in front of that step's
prompt next time (`shared/lessons.py`). It never changes an agent by itself. It
proposes lessons as a pull request against `config/lessons/<agent>.yaml`; a lesson
is active once a person merges it.

The feedback comes from one store, `qa_agents_server/storage/feedback.jsonl`
(`shared/feedback.py`, docs/ARCHITECTURE.md "Human feedback"): PR verdicts and
`/lesson` comments, people's commits on agent branches, edits to drafts and test
cases, `make verdict`, the Studio's verdict, `make reclassify`, FORCE overrides,
Slack replies.

Run it on demand or on a schedule (weekly is plenty). It costs one model call when
there is evidence, plus two per replayed lesson; with no evidence it calls nothing.

---

## Architecture

```
run.sh
  ├─ 01_collect.py    [Python]           GitHub PRs + Slack threads → the store
  ├─ 02_attribute.py  [Python]           evidence vs context; join each to its run
  ├─ 03_classify.py   [Python]           tags + scope guess; groups by agent/step/tag
  ├─ 04_propose.py    [Python + Claude]  add / update / retire, each decided in Python
  ├─ 05_validate.py   [Python (+Claude)] conflicts; optional replay with and without
  └─ 06_ship.py       [Python]           report; with AUTO_PUSH=true, the PRs
```

## Rules

1. **Evidence, not inference.** Only a person saying something (a verdict with a
   reason or note, a `/lesson`, a comment, a corrected classification, a FORCE
   override) or a person changing what an agent wrote (a commit touching its
   files, an edit to its draft or test case) counts. A revert, a closed PR, a
   rewritten input, a cancel are context at most (`lib/evidence.py`). A revert in
   this repo has been made on purpose, to stage a failure for testing.
2. **A new lesson needs a `/lesson`, or two independent pieces of work** pointing
   the same way (`lib/evidence.enough`).
3. **The model proposes; Python decides.** Every operation is checked in
   `04_propose.decide`: cited ids exist, are evidence and are about that agent;
   steps exist for that agent; the statement passes `shared/lessons.problems`.
4. **A lesson adds; it never relaxes.** A statement that reads as skipping,
   weakening or removing a check is refused, here and again when it is loaded.
5. **One change at a time.** Add, update or retire; the set is never rewritten.
6. **Generic text.** No session ids, PR numbers, links or product copy in a
   statement; those stay in `evidence`.
7. **A person approves everything.** The PR is the approval. With AUTO_PUSH=false
   the proposed files stay in `proposed/` in the session.

## Levers

| Lever | Where it goes | Put in a prompt? |
|---|---|---|
| `lesson` | `config/lessons/<agent>.yaml` | yes, by `shared/lessons.py` |
| `convention` | the lessons file, and a PR appending it to the automation repo's `CLAUDE.md` | no: CLAUDE.md already reaches every agent and every person |
| guard candidate | the report only | no: an active lesson that people still correct for the same thing needs a code check (`shared/edit_guards.py`), written by a person |

## Lessons and their outcomes

Each session an agent runs records which lessons each step was given
(`00-lessons.json`), and the analytics row carries them. `shared/lessons.outcomes`
joins those sessions to their later verdicts: how many runs given a lesson were
accepted, edited, rejected or not yet reviewed. That table is in every report and
on the Studio's Analytics page, and it is what a `retire` must point to.

## Replay (step 05)

The call a lesson is for is in an evidence run's `claude-*.log` (logs name their
step since this agent was added; older ones are matched by the files the person
corrected). It is sent twice with no tools, from this repo: as it was, and with
the lesson. The share of the person's added lines in each answer says whether the
lesson moves the agent toward what the person wanted. Both arms share the
handicap of no tools and no worktree, which is why only their difference counts.
Browser-driving calls are never replayed.

## Gate and audit

No `.fix-passed`: a run is completed when it wrote its report. A run that found
nothing to learn calls no model and spends nothing.

`agents/learning-agent/audit/<session>/`:

| File | Step |
|---|---|
| `01-collect.json` / `.md` | what was collected |
| `02-attribute.json` | evidence and context, joined to their runs |
| `03-classify.json` | tags, scope guesses, groups |
| `04-propose.json` | every operation, accepted or refused with the reason; guard candidates; outcomes |
| `proposed/config/lessons/<agent>.yaml` | the files the PR would carry |
| `05-validate.json` | conflicts, replay results |
| `06-report.md`, `06-ship.json` | the report (the PR body); PR links |
| `claude-*.log`, `replay/` | the model calls |

## Environment

| Variable | Purpose |
|---|---|
| `LEARNING_MODEL`, `LEARNING_EFFORT` | the propose call (required model, set in config/.env) |
| `LEARNING_REPLAY_LIMIT` | lessons to replay, two calls each; 0 = off |
| `LEARNING_GUARD_AFTER` | corrected runs, despite the lesson, before it is a guard candidate |
| `AUTO_PUSH` | `true` opens the PRs |
| `GITHUB_TOKEN`, `GITHUB_ORG`, `GITHUB_REPO_AUTOMATION`, `GITHUB_DEFAULT_BRANCH` | collecting and the PRs |
| `SLACK_BOT_TOKEN`, `SLACK_NOTIFY_CHANNEL` | reading reply threads (needs `channels:history`) and the digest |
| `FEEDBACK_FILE`, `LESSONS_DIR` | scratch store and lessons dir, for a test run |

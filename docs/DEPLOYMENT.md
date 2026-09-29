# Deployment Guide

Two ways to run QA Agent Network beyond a laptop:

1. **The server**, so a team drives authoring, healing and adaptation from
   AI-Test-Studio.
2. **CI**, where triaging (and optionally healing) runs after each automation build.

Both need the same host setup.

---

## Host setup

A Linux or macOS host (Windows works through Git Bash or WSL — the agents are
`bash` + `make` underneath; the `.ps1` scripts are thin wrappers) with:

- Python 3.10+, Git, the `claude` CLI signed in as the service user
  (`claude auth login`), `gh`, Node.js 18+ with `npx`, JDK 21 and Maven.
- Network access to GitHub, the application under test, and (for triaging) the
  MySQL results database.

```bash
git clone https://github.com/msr5464/QA-AI-Agent.git QA-Agent-Network
cd QA-Agent-Network
./scripts/setup.sh
.venv/bin/python -m playwright install chromium
```

Then fill in `config/.env` — the [README's minimum configuration](../README.md#2-minimum-configuration)
plus Slack and, for triaging, the `TRIAGING_DB_*` settings. Keep secrets out of
git: `config/.env` is ignored; in CI, write it from your secret store.

---

## Running the server for AI-Test-Studio

```bash
bash scripts/run-server.sh
```

It loads `config/.env`, binds `127.0.0.1:6001` and runs until stopped. Settings
that matter for a shared host (all in `config/.env`; full table in
[SERVER_API.md](SERVER_API.md#starting-the-server)):

| Setting | Recommendation |
|---------|----------------|
| `QA_AGENT_SERVER_HOST` | Keep `127.0.0.1` and run AI-Test-Studio on the same host. If the Studio is elsewhere, bind the private interface **and** set `QA_AGENT_PROXY_SECRET` |
| `QA_AGENT_PROXY_SECRET` | A long random string, identical in both repos' `config/.env`. Without it, anyone who can reach the port can act as any user, including admin |
| `AI_TEST_STUDIO_URL` | The Studio's origin, for CORS |
| `QA_MAX_CONCURRENT_RUNS` | Default 4. Each run holds a worktree, a browser and a JVM — size for the host |
| `QA_WORKTREE_TEMP_DIR` | Default `/tmp/qa-runs`. Point it at a disk with room for one checkout per concurrent run |
| `AUTO_PUSH` | `true` on a shared server. With `false`, runs execute in the server's own checkout of the automation repo, one at a time |

On the Studio side, set `QA_AGENT_NETWORK_URL` (default `http://localhost:6001`)
and the same `QA_AGENT_PROXY_SECRET` in AI-Test-Studio's `config/.env`.

### As a service (systemd)

```ini
# /etc/systemd/system/qa-agents.service
[Unit]
Description=QA Agent Network server
After=network-online.target

[Service]
User=qa
WorkingDirectory=/opt/QA-Agent-Network
ExecStart=/bin/bash scripts/run-server.sh
Restart=on-failure
# Give running agents time to stop cleanly; the server cancels them on SIGTERM.
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
```

The `User` needs its own signed-in `claude` CLI, and `gh`/git credentials (or
`GITHUB_TOKEN` in `config/.env`).

### State and upgrades

- `qa_agents_server/storage/agent_runs.json` — run registry (last 500 runs).
  Runs that were active when the server stopped are marked `interrupted` on boot.
- `qa_agents_server/storage/run_analytics.jsonl` — analytics; append-only, back
  it up if the Studio's Analytics page matters to you.
- `agents/<agent>/audit/` — every session's trail. Git-ignored, grows without
  bound; prune old sessions by age if disk matters (the Studio's
  *Reset Analytics & History* also deletes them).

To upgrade: stop the service, `git pull`, re-run `./scripts/setup.sh`, start it.
Settings saved from the Studio's Agent Settings page live in `config/.env` and
survive upgrades.

---

## CI integration (Jenkins example)

Triaging reads the build's results from MySQL and its HTML reports from
`TRIAGING_INPUT_DIR/<build-tag>/` (or `TRIAGING_INPUT_DIR` itself, if that folder
is named after the tag). Run it after the automation job has written both.

```groovy
stage('QA Agent Network') {
    steps {
        withCredentials([file(credentialsId: 'qa-agents-env', variable: 'QA_ENV')]) {
            sh '''
                set -e
                QAN=/opt/QA-Agent-Network
                cp "$QA_ENV" "$QAN/config/.env"
                cd "$QAN"

                BUILD_TAG="${JOB_NAME}-${BUILD_NUMBER}"
                export TRIAGING_INPUT_DIR="$WORKSPACE/test-output/reports"
                export TRIAGING_OUTPUT_DIR="$WORKSPACE/qa-agent-reports"

                # 1. Classify the failures, write the HTML report, queue locator fixes
                ./scripts/run-triaging-agent.sh "$BUILD_TAG"

                # 2. Optional: heal what triaging queued, and raise a PR
                if [ -f "agents/test-healing-agent/queue/${BUILD_TAG}.json" ]; then
                    ./scripts/run-healing-agent.sh "$BUILD_TAG"
                fi
            '''
        }
    }
    post {
        always {
            publishHTML(target: [reportDir: 'qa-agent-reports',
                                 reportFiles: 'AI-Generated-Report_*.html',
                                 reportName: 'AI Failure Triage'])
        }
    }
}
```

Notes:
- The report file is `AI-Generated-Report_<build-tag>.html` in `TRIAGING_OUTPUT_DIR`.
- Healing needs the automation repo checkout (`WORKSPACE_DIR`/`FRAMEWORK_DIR`)
  and a browser on the agent. Run it on a node that can execute the test suite.
- Session trails stay under `agents/*/audit/`; archive them if you want them
  with the build.

### Keep page baselines fresh

Healing's diagnosis and Locate step compare a failing page with baselines the
framework records on every green page load. After a **green** suite, commit them
back so the next failure has something to compare with:

```bash
python3 scripts/commit_baselines.py "$WORKSPACE" --branch main --push
```

It only stages the baselines directory, skips files whose fingerprints did not
change, and never fails the build.

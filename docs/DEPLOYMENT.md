# QA Agent Network – Deployment Guide

This document summarizes the recommended steps for standing up the QA Agent Network on Windows workstations and wiring it into a Jenkins CI job.

---

## Windows Workstation Setup

1. **Prerequisites**
   - Windows 10/11 with PowerShell 5+ (PowerShell 7 recommended)
   - Git, Python 3.9+ (“Add to PATH” enabled), and MySQL client access
   - Ollama for Windows (if running the default local LLM) or an OpenAI API key
   - `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` (or run scripts with `-ExecutionPolicy Bypass`)

2. **Clone the repo**
   ```powershell
   git clone <repo-url>
   cd QA-Agent-Network
   ```

3. **Bootstrap dependencies**
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\windows\setup.ps1
   ```
   - Creates/refreshes `venv`
   - Upgrades `pip` and installs requirements
   - Copies `config\.env.example` → `config\.env` if needed

4. **Configure environment**
   - Edit `config\.env` with DB credentials, AI provider (`LLM_PROVIDER`, `OPENAI_*` / `OLLAMA_*`), `TRIAGING_INPUT_DIR`, and `TRIAGING_OUTPUT_DIR`.
   - Place automation report folders under `testdata\` (or update `TRIAGING_INPUT_DIR`).

5. **Run the agent**
   ```powershell
   .\scripts\run.ps1 --input-dir testdata\Regression-Growth-Tests-442 --output-dir reports
   ```
   HTML output is written to `reports\AI-Analysis-Report_*.html`.

6. **Optional automation**
   - Schedule recurring runs via Windows Task Scheduler:
     ```
     Action: Start a program
     Program/script: powershell
     Arguments: -ExecutionPolicy Bypass -File C:\path\to\QA-Agent-Network\scripts\run.ps1 --input-dir testdata\Regression-Growth-Tests-442 --output-dir reports
     ```
   - Redirect logs or archive report artifacts as needed.

---

## Jenkins Integration (Windows Agent)

1. **Prepare the Jenkins node**
   - Install Python 3.9+, Git, and (optionally) Ollama on the node.
   - Ensure the node has network access to your MySQL database and report storage.
   - Place `QA-Agent-Network` in a stable location (e.g., `C:\jenkins\tools\QA-Agent-Network`) or allow the pipeline to clone it each run.
   - Create `config\.env` with the same secrets used locally (store it as a Jenkins secret file if necessary).

2. **Pipeline stage (example)**
   Append this stage after your existing automation run:

   ```groovy
   stage('QA Agent Network Analysis') {
       steps {
           script {
               def qaAgentHome = "C:\\jenkins\\tools\\QA-Agent-Network"
               def reportDir = "testdata\\${env.JOB_NAME}-${env.BUILD_NUMBER}"

               bat """
                   if not exist "${qaAgentHome}" (
                       git clone https://github.com/your-org/QA-Agent-Network "${qaAgentHome}"
                   )
               """

               bat """
                   cd /d "${qaAgentHome}"
                   powershell -ExecutionPolicy Bypass -File scripts\\windows\\setup.ps1
               """

               bat """
                   cd /d "${qaAgentHome}"
                   powershell -ExecutionPolicy Bypass -File .\\scripts\\run.ps1 --input-dir "${reportDir}" --output-dir reports
               """

   Adjust paths to match wherever Jenkins stores the automation output. If your Gradle job writes reports outside the agent repo, copy them under `testdata\` (or set `TRIAGING_INPUT_DIR` accordingly) before calling `scripts\run.ps1`.

3. **Environment secrets**
   - Store DB passwords and API keys using Jenkins credentials.
   - You can inject them into `config\.env` during the pipeline via `withCredentials`/`writeFile`.

4. **Parallel suites**
   - Run multiple report analyses by invoking `scripts\run.ps1` with different `--input-dir` arguments, either sequentially or in parallel stages.

5. **Monitoring**
   - Archive the generated HTML or publish it via Jenkins “HTML Publisher” to make it easy to view.
   - Pipe script logs to `QA-Agent-Network\agent.log` for traceability.

---

Keep this document updated as your infrastructure evolves (e.g., if you move from MySQL to another datastore or adopt containerized runners).


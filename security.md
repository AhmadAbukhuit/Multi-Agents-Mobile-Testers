# Security & Privacy Policy

**Multi-Agents-Mobile-Testers** is engineered from the ground up for **local-first security and data privacy**. Pre-release mobile apps, proprietary `package.json` repositories, internal staging URLs, and user test credentials are never sent to external cloud LLM APIs.

---

## Privacy Guarantees via Local Ollama LLM

| Asset | How It Is Handled |
| :--- | :--- |
| **Source Code (`package.json`, routes, forms, auth)** | Analyzed strictly on your local machine by the **5 Code-Understanding Agents** ([core/code_intel/analyze_project.py](core/code_intel/analyze_project.py)). No source files or diffs are uploaded to third-party SaaS models. |
| **App Screenshots & UI Trees** | Screencaps (`*.png`), Set-of-Mark overlays (`*.som.png`), and XML/AXe accessibility dumps stay on disk and are only sent to your configured local `OLLAMA_HOST` (`http://localhost:11434` or the internal `ollama` Docker service). |
| **Typed Credentials & Inputs** | Every typed string (`type.py` / `adb shell input text`) is separated out of `trace.json` into `trace.params.json` (`TEXT_1`, `TEXT_2`). Never commit `trace.params.json`. |
| **Telemetry / LangSmith Tracing** | Disabled by default (`LANGCHAIN_TRACING_V2=false` in `configure_langsmith`). Traces only leave your machine if you explicitly set `LANGCHAIN_TRACING_V2=true` and provide an API key. |

---

## Built-In Technical Security Controls

### 1. Agent Tool & Command Sandboxing ([services/agents_api/agent.py](services/agents_api/agent.py))

- **`ALLOWED_COMMAND_PREFIXES` & `is_allowed_command()`**: Only vetted mobile-testing commands (`adb `, `drivers/android/screen.py`, `drivers/android/type.py`, `drivers/ios/screen.py`, `drivers/ios/type.py`, `drivers/ios/gesture.sh`, `sleep `, `ls `, `cat `, `mkdir `) may execute. Shell chaining/injection tokens (`;`, `&&`, `||`, `` ` ``, `$(`, newlines) are rejected before execution.
- **Path Containment (`resolve_workspace_path` & `exec_write_file`)**: Absolute paths and `..` escapes outside the workspace root are blocked, and `write_file` is strictly restricted to writing inside `results/`.

### 2. Dashboard Server Hardening ([services/dashboard/server.py](services/dashboard/server.py))
- **Localhost Binding**: Binds to `127.0.0.1` by default (`--host 127.0.0.1`).
- **CSRF Mitigation on `POST` Endpoints**: `/api/runs`, `/api/analyze`, `/api/trace`, and `/api/notes` require both `Content-Type: application/json` and the custom `X-Dashboard: 1` header.
- **File Serving Isolation**: `/api/screen` and `/api/live-screen` strictly validate filenames against safe regexes (`SCREEN`, `LIVE_SCREEN`) and verify resolved paths remain within `app.roots`. `trace.params.json` is never served.

### 3. Secret Scrubbing During Replay ([core/trace/ecr_replay.py](core/trace/ecr_replay.py))
- **`SECRET` Environment Filter**: `tool_env()` strips environment variables matching `KEY|TOKEN|SECRET|PASSPHRASE|PASSWORD|^OLLAMA_|^LLM_` before launching model-free `maestro test` replays.

### 4. Emulator Network Isolation ([drivers/android/run_agent.sh](drivers/android/run_agent.sh))
- Supports `BLOCK_HOSTS` pre-flight verification using `adb shell ping` so emulators cannot reach blocked production or sensitive internal hosts during automated exploration.

---

## Operational Best Practices

1. **Docker Exposure:** In [docker-compose.yml](docker-compose.yml), ports for `ollama`, `agents-api`, and `dashboard` are bound to `127.0.0.1`—keep them firewalled if running on a shared build host.
2. **Use Dedicated Test Accounts:** Never test with real customer data or production admin credentials even in local environments.
3. **Never Commit `results/` or `trace.params.json`:** Ensure `results/` artifacts generated during internal QA stay ignored in version control.

---

## Reporting a Vulnerability

If you discover a security vulnerability (command injection bypass, path traversal in the dashboard or API servers, unintended credential leakage in logs/traces, or container privilege issues):

1. **Do not** open a public GitHub Issue with exploit details or sensitive logs.
2. **Report privately** to the repository maintainers with:
   - Affected file path(s) and function/endpoint names
   - Clear reproduction steps or proof of concept
   - Impact assessment and suggested remediation (if available)
3. We aim to acknowledge reports promptly, validate the fix with unit tests, and publish an advisory/patch in the next release.

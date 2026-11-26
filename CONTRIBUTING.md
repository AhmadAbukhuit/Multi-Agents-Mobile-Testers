# Contributing to Multi-Agents-Mobile-Testers

Welcome! Thank you for helping improve **Multi-Agents-Mobile-Testers**, an open-source, privacy-first mobile QA framework powered by **local Ollama** (**5 Code-Understanding Agents** + **20 Persona UI Testers**).

Whether you want to fix a bug, add a new test persona, support another mobile framework in our code-intelligence pipeline, or improve documentation, this guide walks you through how to contribute step by step.

---

## Ways You Can Contribute

1. **Report Bugs:** Open a GitHub Issue describing the bug, your OS, platform (`android` / `ios`), Ollama model used, steps to reproduce, and relevant logs (without private credentials or `trace.params.json`).
2. **Suggest Enhancements:** Propose improvements for the 5 Code-Understanding Agents, Set-of-Mark visual prompting, Maestro compiler, dashboard UI, or Docker microservices.
3. **Add Test Personas & Briefs:** Expand [config/personas.json](config/personas.json) or add specialized testing briefs under `briefs/`.
4. **Extend Code Intelligence & Framework Support:** Add extractors for new navigation libraries, validation patterns, or mobile frameworks in [core/code_intel/analyze_project.py](core/code_intel/analyze_project.py) and `drivers/build/`.
5. **Write Tests & Improve Docs:** Add unit tests in `tests/` and clarify guides in [README.md](README.md) or `docs/`.

---

## Step-by-Step Contribution Workflow

### 1. Fork & Clone the Repository
1. Click **Fork** in the top-right corner of the repository on GitHub.
2. Clone your fork locally and add the upstream remote:
   ```bash
   git clone https://github.com/<your-username>/Multi-Agents-Mobile-Testers.git
   cd Multi-Agents-Mobile-Testers
   git remote add upstream https://github.com/<original-owner>/Multi-Agents-Mobile-Testers.git
   ```

### 2. Create a Branch
Always create a dedicated branch from the latest `main` before making changes:
```bash
git checkout main
git pull upstream main
git checkout -b feat/your-feature-name
# or: git checkout -b fix/short-bug-description
```

### 3. Set Up Your Local Development Environment
1. **Python 3.9+:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
   > **Stdlib Compatibility Rule:** Keep pure-Python stdlib fallbacks for optional dependencies (`langgraph`, `langchain-ollama`, `pydantic`, `pillow`) when modifying core modules so unit tests continue to run even in minimal Python environments.
2. **Local Ollama (for end-to-end device runs):**
   ```bash
   ollama pull qwen2.5vl:7b
   ollama create qwen2.5vl-32k -f config/Modelfile
   ```

### 4. Make Your Changes (Where Everything Lives)

| What You Want to Change | Target File(s) |
| :--- | :--- |
| **5 Code-Understanding Agents** (`package.json` analysis, routes, forms, state, playbooks) | [core/code_intel/analyze_project.py](core/code_intel/analyze_project.py) & [services/agents_api/schemas.py](services/agents_api/schemas.py) |
| **20 Persona Profiles** (device, locale, font scale, goal) | [config/personas.json](config/personas.json) |
| **Tester Instructions & Prompts** | [briefs/code_aware.md](briefs/code_aware.md), [briefs/default.md](briefs/default.md), [services/agents_api/agent.py](services/agents_api/agent.py) |
| **Set-of-Mark Visual Prompting (`*.som.png`)** | [core/som.py](core/som.py), [drivers/android/screen.py](drivers/android/screen.py), [drivers/ios/screen.py](drivers/ios/screen.py) |
| **Native Python Maestro Compiler & Replay** | [core/trace/maestro_compiler.py](core/trace/maestro_compiler.py), [core/trace/ecr_replay.py](core/trace/ecr_replay.py) |
| **REST Microservice API (`:8000`)** | [services/agents_api/server.py](services/agents_api/server.py) |
| **Live Dashboard UI & Server (`:8765`)** | [services/dashboard/server.py](services/dashboard/server.py), `services/dashboard/static/` |
| **Build & Device Runners** | [run_local.sh](run_local.sh), `drivers/build/`, `drivers/android/`, `drivers/ios/` |
| **Docker & Compose Stack** | [docker-compose.yml](docker-compose.yml), [services/agents_api/Dockerfile](services/agents_api/Dockerfile), [services/dashboard/Dockerfile](services/dashboard/Dockerfile) |

### 5. Write Tests & Verify Everything Passes
Add or update unit tests under `tests/` covering your changes, then run all checks before committing:

```bash
# Validate Docker Compose configuration
docker compose config -q

# Check shell script syntax
bash -n run_local.sh

# Run the entire Python test suite
python3 -m unittest discover -s tests -v
```

### 6. Commit & Push Your Changes
Keep commits focused with clear, descriptive messages (Conventional Commits style is encouraged):
```bash
git status
git add <changed-files>
git commit -m "feat(code-intel): support Expo Router nested tab discovery"
git push -u origin feat/your-feature-name
```

### 7. Open a Pull Request
1. Open a Pull Request against `main` from your fork branch.
2. In your PR description, include:
   - **What changed & why** (link any related Issue).
   - **How you tested it** (paste the `python3 -m unittest discover -s tests -v` output and any manual emulator/Docker verification).
   - **Screenshots** (if changing the Dashboard UI or Set-of-Mark rendering).

---

## Code & Security Expectations

- **Keep Privacy Local:** Do not add calls to external cloud LLM APIs or third-party telemetry that runs without explicit user opt-in.
- **Protect Secrets & Traces:** Never commit `results/`, `app.apk`, `.env` secrets, or `trace.params.json`.
- **Keep Changes Focused:** Avoid unrelated refactors outside the scope of your PR.
- **Follow Security Guidelines:** Review [security.md](security.md) before modifying shell execution, file paths, or HTTP handlers.

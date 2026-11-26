# Privacy

`Multi-Agents-Mobile-Testers` runs 100% locally on your machine using your local Ollama server (`OLLAMA_HOST`). There are no cloud runners, no external model API calls, no analytics, and no account required.

- All app builds, emulator/simulator sessions, and agent inference stay on your local machine.
- All screenshots, reports, traces, and optional screen recordings are written locally under `results/`.
- Report grouping in [core/reporting/summarize.py](core/reporting/summarize.py) sends only issue titles, screens, and severities to your local Ollama instance (set `SUMMARY_GROUPING=0` to skip it).
- The local dashboard ([services/dashboard/server.py](services/dashboard/server.py)) binds strictly to `127.0.0.1` and never serves `trace.params.json` (typed test values).

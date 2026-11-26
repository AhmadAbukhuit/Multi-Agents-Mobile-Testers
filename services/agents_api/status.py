"""Says how an agent's `ollama_agent.py` run ended, without printing anything it wrote.

Reads results/agent-output.json and results/agent-errors.txt and prints a
concise status line: finished, out of actions, connection error, missing model,
rate limit, or no output.
Usage: python3 scripts/agent_status.py <agent-output.json> <agent-errors.txt> <persona>"""
import json
import os
import re
import sys

# (status, regex on the agent's final message and its stderr), first match wins
PATTERNS = [
    ("connection", r"connection refused|failed to reach ollama|failed to connect|ollama.*not running|urlopen error"),
    ("model_not_found", r"model .* not found|pull model|ollama http 404|404 not found"),
    ("usage_limit", r"hit your (?:[\w-]+ )?limit|usage limit|limit reached|weekly limit"),
    ("auth", r"invalid api key|authentication_error|api error: 401|invalid bearer token|unauthorized"),
    ("credit", r"credit balance is too low|insufficient[_ ](?:credit|balance|quota)|billing"),
    ("rate_limit", r"rate_limit_error|api error: 429|too many requests"),
    ("overloaded", r"overloaded_error|api error: 529|overloaded"),
]
PROBLEMS = {
    "connection": ("error", "could not reach local Ollama",
                   "Start Ollama with 'ollama serve' and check OLLAMA_HOST (default http://localhost:11434)."),
    "model_not_found": ("error", "Ollama model not found",
                        "Pull or create the model first (e.g. ollama pull qwen2.5vl:7b)."),
    "usage_limit": ("warning", "stopped on a model usage limit",
                    "The model usage limit was reached. Wait for reset or run fewer personas at once."),
    "auth": ("error", "could not sign in to the model API",
             "Check OLLAMA_HOST and local model server access."),
    "credit": ("error", "found no API credit",
               "The configured model endpoint has no credit left."),
    "rate_limit": ("warning", "hit the API rate limit",
                   "Requests were refused (429). Run fewer personas at once."),
    "overloaded": ("warning", "stopped because the model server was overloaded",
                   "The model server returned 529. Run it again later."),
    "no_output": ("error", "produced no output",
                  "The Ollama agent wrote no result: see results/agent-errors.txt."),
    "error": ("warning", "stopped on an error",
              "See results/agent-output.json and results/agent-errors.txt."),
}


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def classify(output_path, errors_path):
    """(status, details) where details has the number of turns and the cost when known."""
    raw, errors = read(output_path).strip(), read(errors_path)
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        data = None
    if isinstance(data, list):
        data = next((m for m in reversed(data) if isinstance(m, dict) and m.get("type") == "result"), None)
    details = {}
    if isinstance(data, dict):
        for key in ("num_turns", "total_cost_usd"):
            if isinstance(data.get(key), (int, float)) and not isinstance(data.get(key), bool):
                details[key] = data[key]
        if data.get("subtype") == "success" and not data.get("is_error"):
            return "finished", details
        if data.get("subtype") == "error_max_turns":
            return "max_turns", details
    text = ((data.get("result") or "") if isinstance(data, dict) else raw) + "\n" + errors
    for status, pattern in PATTERNS:
        if re.search(pattern, str(text), re.I):
            return status, details
    if not isinstance(data, dict):
        return "no_output", details
    return "error", details


def describe(status, details):
    parts = []
    if "num_turns" in details:
        parts.append(f"{details['num_turns']} turns")
    if "total_cost_usd" in details:
        parts.append(f"${details['total_cost_usd']:.2f}")
    label = {"finished": "finished", "max_turns": "used all its turns"}.get(status) or PROBLEMS[status][1]
    return label + (f" ({', '.join(parts)})" if parts else "")


def main(argv):
    if len(argv) != 4:
        sys.exit(__doc__)
    persona = re.sub(r"[^\w.-]", "", argv[3]) or "agent"
    status, details = classify(argv[1], argv[2])
    line = f"{persona}: {describe(status, details)}"
    if os.environ.get("GITHUB_ACTIONS") != "true":
        print(("WARNING: " if status in PROBLEMS else "") + line)
        return status
    if status in PROBLEMS:
        level, title, hint = PROBLEMS[status]
        print(f"::{level} title={persona} {title}::{hint}")
    else:
        print(line)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"Agent {line}\n")
    return status


if __name__ == "__main__":
    main(sys.argv)

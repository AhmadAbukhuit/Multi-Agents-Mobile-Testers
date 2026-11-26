#!/usr/bin/env python3
"""LangGraph + LangChain (Ollama) agent loop for Multi-Agents-Mobile-Testers.

Runs a 5-node LangGraph state machine (load_context -> agent_decide ->
execute_tools -> guard_and_budget -> finalize_run) talking to a local Ollama
server with vision, tool calling, screen-signature loop detection, structured
findings validation, and opt-in LangSmith tracing.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "core"))
import som  # noqa: E402
from schemas import (  # noqa: E402
    FINAL_FINDINGS_SCHEMA,
    FinalFindings,
    ObservedIssue,
    VALID_SEVERITIES,
    validate_findings_json,
)

try:
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage  # type: ignore
    from langchain_ollama import ChatOllama  # type: ignore

    HAS_LANGCHAIN_OLLAMA = True
except ImportError:  # pragma: no cover - stdlib fallback
    HAS_LANGCHAIN_OLLAMA = False

try:
    from langgraph.graph import END, START, StateGraph  # type: ignore

    HAS_LANGGRAPH = True
except ImportError:  # pragma: no cover - stdlib fallback
    HAS_LANGGRAPH = False
    START = "__start__"
    END = "__end__"

    class StateGraph:  # type: ignore
        """Minimal LangGraph-compatible StateGraph adapter for offline/stdlib environments."""

        def __init__(self, _state_schema):
            self._nodes = {}
            self._edges = {}
            self._cond_edges = {}

        def add_node(self, name, fn):
            self._nodes[name] = fn

        def add_edge(self, src, dst):
            self._edges[src] = dst

        def add_conditional_edges(self, src, router, mapping=None):
            self._cond_edges[src] = (router, mapping or {})

        def compile(self):
            nodes = dict(self._nodes)
            edges = dict(self._edges)
            cond_edges = dict(self._cond_edges)

            class _CompiledGraph:
                def invoke(self, initial_state):
                    state = dict(initial_state)
                    current = edges.get(START)
                    while current and current != END:
                        update = nodes[current](state)
                        if isinstance(update, dict):
                            state.update(update)
                        if current in cond_edges:
                            router, mapping = cond_edges[current]
                            route = router(state)
                            current = mapping.get(route, route)
                        else:
                            current = edges.get(current, END)
                    return state

            return _CompiledGraph()

try:
    from langsmith import traceable  # type: ignore

    HAS_LANGSMITH = True
except ImportError:  # pragma: no cover - stdlib fallback
    HAS_LANGSMITH = False

    def traceable(*_args, **_kwargs):
        def _wrap(fn):
            return fn
        return _wrap


ALLOWED_COMMAND_PREFIXES = (
    "adb ",
    "python3 drivers/android/screen.py",
    "python3 drivers/android/type.py",
    "python3 drivers/ios/screen.py",
    "python3 drivers/ios/type.py",
    "bash drivers/ios/gesture.sh",
    "python3 scripts/android/screen.py",
    "python3 scripts/android/type.py",
    "python3 scripts/ios/screen.py",
    "python3 scripts/ios/type.py",
    "bash scripts/ios/gesture.sh",
    "sleep ",
    "ls ",
    "cat ",
    "mkdir ",
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run an allowed mobile testing command (adb, screen.py, type.py, "
                "gesture.sh, sleep, ls, cat, mkdir). When screen.py saves a PNG "
                "screenshot, it is automatically attached as an image."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run."}
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the workspace (briefs, personas.json, results, or PNG screenshots).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to the file."}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write report.md or findings.json under results/.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path under results/ to write."},
                    "content": {"type": "string", "description": "Full text content to write."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tap_element",
            "description": (
                "Tap a numbered Set-of-Mark [element_id] from the latest screen.py output "
                "without computing raw pixel coordinates manually."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "element_id": {"type": "integer", "description": "Numeric [id] shown on the .som.png screenshot."},
                    "directory": {"type": "string", "description": "Optional results directory containing latest_som.json."},
                },
                "required": ["element_id"],
            },
        },
    },
]

SCREENSHOT_RE = re.compile(r"^screenshot:\s+(\S+\.png)\b", re.M)
ELEMENT_LINE_RE = re.compile(r"^-\s+(?:button|field|text)\s+.*$", re.M)
BRIEF_PATH_RE = re.compile(r"\b(AGENT_BRIEF\.md|briefs/[A-Za-z0-9_.-]+\.md)\b")
PERSONA_ID_RE = re.compile(r"\bPERSONA=([A-Za-z0-9_.-]+)\b")


class TesterState(TypedDict, total=False):
    prompt: str
    root: str
    model: str
    host: str
    max_turns: int
    num_ctx: int
    timeout: int
    messages: List[Dict[str, Any]]
    pending_tool_calls: List[Dict[str, Any]]
    screen_signatures: List[str]
    stuck_count: int
    num_turns: int
    output_tokens: int
    last_text: str
    exit_code: int
    error_subtype: Optional[str]
    error_message: Optional[str]
    final_result: Dict[str, Any]


def configure_langsmith(env=None):
    """Ensure opt-in LangSmith environment variables have safe local defaults."""
    target = os.environ if env is None else env
    tracing = str(target.get("LANGCHAIN_TRACING_V2", "false")).strip().lower()
    enabled = tracing in ("1", "true", "yes")
    target.setdefault("LANGCHAIN_TRACING_V2", "true" if enabled else "false")
    target.setdefault("LANGCHAIN_PROJECT", "Multi-Agents-Mobile-Testers")
    target.setdefault("LANGCHAIN_ENDPOINT", "https://api.smith.langchain.com")
    return {
        "enabled": enabled,
        "project": target["LANGCHAIN_PROJECT"],
        "endpoint": target["LANGCHAIN_ENDPOINT"],
    }


def compute_screen_signature(screen_output):
    """Compute a normalized hash from element lines returned by screen.py."""
    lines = [line.strip() for line in ELEMENT_LINE_RE.findall(screen_output or "")]
    if not lines:
        return ""
    joined = "\n".join(lines)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def is_allowed_command(command):
    """Return True if command starts with an allowed prefix and has no chaining operators."""
    cmd = command.strip()
    if any(tok in cmd for tok in (";", "&&", "||", "`", "$(", "\n", "\r")):
        return False
    return any(cmd == prefix.strip() or cmd.startswith(prefix) for prefix in ALLOWED_COMMAND_PREFIXES)


def resolve_workspace_path(root, raw_path):
    """Resolve raw_path within root, returning None if it escapes root."""
    if not raw_path or Path(raw_path).is_absolute():
        return None
    target = (root / raw_path).resolve()
    root_resolved = root.resolve()
    try:
        target.relative_to(root_resolved)
    except ValueError:
        return None
    return target


def encode_png(path):
    return base64.b64encode(path.read_bytes()).decode("ascii")


def exec_run_command(root, command):
    """Execute an allowed testing command and capture any referenced screenshot PNG."""
    if not is_allowed_command(command):
        return f"Error: command not allowed: {command}", []
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return "Error: command timed out after 120s", []
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0 and not output.strip():
        output = f"Command exited with code {proc.returncode}"
    images = []
    match = SCREENSHOT_RE.search(output)
    if match:
        img_path = resolve_workspace_path(root, match.group(1))
        if img_path and img_path.is_file():
            som_path = img_path.with_suffix(".som.png")
            chosen = som_path if som_path.is_file() else img_path
            images.append(encode_png(chosen))
    return output.strip() or f"Command finished (exit {proc.returncode})", images


def exec_tap_element(root, element_id, directory=""):
    """Resolve a Set-of-Mark [element_id] from latest_som.json and tap its centre."""
    resolved = som.resolve_element(root, element_id, directory)
    if not resolved:
        return f"Error: Set-of-Mark element [{element_id}] not found; run screen.py first.", []
    x, y = resolved["tap_x"], resolved["tap_y"]
    platform = resolved["platform"]
    label = resolved["element"].get("label", "")
    if platform == "ios":
        cmd = ["idb", "ui", "tap", str(x), str(y)]
        shell_cmd = f"idb ui tap {x} {y}"
    else:
        cmd = ["adb", "shell", "input", "tap", str(x), str(y)]
        shell_cmd = f"adb shell input tap {x} {y}"
    try:
        proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"Error running {shell_cmd} for [{element_id}] \"{label}\": {exc}", []
    out = (proc.stdout or "") + (proc.stderr or "")
    detail = f" ({out.strip()})" if out.strip() else ""
    if proc.returncode != 0:
        return f"Error: {shell_cmd} exited with code {proc.returncode}{detail}", []
    return f"Tapped [{element_id}] \"{label}\" via {shell_cmd}{detail}", []


def exec_read_file(root, path):
    """Read a workspace text file or attach a PNG image."""
    target = resolve_workspace_path(root, path)
    if not target or not target.is_file():
        return f"Error: file not found: {path}", []
    if target.suffix.lower() == ".png":
        return f"Loaded image {path}", [encode_png(target)]
    try:
        return target.read_text(encoding="utf-8", errors="replace"), []
    except OSError as exc:
        return f"Error reading {path}: {exc}", []


def exec_write_file(root, path, content):
    """Write content only inside results/."""
    target = resolve_workspace_path(root, path)
    results_dir = (root / "results").resolve()
    if not target:
        return f"Error: invalid path: {path}", []
    try:
        target.relative_to(results_dir)
    except ValueError:
        return f"Error: write_file only allows paths under results/: {path}", []
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Wrote {path} ({len(content)} bytes)", []


def execute_tool(root, name, args):
    if not isinstance(args, dict):
        return f"Error: invalid arguments for {name}", []
    if name == "run_command":
        return exec_run_command(root, str(args.get("command") or ""))
    if name == "read_file":
        return exec_read_file(root, str(args.get("path") or ""))
    if name == "write_file":
        return exec_write_file(root, str(args.get("path") or ""), str(args.get("content") or ""))
    if name == "tap_element":
        return exec_tap_element(root, args.get("element_id"), str(args.get("directory") or ""))
    return f"Error: unknown tool {name}", []


def parse_fallback_tool_calls(content):
    """Extract JSON tool call objects from assistant text when tool_calls is empty."""
    text = (content or "").strip()
    if not text:
        return []
    candidates = [text]
    for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S):
        candidates.append(match.group(1))
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("name"), str) and isinstance(obj.get("arguments"), dict):
            return [{"function": {"name": obj["name"], "arguments": obj["arguments"]}}]
    return []


def ollama_chat(host, payload, timeout):
    """Send a chat request to Ollama /api/chat and return parsed JSON."""
    url = f"{host.rstrip('/')}/api/chat"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _langchain_invoke(model, host, num_ctx, timeout, messages):
    """Invoke ChatOllama when langchain_ollama is installed and enabled."""
    lc_messages = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content") or ""
        if role == "system":
            lc_messages.append(SystemMessage(content=content))
        elif role == "assistant":
            lc_messages.append(AIMessage(content=content))
        elif role == "tool":
            lc_messages.append(ToolMessage(content=content, tool_call_id=msg.get("name") or "tool"))
        else:
            imgs = msg.get("images") or []
            if imgs:
                blocks = [{"type": "text", "text": content}] + [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img}"}}
                    for img in imgs
                ]
                lc_messages.append(HumanMessage(content=blocks))
            else:
                lc_messages.append(HumanMessage(content=content))

    llm = ChatOllama(
        model=model,
        base_url=host,
        temperature=0.2,
        num_ctx=num_ctx,
        client_kwargs={"timeout": timeout},
    ).bind_tools(TOOLS)
    ai_msg = llm.invoke(lc_messages)
    tool_calls = [
        {"function": {"name": tc.get("name", ""), "arguments": tc.get("args") or {}}}
        for tc in (getattr(ai_msg, "tool_calls", None) or [])
    ]
    usage = getattr(ai_msg, "usage_metadata", None) or {}
    return {
        "eval_count": usage.get("output_tokens", 0),
        "message": {
            "role": "assistant",
            "content": ai_msg.content if isinstance(ai_msg.content, str) else json.dumps(ai_msg.content),
            "tool_calls": tool_calls,
        },
    }


def reports_ready(root):
    """Return True when at least one report.md and findings.json exist under results/."""
    results = root / "results"
    return any(results.glob("**/report.md")) and any(results.glob("**/findings.json"))


def _preload_context_text(root, prompt):
    """Pre-load referenced brief and persona JSON from the workspace into system context."""
    extras = []
    brief_match = BRIEF_PATH_RE.search(prompt or "")
    if brief_match:
        brief_file = resolve_workspace_path(root, brief_match.group(1))
        if brief_file and brief_file.is_file():
            extras.append(f"Preloaded {brief_match.group(1)}:\n{brief_file.read_text(encoding='utf-8', errors='replace')}")
    persona_match = PERSONA_ID_RE.search(prompt or "")
    personas_file = root / "config" / "personas.json"
    if not personas_file.is_file():
        personas_file = root / "personas.json"
    if persona_match and personas_file.is_file():
        try:
            personas = json.loads(personas_file.read_text(encoding="utf-8"))
            found = next((p for p in personas if p.get("id") == persona_match.group(1)), None)
            if found:
                extras.append(f"Preloaded persona ({persona_match.group(1)}):\n{json.dumps(found, indent=2, ensure_ascii=False)}")
        except (OSError, ValueError):
            pass
    guide_file = root / "results" / "CODE_TEST_GUIDE.md"
    if guide_file.is_file():
        guide_text = guide_file.read_text(encoding="utf-8", errors="replace").strip()
        if guide_text:
            extras.append(f"Preloaded 5-Agent Codebase Test Guide (results/CODE_TEST_GUIDE.md):\n{guide_text[:6000]}")
    code_ctx_file = root / "results" / "code_context.json"
    if persona_match and code_ctx_file.is_file():
        try:
            code_ctx = json.loads(code_ctx_file.read_text(encoding="utf-8"))
            playbook = (code_ctx.get("persona_playbooks") or {}).get(persona_match.group(1))
            if playbook:
                extras.append(
                    f"Preloaded Code-Aware Playbook for persona ({persona_match.group(1)}):\n"
                    f"{json.dumps(playbook, indent=2, ensure_ascii=False)}"
                )
        except (OSError, ValueError):
            pass
    return "\n\n".join(extras)


@traceable(name="mobile_agent_graph")
def build_tester_graph(use_langchain=False):
    """Build the 5-node LangGraph StateGraph for local Ollama mobile testing."""

    @traceable(name="load_context")
    def load_context(state: TesterState) -> Dict[str, Any]:
        root = Path(state["root"])
        system_prompt = (
            "You are an autonomous mobile QA tester running in a LangGraph workflow via local Ollama. "
            "Follow the brief, inspect Set-of-Mark screenshots after every action with run_command, "
            "prefer tap_element(element_id=...) when numbered [id] tags appear on the screen, and finish by "
            "writing report.md and findings.json (valid JSON with score 1-10 and issues) using write_file."
        )
        preloaded = _preload_context_text(root, state.get("prompt", ""))
        if preloaded:
            system_prompt = f"{system_prompt}\n\n{preloaded}"
        return {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": state["prompt"]},
            ],
            "pending_tool_calls": [],
            "screen_signatures": [],
            "stuck_count": 0,
            "num_turns": 0,
            "output_tokens": 0,
            "last_text": "",
            "exit_code": 0,
            "error_subtype": None,
            "error_message": None,
        }

    @traceable(name="agent_decide")
    def agent_decide(state: TesterState) -> Dict[str, Any]:
        turn = state.get("num_turns", 0) + 1
        model = state["model"]
        host = state["host"]
        messages = list(state.get("messages") or [])
        payload = {
            "model": model,
            "messages": messages,
            "tools": TOOLS,
            "stream": False,
            "options": {"num_ctx": state.get("num_ctx", 32768), "temperature": 0.2},
        }
        try:
            if use_langchain and HAS_LANGCHAIN_OLLAMA:
                resp = _langchain_invoke(model, host, state.get("num_ctx", 32768), state.get("timeout", 600), messages)
            else:
                resp = ollama_chat(host, payload, state.get("timeout", 600))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            msg = f"Ollama HTTP {exc.code}: {body or exc.reason}"
            print(msg, file=sys.stderr)
            return {"num_turns": turn, "exit_code": 1, "error_subtype": "error_during_execution", "error_message": msg}
        except Exception as exc:  # URLError, TimeoutError, OSError, ValueError, or LangChain error
            msg = f"Failed to reach Ollama at {host}: {exc}"
            print(msg, file=sys.stderr)
            return {"num_turns": turn, "exit_code": 1, "error_subtype": "error_during_execution", "error_message": msg}

        output_tokens = state.get("output_tokens", 0)
        eval_count = resp.get("eval_count")
        if isinstance(eval_count, int):
            output_tokens += eval_count

        message = resp.get("message") or {}
        last_text = str(message.get("content") or "")
        tool_calls = message.get("tool_calls") or parse_fallback_tool_calls(last_text)
        messages.append(message)

        return {
            "messages": messages,
            "pending_tool_calls": tool_calls,
            "num_turns": turn,
            "output_tokens": output_tokens,
            "last_text": last_text,
        }

    def route_after_decide(state: TesterState) -> str:
        if state.get("error_subtype"):
            return "finalize_run"
        if not state.get("pending_tool_calls"):
            return "finalize_run"
        return "execute_tools"

    @traceable(name="execute_tools")
    def execute_tools(state: TesterState) -> Dict[str, Any]:
        root = Path(state["root"])
        messages = list(state.get("messages") or [])
        signatures = list(state.get("screen_signatures") or [])
        stuck_count = state.get("stuck_count", 0)
        pending_images = []

        for call in state.get("pending_tool_calls") or []:
            fn = call.get("function") or {}
            name = fn.get("name", "")
            raw_args = fn.get("arguments")
            if isinstance(raw_args, str):
                try:
                    raw_args = json.loads(raw_args)
                except ValueError:
                    raw_args = {}
            out, imgs = execute_tool(root, name, raw_args)
            tool_msg = {"role": "tool", "content": out}
            if name:
                tool_msg["name"] = name
            messages.append(tool_msg)
            pending_images.extend(imgs)

            if name == "run_command" and isinstance(raw_args, dict) and "screen.py" in str(raw_args.get("command") or ""):
                sig = compute_screen_signature(out)
                if sig:
                    if signatures and signatures[-1] == sig:
                        stuck_count += 1
                    else:
                        stuck_count = 1
                    signatures.append(sig)

        if pending_images:
            messages.append({
                "role": "user",
                "content": "Here is the screenshot from the tool output above. Inspect it carefully before your next action.",
                "images": pending_images,
            })

        return {
            "messages": messages,
            "pending_tool_calls": [],
            "screen_signatures": signatures,
            "stuck_count": stuck_count,
        }

    @traceable(name="guard_and_budget")
    def guard_and_budget(state: TesterState) -> Dict[str, Any]:
        messages = list(state.get("messages") or [])
        stuck_count = state.get("stuck_count", 0)
        if stuck_count >= 3:
            messages.append({
                "role": "user",
                "content": (
                    "Loop warning: the screen has not changed across your last 3 captures. "
                    "Try a different control, scroll, go back, change input values, or write your report if blocked."
                ),
            })
            stuck_count = 0
        return {"messages": messages, "stuck_count": stuck_count}

    def route_after_guard(state: TesterState) -> str:
        if state.get("num_turns", 0) >= state.get("max_turns", 60):
            return "finalize_run"
        return "agent_decide"

    @traceable(name="finalize_run")
    def finalize_run(state: TesterState) -> Dict[str, Any]:
        root = Path(state["root"])
        for findings_path in sorted((root / "results").glob("**/findings.json")):
            try:
                raw = json.loads(findings_path.read_text(encoding="utf-8"))
                validated = validate_findings_json(raw)
                findings_path.write_text(json.dumps(validated, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            except (OSError, ValueError):
                pass

        model = state["model"]
        turns = state.get("num_turns", 0)
        output_tokens = state.get("output_tokens", 0)

        if state.get("error_subtype"):
            return {
                "final_result": {
                    "type": "result",
                    "subtype": state["error_subtype"],
                    "is_error": True,
                    "num_turns": turns,
                    "result": state.get("error_message") or "Execution error",
                    "modelUsage": {model: {"outputTokens": output_tokens}},
                }
            }

        if state.get("pending_tool_calls") or (turns >= state.get("max_turns", 60) and not reports_ready(root) and state.get("last_text") == ""):
            return {
                "final_result": {
                    "type": "result",
                    "subtype": "error_max_turns",
                    "is_error": True,
                    "num_turns": turns,
                    "result": state.get("last_text") or "Reached max turns",
                    "modelUsage": {model: {"outputTokens": output_tokens}},
                }
            }

        return {
            "final_result": {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "num_turns": turns,
                "result": state.get("last_text") or "",
                "modelUsage": {model: {"outputTokens": output_tokens}},
            }
        }

    graph = StateGraph(TesterState)
    graph.add_node("load_context", load_context)
    graph.add_node("agent_decide", agent_decide)
    graph.add_node("execute_tools", execute_tools)
    graph.add_node("guard_and_budget", guard_and_budget)
    graph.add_node("finalize_run", finalize_run)

    graph.add_edge(START, "load_context")
    graph.add_edge("load_context", "agent_decide")
    graph.add_conditional_edges(
        "agent_decide",
        route_after_decide,
        {"execute_tools": "execute_tools", "finalize_run": "finalize_run"},
    )
    graph.add_edge("execute_tools", "guard_and_budget")
    graph.add_conditional_edges(
        "guard_and_budget",
        route_after_guard,
        {"agent_decide": "agent_decide", "finalize_run": "finalize_run"},
    )
    graph.add_edge("finalize_run", END)
    return graph.compile()


def run_agent(prompt, model, host, max_turns=60, num_ctx=32768, timeout=600, root=None):
    """Run the LangGraph Ollama tester workflow until completion, max_turns, or error."""
    configure_langsmith()
    use_langchain = HAS_LANGCHAIN_OLLAMA and os.environ.get("OLLAMA_USE_LANGCHAIN", "0") == "1"
    compiled = build_tester_graph(use_langchain=use_langchain)
    final_state = compiled.invoke({
        "prompt": prompt,
        "root": str(Path(root or Path.cwd())),
        "model": model,
        "host": host,
        "max_turns": max_turns,
        "num_ctx": num_ctx,
        "timeout": timeout,
    })
    return final_state.get("exit_code", 0), final_state["final_result"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-p", "--prompt", required=True, help="Mission prompt for the agent")
    parser.add_argument("--model", default=os.environ.get("OLLAMA_MODEL") or os.environ.get("MODEL") or "qwen2.5vl:7b")
    parser.add_argument("--host", default=os.environ.get("OLLAMA_HOST") or "http://localhost:11434")
    parser.add_argument("--max-turns", type=int, default=60)
    parser.add_argument("--num-ctx", type=int, default=int(os.environ.get("OLLAMA_NUM_CTX", "32768")))
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("OLLAMA_TIMEOUT", "600")))
    args = parser.parse_args(argv)

    code, result = run_agent(
        prompt=args.prompt,
        model=args.model,
        host=args.host,
        max_turns=args.max_turns,
        num_ctx=args.num_ctx,
        timeout=args.timeout,
    )
    print(json.dumps(result))
    return code


if __name__ == "__main__":
    sys.exit(main())

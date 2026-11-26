#!/usr/bin/env python3
"""REST microservice API for Multi-Agents-Mobile-Testers.

Exposes endpoints to inspect personas, trigger local/containerized tester runs,
check run status, and generate consolidated issue summaries.
Usage: python3 services/agents_api/server.py [--host 0.0.0.0] [--port 8000]
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "core" / "reporting"))
sys.path.insert(0, str(ROOT / "core" / "code_intel"))
import summarize  # noqa: E402
import analyze_project as code_intel  # noqa: E402


def default_run_executor(root, params):
    """Execute run_local.sh for the requested parameters."""
    cmd = ["bash", str(root / "run_local.sh")]
    mapping = (
        ("platform", "--platform"),
        ("personas", "--personas"),
        ("app_dir", "--app-dir"),
        ("app_id", "--app-id"),
        ("framework", "--framework"),
        ("apk", "--apk"),
        ("ios_app", "--ios-app"),
        ("model", "--model"),
        ("ollama_host", "--ollama-host"),
        ("brief", "--brief"),
        ("bug_report", "--bug-report"),
        ("map", "--map"),
    )
    for key, flag in mapping:
        val = params.get(key)
        if val:
            cmd.extend([flag, str(val)])
    if params.get("record_video"):
        cmd.append("--record-video")
    if params.get("skip_build"):
        cmd.append("--skip-build")
    if params.get("langsmith"):
        cmd.append("--langsmith")

    adb_host = os.environ.get("ADB_CONNECT_HOST", "").strip()
    if adb_host:
        try:
            subprocess.run(["adb", "connect", adb_host], cwd=root, capture_output=True, text=True, timeout=15)
        except OSError:
            pass

    proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


class MicroserviceState:
    """Thread-safe run registry and workspace configuration."""

    def __init__(self, root=ROOT, executor=default_run_executor):
        self.root = Path(root)
        self.executor = executor
        self.lock = threading.Lock()
        self.runs = {}
        self.counter = 0

    def next_id(self):
        with self.lock:
            self.counter += 1
            return f"run-{self.counter}"


def collect_findings(results_dir):
    """Read findings.json files from results_dir without throwing on invalid files."""
    findings = {}
    base = Path(results_dir)
    if not base.is_dir():
        return findings
    for p in sorted(base.glob("*/findings.json")) + sorted(base.glob("*/results/*/findings.json")):
        try:
            findings[p.parent.name] = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return findings


def make_handler(state: MicroserviceState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Multi-Agents-Mobile-Testers-API"

        def log_message(self, *args):
            pass

        def send_json(self, code, payload):
            raw = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def read_json_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8", "replace")
            return json.loads(raw)

        def do_GET(self):
            url = urlparse(self.path)
            q = parse_qs(url.query)

            if url.path == "/health":
                tracing = str(os.environ.get("LANGCHAIN_TRACING_V2", "false")).lower() in ("1", "true", "yes")
                return self.send_json(200, {
                    "status": "ok",
                    "service": "Multi-Agents-Mobile-Testers",
                    "ollama_host": os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
                    "ollama_model": os.environ.get("OLLAMA_MODEL", "qwen2.5vl:7b"),
                    "adb_connect_host": os.environ.get("ADB_CONNECT_HOST", ""),
                    "langsmith_enabled": tracing,
                })

            if url.path == "/personas":
                personas_path = state.root / "config" / "personas.json"
                if not personas_path.is_file():
                    personas_path = state.root / "personas.json"
                if not personas_path.is_file():
                    return self.send_json(404, {"error": "personas.json not found"})
                personas = json.loads(personas_path.read_text(encoding="utf-8"))
                platform = (q.get("platform") or [""])[0].strip().lower()
                if platform:
                    personas = [p for p in personas if p.get("platform") == platform]
                return self.send_json(200, {"personas": personas})

            if url.path == "/runs":
                with state.lock:
                    items = list(state.runs.values())
                return self.send_json(200, {"runs": items})

            if url.path == "/code-context":
                ctx_file = state.root / "results" / "code_context.json"
                if not ctx_file.is_file():
                    return self.send_json(404, {"error": "code_context.json not found; POST /analyze first"})
                try:
                    data = json.loads(ctx_file.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    return self.send_json(500, {"error": str(exc)})
                return self.send_json(200, data)

            if url.path.startswith("/runs/"):
                run_id = url.path[len("/runs/"):].strip("/")
                with state.lock:
                    record = state.runs.get(run_id)
                if not record:
                    return self.send_json(404, {"error": f"run '{run_id}' not found"})
                enriched = dict(record)
                enriched["findings"] = collect_findings(state.root / "results")
                return self.send_json(200, enriched)

            return self.send_json(404, {"error": "not found"})

        def do_POST(self):
            url = urlparse(self.path)
            try:
                body = self.read_json_body()
            except ValueError:
                return self.send_json(400, {"error": "invalid JSON body"})
            if not isinstance(body, dict):
                return self.send_json(400, {"error": "JSON body must be an object"})

            if url.path == "/runs":
                run_id = state.next_id()
                record = {
                    "run_id": run_id,
                    "status": "running",
                    "params": body,
                    "created_at": time.time(),
                    "finished_at": None,
                    "exit_code": None,
                    "output": "",
                }
                with state.lock:
                    state.runs[run_id] = record

                def execute():
                    try:
                        code, out = state.executor(state.root, body)
                        status = "completed" if code == 0 else "failed"
                    except Exception as exc:
                        code, out, status = 1, f"{type(exc).__name__}: {exc}", "failed"
                    with state.lock:
                        record["exit_code"] = code
                        record["output"] = out[-4000:]
                        record["status"] = status
                        record["finished_at"] = time.time()

                if body.get("async", True):
                    threading.Thread(target=execute, daemon=True).start()
                    return self.send_json(202, record)
                execute()
                return self.send_json(200, record)

            if url.path == "/analyze":
                raw_dir = str(body.get("app_dir") or "demo-app").strip()
                target_dir = Path(raw_dir)
                if not target_dir.is_absolute():
                    target_dir = (state.root / raw_dir).resolve()
                else:
                    target_dir = target_dir.resolve()
                try:
                    target_dir.relative_to(state.root.resolve())
                except ValueError:
                    return self.send_json(400, {"error": "app_dir must stay inside workspace"})
                out_rel = str(body.get("out_dir") or "results").strip()
                out_dir = (state.root / out_rel).resolve()
                try:
                    out_dir.relative_to(state.root.resolve())
                except ValueError:
                    return self.send_json(400, {"error": "out_dir must stay inside workspace"})
                try:
                    ctx = code_intel.analyze_project(
                        app_dir=target_dir,
                        personas_path=state.root / "config" / "personas.json",
                        out_dir=out_dir,
                        app_id_override=str(body.get("app_id") or ""),
                        ollama_host=os.environ.get("OLLAMA_HOST", ""),
                        model=os.environ.get("OLLAMA_MODEL", ""),
                        use_llm=bool(body.get("use_llm", False)),
                    )
                except FileNotFoundError as exc:
                    return self.send_json(404, {"error": str(exc)})
                except ValueError as exc:
                    return self.send_json(400, {"error": str(exc)})
                guide_path = out_dir / "CODE_TEST_GUIDE.md"
                guide_md = guide_path.read_text(encoding="utf-8") if guide_path.is_file() else ""
                return self.send_json(200, {"ok": True, "code_context": ctx, "guide_markdown": guide_md})

            if url.path == "/summarize":
                rel_dir = body.get("results_dir") or "results"
                target = (state.root / rel_dir).resolve()
                try:
                    target.relative_to(state.root.resolve())
                except ValueError:
                    return self.send_json(400, {"error": "results_dir must stay inside workspace"})
                target.mkdir(parents=True, exist_ok=True)
                findings, scores = summarize.load(target)
                groups = summarize.group_with_ollama(findings)
                md = summarize.render(
                    findings,
                    scores,
                    groups or summarize.group_by_title(findings),
                    groups is not None,
                    summarize.load_reproduction(target),
                    summarize.load_walkthrough(target),
                )
                (target / "SUMMARY.md").write_text(md, encoding="utf-8")
                return self.send_json(200, {
                    "summary_markdown": md,
                    "findings_count": len(findings),
                    "scores": scores,
                })

            return self.send_json(404, {"error": "not found"})

    return Handler


def create_server(host="0.0.0.0", port=8000, root=ROOT, executor=default_run_executor):
    state = MicroserviceState(root=root, executor=executor)
    return ThreadingHTTPServer((host, port), make_handler(state))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default=os.environ.get("API_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("API_PORT", "8000")))
    args = parser.parse_args(argv)

    server = create_server(host=args.host, port=args.port)
    print(f"Multi-Agents-Mobile-Testers API listening on http://{args.host}:{server.server_port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

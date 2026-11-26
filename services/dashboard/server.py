"""The map of an app's screens, on your machine, with the paths of the testers on it.

Usage: python3 scripts/dashboard.py <collected run dir> [<another run dir> ...] [--port 8765] [--ecr <clone>]
       python3 scripts/dashboard.py --demo
       python3 scripts/dashboard.py --demo --export site/     # the online demo: static files, no server

Opens a page on http://127.0.0.1:8765 that draws every screen the testers saw as a map:
one card per screen, arrows from one screen to the next as wide as the number of testers,
a heat map of real or estimated taps on each screen. Comment a screen (trace.notes.json),
or add a check to it: what must be visible or not, possibly a known bug (trace.json).
Run it on the folders that scripts/collect.sh wrote (runs/<run id>), for instance a map
maker run (briefs/map.md) with a persona run.

Every path is a trace.json in the Explore-Compile-Replay format
(https://github.com/pierridotite/explore-compile-replay), which compiles it to a Maestro
flow that replays with no model. The dashboard changes only the checks of a trace, never its
steps, and keeps the explored trace as trace.explored.json the first time. With --ecr <clone>,
a save is refused unless `ecr validate` accepts the trace (Node 22.18+).

The results are private: the server listens on 127.0.0.1 only, and never serves
trace.params.json (the texts the agent typed).
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT_DIR / "core" / "trace"))
sys.path.insert(0, str(ROOT_DIR / "core" / "code_intel"))
import maestro_compiler  # noqa: E402
import analyze_project as code_intel  # noqa: E402

PAGE = Path(__file__).resolve().parent / "static"
STATIC = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
          "/style.css": ("style.css", "text/css")}
SCREEN = re.compile(r"^(\d{1,4})\.(png|svg)$")
LIVE_SCREEN = re.compile(r"^(\d{1,4})\.(som\.png|png|svg)$")
MEDIA = {"png": "image/png", "svg": "image/svg+xml"}
NOTES_SCHEMA = "mobile-agent-testers.notes/0"
SAME_SCREEN = 0.6   # Jaccard similarity above which two screenshots show the same screen


# ---------- reading a run ----------

def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def screen_elements(xml_path):
    """Named elements of a saved screen tree: label, id, bounds, clickable. The system bars are left out."""
    try:
        root = ET.parse(xml_path).getroot()
    except (ET.ParseError, OSError):
        return []
    out = []
    for n in root.iter("node"):
        if (n.get("package") or "") == "com.android.systemui":
            continue
        b = re.findall(r"-?\d+", n.get("bounds", ""))
        label = (n.get("text") or "").strip() or (n.get("content-desc") or "").strip()
        rid = (n.get("resource-id") or "").strip()
        if len(b) != 4 or not (label or rid):
            continue
        out.append({"label": label[:120], "id": rid, "bounds": [int(v) for v in b],
                    "clickable": n.get("clickable") == "true"})
    return out


def tokens(elements):
    """What identifies a screen: its ids (the same in every language) and the names of its controls."""
    found = set()
    for e in elements:
        if e["id"]:
            found.add("id:" + e["id"])
        # a control's name, unless it is only a number or a time, which changes from one visit to the next
        if e["clickable"] and e["label"] and len(e["label"]) <= 40 and e["label"].strip("0123456789 :.,%"):
            found.add("label:" + e["label"])
    return found


def similarity(a, b):
    """Jaccard similarity of two screens. On ids alone when both have a few: ids do not change
    with the language, so the same screen in French and in Japanese still matches."""
    ids_a, ids_b = {t for t in a if t.startswith("id:")}, {t for t in b if t.startswith("id:")}
    if len(ids_a) >= 3 and len(ids_b) >= 3:
        a, b = ids_a, ids_b
    return len(a & b) / len(a | b) if a and b else 0.0


def title_of(elements):
    """A short name for a screen: its top-most text that is not a control."""
    texts = [e for e in elements if e["label"] and not e["clickable"] and 2 <= len(e["label"]) <= 40]
    texts.sort(key=lambda e: (e["bounds"][1], e["bounds"][0]))
    return texts[0]["label"] if texts else ""


def group_screens(personas):
    """Gives each screenshot a group: screenshots of the same screen, by any tester, share it.
    A heat map adds up the taps of a group."""
    groups = []   # [tokens of the first screenshot, titles seen, count, screenshots]
    for p in personas:
        for nn, screen in sorted(p["screens"].items()):
            toks = screen.pop("_tokens")
            best, score = None, 0.0
            for i, g in enumerate(groups):
                s = similarity(toks, g[0])
                if s > score:
                    best, score = i, s
            if best is None or score < SAME_SCREEN:
                groups.append([toks, Counter(), 0, []])
                best = len(groups) - 1
            groups[best][2] += 1
            groups[best][3].append({"key": p["key"], "nn": nn})
            if screen["title"]:
                groups[best][1][screen["title"]] += 1
            screen["group"] = f"g{best + 1}"
    # members: every screenshot of the screen; the first one stands for it on the map
    return {f"g{i + 1}": {"title": (g[1].most_common(1)[0][0] if g[1] else ""), "count": g[2], "members": g[3]}
            for i, g in enumerate(groups)}


def find_traces(root):
    """Every trace.json under a collected run, or a single results dir."""
    root = Path(root)
    found = [p for p in sorted(root.rglob("trace.json")) if "node_modules" not in p.parts]
    return found


def as_roots(roots):
    return [Path(r) for r in (roots if isinstance(roots, (list, tuple)) else [roots])]


def key_of(roots, i, folder):
    """A persona's key: its path in the run, prefixed by the run's number when several runs are open."""
    rel = folder.relative_to(roots[i]).as_posix() if folder != roots[i] else "."
    return rel if len(roots) == 1 else f"{i + 1}:{rel}"


def locate(roots, key):
    """The folder a key names, or None. It must sit inside one of the runs."""
    roots = [r.resolve() for r in roots]
    i, rel = 0, key or ""
    if len(roots) > 1:
        head, _, rel = rel.partition(":")
        if not head.isdigit() or not 1 <= int(head) <= len(roots):
            return None
        i = int(head) - 1
    root = roots[i]
    folder = (root / rel).resolve() if rel and rel != "." else root
    if folder != root and root not in folder.parents:
        return None
    return folder


def load_run(roots):
    roots = as_roots(roots)
    personas = []
    for i, root in enumerate(roots):
        for trace_path in find_traces(root):
            load_persona(personas, roots, i, trace_path)
    groups = group_screens(personas)
    return {"run": " + ".join(r.resolve().name for r in roots), "personas": personas, "groups": groups}


def load_persona(personas, roots, i, trace_path):
    folder = trace_path.parent
    trace = read_json(trace_path)
    if not isinstance(trace, dict):
        return
    screens = {}
    for f in sorted(folder.iterdir()):
        m = SCREEN.match(f.name)
        if not m:
            continue
        elements = screen_elements(folder / f"{m.group(1)}.xml")
        screens[m.group(1)] = {"file": f.name, "elements": elements, "title": title_of(elements),
                               "_tokens": tokens(elements)}
        # Optional: where a saliency model predicts the eye goes, [[x, y, weight], ...] in 0..1.
        # The map shows it instead of its own estimate.
        saliency = read_json(folder / f"{m.group(1)}.saliency.json")
        if isinstance(saliency, dict) and isinstance(saliency.get("points"), list):
            screens[m.group(1)]["saliency"] = [pt for pt in saliency["points"]
                                               if isinstance(pt, list) and len(pt) == 3 and all(isinstance(v, (int, float)) for v in pt)][:2000]
    findings = read_json(folder / "findings.json")
    notes = read_json(folder / "trace.notes.json") or {"schema": NOTES_SCHEMA, "screens": {}}
    personas.append({
        "key": key_of(roots, i, folder),
        "trace": trace,
        "notes": notes,
        "findings": findings,
        # the map maker of briefs/map.md explores every screen; it is not a persona
        "mapper": isinstance(findings, dict) and findings.get("mode") == "map",
        "screens": screens,
    })


# ---------- checking and writing ----------

def check_trace(trace):
    """The checks that keep a trace usable even without ECR at hand. ECR checks the rest."""
    problems = []
    if not isinstance(trace, dict):
        return ["the trace is not a JSON object"]
    if trace.get("schema") != "ecr.trace/0":
        problems.append('schema must be "ecr.trace/0"')
    steps = trace.get("steps")
    if not isinstance(steps, list) or not steps:
        return problems + ["a trace needs at least one step"]
    ids = [s.get("id") if isinstance(s, dict) else None for s in steps]
    if any(not isinstance(i, str) or not i for i in ids):
        problems.append("every step needs an id")
    elif len(set(ids)) != len(ids):
        problems.append("two steps have the same id")
    for s in steps:
        if not isinstance(s, dict) or not isinstance(s.get("action"), dict) or not s["action"].get("kind"):
            problems.append(f"step {s.get('id') if isinstance(s, dict) else '?'} has no action")
    oracle_ids = []
    for s in steps:
        for o in (s.get("checkpoints") or []) if isinstance(s, dict) else []:
            check = o.get("check") if isinstance(o, dict) else None
            target = check.get("target") if isinstance(check, dict) else None
            oracle_ids.append(o.get("id") if isinstance(o, dict) else None)
            if not isinstance(check, dict) or check.get("kind") not in ("visible", "notVisible") or not isinstance(target, dict) or not ({"text", "id"} & set(target)):
                problems.append(f"check {oracle_ids[-1]} must say visible or notVisible, of a text or an id")
            elif o.get("observed", "held") not in ("held", "failed"):
                problems.append(f"check {oracle_ids[-1]}: observed is held or failed")
    if any(not isinstance(i, str) or not i for i in oracle_ids) or len(set(oracle_ids)) != len(oracle_ids):
        problems.append("every check needs its own id")
    first = steps[0].get("action", {}) if isinstance(steps[0], dict) else {}
    if trace.get("adapter", {}).get("id") == "mobile-maestro" and first.get("kind") != "launchApp":
        problems.append("the first step of a mobile trace must be launchApp")
    return problems


def write_atomic(path, data):
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def ecr_command(ecr, *args):
    """Runs the ECR command of a local clone. Returns (exit code, output)."""
    cli = Path(ecr) / "packages" / "cli" / "src" / "main.ts"
    node = shutil.which("node")
    if not node or not cli.exists():
        return 2, f"ECR not found: needs node and {cli}"
    try:
        r = subprocess.run([node, str(cli), *args], cwd=ecr, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return 2, "ECR took more than 60 s"
    return r.returncode, (r.stdout + r.stderr).strip()


def ecr_adapter(ecr):
    return str(Path(ecr) / "packages" / "adapter-maestro" / "src" / "index.ts")


# ---------- the server ----------

class App:
    def __init__(self, roots, ecr=None, agents_api_url=None, workspace_root=None, runner=None):
        self.roots = [r.resolve() for r in as_roots(roots)]
        self.root = self.roots[0]
        self.ecr = str(Path(ecr).resolve()) if ecr else None
        self.agents_api_url = (
            os.environ.get("AGENTS_API_URL", "") if agents_api_url is None else agents_api_url
        ).strip().rstrip("/")
        self.workspace_root = Path(workspace_root).resolve() if workspace_root else ROOT_DIR
        self.runner = runner
        self.live_runs = {}
        self.lock = threading.Lock()

    def folder(self, key):
        """The results dir of a persona, only if it holds a trace and sits inside a run."""
        folder = locate(self.roots, key)
        return folder if folder and (folder / "trace.json").exists() else None

    def personas_catalog(self):
        personas = []
        p_file = self.workspace_root / "config" / "personas.json"
        raw = read_json(p_file, {})
        if isinstance(raw, dict):
            for pid, info in raw.items():
                entry = dict(info) if isinstance(info, dict) else {}
                entry.setdefault("id", pid)
                personas.append(entry)
        elif isinstance(raw, list):
            for info in raw:
                if isinstance(info, dict) and info.get("id"):
                    personas.append(info)
        briefs = []
        briefs_dir = self.workspace_root / "briefs"
        if briefs_dir.is_dir():
            briefs = [f"briefs/{p.name}" for p in sorted(briefs_dir.glob("*.md"))]
        return {
            "personas": personas,
            "briefs": briefs,
            "agentsApiConfigured": bool(self.agents_api_url),
        }

    def analyze_app_dir(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("analyze payload must be a JSON object")
        raw_dir = str(payload.get("app_dir") or "demo-app").strip()
        target = Path(raw_dir)
        if not target.is_absolute():
            target = (self.workspace_root / raw_dir).resolve()
        else:
            target = target.resolve()
        try:
            target.relative_to(self.workspace_root.resolve())
        except ValueError:
            raise ValueError("app_dir must stay inside workspace")
        if not target.is_dir():
            raise FileNotFoundError(f"app_dir not found: {raw_dir}")
        ctx = code_intel.analyze_project(
            app_dir=target,
            personas_path=self.workspace_root / "config" / "personas.json",
            out_dir=self.root,
            app_id_override=str(payload.get("app_id") or ""),
            ollama_host=os.environ.get("OLLAMA_HOST", ""),
            model=os.environ.get("OLLAMA_MODEL", ""),
            use_llm=bool(payload.get("use_llm", False)),
        )
        return ctx

    def get_code_context(self):
        for candidate in [self.root / "code_context.json", self.workspace_root / "results" / "code_context.json"]:
            if candidate.is_file():
                data = read_json(candidate)
                if data:
                    return data
        return None

    def start_run(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("run payload must be a JSON object")
        persona = re.sub(r"[^\w.-]", "", str(payload.get("persona") or "general-user")) or "general-user"
        brief = str(payload.get("brief") or "").strip()
        if not brief:
            brief_candidate = self.workspace_root / "briefs" / f"{persona}.md"
            brief = f"briefs/{persona}.md" if brief_candidate.is_file() else "briefs/default.md"
        app_dir = str(payload.get("app_dir") or "").strip()
        app_id = str(payload.get("app_id") or payload.get("appId") or "").strip()
        if app_dir and payload.get("analyze_code", True):
            try:
                ctx = self.analyze_app_dir({"app_dir": app_dir, "app_id": app_id})
                if not app_id or app_id in ("com.example.app", "com.example.tinytodo"):
                    app_id = (ctx.get("app_metadata") or {}).get("app_id") or app_id
            except (FileNotFoundError, ValueError):
                pass
        platform = str(payload.get("platform") or "android").strip().lower()
        model = str(payload.get("model") or os.environ.get("OLLAMA_MODEL", "qwen2.5vl:7b")).strip()
        max_turns = int(payload.get("max_turns") or 30)
        run_id = re.sub(r"[^\w.-]", "", str(payload.get("run_id") or "")) or time.strftime("run-%Y%m%d-%H%M%S")
        out_rel = f"results/{run_id}/{persona}"
        out_dir = self.root / run_id / persona
        out_dir.mkdir(parents=True, exist_ok=True)
        prompt = (
            f"Run mobile QA persona '{persona}' on platform '{platform}' "
            f"(app_id={app_id or 'com.example.app'}). Read {brief} and config/personas.json, "
            f"use results/CODE_TEST_GUIDE.md if present, capture screens under {out_rel}/ using "
            f"drivers/{platform}/screen.py, use tap_element when numbered boxes are shown, and write "
            f"{out_rel}/report.md and {out_rel}/findings.json."
        )
        run_record = {
            "run_id": run_id,
            "persona": persona,
            "brief": brief,
            "app_dir": app_dir,
            "app_id": app_id,
            "platform": platform,
            "model": model,
            "max_turns": max_turns,
            "results_dir": str(out_dir),
            "status": "queued",
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "finished_at": None,
            "error": None,
        }
        with self.lock:
            self.live_runs[run_id] = run_record

        if self.runner:
            self.runner(run_record, prompt)
            return dict(run_record)

        def _execute():
            with self.lock:
                self.live_runs[run_id]["status"] = "running"
            log_file = out_dir / "agent.log"
            try:
                if self.agents_api_url:
                    req = urllib.request.Request(
                        f"{self.agents_api_url}/runs",
                        data=json.dumps({
                            "prompt": prompt,
                            "model": model,
                            "max_turns": max_turns,
                        }).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=1800) as resp:
                        raw_resp = resp.read().decode("utf-8", "replace")
                    log_file.write_text(raw_resp, encoding="utf-8")
                    parsed = json.loads(raw_resp) if raw_resp.strip() else {}
                    ok = parsed.get("exit_code", 0) == 0 and not parsed.get("is_error")
                    status = "completed" if ok else "failed"
                    err = None if ok else (parsed.get("result") or "agents-api returned error")
                else:
                    agent_py = self.workspace_root / "services" / "agents_api" / "agent.py"
                    proc = subprocess.run(
                        [sys.executable, str(agent_py), "--model", model, "--max-turns", str(max_turns), prompt],
                        cwd=self.workspace_root,
                        capture_output=True,
                        text=True,
                        timeout=1800,
                    )
                    log_file.write_text((proc.stdout or "") + (proc.stderr or ""), encoding="utf-8")
                    status = "completed" if proc.returncode == 0 else "failed"
                    err = None if proc.returncode == 0 else f"agent exited with code {proc.returncode}"
            except Exception as exc:
                status = "failed"
                err = str(exc)
                log_file.write_text(f"Run error: {exc}\n", encoding="utf-8")
            with self.lock:
                self.live_runs[run_id]["status"] = status
                self.live_runs[run_id]["error"] = err
                self.live_runs[run_id]["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        threading.Thread(target=_execute, daemon=True).start()
        return dict(run_record)

    def _latest_disk_snapshot(self):
        """Inspect self.roots for the newest screenshot, SoM map, and log lines."""
        candidates = []
        for root in self.roots:
            if not root.is_dir():
                continue
            for p in root.rglob("latest_som.json"):
                candidates.append(p.parent)
            for p in root.rglob("*.png"):
                if LIVE_SCREEN.match(p.name):
                    candidates.append(p.parent)
        seen_dirs = list({d.resolve(): d for d in candidates}.values())
        if not seen_dirs:
            return None

        def _dir_mtime(folder):
            files = [f for f in folder.iterdir() if f.is_file()]
            return max((f.stat().st_mtime for f in files), default=0)

        newest_dir = max(seen_dirs, key=_dir_mtime)
        pngs = sorted(
            [f for f in newest_dir.iterdir() if f.is_file() and re.match(r"^\d{1,4}\.png$", f.name)],
            key=lambda p: int(p.stem),
        )
        som_pngs = sorted(
            [f for f in newest_dir.iterdir() if f.is_file() and re.match(r"^\d{1,4}\.som\.png$", f.name)],
            key=lambda p: int(p.name.split(".")[0]),
        )
        latest_raw = pngs[-1] if pngs else None
        latest_som = None
        if latest_raw and (newest_dir / f"{latest_raw.stem}.som.png").is_file():
            latest_som = newest_dir / f"{latest_raw.stem}.som.png"
        elif som_pngs:
            latest_som = som_pngs[-1]

        som_data = read_json(newest_dir / "latest_som.json", {}) or {}
        elements = list((som_data.get("elements") or {}).values())

        rel_dir = ""
        for idx, root in enumerate(self.roots):
            try:
                rel_dir = str(newest_dir.resolve().relative_to(root.resolve()))
                root_idx = idx
                break
            except ValueError:
                continue
        else:
            root_idx = 0

        def _screen_url(file_path):
            if not file_path:
                return None
            return f"api/live-screen?root={root_idx}&dir={rel_dir}&name={file_path.name}&t={int(file_path.stat().st_mtime)}"

        log_tail = ""
        for log_name in ("agent.log", "actions.tsv"):
            lf = newest_dir / log_name
            if lf.is_file():
                lines = lf.read_text(encoding="utf-8", errors="replace").splitlines()
                log_tail = "\n".join(lines[-20:])
                break

        return {
            "folder": newest_dir.name,
            "dir": rel_dir,
            "rawUrl": _screen_url(latest_raw),
            "somUrl": _screen_url(latest_som),
            "elementCount": len(elements),
            "elements": elements[:40],
            "hasReport": (newest_dir / "report.md").is_file(),
            "hasFindings": (newest_dir / "findings.json").is_file(),
            "hasTrace": (newest_dir / "trace.json").is_file(),
            "logTail": log_tail,
        }

    def live_status(self, run_id=None):
        with self.lock:
            runs = list(self.live_runs.values())
            if run_id:
                runs = [r for r in runs if r.get("run_id") == run_id]
        snapshot = self._latest_disk_snapshot()
        return {
            "runs": sorted(runs, key=lambda r: r.get("started_at") or "", reverse=True),
            "latest": snapshot,
            "agentsApiConfigured": bool(self.agents_api_url),
        }

    def resolve_live_file(self, root_idx, rel_dir, name):
        if not LIVE_SCREEN.match(name or ""):
            return None
        try:
            base = self.roots[int(root_idx)].resolve()
        except (IndexError, ValueError):
            base = self.root.resolve()
        target = (base / rel_dir / name).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            return None
        return target if target.is_file() else None


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "mobile-agent-testers-dashboard"

        def log_message(self, *args):   # paths can hold app names: keep the terminal quiet
            pass

        def send(self, code, body, kind="application/json"):
            data = body if isinstance(body, bytes) else (json.dumps(body, ensure_ascii=False) if kind == "application/json" else body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", kind + ("; charset=utf-8" if not kind.startswith("image/png") else ""))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlparse(self.path)
            q = parse_qs(url.query)
            if url.path in STATIC:
                name, kind = STATIC[url.path]
                return self.send(200, (PAGE / name).read_bytes(), kind)
            if url.path == "/api/run":
                with app.lock:
                    run = load_run(app.roots)
                run["ecr"] = bool(app.ecr)
                run["nativeMaestro"] = True
                return self.send(200, run)
            if url.path == "/api/personas":
                return self.send(200, app.personas_catalog())
            if url.path == "/api/code-context":
                ctx = app.get_code_context()
                if not ctx:
                    return self.send(404, {"error": "no code_context.json analyzed yet"})
                return self.send(200, ctx)
            if url.path == "/api/live":
                return self.send(200, app.live_status(q.get("run_id", [None])[0]))
            if url.path == "/api/live-screen":
                target = app.resolve_live_file(
                    q.get("root", ["0"])[0],
                    q.get("dir", [""])[0],
                    q.get("name", [""])[0],
                )
                if not target:
                    return self.send(404, {"error": "no such live screen"})
                m = LIVE_SCREEN.match(target.name)
                kind = "image/png" if target.name.endswith(".png") else MEDIA[m.group(2)]
                return self.send(200, target.read_bytes(), kind)
            if url.path == "/api/maestro":
                folder = app.folder(q.get("persona", [""])[0])
                if not folder:
                    return self.send(404, {"error": "no such persona"})
                trace = read_json(folder / "trace.json")
                params = read_json(folder / "trace.params.json", {})
                if not isinstance(params, dict):
                    params = {}
                try:
                    yaml_text = maestro_compiler.compile_trace(trace, params=params)
                except ValueError as exc:
                    return self.send(422, {"ok": False, "error": str(exc)})
                trace_id = str((trace or {}).get("id") or "trace").replace("/", "_")
                if q.get("format", [""])[0] == "yaml":
                    return self.send(200, yaml_text, "text/yaml")
                return self.send(200, {"ok": True, "filename": f"{trace_id}.flow.yaml", "yaml": yaml_text})
            if url.path == "/api/screen":
                folder = app.folder(q.get("persona", [""])[0])
                name = q.get("name", [""])[0]
                m = SCREEN.match(name)
                if not folder or not m or not (folder / name).is_file():
                    return self.send(404, {"error": "no such screen"})
                return self.send(200, (folder / name).read_bytes(), MEDIA[m.group(2)])
            return self.send(404, {"error": "not found"})

        def do_POST(self):
            # A page of another site cannot send this header without the browser asking first.
            if self.headers.get("X-Dashboard") != "1" or "application/json" not in (self.headers.get("Content-Type") or ""):
                return self.send(403, {"error": "refused"})
            url = urlparse(self.path)
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"null")
            except ValueError:
                return self.send(400, {"error": "not JSON"})
            if url.path == "/api/analyze":
                try:
                    ctx = app.analyze_app_dir(body)
                except FileNotFoundError as exc:
                    return self.send(404, {"ok": False, "error": str(exc)})
                except ValueError as exc:
                    return self.send(400, {"ok": False, "error": str(exc)})
                return self.send(200, {"ok": True, "code_context": ctx})
            if url.path == "/api/runs":
                try:
                    run_record = app.start_run(body)
                except (ValueError, OSError) as exc:
                    return self.send(400, {"ok": False, "error": str(exc)})
                return self.send(202, {"ok": True, "run": run_record})
            folder = app.folder(parse_qs(url.query).get("persona", [""])[0])
            if not folder:
                return self.send(404, {"error": "no such persona"})
            if url.path == "/api/notes":
                if not isinstance(body, dict) or not isinstance(body.get("screens", {}), dict) or not isinstance(body.get("steps", {}), dict):
                    return self.send(400, {"error": "notes need a screens object"})
                body["schema"] = NOTES_SCHEMA
                with app.lock:
                    write_atomic(folder / "trace.notes.json", body)
                return self.send(200, {"ok": True})
            if url.path == "/api/trace":
                return self.save(folder, body)
            return self.send(404, {"error": "not found"})

        def save(self, folder, trace):
            """Writes a trace whose checks were edited. Its steps must stay those that were explored."""
            problems = check_trace(trace)
            with app.lock:
                explored = folder / "trace.explored.json"
                before = read_json(explored if explored.exists() else folder / "trace.json") or {}
                strip = lambda t: [{k: v for k, v in st.items() if k != "checkpoints"} for st in t.get("steps", []) if isinstance(st, dict)]
                if not problems and isinstance(trace, dict) and strip(trace) != strip(before):
                    problems = ["only checks can change: the steps must stay as explored"]
                if not problems and app.ecr:
                    with tempfile.TemporaryDirectory(prefix="dashboard-") as tmp:
                        path = Path(tmp) / "trace.json"
                        path.write_text(json.dumps(trace, ensure_ascii=False), encoding="utf-8")
                        code, out = ecr_command(app.ecr, "validate", str(path), "--adapter", ecr_adapter(app.ecr))
                    if code != 0:
                        problems = [out]
                if problems:
                    return self.send(422, {"ok": False, "problems": problems})
                if not explored.exists():
                    shutil.copyfile(folder / "trace.json", explored)
                write_atomic(folder / "trace.json", trace)
            return self.send(200, {"ok": True, "checkedByEcr": bool(app.ecr)})

    return Handler


def export(roots, out):
    """Writes the page and a run as static files, for the online demo. Comments then stay in the
    browser and Save downloads them. Never export a real run: the files are public once hosted."""
    out = Path(out)
    roots = as_roots(roots)
    run = load_run(roots)
    run["static"] = True
    run["ecr"] = False
    run["run"] = "Demo: a made-up to-do app, its map and three testers"
    (out / "api").mkdir(parents=True, exist_ok=True)
    for name, _ in STATIC.values():
        shutil.copyfile(PAGE / name, out / name)
    for i, p in enumerate(run["personas"]):
        folder = locate(roots, p["key"])
        target = out / "screens" / f"t{i + 1}"
        target.mkdir(parents=True, exist_ok=True)
        for screen in p["screens"].values():
            shutil.copyfile(folder / screen["file"], target / screen["file"])
            screen["url"] = f"screens/t{i + 1}/{screen['file']}"
    for name in ("run", "run.json"):   # "run" is what the page asks for first
        (out / "api" / name).write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("run", nargs="*", help="collected runs (runs/<run id>), or results dirs. Several: a map run with persona runs")
    parser.add_argument("--host", default=os.environ.get("DASHBOARD_HOST", "127.0.0.1"), help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--ecr", help="a clone of explore-compile-replay: each save of checks must pass `ecr validate`")
    parser.add_argument("--demo", action="store_true", help="open a made-up run, to try the page")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--allow-empty", action="store_true", help="start even when no trace.json exists yet")
    parser.add_argument("--export", metavar="DIR", help="write a static copy for the online demo instead of serving")
    args = parser.parse_args(argv)

    if args.demo:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import demo as dashboard_demo
        roots = [dashboard_demo.write(Path(tempfile.mkdtemp(prefix="dashboard-demo-")))]
    elif args.run:
        roots = [Path(r) for r in args.run]
    else:
        parser.error("give a run dir (runs/<run id>), or --demo")
    for root in roots:
        if args.allow_empty:
            root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir():
            parser.error(f"{root} is not a folder")
    root = roots[0]
    count = sum(len(find_traces(r)) for r in roots)
    if not count and not args.allow_empty:
        print(f"No trace.json under {root}. Traces come from Android testers (scripts/ecr_trace.py).")
        return 1
    if args.export:
        if not args.demo:
            parser.error("--export is for the demo only: a real run is private")
        print(f"Static demo written to {export(roots, args.export)}")
        return 0
    if args.ecr and not (Path(args.ecr) / "packages" / "cli" / "src" / "main.ts").exists():
        parser.error(f"{args.ecr} is not a clone of explore-compile-replay")

    server = ThreadingHTTPServer((args.host, args.port), make_handler(App(roots, args.ecr)))
    url = f"http://{args.host}:{server.server_port}/"
    print(f"{count} paths. Map: {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

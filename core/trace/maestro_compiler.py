"""Native Python compiler from Explore-Compile-Replay `trace.json` to Maestro `.flow.yaml`.

Eliminates the external Node.js / Explore-Compile-Replay checkout requirement
while keeping full compatibility with `trace.json` files emitted by
`core/trace/ecr_trace.py` and annotated in the dashboard.
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


SUPPORTED_ACTIONS = {
    "launchApp",
    "tapOn",
    "longPressOn",
    "doubleTapOn",
    "inputText",
    "eraseText",
    "swipe",
    "pressKey",
    "scroll",
    "back",
    "hideKeyboard",
    "waitForAnimationToEnd",
}


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    return json.dumps(text, ensure_ascii=False)


def _format_target(target: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if "text" in target and target["text"] is not None:
        out["text"] = str(target["text"])
    if "id" in target and target["id"] is not None:
        out["id"] = str(target["id"])
    if "index" in target and target["index"] is not None:
        out["index"] = int(target["index"])
    if "point" in target and isinstance(target["point"], list) and len(target["point"]) == 2:
        x, y = target["point"]
        out["point"] = f"{x},{y}"
    return out


def _resolve_param(raw: Any, params: Dict[str, Any]) -> str:
    if isinstance(raw, dict) and "param" in raw:
        name = str(raw["param"])
        if name in params:
            return str(params[name])
        return "${ECR_PARAM_" + name + "}"
    return "" if raw is None else str(raw)


def _checkpoint_commands(checkpoint: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Convert one dashboard or trace checkpoint object into Maestro assertions."""
    if not isinstance(checkpoint, dict):
        return []
    # Skip known bugs? Keep comment marker cleanly if marked knownBug/bug, still compile assertion when present.
    kind = checkpoint.get("kind") or checkpoint.get("assert") or ""
    target = checkpoint.get("target")
    commands: List[Dict[str, Any]] = []

    if isinstance(target, dict):
        selector = _format_target(target)
    elif isinstance(target, str) and target.strip():
        selector = {"text": target.strip()}
    elif isinstance(checkpoint.get("text"), str) and checkpoint["text"].strip():
        selector = {"text": checkpoint["text"].strip()}
    elif isinstance(checkpoint.get("id"), str) and checkpoint["id"].strip():
        selector = {"id": checkpoint["id"].strip()}
    else:
        selector = {}

    if not selector:
        return []

    if kind in ("visible", "assertVisible", "present", "exists", ""):
        commands.append({"assertVisible": selector})
    elif kind in ("notVisible", "not_visible", "assertNotVisible", "absent", "gone"):
        commands.append({"assertNotVisible": selector})
    return commands


def validate_trace(trace: Any) -> List[str]:
    """Validate basic structure of an `ecr.trace/0` trace dictionary."""
    problems: List[str] = []
    if not isinstance(trace, dict):
        return ["trace root must be a JSON object"]
    schema = trace.get("schema")
    if schema and schema != "ecr.trace/0":
        problems.append(f"unsupported schema: {schema}")
    steps = trace.get("steps")
    if not isinstance(steps, list) or not steps:
        problems.append("trace must include a non-empty 'steps' list")
        return problems
    for idx, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            problems.append(f"step #{idx} must be an object")
            continue
        action = step.get("action")
        if not isinstance(action, dict) or not action.get("kind"):
            problems.append(f"step #{idx} is missing action.kind")
            continue
        kind = str(action["kind"])
        if kind not in SUPPORTED_ACTIONS:
            problems.append(f"step #{idx} has unsupported action kind: {kind}")
        if kind == "launchApp" and not action.get("appId"):
            problems.append(f"step #{idx} launchApp is missing appId")
    return problems


def _step_commands(step: Dict[str, Any], params: Dict[str, Any], default_app_id: str) -> List[Dict[str, Any]]:
    action = step.get("action") or {}
    kind = action.get("kind")
    cmds: List[Dict[str, Any]] = []

    if kind == "launchApp":
        body: Dict[str, Any] = {"appId": action.get("appId") or default_app_id}
        if "clearState" in action:
            body["clearState"] = bool(action["clearState"])
        cmds.append({"launchApp": body})
    elif kind in ("tapOn", "longPressOn", "doubleTapOn"):
        target = action.get("target")
        body = _format_target(target) if isinstance(target, dict) else {"text": str(target or "")}
        if action.get("at") and "point" not in body:
            at = action["at"]
            if isinstance(at, dict) and "x" in at and "y" in at:
                body["point"] = f"{at['x']},{at['y']}"
            elif isinstance(at, str):
                body["point"] = at
        cmds.append({kind: body})
    elif kind == "inputText":
        cmds.append({"inputText": _resolve_param(action.get("text"), params)})
    elif kind == "eraseText":
        chars = action.get("characters")
        if chars is not None:
            cmds.append({"eraseText": int(chars)})
        else:
            cmds.append({"eraseText": 50})
    elif kind == "swipe":
        body = {}
        if action.get("direction"):
            body["direction"] = str(action["direction"]).upper()
        for key in ("start", "end"):
            if action.get(key) is not None:
                body[key] = action[key]
        if action.get("duration") is not None:
            body["duration"] = int(action["duration"])
        cmds.append({"swipe": body})
    elif kind == "pressKey":
        cmds.append({"pressKey": str(action.get("key") or "")})
    elif kind in ("scroll", "back", "hideKeyboard", "waitForAnimationToEnd"):
        cmds.append(kind)

    for cp in step.get("checkpoints") or []:
        cmds.extend(_checkpoint_commands(cp))
    return cmds


def _dump_command(cmd: Any) -> List[str]:
    if isinstance(cmd, str):
        return [f"- {cmd}"]
    if not isinstance(cmd, dict) or len(cmd) != 1:
        return []
    key, value = next(iter(cmd.items()))
    if not isinstance(value, dict):
        return [f"- {key}: {_yaml_scalar(value)}"]
    lines = [f"- {key}:"]
    for sub_k, sub_v in value.items():
        if sub_v is None or sub_v == "":
            continue
        lines.append(f"    {sub_k}: {_yaml_scalar(sub_v)}")
    return lines


def compile_trace(
    trace: Dict[str, Any],
    params: Optional[Dict[str, Any]] = None,
    app_id: Optional[str] = None,
) -> str:
    """Compile a `trace.json` dict into a Maestro YAML string."""
    problems = validate_trace(trace)
    if problems:
        raise ValueError("; ".join(problems))

    resolved_params = dict(params or {})
    steps = trace.get("steps") or []
    inferred_app = app_id or ""
    if not inferred_app:
        for step in steps:
            act = step.get("action") or {}
            if act.get("kind") == "launchApp" and act.get("appId"):
                inferred_app = str(act["appId"])
                break

    lines = [
        f"# Compiled natively from trace {trace.get('id', 'unknown')}",
        f"appId: {_yaml_scalar(inferred_app or 'com.example.app')}",
    ]
    env_names = [p.get("name") for p in (trace.get("params") or []) if isinstance(p, dict) and p.get("name")]
    if env_names and not resolved_params:
        lines.append("env:")
        for name in env_names:
            lines.append(f"  {name}: {_yaml_scalar('${ECR_PARAM_' + name + '}')}")

    lines.append("---")
    for step in steps:
        for cmd in _step_commands(step, resolved_params, inferred_app or "com.example.app"):
            lines.extend(_dump_command(cmd))
    return "\n".join(lines) + "\n"


def compile_trace_file(
    trace_path: Path,
    out_dir: Optional[Path] = None,
    params_path: Optional[Path] = None,
) -> Tuple[Path, str]:
    """Read `trace_path` (+ optional `trace.params.json`) and write `<trace id>.flow.yaml`."""
    trace_file = Path(trace_path)
    trace = json.loads(trace_file.read_text(encoding="utf-8"))
    p_file = Path(params_path) if params_path else trace_file.with_name("trace.params.json")
    params: Dict[str, Any] = {}
    if p_file.is_file():
        try:
            loaded = json.loads(p_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                params = loaded
        except ValueError:
            params = {}

    yaml_text = compile_trace(trace, params=params)
    target_dir = Path(out_dir) if out_dir else trace_file.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    trace_id = str(trace.get("id") or "trace").replace("/", "_")
    flow_path = target_dir / f"{trace_id}.flow.yaml"
    flow_path.write_text(yaml_text, encoding="utf-8")
    return flow_path, yaml_text


def main(argv=None):
    parser = argparse.ArgumentParser(description="Compile trace.json natively to Maestro YAML.")
    parser.add_argument("trace", help="Path to trace.json or results directory containing trace.json.")
    parser.add_argument("--out", help="Output directory for <id>.flow.yaml.")
    parser.add_argument("--params", help="Optional trace.params.json path.")
    parser.add_argument("--validate-only", action="store_true", help="Only validate trace.json without writing YAML.")
    args = parser.parse_args(argv)

    trace_path = Path(args.trace)
    if trace_path.is_dir():
        trace_path = trace_path / "trace.json"
    if not trace_path.is_file():
        print(f"error: trace not found at {trace_path}", file=sys.stderr)
        return 1

    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    problems = validate_trace(trace)
    if problems:
        for problem in problems:
            print(f"invalid: {problem}", file=sys.stderr)
        return 1
    if args.validate_only:
        print(f"valid: {trace_path}")
        return 0

    flow_path, _ = compile_trace_file(
        trace_path,
        out_dir=Path(args.out) if args.out else None,
        params_path=Path(args.params) if args.params else None,
    )
    print(f"compiled: {flow_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

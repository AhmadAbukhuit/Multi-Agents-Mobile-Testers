"""Shared Pydantic models and JSON schemas for Multi-Agents-Mobile-Testers."""
from typing import List

try:
    from pydantic import BaseModel, Field  # type: ignore

    HAS_PYDANTIC = True
except ImportError:  # pragma: no cover - stdlib fallback
    BaseModel = object  # type: ignore
    Field = None  # type: ignore
    HAS_PYDANTIC = False

VALID_SEVERITIES = ("blocker", "major", "minor", "cosmetic")

FINAL_FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 1, "maximum": 10},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "severity": {"type": "string", "enum": ["blocker", "major", "minor", "cosmetic"]},
                    "screen": {"type": "string"},
                    "screens": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "severity"],
            },
        },
    },
    "required": ["score", "issues"],
}

GROUPED_REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "class": {"type": "string"},
                    "ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["title", "class", "ids"],
            },
        }
    },
    "required": ["groups"],
}

if HAS_PYDANTIC:
    class ObservedIssue(BaseModel):
        title: str = Field(description="Short bug title")
        severity: str = Field(description="blocker, major, minor, or cosmetic")
        screen: str = Field(default="", description="Screen name")
        screens: List[str] = Field(default_factory=list, description="Screenshot numbers")

    class FinalFindings(BaseModel):
        score: int = Field(ge=1, le=10, description="Persona score out of 10")
        issues: List[ObservedIssue] = Field(default_factory=list)
else:  # pragma: no cover - stdlib fallback
    ObservedIssue = None  # type: ignore
    FinalFindings = None  # type: ignore


def validate_findings_json(data):
    """Validate and normalize findings.json content against FINAL_FINDINGS_SCHEMA."""
    if not isinstance(data, dict):
        raise ValueError("findings.json must be a JSON object")
    out = dict(data)
    raw_score = out.get("score")
    if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool):
        out["score"] = max(1, min(10, int(round(raw_score))))
    elif "score" not in out:
        out["score"] = 5

    raw_issues = out.get("issues")
    if not isinstance(raw_issues, list):
        raw_issues = []
    normalized_issues = []
    for item in raw_issues:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        sev = str(item.get("severity") or "minor").strip().lower()
        if sev not in VALID_SEVERITIES:
            sev = "minor"
        screen = str(item.get("screen") or "").strip()
        screens = item.get("screens")
        clean_screens = [str(s) for s in screens] if isinstance(screens, list) else []
        normalized = dict(item)
        normalized.update({"title": title, "severity": sev, "screen": screen, "screens": clean_screens})
        normalized_issues.append(normalized)
    out["issues"] = normalized_issues
    if HAS_PYDANTIC and FinalFindings is not None:
        FinalFindings(**{"score": out["score"], "issues": out["issues"]})
    return out


CODE_AGENT_IDS = (
    "stack-manifest-analyst",
    "navigation-screen-mapper",
    "interaction-form-extractor",
    "state-api-flow-analyst",
    "qa-strategy-synthesizer",
)

CODE_CONTEXT_SCHEMA = {
    "type": "object",
    "properties": {
        "schema": {"type": "string"},
        "app_dir": {"type": "string"},
        "agents": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "role": {"type": "string"},
                    "summary": {"type": "string"},
                    "findings": {"type": "object"},
                },
                "required": ["id", "role", "summary", "findings"],
            },
        },
        "app_metadata": {"type": "object"},
        "screens": {"type": "array"},
        "forms_and_inputs": {"type": "array"},
        "state_and_auth": {"type": "object"},
        "persona_playbooks": {"type": "object"},
    },
    "required": [
        "schema",
        "app_dir",
        "agents",
        "app_metadata",
        "screens",
        "forms_and_inputs",
        "state_and_auth",
        "persona_playbooks",
    ],
}


def validate_code_context_json(data):
    """Validate and normalize the 5-agent code_context.json payload."""
    if not isinstance(data, dict):
        raise ValueError("code_context.json must be a JSON object")
    out = dict(data)
    out["schema"] = str(out.get("schema") or "mobile-agent-testers.code_context/1")
    out["app_dir"] = str(out.get("app_dir") or "")

    raw_agents = out.get("agents")
    if not isinstance(raw_agents, list):
        raise ValueError("code_context.json requires an 'agents' list")
    normalized_agents = []
    seen_ids = set()
    for item in raw_agents:
        if not isinstance(item, dict):
            continue
        aid = str(item.get("id") or "").strip()
        if not aid:
            continue
        seen_ids.add(aid)
        normalized_agents.append({
            "id": aid,
            "role": str(item.get("role") or aid),
            "summary": str(item.get("summary") or ""),
            "findings": item.get("findings") if isinstance(item.get("findings"), dict) else {},
        })
    missing = [aid for aid in CODE_AGENT_IDS if aid not in seen_ids]
    if missing:
        raise ValueError(f"code_context.json missing required agents: {', '.join(missing)}")
    out["agents"] = normalized_agents

    meta = out.get("app_metadata") if isinstance(out.get("app_metadata"), dict) else {}
    out["app_metadata"] = {
        "name": str(meta.get("name") or ""),
        "framework": str(meta.get("framework") or "react-native"),
        "app_id": str(meta.get("app_id") or ""),
        "entry_files": list(meta.get("entry_files") or []),
        "permissions": list(meta.get("permissions") or []),
        "dependencies": list(meta.get("dependencies") or []),
    }
    out["screens"] = list(out.get("screens") or [])
    out["forms_and_inputs"] = list(out.get("forms_and_inputs") or [])

    sa = out.get("state_and_auth") if isinstance(out.get("state_and_auth"), dict) else {}
    out["state_and_auth"] = {
        "auth_required": bool(sa.get("auth_required", False)),
        "state_libraries": list(sa.get("state_libraries") or []),
        "storage_keys": list(sa.get("storage_keys") or []),
        "api_endpoints": list(sa.get("api_endpoints") or []),
        "test_credentials": dict(sa.get("test_credentials") or {}),
        "user_flows": list(sa.get("user_flows") or []),
    }

    playbooks = out.get("persona_playbooks") if isinstance(out.get("persona_playbooks"), dict) else {}
    normalized_playbooks = {}
    for pid, pb in playbooks.items():
        if not isinstance(pb, dict):
            continue
        normalized_playbooks[str(pid)] = {
            "persona_id": str(pb.get("persona_id") or pid),
            "platform": str(pb.get("platform") or "android"),
            "focus_screens": list(pb.get("focus_screens") or []),
            "recommended_actions": list(pb.get("recommended_actions") or []),
            "watch_outs": list(pb.get("watch_outs") or []),
            "sample_inputs": dict(pb.get("sample_inputs") or {}),
        }
    out["persona_playbooks"] = normalized_playbooks
    return out

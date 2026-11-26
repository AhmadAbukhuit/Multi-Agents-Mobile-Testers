#!/usr/bin/env python3
"""5 Code-Understanding Agents that inspect a package.json mobile app folder
and synthesize a test guide for the 20 UI Persona Tester Agents.

Usage:
    python3 core/code_intel/analyze_project.py --app-dir <project_dir>
        [--personas config/personas.json] [--out results] [--app-id <override>]
        [--use-llm] [--print-app-id]
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT_DIR / "services" / "agents_api"))
from schemas import validate_code_context_json  # noqa: E402

SKIP_DIRS = {
    "node_modules",
    ".git",
    ".expo",
    "build",
    "dist",
    "Pods",
    ".gradle",
    "__pycache__",
    ".pytest_cache",
}
SOURCE_EXTS = {".js", ".jsx", ".ts", ".tsx"}

NAV_SCREEN_RE = re.compile(
    r"<(?:[A-Za-z0-9_]+\.)?Screen\b[^>]*\bname\s*=\s*[\"']([^\"']+)[\"']([^>]*)>",
    re.S,
)
NAV_COMPONENT_RE = re.compile(r"\bcomponent\s*=\s*\{([A-Za-z0-9_]+)\}")
NAV_CALL_RE = re.compile(r"\bnavigation\.(?:navigate|push|replace)\(\s*[\"']([^\"']+)[\"']")

TEST_ID_RE = re.compile(r"\btestID\s*=\s*[\"']([^\"']+)[\"']")
A11Y_LABEL_RE = re.compile(r"\baccessibilityLabel\s*=\s*[\"']([^\"']+)[\"']")
PLACEHOLDER_RE = re.compile(r"\bplaceholder\s*=\s*[\"']([^\"']+)[\"']")
BUTTON_TITLE_RE = re.compile(r"<Button\b[^>]*\btitle\s*=\s*[\"']([^\"']+)[\"']")
PRESSABLE_TEXT_RE = re.compile(
    r"<(?:Pressable|TouchableOpacity|TouchableHighlight|Button)\b[^>]*>.*?"
    r"<Text\b[^>]*>([^<{]+)</Text>.*?</(?:Pressable|TouchableOpacity|TouchableHighlight|Button)>",
    re.S,
)
TEXT_INPUT_BLOCK_RE = re.compile(r"<TextInput\b([^>]*)/?>", re.S)
SWITCH_RE = re.compile(r"<Switch\b([^>]*)/?>", re.S)

STORAGE_KEY_RE = re.compile(
    r"(?:AsyncStorage|SecureStore)\.(?:getItem|setItem|removeItem)\(\s*[\"']([^\"']+)[\"']"
)
FETCH_URL_RE = re.compile(r"\bfetch\(\s*[\"'`]([^\"'`$]+)")
AXIOS_URL_RE = re.compile(r"\baxios\.(?:get|post|put|patch|delete)\(\s*[\"'`]([^\"'`$]+)")


def _iter_source_files(app_dir: Path) -> List[Path]:
    files: List[Path] = []
    if not app_dir.is_dir():
        return files
    for dirpath, dirnames, filenames in os.walk(app_dir):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for fname in sorted(filenames):
            p = Path(dirpath) / fname
            if p.suffix.lower() in SOURCE_EXTS:
                files.append(p)
    return files


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _rel(path: Path, app_dir: Path) -> str:
    try:
        return str(path.resolve().relative_to(app_dir.resolve()))
    except ValueError:
        return str(path)


def _unique(seq: List[str]) -> List[str]:
    seen = set()
    out = []
    for item in seq:
        val = str(item or "").strip()
        if val and val not in seen:
            seen.add(val)
            out.append(val)
    return out


# ---------------- Agent 1: Stack & Manifest Analyst ----------------

def run_stack_manifest_analyst(app_dir: Path) -> Dict[str, Any]:
    """Agent 1: Inspects package.json, app.json, build.gradle, and manifests."""
    pkg_path = app_dir / "package.json"
    pkg: Dict[str, Any] = {}
    if pkg_path.is_file():
        try:
            pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pkg = {}

    deps: Dict[str, Any] = {}
    for key in ("dependencies", "devDependencies"):
        if isinstance(pkg.get(key), dict):
            deps.update(pkg[key])

    framework = "native"
    if "expo" in deps or (app_dir / "app.json").is_file():
        framework = "expo"
    if "react-native" in deps and "expo" not in deps:
        framework = "react-native"
    elif "@capacitor/core" in deps:
        framework = "capacitor"
    elif (app_dir / "pubspec.yaml").is_file():
        framework = "flutter"

    app_id = ""
    app_name = str(pkg.get("name") or app_dir.name)
    permissions: List[str] = []

    app_json_path = app_dir / "app.json"
    if app_json_path.is_file():
        try:
            app_json = json.loads(app_json_path.read_text(encoding="utf-8"))
            expo_cfg = app_json.get("expo") if isinstance(app_json.get("expo"), dict) else app_json
            if isinstance(expo_cfg, dict):
                app_name = str(expo_cfg.get("name") or app_name)
                android_cfg = expo_cfg.get("android") if isinstance(expo_cfg.get("android"), dict) else {}
                ios_cfg = expo_cfg.get("ios") if isinstance(expo_cfg.get("ios"), dict) else {}
                app_id = str(android_cfg.get("package") or ios_cfg.get("bundleIdentifier") or app_id)
                for perm in android_cfg.get("permissions") or []:
                    permissions.append(str(perm))
        except (OSError, ValueError):
            pass

    for gradle_rel in ("android/app/build.gradle", "android/app/build.gradle.kts"):
        g_path = app_dir / gradle_rel
        if g_path.is_file() and not app_id:
            txt = _read_text(g_path)
            m = re.search(r"applicationId\s*=?\s*[\"']([^\"']+)[\"']", txt)
            if m:
                app_id = m.group(1).strip()

    manifest_path = app_dir / "android/app/src/main/AndroidManifest.xml"
    if manifest_path.is_file():
        m_txt = _read_text(manifest_path)
        if not app_id:
            pm = re.search(r'\bpackage\s*=\s*["\']([^"\']+)["\']', m_txt)
            if pm:
                app_id = pm.group(1).strip()
        for perm_match in re.findall(r'uses-permission[^>]*android:name\s*=\s*["\']([^"\']+)["\']', m_txt):
            permissions.append(perm_match.rsplit(".", 1)[-1])

    entry_files = [
        rel
        for rel in ("package.json", "App.tsx", "App.jsx", "App.js", "index.js", "index.ts", "app/_layout.tsx")
        if (app_dir / rel).is_file()
    ]

    findings = {
        "name": app_name,
        "framework": framework,
        "app_id": app_id or f"com.example.{re.sub(r'[^a-z0-9]', '', app_name.lower()) or 'app'}",
        "entry_files": entry_files,
        "permissions": _unique(permissions),
        "dependencies": sorted(deps.keys())[:40],
        "scripts": list((pkg.get("scripts") or {}).keys()) if isinstance(pkg.get("scripts"), dict) else [],
        "has_package_json": pkg_path.is_file(),
    }
    summary = (
        f"Detected {framework} project '{app_name}' (appId={findings['app_id']}) with "
        f"{len(findings['dependencies'])} dependencies and {len(entry_files)} entry files."
    )
    return {
        "id": "stack-manifest-analyst",
        "role": "Stack, Config & Manifest Analyst",
        "summary": summary,
        "findings": findings,
    }


# ---------------- Agent 2: Navigation & Screen Mapper ----------------

def run_navigation_screen_mapper(app_dir: Path, stack_agent: Dict[str, Any]) -> Dict[str, Any]:
    """Agent 2: Discovers screens, navigation routes, and transitions."""
    src_files = _iter_source_files(app_dir)
    screens_map: Dict[str, Dict[str, Any]] = {}
    nav_targets_all: List[str] = []

    def _register(name: str, file_path: Path, route: str = ""):
        key = name.strip()
        if not key:
            return
        entry = screens_map.setdefault(
            key,
            {
                "name": key,
                "file": _rel(file_path, app_dir),
                "route": route or key,
                "elements": [],
                "navigation_targets": [],
            },
        )
        if route and not entry["route"]:
            entry["route"] = route

    # 1. Expo Router file-based routing under app/
    expo_app_dir = app_dir / "app"
    if expo_app_dir.is_dir():
        for p in _iter_source_files(expo_app_dir):
            if p.stem.startswith("_"):
                continue
            rel_route = "/" + _rel(p, expo_app_dir).rsplit(".", 1)[0]
            name = p.stem.replace("[", "").replace("]", "").title() + "Screen"
            _register(name, p, route=rel_route)

    # 2. React Navigation JSX declarations & navigation.navigate(...) calls
    for p in src_files:
        txt = _read_text(p)
        targets = _unique(NAV_CALL_RE.findall(txt))
        nav_targets_all.extend(targets)

        for route_name, attrs in NAV_SCREEN_RE.findall(txt):
            comp_m = NAV_COMPONENT_RE.search(attrs)
            comp_name = comp_m.group(1) if comp_m else route_name
            _register(route_name, p, route=route_name)
            screens_map[route_name]["navigation_targets"] = _unique(
                screens_map[route_name]["navigation_targets"] + targets
            )
            if comp_name and comp_name != route_name:
                screens_map[route_name].setdefault("component", comp_name)

        if "screen" in p.stem.lower() or "screens" in p.parts:
            _register(p.stem, p, route=p.stem.replace("Screen", ""))
            screens_map[p.stem]["navigation_targets"] = _unique(
                screens_map[p.stem]["navigation_targets"] + targets
            )

    if not screens_map:
        for candidate in ("App.tsx", "App.jsx", "App.js"):
            p = app_dir / candidate
            if p.is_file():
                _register("MainScreen", p, route="Main")
                screens_map["MainScreen"]["navigation_targets"] = _unique(NAV_CALL_RE.findall(_read_text(p)))
                break

    screens = list(screens_map.values())
    summary = (
        f"Mapped {len(screens)} screen(s) across {len(src_files)} source file(s)"
        + (f" targeting routes: {', '.join(_unique(nav_targets_all)[:6])}." if nav_targets_all else ".")
    )
    return {
        "id": "navigation-screen-mapper",
        "role": "Navigation & Screen Cartographer",
        "summary": summary,
        "findings": {
            "screens": screens,
            "source_file_count": len(src_files),
            "routes": _unique([s["route"] for s in screens] + nav_targets_all),
        },
    }


# ---------------- Agent 3: Form, Validation & testID Extractor ----------------

def _infer_validation_rules(txt: str) -> List[str]:
    rules: List[str] = []
    lower = txt.lower()
    if "includes('@')" in txt or "email" in lower:
        rules.append("Email format validation detected")
    if "minlength" in lower or ".length <" in txt or ".length >" in txt:
        rules.append("String length constraint detected")
    if "required" in lower or "trim()" in txt:
        rules.append("Non-empty required field check detected")
    if "numeric" in lower or "keyboardtype=\"numeric\"" in lower or "keyboardtype='numeric'" in lower:
        rules.append("Numeric input constraint detected")
    if "securetextentry" in lower or "password" in lower:
        rules.append("Masked password entry detected")
    return _unique(rules)


def _sample_inputs_for(fields: List[str], rules: List[str]) -> Dict[str, Any]:
    joined = " ".join(fields + rules).lower()
    sample: Dict[str, Any] = {"primary": "Test Item 101"}
    if "email" in joined:
        sample["email"] = "qa.tester@example.com"
    if "password" in joined:
        sample["password"] = "QaPass!2026"
    if "name" in joined:
        sample["name"] = "Alex QA"
    sample["unicode_edge"] = "İbrahim Şahin ğüş 🚀"
    sample["long_text"] = "Long accessibility label check " * 4
    return sample


def run_interaction_form_extractor(app_dir: Path, nav_agent: Dict[str, Any]) -> Dict[str, Any]:
    """Agent 3: Extracts TextInputs, Pressables/Buttons, testIDs, and validation rules."""
    src_files = _iter_source_files(app_dir)
    screen_by_file = {s["file"]: s["name"] for s in (nav_agent.get("findings", {}).get("screens") or [])}

    forms_and_inputs: List[Dict[str, Any]] = []
    all_test_ids: List[str] = []
    all_buttons: List[str] = []
    all_fields: List[str] = []

    for p in src_files:
        txt = _read_text(p)
        rel_path = _rel(p, app_dir)
        test_ids = _unique(TEST_ID_RE.findall(txt))
        labels = _unique(A11Y_LABEL_RE.findall(txt))
        placeholders = _unique(PLACEHOLDER_RE.findall(txt))

        ti_fields: List[str] = []
        for attrs in TEXT_INPUT_BLOCK_RE.findall(txt):
            pm = PLACEHOLDER_RE.search(attrs)
            tm = TEST_ID_RE.search(attrs)
            lm = A11Y_LABEL_RE.search(attrs)
            ti_fields.append((pm and pm.group(1)) or (lm and lm.group(1)) or (tm and tm.group(1)) or "TextInput")
        fields = _unique(ti_fields + placeholders)

        buttons = _unique(
            [b.strip() for b in BUTTON_TITLE_RE.findall(txt)]
            + [b.strip() for b in PRESSABLE_TEXT_RE.findall(txt)]
        )
        switches = len(SWITCH_RE.findall(txt))
        rules = _infer_validation_rules(txt)

        if not (fields or buttons or test_ids or labels or switches):
            continue

        screen_name = screen_by_file.get(rel_path, p.stem)
        entry = {
            "screen": screen_name,
            "file": rel_path,
            "fields": fields,
            "buttons": buttons,
            "test_ids": test_ids,
            "accessibility_labels": labels,
            "switches": switches,
            "validation_rules": rules,
            "sample_inputs": _sample_inputs_for(fields, rules),
        }
        forms_and_inputs.append(entry)
        all_test_ids.extend(test_ids)
        all_buttons.extend(buttons)
        all_fields.extend(fields)

        for scr in nav_agent.get("findings", {}).get("screens") or []:
            if scr["file"] == rel_path:
                scr["elements"] = _unique(fields + buttons + test_ids + labels)[:25]

    summary = (
        f"Extracted {len(all_fields)} input field(s), {len(all_buttons)} button(s), and "
        f"{len(_unique(all_test_ids))} testID selector(s) across {len(forms_and_inputs)} UI file(s)."
    )
    return {
        "id": "interaction-form-extractor",
        "role": "Component, Form & Selector Analyst",
        "summary": summary,
        "findings": {
            "forms_and_inputs": forms_and_inputs,
            "total_fields": len(_unique(all_fields)),
            "total_buttons": len(_unique(all_buttons)),
            "total_test_ids": len(_unique(all_test_ids)),
        },
    }


# ---------------- Agent 4: State, Auth & API Flow Analyst ----------------

def run_state_api_flow_analyst(
    app_dir: Path,
    stack_agent: Dict[str, Any],
    forms_agent: Dict[str, Any],
) -> Dict[str, Any]:
    """Agent 4: Identifies auth gates, state stores, persistence keys, and end-to-end user flows."""
    src_files = _iter_source_files(app_dir)
    deps = set(stack_agent.get("findings", {}).get("dependencies") or [])

    state_libs: List[str] = []
    for lib in ("zustand", "@reduxjs/toolkit", "redux", "mobx", "recoil", "jotai", "@tanstack/react-query"):
        if lib in deps:
            state_libs.append(lib)

    storage_keys: List[str] = []
    api_endpoints: List[str] = []
    auth_markers = 0
    cred_matches: Dict[str, str] = {}

    for p in src_files:
        txt = _read_text(p)
        if "useState" in txt or "createContext" in txt or "useReducer" in txt:
            state_libs.append("React hooks/Context")
        storage_keys.extend(STORAGE_KEY_RE.findall(txt))
        api_endpoints.extend(FETCH_URL_RE.findall(txt))
        api_endpoints.extend(AXIOS_URL_RE.findall(txt))
        if re.search(r"\b(signIn|login|authenticate|authToken|isAuthenticated|useAuth)\b", txt, re.I):
            auth_markers += 1
        em = re.search(r'(?:demo|default|test)Email\s*[:=]\s*["\']([^"\']+)["\']', txt, re.I)
        if em:
            cred_matches["email"] = em.group(1)
        pw = re.search(r'(?:demo|default|test)Password\s*[:=]\s*["\']([^"\']+)["\']', txt, re.I)
        if pw:
            cred_matches["password"] = pw.group(1)

    auth_required = auth_markers > 0
    forms = forms_agent.get("findings", {}).get("forms_and_inputs") or []
    user_flows: List[str] = []
    if auth_required:
        user_flows.append("Auth Gate Flow: Launch app -> locate Login/Sign-In inputs -> submit valid/demo credentials -> verify main screen loads.")
    if forms:
        first = forms[0]
        btn_hint = ", ".join(first["buttons"][:3]) if first["buttons"] else "primary action"
        fld_hint = ", ".join(first["fields"][:3]) if first["fields"] else "visible form inputs"
        user_flows.append(
            f"Primary Interaction Flow ({first['screen']}): Fill [{fld_hint}] -> tap [{btn_hint}] -> confirm UI updates without errors."
        )
    user_flows.append("Navigation & State Persistence Flow: Move across primary screens/tabs, background or reopen app, and verify state remains consistent.")

    findings = {
        "auth_required": auth_required,
        "state_libraries": _unique(state_libs) or ["React local state"],
        "storage_keys": _unique(storage_keys),
        "api_endpoints": _unique(api_endpoints),
        "test_credentials": cred_matches,
        "user_flows": user_flows,
    }
    summary = (
        f"State via {', '.join(findings['state_libraries'])}; auth_required={auth_required}; "
        f"{len(findings['storage_keys'])} storage key(s), {len(findings['api_endpoints'])} API endpoint(s)."
    )
    return {
        "id": "state-api-flow-analyst",
        "role": "State, Auth & Business Logic Analyst",
        "summary": summary,
        "findings": findings,
    }


# ---------------- Agent 5: QA Strategy Synthesizer ----------------

def _maybe_ollama_strategy(host: str, model: str, digest: Dict[str, Any]) -> Optional[str]:
    if not host or not model:
        return None
    prompt = (
        "Summarize in 4 concise bullet points what QA testers should prioritize when testing this compiled mobile APK:\n"
        + json.dumps(digest, ensure_ascii=False)[:4000]
    )
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0.2, "num_ctx": 8192},
    }
    req = urllib.request.Request(
        f"{host.rstrip('/')}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return str((data.get("message") or {}).get("content") or "").strip() or None
    except (OSError, urllib.error.URLError, ValueError):
        return None


def _build_persona_playbook(
    persona: Dict[str, Any],
    screens: List[Dict[str, Any]],
    forms: List[Dict[str, Any]],
    state_auth: Dict[str, Any],
) -> Dict[str, Any]:
    pid = str(persona.get("id") or "tester")
    platform = str(persona.get("platform") or "android")
    locale = str(persona.get("locale") or "en-US")
    device = persona.get("device") if isinstance(persona.get("device"), dict) else {}
    profile_goal = f"{persona.get('profile', '')} {persona.get('goal', '')}".lower()

    focus_screens = [s["name"] for s in screens[:4]] or ["MainScreen"]
    all_buttons = _unique([b for f in forms for b in f.get("buttons", [])])
    all_fields = _unique([fld for f in forms for fld in f.get("fields", [])])

    actions: List[str] = []
    if state_auth.get("auth_required"):
        creds = state_auth.get("test_credentials") or {}
        if creds:
            actions.append(f"Pass login gate using discovered demo credentials ({creds}).")
        else:
            actions.append("Inspect authentication/onboarding screens and test sign-in/skip paths.")
    if all_fields:
        actions.append(f"Enter test values into code-discovered fields: {', '.join(all_fields[:4])}.")
    if all_buttons:
        actions.append(f"Exercise primary controls discovered in source code: {', '.join(all_buttons[:5])}.")
    if not actions:
        actions.append(f"Explore {', '.join(focus_screens)} and fulfill goal: {persona.get('goal', '')}")

    watch_outs: List[str] = []
    sample_inputs: Dict[str, Any] = {"primary": "Sample QA entry"}
    for f in forms[:1]:
        sample_inputs = dict(f.get("sample_inputs") or sample_inputs)

    font_scale = float(device.get("font_scale") or 1.0)
    text_size = str(device.get("text_size") or "")
    if font_scale >= 1.3 or "accessibility" in text_size or "extra" in text_size:
        watch_outs.append("Verify large accessibility text does not clip labels or overlap buttons inside fixed containers.")
    if locale.startswith("ar"):
        watch_outs.append("Verify RTL mirroring (text alignment, back arrows, drawers, and numeric ordering) on every screen.")
    elif not locale.startswith("en"):
        watch_outs.append(f"Check for untranslated hardcoded English strings in {locale} locale and text truncation.")
    if "dark" in str(device.get("theme") or "").lower():
        watch_outs.append("Check dark-mode contrast for hardcoded light backgrounds, borders, and placeholder text.")
    if "tablet" in str(device.get("profile") or "").lower() or "ipad" in str(device.get("simulator") or "").lower():
        watch_outs.append("Check tablet responsive layout usage (no stretched single-column forms or clipped panes).")
    if "turkish" in profile_goal or "odd input" in profile_goal or "break it" in profile_goal:
        watch_outs.append("Fuzz TextInputs with Turkish characters (İ, ş, ğ), emojis, and very long strings to trigger crashes or overflow.")
        sample_inputs["fuzz"] = "İbrahim Şahin ğüş 🚀 " + ("X" * 60)
    if not watch_outs:
        watch_outs.append("Check navigation back-stack behavior, tap target feedback, and error handling after submitting forms.")

    return {
        "persona_id": pid,
        "platform": platform,
        "focus_screens": focus_screens,
        "recommended_actions": actions,
        "watch_outs": watch_outs,
        "sample_inputs": sample_inputs,
    }


def run_qa_strategy_synthesizer(
    app_dir: Path,
    personas: List[Dict[str, Any]],
    a1: Dict[str, Any],
    a2: Dict[str, Any],
    a3: Dict[str, Any],
    a4: Dict[str, Any],
    ollama_host: Optional[str] = None,
    model: Optional[str] = None,
    use_llm: bool = False,
) -> Dict[str, Any]:
    """Agent 5: Synthesizes code findings into test playbooks for all 20 persona testers."""
    screens = a2.get("findings", {}).get("screens") or []
    forms = a3.get("findings", {}).get("forms_and_inputs") or []
    state_auth = a4.get("findings") or {}

    playbooks: Dict[str, Dict[str, Any]] = {}
    for persona in personas:
        if isinstance(persona, dict) and persona.get("id"):
            pb = _build_persona_playbook(persona, screens, forms, state_auth)
            playbooks[pb["persona_id"]] = pb

    llm_notes = None
    if use_llm and ollama_host and model:
        llm_notes = _maybe_ollama_strategy(
            ollama_host,
            model,
            {
                "app": a1.get("findings"),
                "screens": [s["name"] for s in screens],
                "forms": forms[:6],
                "flows": state_auth.get("user_flows"),
            },
        )

    summary = (
        f"Synthesized tailored code-aware playbooks for {len(playbooks)} persona agent(s) across "
        f"{len(screens)} screen(s) and {len(forms)} interactive component file(s)."
    )
    return {
        "id": "qa-strategy-synthesizer",
        "role": "Lead QA Playbook & 20-Persona Coordinator",
        "summary": summary,
        "findings": {
            "persona_count": len(playbooks),
            "persona_playbooks": playbooks,
            "llm_notes": llm_notes or "",
        },
    }


def render_test_guide_markdown(context: Dict[str, Any]) -> str:
    """Render a concise CODE_TEST_GUIDE.md consumed by the 20 UI tester agents."""
    meta = context.get("app_metadata") or {}
    screens = context.get("screens") or []
    forms = context.get("forms_and_inputs") or []
    sa = context.get("state_and_auth") or {}
    agents = context.get("agents") or []
    playbooks = context.get("persona_playbooks") or {}

    lines = [
        f"# Code-Aware QA Testing Guide — {meta.get('name') or 'Mobile App'}",
        "",
        f"- **Framework:** `{meta.get('framework', 'react-native')}`",
        f"- **App ID:** `{meta.get('app_id', '')}`",
        f"- **Source Directory:** `{context.get('app_dir', '')}`",
        "",
        "## 1. Findings from the 5 Code-Understanding Agents",
        "",
    ]
    for idx, agent in enumerate(agents, start=1):
        lines.append(f"{idx}. **{agent.get('role')}** (`{agent.get('id')}`): {agent.get('summary')}")

    lines.extend(["", "## 2. Screens & Routes Discovered in Code", ""])
    if screens:
        for scr in screens:
            els = ", ".join(scr.get("elements") or [])
            targets = ", ".join(scr.get("navigation_targets") or [])
            lines.append(
                f"- **{scr.get('name')}** (`{scr.get('file')}` · route `{scr.get('route')}`)"
                + (f" — elements: {els}" if els else "")
                + (f" — navigates to: {targets}" if targets else "")
            )
    else:
        lines.append("- No explicit screen modules detected; start exploration from the launcher screen.")

    lines.extend(["", "## 3. Forms, Selectors & Sample Inputs", ""])
    if forms:
        for fm in forms[:12]:
            lines.append(
                f"- **{fm.get('screen')}** (`{fm.get('file')}`): fields={fm.get('fields')}, "
                f"buttons={fm.get('buttons')}, testIDs={fm.get('test_ids')}, rules={fm.get('validation_rules')}"
            )
    else:
        lines.append("- No TextInput/form blocks extracted statically; inspect live UI controls via Set-of-Mark.")

    lines.extend(["", "## 4. End-to-End Flows & State", ""])
    for flow in sa.get("user_flows") or []:
        lines.append(f"- {flow}")
    if sa.get("test_credentials"):
        lines.append(f"- **Detected Demo Credentials:** `{json.dumps(sa['test_credentials'])}`")

    lines.extend(["", "## 5. Persona Playbooks Summary", ""])
    for pid, pb in playbooks.items():
        lines.append(
            f"- **{pid}** ({pb.get('platform')}): focus={pb.get('focus_screens')} | "
            f"actions={'; '.join(pb.get('recommended_actions') or [])} | "
            f"watch={'; '.join(pb.get('watch_outs') or [])}"
        )
    return "\n".join(lines) + "\n"


def _load_personas(personas_path: Path) -> List[Dict[str, Any]]:
    try:
        loaded = json.loads(personas_path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, list) else []
    except (OSError, ValueError):
        return []


def analyze_project(
    app_dir: Any,
    personas_path: Any = ROOT_DIR / "config" / "personas.json",
    out_dir: Any = ROOT_DIR / "results",
    app_id_override: str = "",
    ollama_host: Optional[str] = None,
    model: Optional[str] = None,
    use_llm: bool = False,
) -> Dict[str, Any]:
    """Run all 5 Code-Understanding Agents and write code_context.json + CODE_TEST_GUIDE.md."""
    app_path = Path(app_dir).resolve()
    if not app_path.is_dir():
        raise FileNotFoundError(f"App project directory not found: {app_path}")

    p_path = Path(personas_path)
    if not p_path.is_file():
        p_path = ROOT_DIR / "config" / "personas.json"
    personas = _load_personas(p_path)

    a1 = run_stack_manifest_analyst(app_path)
    if app_id_override and app_id_override != "com.example.tinytodo":
        a1["findings"]["app_id"] = app_id_override
    a2 = run_navigation_screen_mapper(app_path, a1)
    a3 = run_interaction_form_extractor(app_path, a2)
    a4 = run_state_api_flow_analyst(app_path, a1, a3)
    a5 = run_qa_strategy_synthesizer(
        app_path,
        personas,
        a1,
        a2,
        a3,
        a4,
        ollama_host=ollama_host,
        model=model,
        use_llm=use_llm,
    )

    raw_context = {
        "schema": "mobile-agent-testers.code_context/1",
        "app_dir": str(app_path),
        "agents": [a1, a2, a3, a4, a5],
        "app_metadata": {
            "name": a1["findings"]["name"],
            "framework": a1["findings"]["framework"],
            "app_id": a1["findings"]["app_id"],
            "entry_files": a1["findings"]["entry_files"],
            "permissions": a1["findings"]["permissions"],
            "dependencies": a1["findings"]["dependencies"],
        },
        "screens": a2["findings"]["screens"],
        "forms_and_inputs": a3["findings"]["forms_and_inputs"],
        "state_and_auth": a4["findings"],
        "persona_playbooks": a5["findings"]["persona_playbooks"],
    }
    validated = validate_code_context_json(raw_context)
    guide_md = render_test_guide_markdown(validated)

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    (out_path / "code_context.json").write_text(json.dumps(validated, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (out_path / "CODE_TEST_GUIDE.md").write_text(guide_md, encoding="utf-8")
    return validated


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run 5 Code-Understanding Agents on a mobile project folder.")
    parser.add_argument("--app-dir", required=True, help="Mobile app project folder containing package.json")
    parser.add_argument("--personas", default=str(ROOT_DIR / "config" / "personas.json"))
    parser.add_argument("--out", default=str(ROOT_DIR / "results"))
    parser.add_argument("--app-id", default="", help="Optional explicit Android/iOS app id override")
    parser.add_argument("--ollama-host", default=os.environ.get("OLLAMA_HOST", ""))
    parser.add_argument("--model", default=os.environ.get("OLLAMA_MODEL", ""))
    parser.add_argument("--use-llm", action="store_true", help="Also ask local Ollama for strategy notes")
    parser.add_argument("--print-app-id", action="store_true", help="Print detected app_id on stdout")
    args = parser.parse_args(argv)

    ctx = analyze_project(
        app_dir=args.app_dir,
        personas_path=args.personas,
        out_dir=args.out,
        app_id_override=args.app_id,
        ollama_host=args.ollama_host,
        model=args.model,
        use_llm=args.use_llm,
    )
    if args.print_app_id:
        print(ctx["app_metadata"].get("app_id", ""))
    else:
        print(
            f"5-agent analysis complete for {ctx['app_metadata']['name']} ({ctx['app_metadata']['framework']}): "
            f"{len(ctx['screens'])} screens, {len(ctx['forms_and_inputs'])} form files, "
            f"{len(ctx['persona_playbooks'])} persona playbooks -> {args.out}/CODE_TEST_GUIDE.md"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

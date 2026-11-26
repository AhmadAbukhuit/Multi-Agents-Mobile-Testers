#!/usr/bin/env bash
# Local runner for Multi-Agents-Mobile-Testers using a local Ollama server.
# Usage: ./run_local.sh [options]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [ -f "config/ollama.env" ]; then
  # shellcheck disable=SC1091
  source "config/ollama.env"
fi

PLATFORM="android"
PERSONAS="eleanor"
APP_DIR="demo-app"
APP_ID="com.example.tinytodo"
FRAMEWORK="auto"
APK_PATH=""
IOS_APP_PATH=""
MODEL="${OLLAMA_MODEL:-qwen2.5vl:7b}"
HOST="${OLLAMA_HOST:-http://localhost:11434}"
BRIEF="briefs/default.md"
BUG_REPORT=""
RECORD_VIDEO="false"
MAP="none"
SKIP_BUILD="false"
SKIP_CODE_ANALYSIS="false"

usage() {
  cat <<'EOF'
Usage: ./run_local.sh [options]

Options:
  --platform <android|ios>     Target platform (default: android)
  --personas <ids|all>         Comma-separated persona ids from personas.json, or all (default: eleanor)
  --app-dir <dir>              App directory containing package.json (default: demo-app)
  --app-id <id>                Android package / iOS bundle id (default: com.example.tinytodo)
  --framework <name>           auto, react-native, expo, flutter, capacitor, kmp, native, maui (default: auto)
  --apk <path>                 Prebuilt Android .apk path (skips build)
  --ios-app <path>             Prebuilt iOS Simulator .app path (skips build)
  --model <name>               Ollama model name (default from config/ollama.env)
  --ollama-host <url>          Ollama server URL (default: http://localhost:11434)
  --brief <file>               Agent brief file (default: briefs/default.md)
  --bug-report <text|issue>    Bug report text or owner/repo#num (sets brief to briefs/reproduce.md)
  --record-video               Record screen video into results/<persona>/video/
  --langsmith                  Enable opt-in LangSmith tracing (requires LANGCHAIN_API_KEY)
  --map <none|robot|robot+agent>
                               Experimental Android screen mapper (default: none)
  --skip-build                 Reuse existing app.apk or app/*.app
  --skip-code-analysis         Skip the 5 Code-Understanding Agents phase
  -h, --help                   Show this help
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --platform) PLATFORM="$2"; shift 2 ;;
    --personas) PERSONAS="$2"; shift 2 ;;
    --app-dir) APP_DIR="$2"; shift 2 ;;
    --app-id) APP_ID="$2"; shift 2 ;;
    --framework) FRAMEWORK="$2"; shift 2 ;;
    --apk) APK_PATH="$2"; shift 2 ;;
    --ios-app) IOS_APP_PATH="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    --ollama-host) HOST="$2"; shift 2 ;;
    --brief) BRIEF="$2"; shift 2 ;;
    --bug-report) BUG_REPORT="$2"; shift 2 ;;
    --record-video) RECORD_VIDEO="true"; shift ;;
    --langsmith) LANGCHAIN_TRACING_V2="true"; shift ;;
    --map) MAP="$2"; shift 2 ;;
    --skip-build) SKIP_BUILD="true"; shift ;;
    --skip-code-analysis) SKIP_CODE_ANALYSIS="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

export OLLAMA_HOST="$HOST"
export OLLAMA_MODEL="$MODEL"
export MODEL="$MODEL"
export LANGCHAIN_TRACING_V2="${LANGCHAIN_TRACING_V2:-false}"
export LANGCHAIN_PROJECT="${LANGCHAIN_PROJECT:-Multi-Agents-Mobile-Testers}"
export LANGCHAIN_ENDPOINT="${LANGCHAIN_ENDPOINT:-https://api.smith.langchain.com}"

if [ "$MAP" != "robot" ]; then
  if ! curl -fsS -m 5 "${OLLAMA_HOST%/}/api/tags" >/dev/null 2>&1; then
    echo "Error: Ollama is not reachable at $OLLAMA_HOST" >&2
    echo "Start it with 'ollama serve' and ensure model '$OLLAMA_MODEL' is pulled." >&2
    exit 1
  fi
fi

if [ -n "$BUG_REPORT" ]; then
  BUG_REPORT="$BUG_REPORT" bash core/reporting/bug_report.sh
  [ "$BRIEF" = "briefs/default.md" ] && BRIEF="briefs/reproduce.md"
fi

if [ "$SKIP_CODE_ANALYSIS" != "true" ] && [ -d "$APP_DIR" ] && [ -f "$APP_DIR/package.json" ]; then
  echo "=== Phase 1: Running 5 Code-Understanding Agents on $APP_DIR ==="
  mkdir -p "$ROOT/results"
  ANALYZED_ID=$(python3 core/code_intel/analyze_project.py \
    --app-dir "$APP_DIR" \
    --personas config/personas.json \
    --out "$ROOT/results" \
    --app-id "$APP_ID" \
    --ollama-host "$OLLAMA_HOST" \
    --model "$OLLAMA_MODEL" \
    --print-app-id || true)
  if [ "$APP_ID" = "com.example.tinytodo" ] && [ -n "$ANALYZED_ID" ]; then
    APP_ID="$ANALYZED_ID"
    echo "Detected App ID from source project: $APP_ID"
  fi
fi

if [ "$PLATFORM" = "android" ]; then
  echo "=== Phase 2: Preparing Release APK ==="
  if [ -n "$APK_PATH" ]; then
    cp "$APK_PATH" "$ROOT/app.apk"
  elif [ "$SKIP_BUILD" != "true" ]; then
    GITHUB_WORKSPACE="$ROOT" RUNNER_TEMP="${RUNNER_TEMP:-/tmp}" bash drivers/build/android.sh "$APP_DIR" "$FRAMEWORK"
  fi
  [ -f "$ROOT/app.apk" ] || { echo "Error: app.apk not found. Pass --apk <path> or remove --skip-build." >&2; exit 1; }
elif [ "$PLATFORM" = "ios" ]; then
  mkdir -p "$ROOT/app"
  if [ -n "$IOS_APP_PATH" ]; then
    rm -rf "$ROOT/app/"*.app
    cp -R "$IOS_APP_PATH" "$ROOT/app/"
  elif [ "$SKIP_BUILD" != "true" ]; then
    GITHUB_WORKSPACE="$ROOT" RUNNER_TEMP="${RUNNER_TEMP:-/tmp}" bash drivers/build/ios.sh "$APP_DIR" "$FRAMEWORK"
    rm -rf "$ROOT/app/"*.app
    tar xzf "$ROOT/app-ios.tgz" -C "$ROOT/app"
  fi
  find "$ROOT/app" -maxdepth 2 -name '*.app' -type d | grep -q . || {
    echo "Error: no .app found under app/. Pass --ios-app <path> or remove --skip-build." >&2
    exit 1
  }
else
  echo "Error: unsupported platform '$PLATFORM' (expected android or ios)" >&2
  exit 1
fi

MATRIX_OUT=$(PERSONAS="$PERSONAS" PLATFORM="$PLATFORM" python3 core/matrix.py)
MATRIX_JSON="${MATRIX_OUT#*=}"

python3 - "$MATRIX_JSON" <<'PY' | while IFS=$'\t' read -r persona device font_scale theme increase_contrast reduce_motion text_size locale geo simulator ios_ver; do
import json, sys
rows = json.loads(sys.argv[1])
for r in rows:
    fields = [
        r.get("persona", ""),
        r.get("device", ""),
        r.get("font_scale", "1.0"),
        r.get("theme", "light"),
        r.get("increase_contrast", "false"),
        r.get("reduce_motion", "false"),
        r.get("text_size", "large"),
        r.get("locale", "en-US"),
        r.get("geo", "0,0"),
        r.get("simulator", ""),
        str(r.get("ios", "")),
    ]
    print("\t".join(f.replace("\t", " ") for f in fields))
PY
  echo "=== Running persona: $persona ($PLATFORM) with Ollama model $OLLAMA_MODEL ==="
  export PERSONA="$persona"
  export DEVICE="$device"
  export FONT_SCALE="$font_scale"
  export THEME="$theme"
  export INCREASE_CONTRAST="$increase_contrast"
  export REDUCE_MOTION="$reduce_motion"
  export TEXT_SIZE="$text_size"
  export LOCALE="$locale"
  export GEO="$geo"
  export APP_ID="$APP_ID"
  export BRIEF="$BRIEF"
  export RECORD_VIDEO="$RECORD_VIDEO"
  export MAP="$MAP"
  export BLOCK_HOSTS=""

  if [ "$SKIP_CODE_ANALYSIS" != "true" ] && [ -d "$APP_DIR" ] && [ -f "$APP_DIR/package.json" ]; then
  echo "=== Phase 1: Running 5 Code-Understanding Agents on $APP_DIR ==="
  mkdir -p "$ROOT/results"
  ANALYZED_ID=$(python3 core/code_intel/analyze_project.py \
    --app-dir "$APP_DIR" \
    --personas config/personas.json \
    --out "$ROOT/results" \
    --app-id "$APP_ID" \
    --ollama-host "$OLLAMA_HOST" \
    --model "$OLLAMA_MODEL" \
    --print-app-id || true)
  if [ "$APP_ID" = "com.example.tinytodo" ] && [ -n "$ANALYZED_ID" ]; then
    APP_ID="$ANALYZED_ID"
    echo "Detected App ID from source project: $APP_ID"
  fi
fi

if [ "$PLATFORM" = "android" ]; then
  echo "=== Phase 2: Preparing Release APK ==="
    bash drivers/android/run_agent.sh
  else
    if [ -z "${UDID:-}" ]; then
      SIM_ENV=$(python3 drivers/ios/create_sim.py "$simulator" "$ios_ver")
      UDID=$(printf '%s\n' "$SIM_ENV" | grep '^UDID=' | cut -d= -f2)
      DEVICE=$(printf '%s\n' "$SIM_ENV" | grep '^DEVICE=' | cut -d= -f2-)
      export UDID DEVICE
      xcrun simctl boot "$UDID" && xcrun simctl bootstatus "$UDID" -b >/dev/null
    fi
    bash drivers/ios/run_agent.sh
  fi
done

if ls results/*/findings.json >/dev/null 2>&1 || ls results/*/results/*/findings.json >/dev/null 2>&1; then
  python3 core/reporting/summarize.py results > results/SUMMARY.md
  echo "Summary written to results/SUMMARY.md"
fi
echo "Done. Inspect reports under results/ or launch dashboard: python3 services/dashboard/server.py results"

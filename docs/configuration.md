# Configuration

Run tests locally against your own app using [run_local.sh](../run_local.sh):

```bash
./run_local.sh \
  --platform android \
  --app-dir /path/to/your-app \
  --app-id com.you.app \
  --personas eleanor,marcus \
  --model qwen2.5vl-32k
```

## `run_local.sh` options

| Flag | Description | Default |
|---|---|---|
| `--platform` | Target platform: `android` or `ios` | `android` |
| `--personas` | Comma-separated ids from [config/personas.json](../config/personas.json), or `all` | `eleanor` |
| `--app-dir` | App folder containing `package.json` (or `pubspec.yaml` / project root) | `demo-app` |
| `--app-id` | Android package / iOS bundle id (if the other platform differs, the id inside the built app is used) | `com.example.tinytodo` |
| `--framework` | `auto` or force `react-native`, `expo`, `flutter`, `capacitor`, `kmp`, `native`, `maui` | `auto` |
| `--apk` | Prebuilt Android `.apk` path (skips `drivers/build/android.sh`) | `""` |
| `--ios-app` | Prebuilt iOS Simulator `.app` path (skips `drivers/build/ios.sh`) | `""` |
| `--model` | Ollama model name (vision + tool-calling model recommended) | from `config/ollama.env` |
| `--ollama-host` | Local Ollama server URL | `http://localhost:11434` |
| `--brief` | Agent instructions: [briefs/default.md](../briefs/default.md), [briefs/improve.md](../briefs/improve.md), [briefs/map.md](../briefs/map.md), or a custom brief | `briefs/default.md` |
| `--bug-report` | Bug to reproduce instead of exploring (text or `owner/repo#123`); sets brief to [briefs/reproduce.md](../briefs/reproduce.md) | `""` |
| `--record-video` | Record each phone's screen into `results/<persona>/video/` | `false` |
| `--langsmith` | Enable opt-in LangSmith tracing (requires `LANGCHAIN_API_KEY`) | `false` |
| `--map` | Experimental Android screen mapper: `none`, `robot` (no model), or `robot+agent` ([docs/dashboard.md](dashboard.md)) | `none` |
| `--skip-build` | Reuse existing `app.apk` or `app/*.app` | `false` |

## Ollama & LangChain / LangSmith configuration (`config/ollama.env`)

Defaults in [config/ollama.env](../config/ollama.env):

| Variable | Description | Default |
|---|---|---|
| `OLLAMA_HOST` | Ollama API base URL | `http://localhost:11434` |
| `OLLAMA_MODEL` | Default vision + tool-calling model | `qwen2.5vl:7b` |
| `OLLAMA_NUM_CTX` | Context window size passed in `options.num_ctx` | `32768` |
| `OLLAMA_TIMEOUT` | HTTP request timeout in seconds | `600` |
| `LANGCHAIN_TRACING_V2` | Enable opt-in LangSmith tracing (`true`/`false`) | `false` |
| `LANGCHAIN_PROJECT` | LangSmith project name | `Multi-Agents-Mobile-Testers` |
| `LANGCHAIN_ENDPOINT` | LangSmith API endpoint | `https://api.smith.langchain.com` |

## LangGraph agent architecture (`services/agents_api/agent.py`)

Each tester runs as a 5-node **LangGraph** state machine (`build_tester_graph()`):
1. `load_context`: Pre-loads the brief (`briefs/default.md` or `briefs/*.md`) and persona entry from `config/personas.json`.
2. `agent_decide`: Calls local Ollama (or `ChatOllama` with bound tools) using the latest screenshot and UI tree.
3. `execute_tools`: Executes allowed device commands (`adb`, `screen.py`, `type.py`, `gesture.sh`), attaches PNG screenshots, and tracks UI element signatures.
4. `guard_and_budget`: Detects stuck loops (same screen signature 3 times in a row) and enforces the turn budget (`--max-turns`).
5. `finalize_run`: Validates `findings.json` against the structured `FinalFindings` schema ([services/agents_api/schemas.py](../services/agents_api/schemas.py)) and emits the run summary.

Use [config/Modelfile](../config/Modelfile) to create a 32k context preset for multi-turn screenshot sessions:

```bash
ollama pull qwen2.5vl:7b
ollama create qwen2.5vl-32k -f config/Modelfile
```

You can also pass extra build flags to the automatic build scripts via `BUILD_ARGS` (and `IOS_SCHEME` for multi-scheme Xcode workspaces).

## Docker & Docker Compose services

The [docker-compose.yml](../docker-compose.yml) file defines five services:

| Service | Dockerfile / Image | Port | Purpose |
|---|---|---|---|
| `ollama` | `ollama/ollama:latest` | `11434` | Local Ollama model server (`ollama_data` persistent volume) |
| `ollama-init` | `ollama/ollama:latest` | — | Pulls `OLLAMA_BASE_MODEL` and builds `OLLAMA_MODEL` from [config/Modelfile](../config/Modelfile) |
| `agents-api` | [services/agents_api/Dockerfile](../services/agents_api/Dockerfile) | `8000` | REST microservice ([services/agents_api/server.py](../services/agents_api/server.py)) executing LangGraph runs |
| `dashboard` | [services/dashboard/Dockerfile](../services/dashboard/Dockerfile) | `8765` | Web map & tap-heatmap UI ([services/dashboard/server.py](../services/dashboard/server.py)) |
| `android-emulator` | `budtmo/docker-android:emulator_14.0` | `6080`, `5555` | Optional containerized Android 14 emulator (`--profile emulator`, requires `/dev/kvm`) |

## Supported frameworks (auto-detected)

| Framework | Android | iOS Simulator |
|---|---|---|
| Flutter | `flutter build apk --debug` | `flutter build ios --simulator` |
| React Native / Expo | Gradle release (`x86_64`) | Xcode (`arm64`, unsigned) |
| Native Kotlin / Java, Swift / Objective-C | Gradle debug | Xcode (`arm64`, unsigned) |
| Kotlin Multiplatform | Gradle debug | Xcode project of the iOS app |
| Capacitor / Ionic | Gradle debug after `cap sync` | Xcode after `cap sync` |
| .NET MAUI (beta) | `dotnet publish` | `dotnet build` for `iossimulator-arm64` |
| Prebuilt binary | `--apk <path>` | `--ios-app <path>` |

Agents read the screen from screenshots and the accessibility tree, so any UI toolkit works. Labelled widgets (`accessibilityLabel`, `contentDescription`, Flutter `Semantics`) give them more precise tap targets.

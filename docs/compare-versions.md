# Compare two versions

Run the same personas on two versions of your app locally, then put the two runs side by side.

1. Version A (for example, your `main` branch):
   ```bash
   ./run_local.sh --platform android --app-dir /path/to/app --app-id com.you.app \
     --brief briefs/improve.md --personas eleanor,marcus --model qwen2.5vl-32k
   mv results runs-a
   ```
2. Version B (checkout your feature branch or pass another `--apk` / `--ios-app`):
   ```bash
   ./run_local.sh --platform android --app-dir /path/to/app --app-id com.you.app \
     --brief briefs/improve.md --personas eleanor,marcus --model qwen2.5vl-32k
   mv results runs-b
   ```
3. Compare both runs:
   ```bash
   python3 core/reporting/compare.py runs-a runs-b main new-onboarding > COMPARE.md
   ```

`COMPARE.md` shows, for each version, the goals reached (`yes` / `partly` / `no`), the median number of actions, the average score, and the issues by severity. Then each persona on both versions, the issues seen on one version only, and the improvements the personas suggested, by area.

## What it tells you, and what it doesn't

It shows where each version blocks or confuses simulated new users: a step nobody gets through, a screen that takes twice the actions, a sentence that most personas misread, or a regression on one device.

It does not tell you which version will convert or retain better. The personas are patient, read everything, never pay, and come from the same model, so their opinions move together. Use it to fix regressions before a real user test.

## Tips

- Change only the app: same personas, same brief, and same Ollama model on both runs.
- [briefs/improve.md](../briefs/improve.md) gives every persona a goal and asks how many actions it took, which is what the comparison needs. Your own brief works too if it writes the same fields (`goal_reached`, `actions`, `score`).
- Each run explores in its own way. When a difference matters, run each version twice and keep what repeats.

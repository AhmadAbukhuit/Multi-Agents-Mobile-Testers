# Replay the path of a tester (experimental)

Two runs never take the same path. So when a tester finds a bug, the path that led to it is worth keeping.

Each Android tester leaves two trace files in its results directory next to `report.md`:

- `trace.json`: what the agent did, step by step. A tap is stored with the name of the element that was under the finger, not with its coordinates, so the path still replays when the layout moves.
- `trace.params.json`: every text the agent typed (an e-mail, a password). The trace only holds their names (`TEXT_1`, `TEXT_2`). **Do not commit this file.**

Nothing changes for the agent: same tools, same brief. The taps are noted by a pass-through in front of `adb` ([drivers/android/shim/adb](../drivers/android/shim/adb)), and [core/trace/ecr_trace.py](../core/trace/ecr_trace.py) writes the trace when the agent is done. An agent often taps several times after one screenshot: the pass-through then saves the screen tree again just before the tap (`tree-N.xml`), so each tap is named after what was under the finger at that moment.

To see these paths on a map of the app, comment each screen and add checks before compiling: [docs/dashboard.md](dashboard.md).

## From a trace to a test that reruns with no model

The trace can be compiled into a [Maestro](https://maestro.dev) flow directly using the native Python compiler (no Node.js checkout required), or via `--ecr` if you use an external compiler checkout:

```bash
python3 core/trace/maestro_compiler.py results/eleanor/trace.json
maestro test -e TEXT_1="..." results/eleanor/eleanor-<date>.flow.yaml
```

## Replay locally right after a run

With `REPLAY_PATH=true` (native Python compilation when `--ecr` is omitted, or `--ecr /path/to/ecr`), [drivers/android/run_agent.sh](../drivers/android/run_agent.sh) replays each Android tester's path on the same emulator once the agent finishes, or you can invoke [core/trace/ecr_replay.py](../core/trace/ecr_replay.py) directly:

```bash
python3 core/trace/ecr_replay.py results/eleanor --persona eleanor
```

It prints one summary line (for example `replay: pass, 22 steps with no model in 41 s` or `replay: stopped at step 7 of 22 (target_not_found, tapOn by name, after tapOn by name, eraseText, inputText), after 38 s`) and writes the compiled flow (`eleanor-<date>.flow.yaml`), `replay.json`, `replay.txt`, and Maestro debug artifacts in `results/eleanor/replay-debug/`.

When a replay stops, the app may have shown something else the second time (an account that now exists, a list that is no longer empty), or the trace missed something the agent did.

## Notes and limits

- **Experimental.** Traces do not record agent wait durations; Maestro waits for each element by itself, which may not be enough on a slow screen.
- **A path, with no check of its own.** The trace of an agent has no assertions: replayed as it is, it only fails when a step no longer finds its target. Checks are added in the [dashboard](dashboard.md).
- **Android only.**
- An element with no name and no id is stored as a point on the screen, in percent. It breaks on another screen size.
- When several elements share a name, the trace records which instance was tapped, counted as Maestro counts them: the deepest matching element of each, ordered from top to bottom, then left to right.
- A swipe keeps its direction and its two ends, in percent of the screen.
- `drivers/android/type.py` empties a field from its end, whereas Maestro erases before the cursor, so taps before field clearing are anchored near the text end (`"at": "97%,90%"` or `"3%,90%"` for RTL).
- Typed texts stay out of the trace: a tap on a task the agent just added is stored as `TEXT_1`.

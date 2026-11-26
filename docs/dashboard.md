# The map of your app's screens (experimental)

Each Android tester leaves its path in `trace.json` ([docs/replay.md](replay.md)). The dashboard draws every screen of the app as a map, puts the paths of the testers on it, and lets you comment each screen or turn what is wrong on it into a check that the replay verifies.

```bash
./run_local.sh --platform android --personas eleanor --model qwen2.5vl-32k
python3 services/dashboard/server.py results --allow-empty   # opens http://127.0.0.1:8765 (Run Control + Live Feed)
python3 services/dashboard/server.py --demo                    # a made-up run, to try it
python3 services/dashboard/server.py results-map results       # several result folders on one map
```

With the **New Run** and **Live Feed** panels, you can also trigger persona runs straight from the browser, inspect live Set-of-Mark (`NN.som.png`) captures as the agent explores, and export any screen path directly via **Export Maestro YAML** (`/api/maestro`).

Python 3.9 or later, nothing to install. The page runs in your browser and the results never leave your machine: the server listens on `127.0.0.1` only and never serves `trace.params.json`, the texts the agent typed.

## First, find every screen

ECR starts by exploring. Here, that is the job of the map maker, chosen with `--map` on [run_local.sh](../run_local.sh) (one persona is enough: its phone settings are kept):

| `--map` | Who explores | Model usage |
| --- | --- | --- |
| `robot` | A robot with no model ([drivers/android/crawl.py](../drivers/android/crawl.py)): on each screen it fills the empty fields with test values, then taps each control once, depth first, and comes back with Back. When Back does not lead back, it starts the app again from a clean state and taps the way there. | None |
| `robot+agent` | The robot first. When it met a form it could not pass (fields filled, no control led anywhere, and the screen answered only with an error or not at all), the local Ollama agent with [briefs/map.md](../briefs/map.md) is called for those forms only, with the robot's report in hand. | Only for the forms the robot could not pass |
| `none`, with `--brief briefs/map.md` | The local Ollama agent explores every screen alone, breadth first. | The whole exploration |

The robot never taps a control whose name says it deletes, pays, buys or signs out (in English and French), leaves search fields empty, and does not scroll: a control below the fold is not opened. Each start of the app is a folder (`map-01`, `map-02`...) with its own trace, which starts with `launchApp` on a clean state, so each one replays with no model. Each map run writes `map-stats.json` (who found each screen first, forms handed over, robot time/taps, agent turns) and `report.md` / `findings.json` (`"robot": true`) under `results/<persona>/`.

Open a map run folder together with your persona results (`python3 services/dashboard/server.py results-map results`). The map then shows every screen of the app, and the screens no persona reached are marked "No persona came here": a part of the app the testers never saw, or never found.

## The map

- **One card per screen**, with the screenshot, how many testers came, how many taps, and the findings seen there. Screenshots of the same screen are grouped, even across languages (by the ids of their elements).
- **Arrows** from one screen to the next. The wider the arrow, the more testers went that way. A dashed arrow is a way only the map maker took.
- **A heat map on each screen**: the real taps of the testers, an estimate of where people would look and tap, or both (the estimate fills in where testers tapped little). The estimate weighs each control by its size and by how often testers tapped it on other screens. A file `NN.saliency.json` next to a screenshot (`{"points": [[x, y, weight], ...]}`, x and y from 0 to 1), from a saliency model of your choice, replaces it.
- Click a screen to open it on the right: its screenshots, its comments, the findings, the testers who came, where they came from and where they went next.
- **Checks on a screen**: "Add a check", click an element on the screen, then say whether it must be visible or must not be. From a finding, "Make it a test" does the same, marked as a known bug. See below.
- **One tester's path**: pick it in "Path", or "Show path" on a screen. Its arrows stand out, the other screens fade, and the panel lists its screens in order.

Drag to move the map, scroll to zoom. Comments on a screen go to `trace.notes.json` of the first persona who saw it, under `screens`. Undo and redo with Ctrl+Z and Ctrl+Shift+Z, save with Ctrl+S.

## From a finding to a test

A trace recorded from an agent is a path with no check: replayed, it only proves that the taps still work. The checks come from here. A check added on a screen becomes an oracle of the step that led to that screen, in `checkpoints` of `trace.json`: `visible` or `notVisible`, of an element found by its name or its id, never by a position or by its rank among elements with the same name: "visible" holds when any element with that name is on the screen, "not visible" when none is. "Known bug" records it as `observed: "failed"`: the compiled flow is red until the bug is fixed, then "Fixed" keeps the check as a guard ("a defect is a test").

Only checks can change. The steps stay as the agent explored them, the server refuses a trace whose steps differ, and the first save keeps the explored trace as `trace.explored.json`. Step ids never change. No check is verified when you write it: the first replay on the unchanged app does ([docs/replay.md](replay.md)).

To validate each save against the local trace compiler (`--ecr`, Node 22.18 or later):

```bash
python3 services/dashboard/server.py results --ecr /path/to/ecr
```

A save is then refused if `ecr validate` finds the trace invalid. Then `ecr compile` turns it into a Maestro flow that replays on every build with no model.

`trace.notes.json` also counts, per trace, the checks added and the seconds spent adding them (`effort`), the human time that a test costs.

## Limits

- Android only, like the traces.
- The robot does not scroll, does not guess what a form wants beyond the name of its fields, and counts a screen as new when it shares under 60% of its controls with every screen seen: a screen whose controls change with its data may be counted twice.
- The estimated heat map is a guess from the screen's controls, not a model trained on eye tracking. Use real taps, or a saliency file, when it matters.
- Screenshots are grouped by the ids of their elements. An app without ids (many Flutter apps) is grouped by the names of its buttons, so the same screen in two languages counts as two.

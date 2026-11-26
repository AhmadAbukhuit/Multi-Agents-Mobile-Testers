"""Unit tests for Set-of-Mark visual prompting, native Maestro compilation, and Live Dashboard Run Control."""
import base64
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

try:
    from PIL import Image
except ImportError:  # pragma: no cover - host environment may lack Pillow
    Image = None

TINY_PNG_B64 = b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
TINY_PNG = base64.b64decode(TINY_PNG_B64)


def _write_test_png(path, color=(240, 240, 240)):
    if Image is not None:
        img = Image.new("RGB", (80, 120), color=color)
        img.save(path)
        return img
    Path(path).write_bytes(TINY_PNG)
    return Path(path)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "core"))
sys.path.insert(0, str(REPO_ROOT / "core" / "trace"))
sys.path.insert(0, str(REPO_ROOT / "drivers" / "android"))
sys.path.insert(0, str(REPO_ROOT / "services" / "agents_api"))
sys.path.insert(0, str(REPO_ROOT / "services" / "dashboard"))

import agent as local_agent  # noqa: E402
import ecr_replay  # noqa: E402
import maestro_compiler  # noqa: E402
import replay_check  # noqa: E402
import server as dashboard_server  # noqa: E402
import som  # noqa: E402


def _sample_trace(trace_id="eleanor-20261126T000000"):
    return {
        "schema": "ecr.trace/0",
        "id": trace_id,
        "adapter": {"id": "mobile-maestro"},
        "persona": {"id": "eleanor", "profile": "QA", "goal": "Sign in", "context": {"platform": "android"}},
        "params": [{"name": "TEXT_1", "secret": True, "description": "Email"}],
        "invariants": [],
        "steps": [
            {"id": "s1", "action": {"kind": "launchApp", "appId": "com.example.todo", "clearState": True}, "checkpoints": []},
            {
                "id": "s2",
                "action": {"kind": "tapOn", "target": {"text": "Email"}},
                "checkpoints": [{"kind": "visible", "target": {"text": "Continue"}}],
            },
            {"id": "s3", "action": {"kind": "inputText", "text": {"param": "TEXT_1"}}, "checkpoints": []},
            {"id": "s4", "action": {"kind": "swipe", "direction": "UP"}, "checkpoints": []},
            {
                "id": "s5",
                "action": {"kind": "tapOn", "target": {"id": "com.example.todo:id/submit"}},
                "checkpoints": [{"kind": "notVisible", "target": {"text": "Invalid email"}}],
            },
        ],
    }


class TestSetOfMark(unittest.TestCase):
    def test_annotate_and_resolve_element(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "results" / "eleanor"
            out.mkdir(parents=True, exist_ok=True)
            img = _write_test_png(out / "01.png")
            elements = [
                {"index": 1, "kind": "field", "label": "Email", "cx": 540, "cy": 400, "bounds": [100, 340, 980, 460]},
                {"index": 2, "kind": "button", "label": "Continue", "cx": 540, "cy": 1000, "bounds": [140, 920, 940, 1080]},
            ]
            som_png, som_json = som.annotate_and_save(out, "01", img, elements, "android", draw_scale=0.5)
            self.assertTrue(som_png.is_file())
            self.assertTrue(som_json.is_file())
            self.assertTrue((out / "latest_som.json").is_file())

            resolved = som.resolve_element(Path(tmp), 2)
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved["tap_x"], 540)
            self.assertEqual(resolved["tap_y"], 1000)
            self.assertEqual(resolved["platform"], "android")

    def test_replay_check_seen_accepts_som_and_legacy(self):
        self.assertIsNotNone(replay_check.SEEN.match('- button "Continue" centre=(540,1200)'))
        m = replay_check.SEEN.match('- [3] button "Continue" centre=(540,1200)')
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "button")
        self.assertEqual(m.group(2), "Continue")

    def test_agent_prefers_som_png_and_executes_tap_element(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            res = root / "results" / "eleanor"
            res.mkdir(parents=True)
            raw_png = res / "01.png"
            som_png = res / "01.som.png"
            _write_test_png(raw_png, color=(0, 0, 0))
            img = _write_test_png(som_png, color=(255, 0, 0))
            som.annotate_and_save(
                res,
                "01",
                img,
                [{"index": 1, "kind": "button", "label": "Sign in", "cx": 320, "cy": 840, "bounds": [200, 800, 440, 880]}],
                "android",
                draw_scale=0.1,
            )

            fake_proc = mock.Mock(returncode=0, stdout="screenshot: results/eleanor/01.png (screen 1080x2400)\n", stderr="")
            with mock.patch.object(local_agent.subprocess, "run", return_value=fake_proc):
                out, images = local_agent.exec_run_command(root, "python3 drivers/android/screen.py results/eleanor")
            self.assertIn("screenshot:", out)
            self.assertEqual(len(images), 1)
            self.assertEqual(base64.b64decode(images[0]), som_png.read_bytes())

            with mock.patch.object(local_agent.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")) as run_mock:
                tap_out, tap_imgs = local_agent.execute_tool(root, "tap_element", {"element_id": 1})
            self.assertEqual(tap_imgs, [])
            self.assertIn('Tapped [1] "Sign in"', tap_out)
            run_mock.assert_called_once()
            self.assertEqual(run_mock.call_args.args[0], ["adb", "shell", "input", "tap", "320", "840"])


class TestNativeMaestroCompiler(unittest.TestCase):
    def test_compile_trace_and_checkpoints(self):
        trace = _sample_trace()
        yaml_text = maestro_compiler.compile_trace(trace, params={"TEXT_1": "user@example.com"})
        self.assertIn('appId: "com.example.todo"', yaml_text)
        self.assertIn("---", yaml_text)
        self.assertIn('- tapOn:\n    text: "Email"', yaml_text)
        self.assertIn('- assertVisible:\n    text: "Continue"', yaml_text)
        self.assertIn('- inputText: "user@example.com"', yaml_text)
        self.assertIn("- swipe:\n    direction: \"UP\"", yaml_text)
        self.assertIn('- assertNotVisible:\n    text: "Invalid email"', yaml_text)

    def test_ecr_replay_without_ecr_uses_native_compiler(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = Path(tmp)
            trace = _sample_trace("native-001")
            (res / "trace.json").write_text(json.dumps(trace), encoding="utf-8")
            (res / "trace.params.json").write_text(json.dumps({"TEXT_1": "hi@example.com"}), encoding="utf-8")

            calls = []

            def fake_run(args, cwd, env, timeout):
                calls.append((args, cwd, env))
                return 0, "Maestro flow passed\n"

            rc = ecr_replay.main([str(res), "--persona", "eleanor"], run=fake_run)
            self.assertEqual(rc, 0)
            self.assertTrue((res / "native-001.flow.yaml").is_file())
            replay_json = json.loads((res / "replay.json").read_text(encoding="utf-8"))
            self.assertEqual(replay_json["status"], "pass")
            self.assertEqual(calls[0][0][:2], ["maestro", "test"])


class TestDashboardRunControl(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.results_root = Path(self.tmp.name)
        run_dir = self.results_root / "run-01" / "eleanor"
        run_dir.mkdir(parents=True)
        (run_dir / "trace.json").write_text(json.dumps(_sample_trace()), encoding="utf-8")
        (run_dir / "trace.params.json").write_text(json.dumps({"TEXT_1": "secret@example.com"}), encoding="utf-8")
        _write_test_png(run_dir / "01.png", color=(30, 120, 220))
        som_img = _write_test_png(run_dir / "01.som.png", color=(220, 60, 60))
        som.annotate_and_save(
            run_dir,
            "01",
            som_img,
            [{"index": 1, "kind": "button", "label": "Continue", "cx": 40, "cy": 80, "bounds": [10, 60, 70, 100]}],
            "android",
            draw_scale=1.0,
        )
        (run_dir / "agent.log").write_text("turn 1 screen captured\nturn 2 tap_element 1\n", encoding="utf-8")

        self.started_runs = []

        def fake_runner(record, prompt):
            self.started_runs.append((record["run_id"], record["persona"], prompt))

        app = dashboard_server.App([self.results_root], runner=fake_runner, agents_api_url="")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), dashboard_server.make_handler(app))
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def test_personas_live_maestro_and_run_launch(self):
        with urllib.request.urlopen(f"{self.base}/api/personas") as r:
            catalog = json.loads(r.read().decode("utf-8"))
        self.assertTrue(len(catalog["personas"]) >= 1)
        self.assertIn("briefs/default.md", catalog["briefs"])

        with urllib.request.urlopen(f"{self.base}/api/run") as r:
            run_data = json.loads(r.read().decode("utf-8"))
        persona_key = run_data["personas"][0]["key"]

        with urllib.request.urlopen(f"{self.base}/api/maestro?persona={persona_key}") as r:
            maestro_data = json.loads(r.read().decode("utf-8"))
        self.assertTrue(maestro_data["ok"])
        self.assertIn("assertVisible", maestro_data["yaml"])
        self.assertTrue(maestro_data["filename"].endswith(".flow.yaml"))

        req = urllib.request.Request(
            f"{self.base}/api/runs",
            data=json.dumps({"persona": "marcus", "platform": "android", "app_id": "com.test.app", "run_id": "run-live-1"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Dashboard": "1"},
            method="POST",
        )
        with urllib.request.urlopen(req) as r:
            self.assertEqual(r.status, 202)
            started = json.loads(r.read().decode("utf-8"))
        self.assertTrue(started["ok"])
        self.assertEqual(self.started_runs[0][0], "run-live-1")

        # Without X-Dashboard header, POST must still be refused
        bad_req = urllib.request.Request(
            f"{self.base}/api/runs",
            data=json.dumps({"persona": "marcus"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(bad_req)
        self.assertEqual(ctx.exception.code, 403)

        with urllib.request.urlopen(f"{self.base}/api/live") as r:
            live = json.loads(r.read().decode("utf-8"))
        self.assertTrue(live["runs"])
        self.assertIsNotNone(live["latest"])
        self.assertIn("01.som.png", live["latest"]["somUrl"])
        self.assertIn("turn 2 tap_element 1", live["latest"]["logTail"])

        with urllib.request.urlopen(f"{self.base}/{live['latest']['somUrl']}") as r:
            self.assertEqual(r.headers.get("Content-Type"), "image/png")
            self.assertTrue(r.read().startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()

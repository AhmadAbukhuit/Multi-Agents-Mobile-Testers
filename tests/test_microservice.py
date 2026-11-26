"""scripts/microservice.py & dashboard container flags, offline unit tests."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
import importlib.util as _iu


def _load_module(name, path):
    spec = _iu.spec_from_file_location(name, path)
    mod = _iu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


microservice = _load_module("agents_api_server", ROOT / "services" / "agents_api" / "server.py")
dashboard = _load_module("dashboard_server", ROOT / "services" / "dashboard" / "server.py")


class MicroserviceApiTests(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work, True)
        (self.work / "config").mkdir(parents=True)
        shutil.copyfile(ROOT / "config" / "personas.json", self.work / "config" / "personas.json")
        (self.work / "results" / "eleanor").mkdir(parents=True)
        (self.work / "results" / "eleanor" / "findings.json").write_text(json.dumps({
            "score": 7,
            "issues": [{"title": "Button clipped", "severity": "major", "screen": "Home"}],
        }), encoding="utf-8")

        def fake_executor(root, params):
            return 0, f"ran {params.get('personas', 'eleanor')} on {params.get('platform', 'android')}"

        self.server = microservice.create_server(host="127.0.0.1", port=0, root=self.work, executor=fake_executor)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def request(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read().decode("utf-8"))

    def test_health_and_personas(self):
        code, health = self.request("GET", "/health")
        self.assertEqual(code, 200)
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["service"], "Multi-Agents-Mobile-Testers")

        code, all_p = self.request("GET", "/personas")
        self.assertEqual(code, 200)
        self.assertEqual(len(all_p["personas"]), 20)

        code, android_p = self.request("GET", "/personas?platform=android")
        self.assertEqual(code, 200)
        self.assertEqual(len(android_p["personas"]), 10)
        self.assertTrue(all(p["platform"] == "android" for p in android_p["personas"]))

    def test_sync_and_async_runs(self):
        code, run = self.request("POST", "/runs", {"platform": "android", "personas": "eleanor", "async": False})
        self.assertEqual(code, 200)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["exit_code"], 0)
        self.assertIn("ran eleanor", run["output"])

        code, fetched = self.request("GET", f"/runs/{run['run_id']}")
        self.assertEqual(code, 200)
        self.assertIn("eleanor", fetched["findings"])

        code, async_run = self.request("POST", "/runs", {"platform": "android", "personas": "marcus"})
        self.assertEqual(code, 202)
        for _ in range(20):
            _, cur = self.request("GET", f"/runs/{async_run['run_id']}")
            if cur["status"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(cur["status"], "completed")

        code, missing = self.request("GET", "/runs/run-999")
        self.assertEqual(code, 404)

    def test_summarize_endpoint_writes_summary_md(self):
        with mock.patch.dict(os.environ, {"SUMMARY_GROUPING": "0"}):
            code, res = self.request("POST", "/summarize", {"results_dir": "results"})
        self.assertEqual(code, 200)
        self.assertEqual(res["findings_count"], 1)
        self.assertEqual(res["scores"], [7])
        self.assertTrue((self.work / "results" / "SUMMARY.md").is_file())
        self.assertIn("Button clipped", res["summary_markdown"])


class DashboardContainerTests(unittest.TestCase):
    def test_dashboard_allow_empty_starts_without_traces(self):
        with tempfile.TemporaryDirectory() as d:
            empty_dir = Path(d) / "results"
            with mock.patch.object(dashboard, "ThreadingHTTPServer") as srv_cls:
                srv = srv_cls.return_value
                srv.server_port = 8765
                srv.serve_forever.side_effect = KeyboardInterrupt
                code = dashboard.main([str(empty_dir), "--host", "0.0.0.0", "--port", "8765", "--no-browser", "--allow-empty"])
            self.assertEqual(code, 0)
            self.assertTrue(empty_dir.is_dir())
            srv_cls.assert_called_once()
            self.assertEqual(srv_cls.call_args[0][0], ("0.0.0.0", 8765))

    def test_compose_runs_containerized_emulator_by_default(self):
        compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("android-emulator:", compose_text)
        self.assertNotIn("profiles:", compose_text)
        self.assertIn("android-emulator:5555", compose_text)


if __name__ == "__main__":
    unittest.main()

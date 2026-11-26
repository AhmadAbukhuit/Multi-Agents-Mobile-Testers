"""scripts/ollama_agent.py: native Ollama tool-calling and vision agent tests."""
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "services" / "agents_api"))
import agent as ollama_agent  # noqa: E402


class OllamaServer:
    """Minimal mock Ollama /api/chat server recording incoming payloads."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                outer.requests.append(body)
                if not outer.responses:
                    self.send_response(500)
                    self.end_headers()
                    return
                code, reply = outer.responses.pop(0)
                raw = json.dumps(reply).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()

    @property
    def url(self):
        host, port = self.server.server_address
        return f"http://{host}:{port}"


class OllamaAgentTests(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        (self.work / "AGENT_BRIEF.md").write_text("Mission brief", encoding="utf-8")
        (self.work / "scripts" / "android").mkdir(parents=True)
        # Tiny 1x1 PNG bytes
        self.png_bytes = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        (self.work / "results" / "anna").mkdir(parents=True)
        (self.work / "results" / "anna" / "01.png").write_bytes(self.png_bytes)

    def test_multi_turn_tools_attach_screenshot_and_write_report(self):
        responses = [
            (200, {
                "eval_count": 50,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "read_file", "arguments": {"path": "AGENT_BRIEF.md"}}},
                        {"function": {"name": "run_command", "arguments": {"command": "ls results/anna"}}},
                    ],
                },
            }),
            (200, {
                "eval_count": 70,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "read_file", "arguments": {"path": "results/anna/01.png"}}},
                    ],
                },
            }),
            (200, {
                "eval_count": 80,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "write_file", "arguments": {"path": "results/anna/report.md", "content": "# Report\n"}}},
                        {"function": {"name": "write_file", "arguments": {"path": "results/anna/findings.json", "content": '{"score": 8, "issues": []}'}}},
                    ],
                },
            }),
            (200, {
                "eval_count": 25,
                "message": {"role": "assistant", "content": "Finished testing."},
            }),
        ]
        with OllamaServer(responses) as srv:
            code, result = ollama_agent.run_agent(
                prompt="Test app",
                model="qwen2.5vl:7b",
                host=srv.url,
                max_turns=10,
                root=self.work,
            )
        self.assertEqual(code, 0)
        self.assertEqual(result["subtype"], "success")
        self.assertFalse(result["is_error"])
        self.assertEqual(result["num_turns"], 4)
        self.assertEqual(result["modelUsage"], {"qwen2.5vl:7b": {"outputTokens": 225}})
        self.assertEqual((self.work / "results" / "anna" / "report.md").read_text(), "# Report\n")
        self.assertEqual(json.loads((self.work / "results" / "anna" / "findings.json").read_text())["score"], 8)

        # Request 3 (after reading 01.png) must include the base64 PNG in a user message
        third_req_msgs = srv.requests[2]["messages"]
        img_msgs = [m for m in third_req_msgs if m.get("images")]
        self.assertEqual(len(img_msgs), 1)
        self.assertEqual(img_msgs[0]["images"][0], base64.b64encode(self.png_bytes).decode("ascii"))

    def test_screen_command_output_auto_attaches_png(self):
        # Simulate scripts/android/screen.py output by checking exec_run_command with ls? No, use allowed prefix:
        # We can create a tiny fake scripts/android/screen.py inside self.work!
        (self.work / "scripts" / "android" / "screen.py").write_text(
            "import sys\nprint('screenshot: results/anna/01.png (screen 100x200)')\n",
            encoding="utf-8",
        )
        out, imgs = ollama_agent.exec_run_command(self.work, "python3 scripts/android/screen.py results/anna 01")
        self.assertIn("screenshot: results/anna/01.png", out)
        self.assertEqual(len(imgs), 1)
        self.assertEqual(imgs[0], base64.b64encode(self.png_bytes).decode("ascii"))

    def test_security_guards_block_disallowed_commands_and_paths(self):
        out, imgs = ollama_agent.exec_run_command(self.work, "rm -rf /")
        self.assertIn("not allowed", out)
        self.assertEqual(imgs, [])

        out2, _ = ollama_agent.exec_run_command(self.work, "adb devices; rm -rf /")
        self.assertIn("not allowed", out2)

        w_out, _ = ollama_agent.exec_write_file(self.work, "../outside.txt", "bad")
        self.assertIn("invalid path", w_out)

        w_out2, _ = ollama_agent.exec_write_file(self.work, "AGENT_BRIEF.md", "overwrite")
        self.assertIn("only allows paths under results/", w_out2)

    def test_unreachable_server_returns_error(self):
        code, result = ollama_agent.run_agent(
            prompt="Test app",
            model="qwen2.5vl:7b",
            host="http://127.0.0.1:1",
            max_turns=5,
            timeout=2,
            root=self.work,
        )
        self.assertEqual(code, 1)
        self.assertEqual(result["subtype"], "error_during_execution")
        self.assertTrue(result["is_error"])

    def test_max_turns_reached(self):
        tool_resp = (200, {
            "eval_count": 10,
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "run_command", "arguments": {"command": "ls results"}}}],
            },
        })
        with OllamaServer([tool_resp, tool_resp]) as srv:
            code, result = ollama_agent.run_agent(
                prompt="Test app",
                model="qwen2.5vl:7b",
                host=srv.url,
                max_turns=2,
                root=self.work,
            )
        self.assertEqual(code, 0)
        self.assertEqual(result["subtype"], "error_max_turns")
        self.assertTrue(result["is_error"])
        self.assertEqual(result["num_turns"], 2)

    def test_stuck_loop_detection_injects_warning_after_three_identical_screens(self):
        (self.work / "scripts" / "android" / "screen.py").write_text(
            "print('screenshot: results/anna/01.png (screen 100x200)')\n"
            "print('- button \"Continue\" centre=(50,100)')\n",
            encoding="utf-8",
        )
        screen_call = (200, {
            "eval_count": 10,
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "run_command", "arguments": {"command": "python3 scripts/android/screen.py results/anna 01"}}}],
            },
        })
        finish = (200, {"eval_count": 5, "message": {"role": "assistant", "content": "Stopped."}})
        with OllamaServer([screen_call, screen_call, screen_call, finish]) as srv:
            code, result = ollama_agent.run_agent(
                prompt="Test app",
                model="qwen2.5vl:7b",
                host=srv.url,
                max_turns=6,
                root=self.work,
            )
        self.assertEqual(code, 0)
        self.assertEqual(result["subtype"], "success")
        last_req_msgs = srv.requests[-1]["messages"]
        self.assertTrue(any("Loop warning:" in str(m.get("content", "")) for m in last_req_msgs))

    def test_validate_findings_json_normalizes_and_finalize_applies_schema(self):
        normalized = ollama_agent.validate_findings_json({
            "score": 99,
            "issues": [
                {"title": "Overlap", "severity": "CRITICAL", "screen": "Home", "screens": [1, "02"]},
                {"title": "", "severity": "minor"},
            ],
        })
        self.assertEqual(normalized["score"], 10)
        self.assertEqual(len(normalized["issues"]), 1)
        self.assertEqual(normalized["issues"][0]["severity"], "minor")
        self.assertEqual(normalized["issues"][0]["screens"], ["1", "02"])

    def test_configure_langsmith_opt_in(self):
        env = {}
        cfg_off = ollama_agent.configure_langsmith(env)
        self.assertFalse(cfg_off["enabled"])
        self.assertEqual(env["LANGCHAIN_TRACING_V2"], "false")
        self.assertEqual(cfg_off["project"], "Multi-Agents-Mobile-Testers")

        env_on = {"LANGCHAIN_TRACING_V2": "true"}
        cfg_on = ollama_agent.configure_langsmith(env_on)
        self.assertTrue(cfg_on["enabled"])

    def test_load_context_preloads_brief_and_persona(self):
        (self.work / "personas.json").write_text(json.dumps([{"id": "anna", "locale": "en-GB"}]), encoding="utf-8")
        with OllamaServer([(200, {"eval_count": 5, "message": {"role": "assistant", "content": "Done"}})]) as srv:
            code, _ = ollama_agent.run_agent(
                prompt="Read AGENT_BRIEF.md. PERSONA=anna",
                model="qwen2.5vl:7b",
                host=srv.url,
                max_turns=3,
                root=self.work,
            )
        self.assertEqual(code, 0)
        sys_msg = srv.requests[0]["messages"][0]["content"]
        self.assertIn("Preloaded AGENT_BRIEF.md", sys_msg)
        self.assertIn("Preloaded persona (anna)", sys_msg)


if __name__ == "__main__":
    unittest.main()

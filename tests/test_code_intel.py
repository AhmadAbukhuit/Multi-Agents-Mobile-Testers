"""Unit tests for the 5 Code-Understanding Agents -> 20 Persona Tester Handoff."""
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "core" / "code_intel"))
sys.path.insert(0, str(REPO_ROOT / "services" / "agents_api"))

import agent as local_agent  # noqa: E402
import analyze_project  # noqa: E402
import schemas  # noqa: E402


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dashboard_server = _load_module("dashboard_server_mod", REPO_ROOT / "services" / "dashboard" / "server.py")
api_server = _load_module("agents_api_server_mod", REPO_ROOT / "services" / "agents_api" / "server.py")


def _make_sample_rn_project(project_dir: Path):
    (project_dir / "src" / "screens").mkdir(parents=True, exist_ok=True)
    (project_dir / "android" / "app" / "src" / "main").mkdir(parents=True, exist_ok=True)

    (project_dir / "package.json").write_text(
        json.dumps(
            {
                "name": "ShopDemoApp",
                "version": "1.0.0",
                "scripts": {"android": "react-native run-android"},
                "dependencies": {
                    "react": "18.2.0",
                    "react-native": "0.74.1",
                    "@react-navigation/native": "^6.1.0",
                    "@react-native-async-storage/async-storage": "^1.23.0",
                    "zustand": "^4.5.0",
                },
            }
        ),
        encoding="utf-8",
    )
    (project_dir / "app.json").write_text(
        json.dumps({"name": "ShopDemoApp", "expo": {"android": {"package": "com.acme.shopdemo"}}}),
        encoding="utf-8",
    )
    (project_dir / "android" / "app" / "build.gradle").write_text(
        'android {\n  defaultConfig {\n    applicationId "com.acme.shopdemo"\n  }\n}\n',
        encoding="utf-8",
    )
    (project_dir / "android" / "app" / "src" / "main" / "AndroidManifest.xml").write_text(
        '<manifest package="com.acme.shopdemo"><uses-permission android:name="android.permission.CAMERA"/></manifest>',
        encoding="utf-8",
    )
    (project_dir / "App.tsx").write_text(
        """
import React from 'react';
import { NavigationContainer } from '@react-navigation/native';
export default function App() {
  return (
    <NavigationContainer>
      <Stack.Screen name="LoginScreen" component={LoginScreen} />
      <Stack.Screen name="CheckoutScreen" component={CheckoutScreen} />
    </NavigationContainer>
  );
}
""",
        encoding="utf-8",
    )
    (project_dir / "src" / "screens" / "LoginScreen.tsx").write_text(
        """
import React, { useState } from 'react';
import { TextInput, Pressable, Text } from 'react-native';
import AsyncStorage from '@react-native-async-storage/async-storage';

const demoEmail = "demo@acme.test";

export function LoginScreen({ navigation }) {
  const [email, setEmail] = useState("");
  async function signIn() {
    if (!email.includes('@')) return;
    await AsyncStorage.setItem('auth_token', email);
    navigation.navigate('CheckoutScreen');
  }
  return (
    <>
      <TextInput testID="login-email-input" placeholder="Email address" accessibilityLabel="Email input" />
      <TextInput testID="login-password-input" placeholder="Password" secureTextEntry />
      <Pressable testID="login-submit-btn" onPress={signIn}><Text>Sign In</Text></Pressable>
    </>
  );
}
""",
        encoding="utf-8",
    )
    (project_dir / "src" / "screens" / "CheckoutScreen.tsx").write_text(
        """
import React from 'react';
import { Button, Switch } from 'react-native';

export function CheckoutScreen() {
  return (
    <>
      <Switch testID="express-shipping-switch" />
      <Button title="Place Order" onPress={() => fetch('/api/orders')} />
    </>
  );
}
""",
        encoding="utf-8",
    )


class TestCodeIntelligencePipeline(unittest.TestCase):
    def test_five_agents_generate_valid_context_and_20_persona_playbooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "MobileApplication"
            out_dir = Path(tmp) / "results"
            _make_sample_rn_project(project_dir)

            ctx = analyze_project.analyze_project(
                app_dir=project_dir,
                personas_path=REPO_ROOT / "config" / "personas.json",
                out_dir=out_dir,
            )
            schemas.validate_code_context_json(ctx)

            agent_ids = [a["id"] for a in ctx["agents"]]
            self.assertEqual(agent_ids, list(schemas.CODE_AGENT_IDS))
            self.assertEqual(ctx["app_metadata"]["framework"], "react-native")
            self.assertEqual(ctx["app_metadata"]["app_id"], "com.acme.shopdemo")
            self.assertIn("CAMERA", ctx["app_metadata"]["permissions"])

            screen_names = {s["name"] for s in ctx["screens"]}
            self.assertIn("LoginScreen", screen_names)
            self.assertIn("CheckoutScreen", screen_names)

            all_test_ids = {tid for fm in ctx["forms_and_inputs"] for tid in fm["test_ids"]}
            self.assertIn("login-email-input", all_test_ids)
            self.assertIn("login-submit-btn", all_test_ids)
            self.assertTrue(ctx["state_and_auth"]["auth_required"])
            self.assertEqual(ctx["state_and_auth"]["test_credentials"].get("email"), "demo@acme.test")
            self.assertIn("auth_token", ctx["state_and_auth"]["storage_keys"])
            self.assertIn("/api/orders", ctx["state_and_auth"]["api_endpoints"])

            # Verify tailored playbooks for all 20 personas in config/personas.json
            personas = json.loads((REPO_ROOT / "config" / "personas.json").read_text(encoding="utf-8"))
            self.assertEqual(len(personas), 20)
            self.assertEqual(len(ctx["persona_playbooks"]), 20)
            for p in personas:
                self.assertIn(p["id"], ctx["persona_playbooks"])
                self.assertTrue(ctx["persona_playbooks"][p["id"]]["recommended_actions"])

            self.assertTrue((out_dir / "code_context.json").is_file())
            self.assertTrue((out_dir / "CODE_TEST_GUIDE.md").is_file())
            guide_md = (out_dir / "CODE_TEST_GUIDE.md").read_text(encoding="utf-8")
            self.assertIn("Sign In", guide_md)
            self.assertIn("com.acme.shopdemo", guide_md)

    def test_agent_preloads_code_guide_and_persona_playbook(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_dir = root / "MobileApplication"
            _make_sample_rn_project(project_dir)
            analyze_project.analyze_project(
                app_dir=project_dir,
                personas_path=REPO_ROOT / "config" / "personas.json",
                out_dir=root / "results",
            )
            (root / "config").mkdir(parents=True, exist_ok=True)
            (root / "config" / "personas.json").write_text(
                (REPO_ROOT / "config" / "personas.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (root / "briefs").mkdir(parents=True, exist_ok=True)
            (root / "briefs" / "code_aware.md").write_text(
                (REPO_ROOT / "briefs" / "code_aware.md").read_text(encoding="utf-8"),
                encoding="utf-8",
            )

            preloaded = local_agent._preload_context_text(
                root,
                "Read briefs/code_aware.md and config/personas.json. PERSONA=emre PLATFORM=android",
            )
            self.assertIn("Preloaded 5-Agent Codebase Test Guide", preloaded)
            self.assertIn("Preloaded Code-Aware Playbook for persona (emre)", preloaded)
            self.assertIn("Turkish characters", preloaded)

    def test_dashboard_and_agents_api_analyze_endpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            project_dir = workspace / "MobileApplication"
            _make_sample_rn_project(project_dir)
            (workspace / "config").mkdir(parents=True, exist_ok=True)
            (workspace / "config" / "personas.json").write_text(
                (REPO_ROOT / "config" / "personas.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            results_dir = workspace / "results"
            results_dir.mkdir(parents=True, exist_ok=True)

            dash_app = dashboard_server.App([results_dir], workspace_root=workspace, agents_api_url="")
            dash_srv = ThreadingHTTPServer(("127.0.0.1", 0), dashboard_server.make_handler(dash_app))
            dash_thread = threading.Thread(target=dash_srv.serve_forever, daemon=True)
            dash_thread.start()
            try:
                base = f"http://127.0.0.1:{dash_srv.server_port}"
                req = urllib.request.Request(
                    f"{base}/api/analyze",
                    data=json.dumps({"app_dir": "MobileApplication"}).encode("utf-8"),
                    headers={"Content-Type": "application/json", "X-Dashboard": "1"},
                    method="POST",
                )
                with urllib.request.urlopen(req) as resp:
                    self.assertEqual(resp.status, 200)
                    body = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(body["ok"])
                self.assertEqual(len(body["code_context"]["agents"]), 5)
                self.assertEqual(len(body["code_context"]["persona_playbooks"]), 20)

                with urllib.request.urlopen(f"{base}/api/code-context") as resp:
                    ctx = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(ctx["app_metadata"]["app_id"], "com.acme.shopdemo")
            finally:
                dash_srv.shutdown()
                dash_srv.server_close()

            api_srv = api_server.create_server(host="127.0.0.1", port=0, root=workspace)
            api_thread = threading.Thread(target=api_srv.serve_forever, daemon=True)
            api_thread.start()
            try:
                api_base = f"http://127.0.0.1:{api_srv.server_port}"
                req = urllib.request.Request(
                    f"{api_base}/analyze",
                    data=json.dumps({"app_dir": "MobileApplication"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req) as resp:
                    self.assertEqual(resp.status, 200)
                    api_body = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(api_body["ok"])
                self.assertIn("Sign In", api_body["guide_markdown"])
                with urllib.request.urlopen(f"{api_base}/code-context") as resp:
                    api_ctx = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(len(api_ctx["persona_playbooks"]), 20)
            finally:
                api_srv.shutdown()
                api_srv.server_close()


if __name__ == "__main__":
    unittest.main()

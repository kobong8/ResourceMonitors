import importlib.util
import json
import sys
import os
import shutil
import subprocess
import threading
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


agent = load("monitor_agent", ROOT / "agent/agent.py")
hub = load("monitor_hub", ROOT / "hub/hub.py")


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = hub.DB_PATH
        self.old_key = hub.CONFIG["security"]["api_key"]
        hub.DB_PATH = Path(self.tmp.name) / "test.db"
        hub.CONFIG["security"]["api_key"] = "test-key"
        hub.init_db()
        self.client = hub.app.test_client()
        self.headers = {"X-API-Key": "test-key"}
        self.payload = dict(hostname="test-pc", ip_address="127.0.0.1",
                            timestamp="2099-01-01T00:00:00",
                            cpu={"percent": 20}, memory={"percent": 30}, gpu=[])

    def tearDown(self):
        hub.DB_PATH = self.old_path
        hub.CONFIG["security"]["api_key"] = self.old_key
        self.tmp.cleanup()

    def test_report_auth_storage_and_pages(self):
        self.assertEqual(self.client.post("/api/report", json=self.payload).status_code, 401)
        self.assertEqual(self.client.post("/api/report", json=self.payload, headers=self.headers).status_code, 200)
        for url in ("/", "/computer/test-pc", "/settings", "/api/config", "/api/status"):
            self.assertEqual(self.client.get(url, headers=self.headers).status_code, 200, url)
        status = self.client.get("/api/status", headers=self.headers).json
        self.assertEqual((status["total"], status["online"]), (1, 1))
        with hub.app.app_context():
            row = hub.get_db().execute("SELECT timestamp FROM data_history").fetchone()
            self.assertFalse(row["timestamp"].startswith("2099"))
        self.assertFalse(hub.is_online((datetime.now() - timedelta(seconds=61)).isoformat(), 60))

    def test_history_window_and_retention(self):
        self.client.post("/api/report", json=self.payload, headers=self.headers)
        with hub.app.app_context():
            db = hub.get_db()
            for hours in (2, 25):
                db.execute("INSERT INTO data_history(hostname,timestamp,cpu_percent,memory_percent,gpu_data) VALUES(?,?,?,?,?)",
                           ("test-pc", (datetime.now()-timedelta(hours=hours)).isoformat(), 99, 99, "[]"))
            db.commit()
        self.client.post("/api/report", json=self.payload, headers=self.headers)
        with hub.app.app_context():
            self.assertEqual(hub.get_db().execute("SELECT count(*) FROM data_history").fetchone()[0], 3)
        with patch.object(hub, "render_template", return_value="ok") as render:
            self.client.get("/computer/test-pc")
            self.assertEqual(len(render.call_args.kwargs["history"]["cpu"]), 2)

    def test_forwarded_header_cannot_bypass_whitelist(self):
        with hub.app.app_context():
            hub.set_setting("allowed_ips", json.dumps(["10.0.0.1"]))
        self.assertEqual(self.client.get("/", headers={"X-Forwarded-For": "10.0.0.1"}).status_code, 403)

    def test_invalid_metrics(self):
        for value in (None, [], {"percent": "bad"}, {"percent": 101}):
            self.payload["cpu"] = value
            self.assertEqual(self.client.post("/api/report", json=self.payload, headers=self.headers).status_code, 400)

    def test_frozen_path_and_config_validation(self):
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(Path(self.tmp.name)/"Agent.exe")):
            self.assertEqual(agent.application_dir(), Path(self.tmp.name).resolve())
        path = Path(self.tmp.name)/"config.yaml"
        path.write_text("hub:\n  url: http://localhost:5000\n  api_key: test-key\nagent:\n  report_interval_seconds: 0\n", encoding="utf8")
        with self.assertRaises(ValueError):
            agent.load_config(path)

    def test_no_substring_process_match(self):
        process = Mock(info={"name": "decode.exe"})
        with patch.object(agent.psutil, "process_iter", return_value=[process]):
            self.assertEqual(agent.check_running_programs([{"name": "Code", "process": "code"}]), {"Code": False})

    def test_console_is_not_remote(self):
        result = Mock(returncode=0, stdout="SESSIONNAME USERNAME ID STATE\n>console user 1 Active\nrdp-tcp#1 remote 2 Active\n")
        with patch.object(agent.platform, "system", return_value="Windows"), patch.object(agent.subprocess, "run", return_value=result), patch.object(agent.psutil, "net_connections", return_value=[]):
            sessions = agent.get_remote_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["user"], "remote")

    def test_once_exit_code(self):
        with patch.object(agent, "setup_logging"), patch.object(agent, "load_config", return_value={}), patch.object(agent, "fetch_hub_monitoring_config", return_value=None), patch.object(agent, "collect_system_info", return_value=self.payload), patch.object(agent, "report_to_hub", return_value=False):
            self.assertEqual(agent.main(["--once"]), 1)

    @unittest.skipUnless((ROOT / "agent/dist/ResourceMonitorAgent.exe").exists() and os.name == "nt", "Build Windows EXE first")
    def test_packaged_exe_to_real_hub(self):
        from werkzeug.serving import make_server
        import yaml
        exe = Path(self.tmp.name) / "ResourceMonitorAgent.exe"
        shutil.copy2(ROOT / "agent/dist/ResourceMonitorAgent.exe", exe)
        with hub.app.app_context():
            hub.set_setting("programs_to_check_running", "[]")
            hub.set_setting("programs_to_check_installed", "[]")
        server = make_server("127.0.0.1", 0, hub.app)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            for hostname in ("exe-pc-1", "exe-pc-2"):
                config = {"hub": {"url": f"http://127.0.0.1:{server.server_port}", "api_key": "test-key"},
                          "agent": {"hostname": hostname, "report_interval_seconds": 30}}
                (exe.parent / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf8")
                # No Python or pip on PATH; a different cwd checks EXE-relative config.
                env = {**os.environ, "PATH": str(Path(os.environ["SystemRoot"]) / "System32")}
                result = subprocess.run([str(exe), "--once", "--log-dir", self.tmp.name],
                                        cwd=ROOT, env=env, capture_output=True, timeout=60)
                self.assertEqual(result.returncode, 0, repr(result.stderr))
                self.assertEqual(self.client.get(f"/computer/{hostname}").status_code, 200)
            self.assertEqual(self.client.get("/api/status", headers=self.headers).json["online"], 2)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)

    def test_install_check_rejects_code_execution(self):
        with patch.object(agent.subprocess, "run") as run:
            result = agent.check_installed_programs([{"name": "bad", "check_command": "python -c print(1)"}])
            self.assertFalse(result["bad"])
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

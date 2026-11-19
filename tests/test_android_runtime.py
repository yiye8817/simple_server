"""Current-app and force-stop contracts against isolated SQLite and HTTP."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from dev_control import create_app
from dev_control.db import get_db


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="android-runtime-")
        self.app = create_app({"TESTING": True, "ENABLE_LEGACY_TOOLS": False,
                               "DATA_DIR": self.tmp.name + "/data", "SERVER_FILES_ROOT": self.tmp.name + "/files",
                               "ADMIN_PASSWORD": "runtime-test-password"})
        self.c = self.app.test_client()
        self.admin = self.login("admin")
        for name in ("owner", "other"):
            self.c.post("/api/auth/users", headers=self.admin, json={"username": name, "password": "runtime-test-password"})
        self.owner, self.other = self.login("owner"), self.login("other")
        self.device, self.agent = self.pair("one")
        self.base = "/api/android/devices/" + str(self.device["id"])
        self.install_inventory()

    def tearDown(self):
        self.tmp.cleanup()

    def login(self, name):
        token = self.c.post("/api/auth/login", json={"username": name, "password": "runtime-test-password"}).json["token"]
        return {"Authorization": "Bearer " + token}

    def pair(self, uid):
        code = self.c.post("/api/android/pairing-codes", headers=self.owner).json["code"]
        device = self.c.post("/api/android/pair", json={"code": code, "device_uid": uid, "name": "Tablet"}).json["device"]
        return device, {"X-Device-Token": device["device_token"]}

    def app_row(self, pkg="org.example.reader", **extra):
        return {"package_name": pkg, "label": "阅读器 <script>", "version_name": "1", "version_code": 1,
                "system": False, "launchable": True, "protected": False, **extra}

    def install_inventory(self, apps=None):
        self.assertEqual(self.c.post("/api/android/agent/inventory", headers=self.agent, json={
            "name": "Tablet", "timezone": "Asia/Shanghai", "capabilities": {"accessibility_enabled": True},
            "apps": apps or [self.app_row(), self.app_row("org.example.other", label="Other")]}).status_code, 200)

    def report(self, **extra):
        return {"observation_id": "12345678-1234-1234-1234-123456789abc:1", "state": "foreground",
                "package_name": "org.example.reader", "observed_at": 1000, "can_force_stop": True,
                "recent": [{"package_name": "org.example.other", "last_seen": 900}], **extra}

    def send(self, **extra):
        return self.c.post("/api/android/agent/runtime", headers=self.agent, json=self.report(**extra))

    def read(self, headers=None):
        return self.c.get(self.base + "/runtime", headers=headers or self.owner)

    def stop(self, **extra):
        return self.c.post(self.base + "/apps/force-stop", headers=self.owner, json={
            "package_name": "org.example.reader", "observation_id": self.report()["observation_id"], **extra})

    def test_owned_routes_and_device_report_are_scoped(self):
        self.assertEqual(self.c.post("/api/android/agent/runtime", json=self.report()).status_code, 401)
        legacy = self.c.post("/api/agent/enroll", json={"device_uid": "legacy"}).json["device"]
        self.assertEqual(self.c.post("/api/android/agent/runtime", headers={"X-Device-Token": legacy["device_token"]}, json=self.report()).status_code, 403)
        for method, path, body in (("get", "/runtime", None), ("post", "/apps/force-stop", self.report())):
            self.assertEqual(getattr(self.c, method)(self.base + path, headers=self.other, json=body).status_code, 403)
            self.assertEqual(getattr(self.c, method)(self.base + path, json=body).status_code, 401)
        self.assertEqual(self.read(self.admin).status_code, 200)
        self.assertIsNone(self.read().json["runtime"])
        self.assertTrue(self.read().json["stale"])

    def test_snapshot_strict_validation_is_atomic(self):
        self.assertEqual(self.send().status_code, 200)
        good = self.read().json["runtime"]
        bad = [[], {**self.report(), "extra": 1}]
        bad += [{k: v for k, v in self.report().items() if k != missing} for missing in self.report()]
        for key, values in {"observation_id": ["old", "12345678-1234-1234-1234-123456789abc:0", True],
                            "state": ["running", None], "package_name": ["../bad", 2],
                            "observed_at": [True, -1, 1.5], "can_force_stop": [1, "true"],
                            "recent": [None, [{}], [{"package_name": "org.example.other", "last_seen": True}],
                                       [{"package_name": "org.example.other", "last_seen": 1, "extra": 1}],
                                       self.report()["recent"] * 11, self.report()["recent"] * 2]}.items():
            bad += [{**self.report(), key: value} for value in values]
        bad += [self.report(state="locked"), self.report(state="unknown", package_name="", can_force_stop=True)]
        for body in bad:
            with self.subTest(body=body):
                self.assertEqual(self.c.post("/api/android/agent/runtime", headers=self.agent, json=body).status_code, 400)
        self.assertEqual(self.read().json["runtime"], good)

    def test_receipt_clock_gates_staleness_and_device_clock_is_metadata(self):
        now = time.time()
        with patch("dev_control.android.time.time", return_value=now):
            self.assertEqual(self.send(observed_at=2**60).status_code, 200)
            data = self.read().json
            self.assertFalse(data["stale"])
            self.assertTrue(data["force_stop_allowed"])
            self.assertEqual(data["runtime"]["label"], "阅读器 <script>")
            self.assertEqual(data["runtime"]["recent"][0]["label"], "Other")
        with patch("dev_control.android.time.time", return_value=now + 30):
            self.assertTrue(self.read().json["stale"])
            self.assertEqual(self.stop().status_code, 409)
        with patch("dev_control.android.time.time", return_value=now + 31):
            self.assertEqual(self.send(observed_at=1).status_code, 200)
            self.assertFalse(self.read().json["stale"])

    def test_identity_change_and_aba_reject_old_action(self):
        self.assertEqual(self.send().status_code, 200)
        self.assertEqual(self.send(package_name="org.example.other").status_code, 409)
        self.assertEqual(self.send(observation_id="12345678-1234-1234-1234-123456789abc:2", package_name="org.example.other").status_code, 200)
        self.assertEqual(self.send().status_code, 409)
        self.assertEqual(self.send(observation_id="12345678-1234-1234-1234-123456789abc:3").status_code, 200)
        self.assertEqual(self.stop().status_code, 409)
        self.assertEqual(self.stop(observation_id="12345678-1234-1234-1234-123456789abc:3").status_code, 201)

    def test_stop_rejects_unknown_protected_nonlaunchable_and_incapable_targets(self):
        cases = [(self.app_row(protected=True), {}), (self.app_row(launchable=False), {}),
                 (self.app_row("org.example.other"), {}), (self.app_row(), {"can_force_stop": False}),
                 (self.app_row(), {"state": "locked", "package_name": "", "can_force_stop": False}),
                 (self.app_row("com.android.settings"), {"package_name": "com.android.settings"}),
                 (self.app_row("org.example.keyboard", protected=True), {"package_name": "org.example.keyboard"})]
        for index, (app, change) in enumerate(cases, 1):
            with self.subTest(app=app, change=change):
                self.install_inventory([app])
                observation = "12345678-1234-1234-1234-123456789abc:" + str(index)
                self.assertEqual(self.send(observation_id=observation, **change).status_code, 200)
                result = self.stop(observation_id=observation, package_name=change.get("package_name") or "org.example.reader")
                self.assertIn(result.status_code, (400, 409))
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT COUNT(*) FROM tasks WHERE kind='android.app.force_stop'").fetchone()[0], 0)

    def test_stop_payload_is_strict_typed_and_expires_in_thirty_seconds(self):
        self.assertEqual(self.send().status_code, 200)
        for body in ([], {}, {"package_name": "org.example.reader"},
                     {**{"package_name": "org.example.reader", "observation_id": self.report()["observation_id"]}, "expires_at": 2**60}):
            self.assertEqual(self.c.post(self.base + "/apps/force-stop", headers=self.owner, json=body).status_code, 400)
        now = time.time()
        with patch("dev_control.android.time.time", return_value=now):
            result = self.stop()
        self.assertEqual(result.status_code, 201)
        tasks = self.c.get("/api/agent/poll", headers=self.agent).json["tasks"]
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(task["kind"], "android.app.force_stop")
        self.assertEqual(task["payload"], {"package_name": "org.example.reader", "observation_id": self.report()["observation_id"], "expires_at": int(now * 1000) + 30000})
        self.assertEqual(self.c.get("/api/android/agent/recover-tasks", headers=self.agent).json["tasks"][0]["id"], task["id"])

    def test_duplicate_active_stop_and_device_scoped_results(self):
        self.assertEqual(self.send().status_code, 200)
        self.assertEqual(self.stop().status_code, 201)
        self.assertEqual(self.stop().status_code, 409)
        task = self.c.get("/api/agent/poll", headers=self.agent).json["tasks"][0]
        self.assertEqual(self.stop().status_code, 409)
        device2, agent2 = self.pair("two")
        self.assertEqual(self.c.post("/api/agent/tasks/" + str(task["id"]) + "/result", headers=agent2,
                                     json={"claim_token": task["claim_token"], "status": "done", "result": {"state": "force_stopped"}}).status_code, 404)
        self.c.post("/api/agent/tasks/" + str(task["id"]) + "/result", headers=self.agent,
                    json={"claim_token": task["claim_token"], "status": "error", "result": {"state": "foreground_changed", "message": "前台已变化"}})
        result = self.read().json["exit_tasks"][0]
        self.assertEqual(result["result"], {"state": "foreground_changed", "message": "前台已变化"})
        self.assertNotIn("claim_token", json.dumps(result))
        self.assertEqual(self.stop().status_code, 201)
        self.assertEqual(self.c.get("/api/android/devices/" + str(device2["id"]) + "/runtime", headers=self.owner).json["exit_tasks"], [])

    def test_pending_stop_older_than_result_window_still_blocks_panel(self):
        self.assertEqual(self.send().status_code, 200)
        self.assertEqual(self.stop().status_code, 201)
        with self.app.app_context():
            for _ in range(10):
                get_db().execute("INSERT INTO tasks(device_id,kind,status,created_at) VALUES(?,'android.app.force_stop','done',?)",
                                 (self.device["id"], int(time.time())))
            get_db().commit()
        data = self.read().json
        self.assertEqual(len(data["exit_tasks"]), 10)
        self.assertFalse(data["force_stop_allowed"])
        self.assertEqual(self.stop().status_code, 409)


if __name__ == "__main__":
    unittest.main()

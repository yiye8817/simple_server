"""Managed Android API contract tests using isolated SQLite and actual HTTP routes."""
import hashlib
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from dev_control import create_app
from dev_control.db import get_db
from dev_control.security import hash_token

HOUR = 3600000


def millis(iso):
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1000)


class AndroidTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = create_app({"TESTING": True, "ENABLE_LEGACY_TOOLS": False,
                               "DATA_DIR": str(self.root / "data"),
                               "SERVER_FILES_ROOT": str(self.root / "files"),
                               "ADMIN_PASSWORD": "test-password"})
        self.c = self.app.test_client()
        self.admin = self.login("admin")
        for username in ("alice", "other"):
            self.assertEqual(self.c.post("/api/auth/users", headers=self.admin,
                                        json={"username": username, "password": "test-password"}).status_code, 201)
        self.alice = self.login("alice")
        self.other = self.login("other")

    def tearDown(self):
        self.tmp.cleanup()

    def login(self, name):
        response = self.c.post("/api/auth/login", json={"username": name, "password": "test-password"})
        return {"Authorization": "Bearer " + response.json["token"]}

    def code(self, headers=None):
        r = self.c.post("/api/android/pairing-codes", headers=headers or self.alice)
        self.assertEqual(r.status_code, 200)
        return r.json["code"]

    def pair(self, uid="managed-1", headers=None):
        r = self.c.post("/api/android/pair", json={"code": self.code(headers), "device_uid": uid,
                                                  "name": "Tablet", "capabilities": {"usage_access": False}})
        self.assertEqual(r.status_code, 200)
        return r.json["device"], {"X-Device-Token": r.json["device"]["device_token"]}

    def app_row(self, package="org.example.reader", **extra):
        row = {"package_name": package, "label": "Reader <script>", "version_name": "1.2",
               "version_code": 12, "system": False, "launchable": True, "protected": False}
        row.update(extra)
        return row

    def inventory(self, dh, apps=None, zone="Asia/Shanghai"):
        return self.c.post("/api/android/agent/inventory", headers=dh,
                           json={"name": "Tablet", "timezone": zone,
                                 "capabilities": {"usage_access": True, "accessibility_enabled": False,
                                                  "install_permission": False},
                                 "apps": apps if apps is not None else [self.app_row()]})

    def effective_rules(self, rules):
        return {"com.hihonor.appmarket": {"allowed": False, "visible": False},
                "com.huawei.appmarket": {"allowed": False, "visible": False}, **rules}

    def snapshot(self, dh, buckets=None, **extra):
        data = {"timezone": "UTC", "permission_granted": True,
                "generated_at": millis("2026-10-25T04:00:00"),
                "window_start": millis("2026-10-24T00:00:00"),
                "window_end": millis("2026-10-25T04:00:00"),
                "buckets": buckets if buckets is not None else [
                    {"package_name": "org.example.reader", "bucket_start": millis("2026-10-25T01:00:00"), "duration_ms": 12000}]}
        data.update(extra)
        return self.c.post("/api/android/agent/usage", headers=dh, json=data)

    def test_pairing_hash_expiry_replay_and_owner_binding(self):
        self.assertEqual(self.c.post("/api/android/pairing-codes").status_code, 401)
        code = self.code()
        self.assertRegex(code, r"^[0-9a-f]{12}$")
        with self.app.app_context():
            row = get_db().execute("SELECT * FROM android_pairing_codes").fetchone()
            self.assertEqual(row["code_hash"], hash_token(code))
            self.assertNotIn(code, tuple(str(v) for v in row))
            get_db().execute("UPDATE android_pairing_codes SET expires_at=0")
            get_db().commit()
        expired = self.c.post("/api/android/pair", json={"code": code, "device_uid": "expired", "name": "bad"})
        self.assertEqual(expired.status_code, 400)
        code = self.code()
        body = {"code": code, "device_uid": "bound", "name": "Tablet", "capabilities": {}}
        paired = self.c.post("/api/android/pair", json=body)
        self.assertEqual(paired.status_code, 200)
        self.assertEqual(self.c.post("/api/android/pair", json=body).status_code, 400)
        self.assertEqual(len(self.c.get("/api/android/devices", headers=self.alice).json["devices"]), 1)
        self.assertEqual(self.c.get("/api/android/devices", headers=self.other).json["devices"], [])

    def test_managed_uid_cannot_be_reenrolled_or_taken_over(self):
        dev, dh = self.pair()
        self.assertEqual(self.c.post("/api/agent/enroll", json={"device_uid": dev["device_uid"]}).status_code, 409)
        r = self.c.post("/api/android/pair", json={"code": self.code(self.other),
                                                  "device_uid": dev["device_uid"], "name": "Stolen"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.c.get("/api/android/agent/state", headers=dh).status_code, 200)

    def test_pairing_validates_input_and_does_not_consume_code_on_bad_input(self):
        code = self.code()
        for bad in ([], {"code": code, "device_uid": "../bad", "name": "T"},
                    {"code": code, "device_uid": "good", "name": "T", "capabilities": []}):
            self.assertEqual(self.c.post("/api/android/pair", json=bad).status_code, 400)
        self.assertEqual(self.c.post("/api/android/pair", json={"code": code, "device_uid": "valid", "name": "T"}).status_code, 200)

    def test_android_agent_endpoints_reject_legacy_device_and_missing_token(self):
        legacy = self.c.post("/api/agent/enroll", json={"device_uid": "linux"}).json["device"]
        for headers in ({}, {"X-Device-Token": legacy["device_token"]}):
            self.assertIn(self.c.get("/api/android/agent/state", headers=headers).status_code, (401, 403))
            self.assertIn(self.c.get("/api/android/agent/recover-tasks", headers=headers).status_code, (401, 403))
            self.assertIn(self.inventory(headers).status_code, (401, 403))
            self.assertIn(self.snapshot(headers).status_code, (401, 403))

    def test_every_management_and_reused_route_checks_owner(self):
        dev, dh = self.pair()
        base = f'/api/android/devices/{dev["id"]}'
        cases = [("get", base, {}), ("put", base + "/policy", {"json": {"apps": {}}}),
                 ("get", base + "/usage?date=2026-10-25", {}), ("get", base + "/files", {}),
                 ("post", base + "/files?filename=a.txt", {"data": b"secret"}),
                 ("post", base + "/files/1/open", {}),
                 ("get", f'/api/devices/{dev["id"]}/tasks', {}),
                 ("post", f'/api/devices/{dev["id"]}/tasks', {"json": {"kind": "exec"}}),
                 ("post", f'/api/devices/{dev["id"]}/blobs', {"data": b"malicious"})]
        for method, path, args in cases:
            with self.subTest(path=path, method=method):
                self.assertEqual(getattr(self.c, method)(path, headers=self.other, **args).status_code, 403)
        self.assertEqual(self.c.get(base, headers=self.admin).status_code, 200)
        self.assertEqual(self.c.post(f'/api/devices/{dev["id"]}/tasks', headers=self.alice,
                                     json={"kind": "exec", "payload": {"command": "id"}}).status_code, 400)

    def test_policy_revision_independence_replacement_and_protected_packages(self):
        dev, dh = self.pair()
        self.assertEqual(self.inventory(dh, [self.app_row(), self.app_row("org.example.keyboard", protected=True)]).status_code, 200)
        base = f'/api/android/devices/{dev["id"]}'
        r = self.c.put(base + "/policy", headers=self.alice,
                       json={"apps": {"org.example.reader": {"allowed": False, "visible": True}}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json["policy"], {"revision": 1, "layout": "paged", "apps": self.effective_rules({"org.example.reader": {"allowed": False, "visible": True}}), "foreground_kill_enabled": True})
        self.assertEqual(self.c.get("/api/android/agent/state", headers=dh).json["policy"], r.json["policy"])
        for pkg in ("android", "com.android.settings", "com.android.systemui", "app.lawnchair.managed", "org.example.keyboard"):
            for rule in ({"allowed": False, "visible": True}, {"allowed": True, "visible": False}):
                self.assertEqual(self.c.put(base + "/policy", headers=self.alice,
                                           json={"apps": {pkg: rule}}).status_code, 400)
        cleared = self.c.put(base + "/policy", headers=self.alice, json={"apps": {}})
        self.assertEqual(cleared.json["policy"]["revision"], 2)
        self.assertEqual(cleared.json["policy"]["apps"], self.effective_rules({}))
        self.assertEqual([t["kind"] for t in self.c.get("/api/agent/poll", headers=dh).json["tasks"]], ["android.policy.sync", "android.policy.sync"])

    def test_guard_default_switch_persistence_and_explicit_market_grant(self):
        dev, dh = self.pair()
        path = f'/api/android/devices/{dev["id"]}/policy'
        initial = self.c.get('/api/android/agent/state', headers=dh).json['policy']
        self.assertIs(initial['foreground_kill_enabled'], True)
        for market in ('com.hihonor.appmarket', 'com.huawei.appmarket'):
            self.assertEqual(initial['apps'][market], {'allowed': False, 'visible': False})
        rules = {'com.hihonor.appmarket': {'allowed': True, 'visible': True}}
        response = self.c.put(path, headers=self.alice, json={'apps': rules, 'foreground_kill_enabled': False})
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.json['policy']['foreground_kill_enabled'], False)
        self.assertIs(response.json['policy']['apps']['com.hihonor.appmarket']['allowed'], True)
        # Old clients changing app rules must not reset an explicitly disabled guard.
        response = self.c.put(path, headers=self.alice, json={'apps': {}})
        self.assertIs(response.json['policy']['foreground_kill_enabled'], False)
        with self.app.app_context():
            self.assertEqual(get_db().execute('SELECT foreground_kill_enabled FROM android_devices WHERE device_id=?', (dev['id'],)).fetchone()[0], 0)
        self.assertEqual(self.c.put(path, headers=self.other, json={'apps': {}, 'foreground_kill_enabled': True}).status_code, 403)
        revision = response.json['policy']['revision']
        for value in ('false', 0, 1, None, [], {}):
            self.assertEqual(self.c.put(path, headers=self.alice, json={'apps': {}, 'foreground_kill_enabled': value}).status_code, 400)
        current = self.c.get('/api/android/agent/state', headers=dh).json['policy']
        self.assertEqual(current['revision'], revision)
        self.assertIs(current['foreground_kill_enabled'], False)

    def test_guard_column_migrates_legacy_database_without_losing_devices(self):
        dev, dh = self.pair()
        with self.app.app_context():
            users = [tuple(row) for row in get_db().execute('SELECT * FROM users ORDER BY id')]
            devices = [tuple(row) for row in get_db().execute('SELECT * FROM devices ORDER BY id')]
            get_db().execute('ALTER TABLE android_devices DROP COLUMN foreground_kill_enabled')
            get_db().commit()
        migrated = create_app(dict(self.app.config))
        with migrated.app_context():
            self.assertEqual([tuple(row) for row in get_db().execute('SELECT * FROM users ORDER BY id')], users)
            self.assertEqual([tuple(row) for row in get_db().execute('SELECT * FROM devices ORDER BY id')], devices)
        response = migrated.test_client().get('/api/android/agent/state', headers=dh)
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.json['policy']['foreground_kill_enabled'], True)
        self.assertEqual(response.json['device_id'], dev['id'])

    def test_policy_rejects_non_boolean_missing_extra_and_invalid_package(self):
        dev, _ = self.pair()
        path = f'/api/android/devices/{dev["id"]}/policy'
        for bad in ([], {}, {"apps": []}, {"apps": {"../bad": {"allowed": True, "visible": True}}},
                    {"apps": {"org.a": {"allowed": 1, "visible": True}}},
                    {"apps": {"org.a": {"allowed": True}}},
                    {"apps": {"org.a": {"allowed": True, "visible": True, "shell": "id"}}}):
            with self.subTest(bad=bad):
                self.assertEqual(self.c.put(path, headers=self.alice, json=bad).status_code, 400)
        self.assertEqual(self.c.get(path.rsplit('/', 1)[0], headers=self.alice).json["policy"]["revision"], 0)

    def test_inventory_replacement_summary_and_validation_are_atomic(self):
        dev, dh = self.pair()
        self.assertEqual(self.inventory(dh).status_code, 200)
        path = f'/api/android/devices/{dev["id"]}'
        info = self.c.get(path, headers=self.alice).json
        self.assertEqual(info["apps"][0]["label"], "Reader <script>")
        self.assertEqual(info["device"]["apps_count"], 1)
        self.assertEqual(info["device"]["timezone"], "Asia/Shanghai")
        for badapps in ({}, [self.app_row(launchable=1)], [self.app_row("../bad")],
                        [self.app_row(), self.app_row()], [self.app_row(label="x" * 1001)],
                        [self.app_row("org.p" + str(i)) for i in range(2001)]):
            self.assertEqual(self.inventory(dh, badapps).status_code, 400)
        self.assertEqual(self.inventory(dh, zone="Invalid/Zone").status_code, 400)
        self.assertEqual(self.c.get(path, headers=self.alice).json["device"]["apps_count"], 1)
        self.assertEqual(self.inventory(dh, []).status_code, 200)
        self.assertEqual(self.c.get(path, headers=self.alice).json["apps"], [])

    def test_usage_absolute_snapshot_resend_replace_and_permission_preserve_history(self):
        dev, dh = self.pair()
        path = f'/api/android/devices/{dev["id"]}/usage?date=2026-10-25'
        self.assertIsNone(self.c.get(path, headers=self.alice).json["permission_granted"])
        self.assertEqual(self.snapshot(dh).status_code, 200)
        self.assertEqual(self.snapshot(dh).status_code, 200)
        view = self.c.get(path, headers=self.alice).json
        self.assertEqual(view["apps"][0]["total_ms"], 12000)
        self.assertEqual(sum(x["duration_ms"] for x in view["hours"]), 12000)
        self.assertEqual(len(view["hours"]), 24)
        self.assertEqual(self.snapshot(dh, buckets=[]).status_code, 200)
        self.assertEqual(self.c.get(path, headers=self.alice).json["apps"], [])
        self.assertEqual(self.snapshot(dh).status_code, 200)
        self.assertEqual(self.snapshot(dh, buckets=[], permission_granted=False).status_code, 200)
        view = self.c.get(path, headers=self.alice).json
        self.assertFalse(view["permission_granted"])
        self.assertEqual(view["apps"][0]["total_ms"], 12000)
        self.assertIsNotNone(view["last_synced_at"])

    def test_usage_window_preserves_previous_days(self):
        dev, dh = self.pair()
        self.assertEqual(self.snapshot(dh, buckets=[{"package_name": "org.example.reader", "bucket_start": millis("2026-10-24T01:00:00"), "duration_ms": 8000}]).status_code, 200)
        self.assertEqual(self.snapshot(dh, buckets=[], window_start=millis("2026-10-25T00:00:00")).status_code, 200)
        view = self.c.get(f'/api/android/devices/{dev["id"]}/usage?date=2026-10-24', headers=self.alice).json
        self.assertEqual(view["apps"][0]["total_ms"], 8000)

    def test_invalid_usage_snapshot_never_partially_erases_history(self):
        dev, dh = self.pair()
        self.assertEqual(self.snapshot(dh).status_code, 200)
        invalid = [{"timezone": "Invalid/Zone"}, {"permission_granted": 1},
                   {"window_start": millis("2026-10-24T00:00:00") + 1},
                   {"window_start": millis("2026-10-20T00:00:00")},
                   {"window_end": millis("2026-10-25T05:00:00")},
                   {"buckets": [{"package_name": "org.example.reader", "bucket_start": millis("2026-10-25T01:00:00"), "duration_ms": 3600001}]},
                   {"buckets": [{"package_name": "org.example.reader", "bucket_start": millis("2026-10-25T01:00:00") + 1, "duration_ms": 10}]},
                   {"buckets": [{"package_name": "org.example.reader", "bucket_start": millis("2026-10-25T04:00:00"), "duration_ms": 10}]},
                   {"buckets": [{"package_name": "org.a", "bucket_start": millis("2026-10-25T01:00:00"), "duration_ms": True}]},
                   {"buckets": [{"package_name": "org.a", "bucket_start": millis("2026-10-25T01:00:00"), "duration_ms": 10}] * 10001}]
        for changes in invalid:
            with self.subTest(changes=str(changes)[:180]):
                self.assertEqual(self.snapshot(dh, **changes).status_code, 400)
        view = self.c.get(f'/api/android/devices/{dev["id"]}/usage?date=2026-10-25', headers=self.alice).json
        self.assertEqual(view["apps"][0]["total_ms"], 12000)

    def test_usage_device_local_days_include_dst_23_and_25_hours(self):
        dev, dh = self.pair()
        self.assertEqual(self.inventory(dh, zone="America/New_York").status_code, 200)
        base = f'/api/android/devices/{dev["id"]}/usage'
        spring = self.c.get(base + "?date=2026-03-08", headers=self.alice).json
        fall = self.c.get(base + "?date=2026-11-01", headers=self.alice).json
        self.assertEqual(len(spring["hours"]), 23)
        self.assertEqual(spring["hours"][0]["bucket_start"], millis("2026-03-08T05:00:00"))
        self.assertEqual(len(fall["hours"]), 25)
        self.assertEqual(fall["hours"][-1]["bucket_start"], millis("2026-11-02T04:00:00"))
        for bad in ("2026-02-30", "2026-3-8", "2026-10-25T00:00:00", ""):
            self.assertEqual(self.c.get(base + "?" + urlencode({"date": bad}), headers=self.alice).status_code, 400)

    def test_upload_digest_typed_payload_expiry_blob_scope_and_reopen(self):
        dev, dh = self.pair()
        other, otherdh = self.pair("second", self.other)
        path = f'/api/android/devices/{dev["id"]}/files'
        raw = b"fake PDF\x00\xff"
        response = self.c.post(path + "?filename=note.pdf&open=1&install=0", headers=self.alice, data=raw)
        self.assertEqual(response.status_code, 201)
        file = response.json["file"]
        self.assertEqual(file["size"], 10)
        self.assertEqual(file["sha256"], hashlib.sha256(raw).hexdigest())
        polled = self.c.get("/api/agent/poll", headers=dh).json["tasks"][0]
        self.assertEqual(polled["kind"], "android.file.receive")
        self.assertEqual(polled["payload"], {"file_id": file["id"], "filename": "note.pdf", "mime": "application/pdf", "size": 10,
                                            "sha256": hashlib.sha256(raw).hexdigest(), "blob_token": file["blob_token"], "open": True})
        with self.app.app_context():
            blob = get_db().execute("SELECT * FROM blobs WHERE token=?", (file["blob_token"],)).fetchone()
            self.assertGreaterEqual(blob["expires_at"], int(time.time()) + 29 * 86400)
        url = "/api/agent/fs-blobs/" + file["blob_token"]
        with self.c.get(url, headers=dh) as downloaded:
            self.assertEqual(downloaded.data, raw)
        self.assertEqual(self.c.get(url, headers=otherdh).status_code, 404)
        self.assertEqual(self.c.get(url).status_code, 401)
        self.assertEqual(self.c.post(f'{path}/{file["id"]}/open', headers=self.alice).status_code, 201)
        self.assertEqual(self.c.post(f'/api/android/devices/{other["id"]}/files/{file["id"]}/open', headers=self.other).status_code, 404)
        reopen = self.c.get("/api/agent/poll", headers=dh).json["tasks"][0]
        self.assertEqual(reopen["kind"], "android.file.open")
        self.assertTrue(reopen["payload"]["open"])

    def test_upload_rejects_traversal_invalid_options_and_oversize_without_files(self):
        dev, _ = self.pair()
        path = f'/api/android/devices/{dev["id"]}/files'
        for filename in ("../bad.pdf", "dir/a.txt", "a\\b.txt", ".", "..", "", "a\x00.txt", "x" * 241):
            self.assertEqual(self.c.post(path + "?" + urlencode({"filename": filename}), headers=self.alice, data=b"x").status_code, 400)
        for args in ("filename=a.txt&install=1", "filename=a.apk&open=yes", "filename=a.apk&install=2"):
            self.assertEqual(self.c.post(path + "?" + args, headers=self.alice, data=b"x").status_code, 400)
        with patch("dev_control.android.MAX_FILE_SIZE", 4):
            self.assertEqual(self.c.post(path + "?filename=a.txt", headers=self.alice, data=b"12345").status_code, 413)
        self.assertEqual(self.c.get(path, headers=self.alice).json["files"], [])
        self.assertEqual(list((self.root / "data" / "blobs").iterdir()), [])

    def test_install_waiting_user_then_success_reports_actual_result(self):
        dev, dh = self.pair()
        path = f'/api/android/devices/{dev["id"]}/files'
        r = self.c.post(path + "?filename=app.apk&install=1", headers=self.alice, data=b"APK")
        self.assertEqual(r.status_code, 201)
        task = self.c.get("/api/agent/poll", headers=dh).json["tasks"][0]
        self.assertEqual(task["kind"], "android.app.install")
        resultpath = f'/api/agent/tasks/{task["id"]}/result'
        waiting = {"status": "waiting_user", "claim_token": task["claim_token"], "result": {"state": "pending_user_action", "message": "Tap to confirm"}}
        self.assertEqual(self.c.post(resultpath, headers=dh, json=waiting).status_code, 200)
        with self.app.app_context():
            row = get_db().execute("SELECT * FROM tasks WHERE id=?", (task["id"],)).fetchone()
            self.assertIsNone(row["finished_at"])
        files = self.c.get(path, headers=self.alice).json["files"]
        self.assertEqual(files[0]["status"], "waiting_user")
        self.assertEqual(files[0]["result"]["state"], "pending_user_action")
        done = dict(waiting, status="done", result={"state": "installed"})
        self.assertEqual(self.c.post(resultpath, headers=dh, json=done).status_code, 200)
        self.assertEqual(self.c.post(resultpath, headers=dh, json=done).status_code, 200)
        self.assertEqual(self.c.post(resultpath, headers=dh, json=waiting).status_code, 409)
        self.assertEqual(self.c.get("/api/android/agent/recover-tasks", headers=dh).json["tasks"], [])

    def test_results_require_claim_valid_status_and_result_object(self):
        dev, dh = self.pair()
        path = f'/api/android/devices/{dev["id"]}/policy'
        self.c.put(path, headers=self.alice, json={"apps": {}})
        with self.app.app_context():
            tid = get_db().execute("SELECT id FROM tasks WHERE device_id=?", (dev["id"],)).fetchone()[0]
        resultpath = f'/api/agent/tasks/{tid}/result'
        self.assertEqual(self.c.post(resultpath, headers=dh, json={"status": "done", "result": {}}).status_code, 409)
        task = self.c.get("/api/agent/poll", headers=dh).json["tasks"][0]
        for body in ({"status": "pending", "result": {}}, {"status": "done", "result": []}):
            self.assertEqual(self.c.post(resultpath, headers=dh, json=dict(body, claim_token=task["claim_token"])).status_code, 400)
        self.assertEqual(self.c.post(resultpath, headers=dh, json={"status": "done", "claim_token": "wrong", "result": {}}).status_code, 409)

    def test_recovery_only_own_unfinished_claimed_android_jobs(self):
        dev, dh = self.pair()
        second, _ = self.pair("second", self.other)
        with self.app.app_context():
            db = get_db()
            for did, kind, status in [(dev["id"], "android.file.open", "claimed"), (dev["id"], "android.app.install", "waiting_user"),
                                      (dev["id"], "android.file.receive", "pending"), (dev["id"], "android.policy.sync", "done"),
                                      (dev["id"], "exec", "claimed"), (second["id"], "android.file.open", "claimed")]:
                db.execute("INSERT INTO tasks(device_id,kind,status,claim_token,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                           (did, kind, status, "test-claim" if status != "pending" else None, '{}', int(time.time())))
            db.commit()
        tasks = self.c.get("/api/android/agent/recover-tasks", headers=dh).json["tasks"]
        self.assertEqual([t["kind"] for t in tasks], ["android.file.open", "android.app.install"])
        self.assertEqual([t["status"] for t in tasks], ["claimed", "waiting_user"])
        self.assertTrue(all(t["claim_token"] == "test-claim" for t in tasks))

    def test_recovery_paginates_more_than_one_hundred_waiting_jobs(self):
        dev, headers = self.pair()
        ids = []
        with self.app.app_context():
            db = get_db()
            for _ in range(205):
                cursor = db.execute(
                    "INSERT INTO tasks(device_id,kind,status,claim_token,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                    (dev["id"], "android.file.open", "waiting_user", "claim", "{}", int(time.time())))
                ids.append(cursor.lastrowid)
            db.commit()
        collected = []
        after = 0
        sizes = []
        while True:
            response = self.c.get(f"/api/android/agent/recover-tasks?after_id={after}", headers=headers)
            self.assertEqual(response.status_code, 200)
            batch = response.json["tasks"]
            self.assertLessEqual(len(batch), 100, "A recovery page must fit the Android client's batch bound")
            sizes.append(len(batch))
            collected.extend(task["id"] for task in batch)
            cursor = response.json["next_after"]
            if cursor is None:
                break
            self.assertGreater(cursor, after)
            after = cursor
        self.assertEqual(sizes, [100, 100, 5])
        self.assertEqual(collected, ids)
        for bad in ("-1", "abc", "1.5"):
            self.assertEqual(self.c.get(f"/api/android/agent/recover-tasks?after_id={bad}", headers=headers).status_code, 400)

    def test_poll_never_returns_older_claims_from_same_second(self):
        legacy = self.c.post("/api/agent/enroll", json={"device_uid": "linux"}).json["device"]
        dh = {"X-Device-Token": legacy["device_token"]}
        with patch("dev_control.agents.time.time", return_value=1792886400):
            first = self.c.post(f'/api/devices/{legacy["id"]}/tasks', headers=self.admin, json={"kind": "exec"}).json["id"]
            self.assertEqual([t["id"] for t in self.c.get("/api/agent/poll", headers=dh).json["tasks"]], [first])
            second = self.c.post(f'/api/devices/{legacy["id"]}/tasks', headers=self.admin, json={"kind": "exec"}).json["id"]
            self.assertEqual([t["id"] for t in self.c.get("/api/agent/poll", headers=dh).json["tasks"]], [second])

    def test_android_token_cannot_download_legacy_service_archives(self):
        dev, dh = self.pair()
        archive = self.root / "archive.tar"
        archive.write_bytes(b"private service")
        with self.app.app_context():
            db = get_db()
            owner = db.execute("SELECT id FROM users WHERE username='other'").fetchone()[0]
            sid = db.execute("INSERT INTO services(owner_id,name,archive_path,sha256,created_at) VALUES(?,?,?,?,?)",
                             (owner, "private", str(archive), "a" * 64, int(time.time()))).lastrowid
            db.commit()
        with self.c.get(f'/api/agent/services/{sid}/archive', headers=dh) as response:
            self.assertEqual(response.status_code, 403)

    def test_inventory_drops_old_deny_rule_when_app_becomes_protected(self):
        dev, dh = self.pair()
        path = f'/api/android/devices/{dev["id"]}'
        self.inventory(dh)
        self.c.put(path + "/policy", headers=self.alice, json={"apps": {"org.example.reader": {"allowed": False, "visible": False}}})
        self.assertEqual(self.inventory(dh, [self.app_row(protected=True)]).status_code, 200)
        state = self.c.get("/api/android/agent/state", headers=dh).json["policy"]
        self.assertEqual(state["apps"], self.effective_rules({}))
        self.assertEqual(state["revision"], 2)

    def test_markets_reported_as_protected_are_editable_in_detail(self):
        dev, dh = self.pair()
        markets = ['com.hihonor.appmarket', 'com.huawei.appmarket']
        apps = [self.app_row(pkg, label='应用市场', protected=True) for pkg in markets]
        apps += [self.app_row('com.hihonor.packageinstaller', protected=True),
                 self.app_row('org.example.keyboard', protected=True)]
        self.assertEqual(self.inventory(dh, apps).status_code, 200)
        response = self.c.get(f'/api/android/devices/{dev["id"]}', headers=self.alice)
        self.assertEqual(response.status_code, 200)
        protected = {app['package_name']:app['protected'] for app in response.json['apps']}
        for pkg in markets:
            self.assertFalse(protected[pkg], pkg)
        self.assertTrue(protected['com.hihonor.packageinstaller'])
        self.assertTrue(protected['org.example.keyboard'])

    def test_stale_market_protection_does_not_block_independent_policy_flags(self):
        dev, dh = self.pair()
        path = f'/api/android/devices/{dev["id"]}'
        markets = ['com.hihonor.appmarket', 'com.huawei.appmarket']
        stale = [self.app_row(pkg, label='应用市场', protected=True) for pkg in markets]
        # Simulate a device paired before the corrected server was deployed:
        # GET and PUT must work before another inventory upload arrives.
        with self.app.app_context():
            get_db().execute('UPDATE android_devices SET apps_json=? WHERE device_id=?', (json.dumps(stale), dev['id']))
            get_db().commit()
        detail = self.c.get(path, headers=self.alice).json
        self.assertTrue(all(app['protected'] is False for app in detail['apps']))
        for pkg in markets:
            for allowed, visible in ((False,False),(False,True),(True,False),(True,True)):
                with self.subTest(package=pkg, allowed=allowed, visible=visible):
                    rules = {pkg:{'allowed':allowed,'visible':visible},
                             'org.example.reader':{'allowed':False,'visible':True}}
                    response = self.c.put(path+'/policy', headers=self.alice, json={'apps':rules})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json['policy']['apps'], self.effective_rules(rules))
                    self.assertEqual(self.c.get('/api/android/agent/state', headers=dh).json['policy'], response.json['policy'])
        self.assertEqual(self.c.put(path+'/policy', headers=self.other, json={'apps':{markets[0]:{'allowed':False,'visible':False}}}).status_code, 403)
        with self.app.app_context():
            persisted = json.loads(get_db().execute('SELECT apps_json FROM android_devices WHERE device_id=?',(dev['id'],)).fetchone()[0])
            self.assertTrue(all(app['protected'] is True for app in persisted))

    def test_stale_market_inventory_preserves_denial_while_true_keyboard_protection_recovers(self):
        dev, dh = self.pair()
        path = f'/api/android/devices/{dev["id"]}'
        markets = ['com.hihonor.appmarket','com.huawei.appmarket']
        rows = [self.app_row(pkg) for pkg in markets]+[self.app_row('org.example.keyboard')]
        self.assertEqual(self.inventory(dh, rows).status_code, 200)
        rules = {pkg:{'allowed':False,'visible':False} for pkg in markets+['org.example.keyboard']}
        applied = self.c.put(path+'/policy',headers=self.alice,json={'apps':rules})
        self.assertEqual(applied.status_code, 200)
        for row in rows: row['protected']=True
        self.assertEqual(self.inventory(dh, rows).status_code, 200)
        state = self.c.get('/api/android/agent/state',headers=dh).json['policy']
        self.assertEqual(state['apps'],{pkg:rules[pkg] for pkg in markets})
        self.assertEqual(state['revision'],applied.json['policy']['revision']+1)
        # A repeated stale upload must not erase either market's policy or bump
        # the policy revision merely because an installer handler is protected.
        self.assertEqual(self.inventory(dh, rows).status_code,200)
        self.assertEqual(self.c.get('/api/android/agent/state',headers=dh).json['policy'],state)

    def test_real_installers_settings_home_and_dynamic_recovery_remain_protected(self):
        dev, dh = self.pair()
        path = f'/api/android/devices/{dev["id"]}'
        fixed = ['android','com.android.settings','com.android.packageinstaller',
                 'com.google.android.packageinstaller','com.hihonor.packageinstaller',
                 'com.android.permissioncontroller','com.google.android.permissioncontroller',
                 'com.hihonor.android.launcher','com.huawei.android.launcher',
                 'app.lawnchair.managed','local.lawnchair.homebridge']
        dynamic = ['org.example.currenthome','org.example.currentime','com.huawei.coauthservice']
        self.assertEqual(self.inventory(dh,[self.app_row(pkg,protected=pkg in dynamic) for pkg in fixed+dynamic]).status_code,200)
        detail = self.c.get(path,headers=self.alice).json
        self.assertTrue(all(app['protected'] for app in detail['apps']))
        for pkg in fixed+dynamic:
            for rule in ({'allowed':False,'visible':True},{'allowed':True,'visible':False}):
                with self.subTest(package=pkg,rule=rule):
                    self.assertEqual(self.c.put(path+'/policy',headers=self.alice,json={'apps':{pkg:rule}}).status_code,400)

    def test_usage_rejects_duplicate_keys_negative_durations_and_malformed_rows(self):
        _, dh = self.pair()
        bucket = {"package_name": "org.example.reader", "bucket_start": millis("2026-10-25T01:00:00"), "duration_ms": 10}
        for rows in ([bucket, bucket], [dict(bucket, duration_ms=-1)], [dict(bucket, package_name="../bad")], ["not an object"]):
            self.assertEqual(self.snapshot(dh, buckets=rows).status_code, 400)
        self.assertEqual(self.snapshot(dh, permission_granted=False).status_code, 400)

    def test_terminal_results_are_scoped_to_device_token_and_device_owner(self):
        dev, dh = self.pair()
        _, otherdh = self.pair("second", self.other)
        files = f'/api/android/devices/{dev["id"]}/files'
        uploaded = self.c.post(files + "?filename=private.pdf&open=1", headers=self.alice, data=b"private")
        self.assertEqual(uploaded.status_code, 201)
        task = self.c.get("/api/agent/poll", headers=dh).json["tasks"][0]
        path = f'/api/agent/tasks/{task["id"]}/result'
        result = {"claim_token": task["claim_token"], "status": "done", "result": {"state": "open_requested", "message": "private result"}}
        self.assertEqual(self.c.post(path, headers=otherdh, json=result).status_code, 404)
        self.assertEqual(self.c.post(path, headers=dh, json=result).status_code, 200)
        self.assertEqual(self.c.get(files, headers=self.alice).json["files"][0]["result"]["message"], "private result")
        self.assertEqual(self.c.get(files, headers=self.other).status_code, 403)
        self.assertEqual(self.c.get(f'/api/devices/{dev["id"]}/tasks', headers=self.other).status_code, 403)

    def test_management_page_and_openapi_include_android_contract(self):
        self.assertEqual(self.c.get("/android/").status_code, 200)
        spec = self.c.get("/api/openapi.json", headers=self.alice).json
        for path in ("/api/android/pair", "/api/android/pairing-codes", "/api/android/devices", "/api/android/devices/{device_id}",
                     "/api/android/devices/{device_id}/policy", "/api/android/devices/{device_id}/usage", "/api/android/devices/{device_id}/files",
                     "/api/android/devices/{device_id}/files/{file_id}/open", "/api/android/agent/state", "/api/android/agent/inventory",
                     "/api/android/agent/usage", "/api/android/agent/recover-tasks"):
            self.assertIn(path, spec["paths"])
        self.assertIn("deviceToken", spec["components"]["securitySchemes"])
        self.assertEqual(spec["paths"]["/api/android/agent/state"]["get"]["security"], [{"deviceToken": []}])


if __name__ == "__main__":
    unittest.main()

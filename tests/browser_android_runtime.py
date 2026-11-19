"""Optional isolated Chromium runtime checks. Run with /usr/bin/python3."""
import json
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dev_control import create_app
from dev_control.db import get_db
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server, WSGIRequestHandler


class QuietHandler(WSGIRequestHandler):
    def log(self, *args, **kwargs):
        pass


class RuntimeBrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="runtime-browser-")
        cls.app = create_app({"TESTING": True, "ENABLE_LEGACY_TOOLS": False,
                              "DATA_DIR": cls.tmp.name + "/data", "SERVER_FILES_ROOT": cls.tmp.name + "/files",
                              "ADMIN_PASSWORD": "runtime-browser-password"})
        cls.c = cls.app.test_client()
        cls.token = cls.c.post("/api/auth/login", json={"username": "admin", "password": "runtime-browser-password"}).json["token"]
        cls.auth = {"Authorization": "Bearer " + cls.token}
        cls.devices = []
        for suffix in ("A", "B"):
            code = cls.c.post("/api/android/pairing-codes", headers=cls.auth).json["code"]
            device = cls.c.post("/api/android/pair", json={"code": code, "device_uid": suffix, "name": "Tablet " + suffix}).json["device"]
            cls.devices.append((device, {"X-Device-Token": device["device_token"]}, "/api/android/devices/" + str(device["id"])))
        cls.server = make_server("127.0.0.1", 0, cls.app, threaded=True, request_handler=QuietHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = "http://127.0.0.1:" + str(cls.server.server_port)
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)
        cls.evidence = Path("/tmp/remote-app-control-server-evidence/browser")
        cls.evidence.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.server.shutdown()
        cls.thread.join()
        cls.tmp.cleanup()

    def setUp(self):
        self.identities = [str(uuid.uuid4()) + ":1", str(uuid.uuid4()) + ":1"]
        with self.app.app_context():
            get_db().execute("DELETE FROM tasks")
            get_db().execute("DELETE FROM android_runtime")
            get_db().commit()
        for index in (0, 1):
            self.inventory(index, "Reader " + "AB"[index] + " <img src=x onerror=alert(1)>")
            self.report(index)
        self.context = self.browser.new_context(viewport={"width": 1000, "height": 1000}, locale="zh-CN")
        self.context.add_init_script("localStorage.setItem('devControlToken', " + json.dumps(self.token) + ")")
        self.context.tracing.start(screenshots=True, snapshots=True)
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.set_default_timeout(8000)

    def tearDown(self):
        self.context.tracing.stop(path=str(self.evidence / (self._testMethodName + ".zip")))
        self.context.close()
        self.assertEqual(self.errors, [])

    def inventory(self, index, label):
        return self.c.post("/api/android/agent/inventory", headers=self.devices[index][1], json={
            "name": "Tablet " + "AB"[index], "timezone": "Asia/Shanghai", "capabilities": {"accessibility_enabled": True},
            "apps": [{"package_name": package, "label": label if package.endswith("reader") else "Recent only",
                      "version_name": "1", "version_code": 1, "system": False, "protected": False, "launchable": True}
                     for package in ("org.example.reader", "org.example.recent")]})

    def report(self, index, **extra):
        return self.c.post("/api/android/agent/runtime", headers=self.devices[index][1], json={
            "observation_id": self.identities[index], "state": "foreground", "package_name": "org.example.reader",
            "observed_at": 1000, "can_force_stop": True,
            "recent": [{"package_name": "org.example.recent", "last_seen": 900}], **extra})

    def open(self):
        self.page.goto(self.origin + "/android/")
        self.page.wait_for_load_state("networkidle")

    def hold_next(self, suffix, method="GET"):
        self.page.evaluate("""({suffix,method})=>{const original=window.fetch;let once=true;window.fetch=async(url,options={})=>{
          const response=await original(url,options);
          if(once&&String(url).endsWith(suffix)&&(options.method||'GET')===method){once=false;return await new Promise(resolve=>{
            window.releaseHeld=()=>{resolve(response);delete window.releaseHeld;};});}return response;};}""", {"suffix": suffix, "method": method})

    def test_current_only_action_uses_captured_identity_and_renders_actual_results_safely(self):
        self.open()
        expect(self.page.locator("#runtimeCurrent")).to_contain_text("Reader A <img")
        expect(self.page.locator("#runtimeCurrent img")).to_have_count(0)
        expect(self.page.locator("#runtimeRecent")).to_contain_text("Recent only")
        expect(self.page.locator("#runtimeRecent button")).to_have_count(0)
        expect(self.page.locator("#forceStop")).to_be_enabled()
        self.page.locator("#forceStop").click()
        expect(self.page.locator("#runtimeResults")).to_contain_text("等待设备领取")
        task = self.c.get("/api/agent/poll", headers=self.devices[0][1]).json["tasks"][0]
        self.assertEqual(task["payload"]["observation_id"], self.identities[0])
        self.c.post("/api/agent/tasks/" + str(task["id"]) + "/result", headers=self.devices[0][1], json={
            "claim_token": task["claim_token"], "status": "error", "result": {"state": "foreground_changed", "message": "前台已变化 <script>"}})
        self.page.locator("#refresh").click()
        expect(self.page.locator("#runtimeResults")).to_contain_text("前台已变化 <script>")
        expect(self.page.locator("#runtimeResults script")).to_have_count(0)
        self.page.locator("#forceStop").click()
        task = self.c.get("/api/agent/poll", headers=self.devices[0][1]).json["tasks"][0]
        self.c.post("/api/agent/tasks/" + str(task["id"]) + "/result", headers=self.devices[0][1], json={
            "claim_token": task["claim_token"], "status": "done", "result": {"state": "force_stopped", "message": "已验证停止"}})
        self.page.locator("#refresh").click()
        expect(self.page.locator("#runtimeResults")).to_contain_text("已验证停止")
        self.page.screenshot(path=str(self.evidence / "runtime-action-results.png"), full_page=True)

    def test_stale_locked_no_a11y_missing_launcher_and_old_server_states(self):
        self.open()
        with self.app.app_context():
            get_db().execute("UPDATE android_runtime SET received_at=received_at-31000")
            get_db().commit()
        self.page.locator("#refresh").click()
        expect(self.page.locator("#runtimeNote")).to_contain_text("过期")
        expect(self.page.locator("#runtimeCurrent")).to_contain_text("上次观测")
        expect(self.page.locator("#forceStop")).to_be_disabled()
        for seq, state, want in ((2, "locked", "锁屏"), (3, "unavailable", "无障碍")):
            self.identities[0] = self.identities[0].rsplit(":", 1)[0] + ":" + str(seq)
            self.report(0, state=state, package_name="", can_force_stop=False)
            self.page.locator("#refresh").click()
            expect(self.page.locator("#runtimeNote")).to_contain_text(want)
            expect(self.page.locator("#forceStop")).to_be_disabled()
        with self.app.app_context():
            get_db().execute("UPDATE devices SET online=0")
            get_db().commit()
        self.page.locator("#refresh").click()
        expect(self.page.locator("#runtimeNote")).to_contain_text("离线")
        expect(self.page.locator("#forceStop")).to_be_disabled()
        self.report(0, state="unavailable", package_name="", can_force_stop=False)
        with self.app.app_context():
            get_db().execute("DELETE FROM android_runtime")
            get_db().commit()
        self.page.locator("#refresh").click()
        expect(self.page.locator("#runtimeNote")).to_contain_text("更新平板 Launcher")
        self.page.route("**/runtime", lambda route: route.fulfill(status=404, content_type="text/html", body="old server"))
        self.page.locator("#refresh").click()
        expect(self.page.locator("#runtimeNote")).to_contain_text("更新服务器")
        expect(self.page.locator("#apps")).to_contain_text("Reader A")
        expect(self.page.locator("#message")).to_have_text("")

    def test_delayed_runtime_and_stop_callbacks_cannot_survive_a_b_a_selection(self):
        self.open()
        self.hold_next("/runtime")
        self.page.evaluate("void loadRuntime()")
        self.page.wait_for_function("typeof window.releaseHeld==='function'")
        self.inventory(0, "NEW Reader A")
        self.page.locator("#deviceSelect").select_option(str(self.devices[1][0]["id"]))
        expect(self.page.locator("#runtimeCurrent")).to_contain_text("Reader B")
        self.page.locator("#deviceSelect").select_option(str(self.devices[0][0]["id"]))
        expect(self.page.locator("#runtimeCurrent")).to_contain_text("NEW Reader A")
        self.page.evaluate("window.releaseHeld()")
        self.page.wait_for_timeout(150)
        expect(self.page.locator("#runtimeCurrent")).to_contain_text("NEW Reader A")
        self.hold_next("/apps/force-stop", "POST")
        self.page.locator("#forceStop").click()
        self.page.wait_for_function("typeof window.releaseHeld==='function'")
        self.page.locator("#deviceSelect").select_option(str(self.devices[1][0]["id"]))
        expect(self.page.locator("#runtimeCurrent")).to_contain_text("Reader B")
        self.page.locator("#deviceSelect").select_option(str(self.devices[0][0]["id"]))
        expect(self.page.locator("#runtimeCurrent")).to_contain_text("NEW Reader A")
        expect(self.page.locator("#runtimeAction")).to_have_text("")
        self.page.evaluate("window.releaseHeld()")
        self.page.wait_for_timeout(150)
        expect(self.page.locator("#runtimeAction")).to_have_text("")
        expect(self.page.locator("#message")).to_have_text("")

    def test_five_second_runtime_refresh_preserves_policy_draft_and_expires_locally(self):
        self.open()
        toggle = self.page.locator("#apps input").first
        toggle.uncheck()
        self.inventory(0, "AUTO Reader A")
        expect(self.page.locator("#runtimeCurrent")).to_contain_text("AUTO Reader A", timeout=7000)
        expect(toggle).not_to_be_checked()
        expect(self.page.locator("#policyStatus")).to_contain_text("未保存")
        # Block subsequent runtime requests and let the already received lease expire.
        self.page.route("**/runtime", lambda route: route.abort())
        self.page.clock.install()
        self.page.clock.fast_forward(31000)
        expect(self.page.locator("#forceStop")).to_be_disabled()
        expect(self.page.locator("#runtimeNote")).to_contain_text("过期")

    def test_runtime_response_latency_counts_toward_snapshot_expiry(self):
        self.open()
        expect(self.page.locator("#forceStop")).to_be_enabled()
        self.hold_next("/runtime")
        self.page.evaluate("() => {window.delayedRuntime=loadRuntime();}")
        self.page.wait_for_function("typeof window.releaseHeld==='function'")
        # Advance only the external monotonic clock dependency, so refresh
        # intervals cannot replace the held response and hide the regression.
        self.page.evaluate("""() => {
          const now=performance.now.bind(performance);
          Object.defineProperty(performance,'now',{value:()=>now()+31000});
          window.releaseHeld();
          return window.delayedRuntime;
        }""")
        expect(self.page.locator("#forceStop")).to_be_disabled()
        expect(self.page.locator("#runtimeCurrent")).to_contain_text("上次观测")
        expect(self.page.locator("#runtimeNote")).to_contain_text("过期")


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path

from dev_control import create_app


class XiaobuProgressTest(unittest.TestCase):
    path = "/api/xiaobu-words/progress"
    device = "bc45ef7e-fd09-499b-a98c-76130dc6a9d2"

    def setUp(self):
        storage = tempfile.TemporaryDirectory()
        self.addCleanup(storage.cleanup)
        root = Path(storage.name)
        app = create_app({"TESTING": True, "ENABLE_LEGACY_TOOLS": False,
                          "DATA_DIR": str(root / "data"), "SERVER_FILES_ROOT": str(root / "files"),
                          "ADMIN_PASSWORD": "test-password"})
        self.client = app.test_client()
        admin = self.login("admin")
        user = self.client.post("/api/auth/users", headers=admin,
                                json={"username": "learner", "password": "test-password"}).json
        learner = self.login("learner")
        token = self.client.post(f"/api/user-management/users/{user['id']}/tokens", headers=learner,
                                 json={"label": "Pad"}).json["token"]
        self.learner = {"Authorization": f"Bearer {token}"}
        self.admin = admin
        self.device_path = f"{self.path}/{self.device}"

    def login(self, username):
        token = self.client.post("/api/auth/login", json={"username": username, "password": "test-password"}).json["token"]
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def record(kind, key, data):
        return {"kind": kind, "key": key, "data": data}

    def upload(self, revision, records):
        return self.client.put(self.device_path, headers=self.learner, json={
            "schema_version": 1, "device_name": "Test Pad", "revision": revision, "records": records})

    def test_incremental_retry_and_account_isolation(self):
        course = self.record("course", "body-front", {"solved": ["head"], "mistakes": 2})
        review = self.record("review_word", "review-a", {"term": "head", "tracks": {"WORD": {"mistakes": 2}}})
        self.assertEqual(self.client.get(self.path).status_code, 401)
        self.assertEqual(self.upload(1, [course, review]).json["received"], 2)
        self.assertEqual(self.upload(1, [course, review]).status_code, 200)
        revised = self.record("course", "body-front", {"solved": ["head", "arm"], "mistakes": 3})
        self.assertEqual(self.upload(2, [revised]).status_code, 200)
        self.assertEqual(self.upload(1, [course]).status_code, 200)

        self.assertEqual(self.client.get(self.path, headers=self.admin).json["devices"], [])
        self.assertEqual(self.client.get(self.device_path, headers=self.admin).status_code, 404)
        devices = self.client.get(self.path, headers=self.learner).json["devices"]
        self.assertEqual([(row["name"], row["record_count"]) for row in devices], [("Test Pad", 2)])

        first = self.client.get(self.device_path + "?limit=1", headers=self.learner)
        self.assertEqual(first.headers["Cache-Control"], "no-store")
        self.assertEqual(first.json["records"][0]["data"], revised["data"])
        second = self.client.get(self.device_path + f"?after={first.json['next_after']}", headers=self.learner)
        self.assertEqual(second.json["records"][0]["data"], review["data"])
        self.assertIsNone(second.json["next_after"])

    def test_invalid_batch_never_creates_device_or_partially_writes(self):
        row = self.record("course", "body-front", {})
        self.assertEqual(self.upload(1, [row, row]).status_code, 400)
        self.assertEqual(self.upload(1, [row, self.record("other", "key", {})]).status_code, 400)
        oversized = self.record("course", "body-front", {"blob": "a" * (512 * 1024)})
        self.assertEqual(self.upload(1, [oversized]).status_code, 413)
        self.assertEqual(self.client.get(self.path, headers=self.learner).json["devices"], [])

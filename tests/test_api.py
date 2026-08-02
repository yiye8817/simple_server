import tempfile, unittest
from pathlib import Path
from dev_control import create_app

class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); root=Path(self.tmp.name)
        self.app=create_app({"TESTING":True,"ENABLE_LEGACY_TOOLS":False,"DATA_DIR":str(root/"data"),"SERVER_FILES_ROOT":str(root/"files"),"ADMIN_PASSWORD":"test-password"})
        self.c=self.app.test_client()
        r=self.c.post("/api/auth/login",json={"username":"admin","password":"test-password"})
        self.assertEqual(r.status_code,200); self.token=r.json["token"]; self.auth={"Authorization":"Bearer "+self.token}
    def tearDown(self): self.tmp.cleanup()
    def test_file_roundtrip_and_traversal(self):
        r=self.c.put("/api/files/content?path=a/test.drawio",data=b"<mxfile/>",headers=self.auth);self.assertEqual(r.status_code,200)
        r=self.c.get("/api/files/content?path=a/test.drawio",headers=self.auth);self.assertEqual(r.data,b"<mxfile/>")
        self.assertEqual(self.c.get("/api/files?path=../../etc",headers=self.auth).status_code,400)
    def test_agent_contract(self):
        r=self.c.post("/api/agent/enroll",json={"device_uid":"dev-1","hostname":"box"});self.assertEqual(r.status_code,200)
        token=r.json["device"]["device_token"]; dh={"X-Device-Token":token}
        self.assertEqual(self.c.post("/api/agent/register",json={"hostname":"box"},headers=dh).status_code,200)
        dev=self.c.get("/api/devices",headers=self.auth).json["devices"][0]
        task=self.c.post(f'/api/devices/{dev["id"]}/tasks',json={"kind":"exec","payload":{"command":"true"}},headers=self.auth);self.assertEqual(task.status_code,201)
        polled=self.c.get("/api/agent/poll?wait=0",headers=dh).json["tasks"];self.assertEqual(polled[0]["kind"],"exec")
        result=self.c.post(f'/api/agent/tasks/{task.json["id"]}/result',json={"status":"done","claim_token":polled[0]["claim_token"]},headers=dh);self.assertEqual(result.status_code,200)
    def test_typing_word_has_chinese_translation(self):
        r=self.c.get("/api/typing/word",headers=self.auth)
        self.assertEqual(r.status_code,200)
        self.assertTrue(r.json["word"])
        self.assertTrue(r.json["chinese"])
        self.assertIn("pronunciation",r.json)
        self.assertGreater(r.json["word_count"],4000)

if __name__ == "__main__": unittest.main()

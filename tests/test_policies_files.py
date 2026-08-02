import tempfile, unittest
from pathlib import Path
from dev_control import create_app

class PoliciesFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name);self.root=root
        self.app=create_app({"TESTING":True,"ENABLE_LEGACY_TOOLS":False,"DATA_DIR":str(root/"data"),"SERVER_FILES_ROOT":str(root/"files"),"ADMIN_PASSWORD":"test-password"});self.c=self.app.test_client();t=self.c.post("/api/auth/login",json={"username":"admin","password":"test-password"}).json["token"];self.h={"Authorization":"Bearer "+t}
    def tearDown(self):self.tmp.cleanup()
    def test_scenario_get_and_evaluate(self):
        r=self.c.post("/api/access-policies/scenarios",headers=self.h,json={"name":"deploy","description":"部署目标","whitelist":["prod-a","prod-b"],"blacklist":["prod-b"]});self.assertEqual(r.status_code,201)
        d=self.c.get("/api/access-policies/scenarios/deploy",headers=self.h).json;self.assertEqual(d["whitelist"],["prod-a","prod-b"])
        self.assertTrue(self.c.post("/api/access-policies/evaluate",headers=self.h,json={"scenario":"deploy","value":"prod-a"}).json["allowed"])
        self.assertFalse(self.c.post("/api/access-policies/evaluate",headers=self.h,json={"scenario":"deploy","value":"prod-b"}).json["allowed"])
    def test_bulk_delete(self):
        files=self.root/"files";(files/"folder").mkdir(parents=True);(files/"a.txt").write_text("a");(files/"folder"/"b.txt").write_text("b")
        r=self.c.post("/api/files/bulk-delete",headers=self.h,json={"paths":["a.txt","folder"]});self.assertEqual(r.status_code,200);self.assertEqual(r.json["deleted"],2);self.assertFalse((files/"a.txt").exists());self.assertFalse((files/"folder").exists())

if __name__=="__main__":unittest.main()

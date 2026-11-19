import tempfile, unittest
from pathlib import Path
from dev_control import create_app

class NotesTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        self.app=create_app({"TESTING":True,"ENABLE_LEGACY_TOOLS":False,"DATA_DIR":str(root/"data"),"SERVER_FILES_ROOT":str(root/"files"),"ADMIN_PASSWORD":"test-password"})
        self.c=self.app.test_client()
        login=self.c.post("/api/auth/login",json={"username":"admin","password":"test-password"})
        self.admin={"Authorization":"Bearer "+login.json["token"]}
    def tearDown(self):self.tmp.cleanup()
    def test_notes_crud(self):
        r=self.c.post("/api/notes",headers=self.admin,json={"title":"购物","content":"牛奶"})
        self.assertEqual(r.status_code,201);nid=r.json["id"];self.assertEqual(r.json["title"],"购物")
        listed=self.c.get("/api/notes",headers=self.admin).json["notes"]
        self.assertEqual(len(listed),1);self.assertEqual(listed[0]["content"],"牛奶")
        patched=self.c.patch(f"/api/notes/{nid}",headers=self.admin,json={"title":"备忘","content":"面包"})
        self.assertEqual(patched.status_code,200);self.assertEqual(patched.json["title"],"备忘")
        one=self.c.get(f"/api/notes/{nid}",headers=self.admin);self.assertEqual(one.json["content"],"面包")
        self.assertEqual(self.c.delete(f"/api/notes/{nid}",headers=self.admin).status_code,200)
        self.assertEqual(self.c.get("/api/notes",headers=self.admin).json["notes"],[])
    def test_notes_isolated_and_validation(self):
        self.c.post("/api/auth/users",headers=self.admin,json={"username":"alice","password":"alice-pass","role":"user"})
        alice_token=self.c.post("/api/auth/login",json={"username":"alice","password":"alice-pass"}).json["token"]
        alice={"Authorization":"Bearer "+alice_token}
        admin_note=self.c.post("/api/notes",headers=self.admin,json={"title":"仅管理员"}).json["id"]
        alice_note=self.c.post("/api/notes",headers=alice,json={"title":"仅 Alice","content":"私密"}).json["id"]
        self.assertEqual([x["title"] for x in self.c.get("/api/notes",headers=alice).json["notes"]],["仅 Alice"])
        self.assertEqual(self.c.get(f"/api/notes/{admin_note}",headers=alice).status_code,404)
        self.assertEqual(self.c.delete(f"/api/notes/{alice_note}",headers=self.admin).status_code,404)
        self.assertEqual(self.c.post("/api/notes",headers=self.admin,json={"title":"  "}).status_code,400)
    def test_table_note(self):
        r=self.c.post("/api/notes",headers=self.admin,json={"title":"日程","kind":"table"})
        self.assertEqual(r.status_code,201);self.assertEqual(r.json["kind"],"table");self.assertIn("headers",r.json["content"])
        nid=r.json["id"];body='{"headers":["日","事项"],"rows":[["周一","开会"]]}'
        patched=self.c.patch(f"/api/notes/{nid}",headers=self.admin,json={"content":body,"kind":"table"})
        self.assertEqual(patched.status_code,200);self.assertEqual(patched.json["kind"],"table");self.assertIn("开会",patched.json["content"])
        self.assertEqual(self.c.post("/api/notes",headers=self.admin,json={"title":"x","kind":"other"}).status_code,400)
    def test_notes_page(self):
        self.assertEqual(self.c.get("/notes/").status_code,200)

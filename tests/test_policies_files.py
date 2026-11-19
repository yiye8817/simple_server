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
    def test_listing_preview_metadata(self):
        files=self.root/"files";(files/"a.txt").write_text("hello",encoding="utf-8");(files/"pic.png").write_bytes(b"\x89PNG\r\n");(files/"bin.dat").write_bytes(b"\x00\x01");(files/"plan.drawio").write_text("<mxfile/>",encoding="utf-8")
        items={x["name"]:x for x in self.c.get("/api/files",headers=self.h).json["items"]}
        self.assertEqual(items["a.txt"]["preview"],"text");self.assertTrue(str(items["a.txt"].get("mime") or "").startswith("text/"))
        self.assertEqual(items["pic.png"]["preview"],"image");self.assertIsNone(items["bin.dat"]["preview"])
        self.assertEqual(items["plan.drawio"]["preview"],"drawio")
        body=self.c.get("/api/files/content?path=a.txt",headers=self.h);self.assertEqual(body.data,b"hello")
        self.assertNotIn("attachment", (body.headers.get("Content-Disposition") or "").lower())
    def test_preview_kinds_and_size_limit(self):
        files=self.root/"files"
        (files/"a.json").write_text('{"a":1}',encoding="utf-8")
        (files/"readme.md").write_text("# hi",encoding="utf-8")
        (files/"app.py").write_text("print(1)",encoding="utf-8")
        (files/"song.mp3").write_bytes(b"ID3")
        (files/"clip.mp4").write_bytes(b"\x00\x00")
        items={x["name"]:x for x in self.c.get("/api/files",headers=self.h).json["items"]}
        self.assertEqual(items["a.json"]["preview"],"json")
        self.assertEqual(items["readme.md"]["preview"],"markdown")
        self.assertEqual(items["app.py"]["preview"],"code")
        self.assertEqual(items["song.mp3"]["preview"],"audio")
        self.assertEqual(items["clip.mp4"]["preview"],"video")
        from dev_control import files as fmod
        old=fmod.PREVIEW_MAX
        fmod.PREVIEW_MAX=3
        try:
            r=self.c.get("/api/files/content?preview=1&path=a.json",headers=self.h)
            self.assertEqual(r.status_code,413)
        finally:
            fmod.PREVIEW_MAX=old
        self.assertEqual(self.c.get("/api/files/content?download=1&path=a.json",headers=self.h).status_code,200)
        ok=self.c.get("/api/files/content?preview=1&path=a.json",headers=self.h)
        self.assertEqual(ok.status_code,200);self.assertEqual(ok.data,b'{"a":1}')
    def test_bulk_delete(self):
        files=self.root/"files";(files/"folder").mkdir(parents=True);(files/"a.txt").write_text("a");(files/"folder"/"b.txt").write_text("b")
        r=self.c.post("/api/files/bulk-delete",headers=self.h,json={"paths":["a.txt","folder"]});self.assertEqual(r.status_code,200);self.assertEqual(r.json["deleted"],2);self.assertFalse((files/"a.txt").exists());self.assertFalse((files/"folder").exists())

if __name__=="__main__":unittest.main()

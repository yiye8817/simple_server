import io, tarfile, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from dev_control import create_app

def archive(files):
    buf=io.BytesIO()
    with tarfile.open(fileobj=buf,mode="w:gz") as tar:
        for name,data in files.items():
            raw=data.encode();info=tarfile.TarInfo(name);info.size=len(raw);info.mode=0o644;tar.addfile(info,io.BytesIO(raw))
    buf.seek(0);return buf

class UpdatesTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();base=Path(self.tmp.name);self.project=base/"project";self.project.mkdir();(self.project/"VERSION").write_text("1.0.0");(self.project/"same.txt").write_text("same");(self.project/"change.txt").write_text("old")
        self.app=create_app({"TESTING":True,"ENABLE_LEGACY_TOOLS":False,"DATA_DIR":str(base/"data"),"SERVER_FILES_ROOT":str(base/"files"),"ADMIN_PASSWORD":"test-password"});self.c=self.app.test_client();t=self.c.post("/api/auth/login",json={"username":"admin","password":"test-password"}).json["token"];self.h={"Authorization":"Bearer "+t};self.patch=patch("dev_control.updates.project_root",return_value=self.project);self.patch.start()
    def tearDown(self):self.patch.stop();self.tmp.cleanup()
    def test_stage_compare_apply_and_rollback(self):
        pkg=archive({"./VERSION":"1.1.0\n","./same.txt":"same","./change.txt":"new","./added.txt":"added"});r=self.c.post("/api/updates/upload",headers=self.h,data={"version":"1.1.0","file":(pkg,"update.tar.gz")},content_type="multipart/form-data");self.assertEqual(r.status_code,201);self.assertEqual(r.json["summary"],{"added":1,"modified":2,"unchanged":1,"deleted":0});uid=r.json["id"]
        applied=self.c.post(f"/api/updates/{uid}/apply",headers=self.h,json={"restart":False});self.assertEqual(applied.status_code,200);self.assertEqual((self.project/"change.txt").read_text(),"new");self.assertTrue((self.project/"added.txt").exists())
        rolled=self.c.post(f"/api/updates/{uid}/rollback",headers=self.h);self.assertEqual(rolled.status_code,200);self.assertEqual((self.project/"change.txt").read_text(),"old");self.assertFalse((self.project/"added.txt").exists())
    def test_reject_link(self):
        buf=io.BytesIO()
        with tarfile.open(fileobj=buf,mode="w:gz") as tar:
            info=tarfile.TarInfo("bad-link");info.type=tarfile.SYMTYPE;info.linkname="/etc/passwd";tar.addfile(info)
        buf.seek(0);r=self.c.post("/api/updates/upload",headers=self.h,data={"version":"bad","file":(buf,"bad.tar.gz")},content_type="multipart/form-data");self.assertEqual(r.status_code,400)

if __name__=="__main__":unittest.main()

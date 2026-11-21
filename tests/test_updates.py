import io, shutil, tarfile, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from dev_control import create_app
from dev_control.updates import version_from_filename

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
        pkg=archive({"./VERSION":"1.1.0\n","./same.txt":"same","./change.txt":"new","./added.txt":"added"});r=self.c.post("/api/updates/upload",headers=self.h,data={"file":(pkg,"simple-server-1.1.0.tar.gz")},content_type="multipart/form-data");self.assertEqual(r.status_code,201);self.assertEqual(r.json["summary"],{"added":1,"modified":2,"unchanged":1,"deleted":0});uid=r.json["id"]
        applied=self.c.post(f"/api/updates/{uid}/apply",headers=self.h,json={"restart":False});self.assertEqual(applied.status_code,200);self.assertEqual((self.project/"change.txt").read_text(),"new");self.assertTrue((self.project/"added.txt").exists())
        rolled=self.c.post(f"/api/updates/{uid}/rollback",headers=self.h);self.assertEqual(rolled.status_code,200);self.assertEqual((self.project/"change.txt").read_text(),"old");self.assertFalse((self.project/"added.txt").exists())
    def test_version_is_read_from_filename(self):
        self.assertEqual(version_from_filename("simple-server-1.2.3.tar.gz"),"1.2.3")
        self.assertEqual(version_from_filename("update-v2.0.0-rc.1.tar.gz"),"2.0.0-rc.1")
        self.assertEqual(version_from_filename("update.tar.gz"),"")
        pkg=archive({"VERSION":"2.3.4\n"})
        r=self.c.post("/api/updates/upload",headers=self.h,data={"version":"9.9.9","file":(pkg,"simple-server-2.3.4.tar.gz")},content_type="multipart/form-data")
        self.assertEqual(r.status_code,201)
        self.assertEqual(r.json["version"],"2.3.4")
    def test_reject_link(self):
        buf=io.BytesIO()
        with tarfile.open(fileobj=buf,mode="w:gz") as tar:
            info=tarfile.TarInfo("bad-link");info.type=tarfile.SYMTYPE;info.linkname="/etc/passwd";tar.addfile(info)
        buf.seek(0);r=self.c.post("/api/updates/upload",headers=self.h,data={"file":(buf,"simple-server-1.1.1.tar.gz")},content_type="multipart/form-data");self.assertEqual(r.status_code,400)

    def upload_version(self):
        package = archive({"VERSION": "1.1.0\n", "change.txt": "new"})
        original = package.getvalue()
        response = self.c.post("/api/updates/upload", headers=self.h,
                               data={"file": (package, "server-v1.1.0.tar.gz")})
        self.assertEqual(response.status_code, 201)
        return response.json["id"], original

    def test_download_returns_original_archive_and_requires_admin(self):
        update_id, original = self.upload_version()
        url = f"/api/updates/{update_id}"
        self.c.post("/api/auth/users", headers=self.h,
                    json={"username": "reader", "password": "reader-password", "role": "user"})
        token = self.c.post("/api/auth/login", json={"username": "reader", "password": "reader-password"}).json["token"]
        ordinary = {"Authorization": f"Bearer {token}"}
        self.assertEqual(self.c.get(url + "/download").status_code, 401)
        self.assertEqual(self.c.get(url + "/download", headers=ordinary).status_code, 403)
        self.assertEqual(self.c.delete(url).status_code, 401)
        self.assertEqual(self.c.delete(url, headers=ordinary).status_code, 403)
        with self.c.get(url + "/download", headers=self.h) as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, original)
            self.assertEqual(response.mimetype, "application/gzip")
            self.assertIn("attachment; filename=simple-server-1.1.0.tar.gz", response.headers["Content-Disposition"])
            self.assertTrue(response.cache_control.no_store)

    def test_delete_applied_update_removes_files_but_keeps_running_version(self):
        update_id, _ = self.upload_version()
        url = f"/api/updates/{update_id}"
        applied = self.c.post(url + "/apply", headers=self.h, json={"restart": False})
        self.assertEqual(applied.status_code, 200)
        row = self.c.get("/api/updates", headers=self.h).json["updates"][0]
        directory = Path(row["archive_path"]).parent
        backup = Path(row["backup_path"])
        self.assertTrue(directory.is_dir())
        self.assertTrue(backup.is_dir())

        self.assertEqual(self.c.delete(url, headers=self.h).status_code, 200)
        self.assertFalse(directory.exists())
        self.assertFalse(backup.exists())
        listing = self.c.get("/api/updates", headers=self.h).json
        self.assertEqual(listing, {"current_version": "1.1.0", "updates": []})
        self.assertEqual((self.project / "change.txt").read_text(), "new")
        self.assertEqual(self.c.get(url + "/download", headers=self.h).status_code, 404)
        self.assertEqual(self.c.post(url + "/rollback", headers=self.h).status_code, 409)
        self.assertEqual(self.c.delete(url, headers=self.h).status_code, 200)
        self.assertNotEqual(self.upload_version()[0], update_id)

    def test_delete_can_retry_after_partial_cleanup(self):
        update_id, _ = self.upload_version()
        url = f"/api/updates/{update_id}"
        row = self.c.get("/api/updates", headers=self.h).json["updates"][0]
        directory = Path(row["archive_path"]).parent
        rmtree = shutil.rmtree

        def fail_after_removing_package(path):
            if path == directory:
                rmtree(path)
                return
            raise PermissionError

        with patch("dev_control.updates.shutil.rmtree", side_effect=fail_after_removing_package):
            with self.assertLogs(self.app.logger, level="ERROR"):
                self.assertEqual(self.c.delete(url, headers=self.h).status_code, 500)
        self.assertFalse(directory.exists())
        self.assertEqual(self.c.get("/api/updates", headers=self.h).json["updates"][0]["status"], "deleting")
        self.assertEqual(self.c.post(url + "/apply", headers=self.h).status_code, 409)
        self.assertEqual(self.c.delete(url, headers=self.h).status_code, 200)
        self.assertEqual(self.c.get("/api/updates", headers=self.h).json["updates"], [])

if __name__=="__main__":unittest.main()

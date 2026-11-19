import tempfile, unittest
from pathlib import Path
from dev_control import create_app

class ApisTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        self.app=create_app({"TESTING":True,"ENABLE_LEGACY_TOOLS":False,"DATA_DIR":str(root/"data"),"SERVER_FILES_ROOT":str(root/"files"),"ADMIN_PASSWORD":"test-password"})
        self.c=self.app.test_client()
        login=self.c.post("/api/auth/login",json={"username":"admin","password":"test-password"})
        self.admin={"Authorization":"Bearer "+login.json["token"]}
    def tearDown(self):self.tmp.cleanup()
    def test_openapi_requires_auth_and_contains_paths(self):
        self.assertEqual(self.c.get("/api/openapi.json").status_code,401)
        r=self.c.get("/api/openapi.json",headers=self.admin)
        self.assertEqual(r.status_code,200)
        self.assertTrue(r.json["openapi"].startswith("3."))
        paths=r.json["paths"]
        for p in ("/api/files","/api/notes","/api/auth/login","/api/health"):
            self.assertIn(p,paths)
    def test_openapi_download_attachment(self):
        r=self.c.get("/api/openapi.json?download=1",headers=self.admin)
        self.assertEqual(r.status_code,200)
        self.assertIn("attachment",r.headers.get("Content-Disposition",""))
        self.assertIn("openapi.json",r.headers.get("Content-Disposition",""))
    def test_apis_page(self):
        self.assertEqual(self.c.get("/apis/").status_code,200)

# Kept as a separate assertion so new public and admin Word Atlas contracts cannot
# disappear when the generated API catalog is edited.
def _test_word_atlas_paths(self):
    spec = self.c.get("/api/openapi.json", headers=self.admin).json["paths"]
    for path in ("/api/words", "/api/words/{word_id}", "/api/words/batch", "/api/words/import", "/api/words/sync"):
        self.assertIn(path, spec)
    self.assertEqual(spec["/api/words"]["get"]["security"], [])
    self.assertEqual(spec["/api/words/import"]["post"]["security"], [{"bearerAuth": []}])
    self.assertIn("multipart/form-data", spec["/api/words/import"]["post"]["requestBody"]["content"])

ApisTest.test_word_atlas_paths = _test_word_atlas_paths

import tempfile, unittest
from pathlib import Path
from dev_control import create_app

class UsersTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name);self.app=create_app({"TESTING":True,"ENABLE_LEGACY_TOOLS":False,"DATA_DIR":str(root/"data"),"SERVER_FILES_ROOT":str(root/"files"),"ADMIN_PASSWORD":"test-password"});self.c=self.app.test_client();login=self.c.post("/api/auth/login",json={"username":"admin","password":"test-password"});self.admin={"Authorization":"Bearer "+login.json["token"]}
    def tearDown(self):self.tmp.cleanup()
    def test_user_token_auth_and_stats(self):
        u=self.c.post("/api/auth/users",headers=self.admin,json={"username":"alice","password":"alice-pass","role":"user"});self.assertEqual(u.status_code,201);uid=u.json["id"]
        made=self.c.post(f"/api/user-management/users/{uid}/tokens",headers=self.admin,json={"label":"automation"});self.assertEqual(made.status_code,201);token=made.json["token"]
        user_h={"Authorization":"Bearer "+token};me=self.c.get("/api/user-management/me/stats",headers=user_h);self.assertEqual(me.status_code,200);self.assertEqual(me.json["username"],"alice")
        tokens=self.c.get(f"/api/user-management/users/{uid}/tokens",headers=user_h);self.assertEqual(tokens.json["tokens"][0]["token"],token)
        users=self.c.get("/api/user-management/users",headers=user_h).json["users"];self.assertEqual([x["username"] for x in users],["alice"])
        tid=tokens.json["tokens"][0]["id"];self.c.delete(f"/api/user-management/users/{uid}/tokens/{tid}",headers=self.admin);self.assertEqual(self.c.get("/api/user-management/me/stats",headers=user_h).status_code,401)

if __name__=="__main__":unittest.main()

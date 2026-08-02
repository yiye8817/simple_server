import json, tempfile, threading, unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from dev_control import create_app

class ModelsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body=json.dumps({"data":[{"id":"model-a"},{"id":"model-b"}]}).encode()
        self.send_response(200);self.send_header("Content-Type","application/json");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
    def log_message(self,*_):pass

class LlmApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        self.app=create_app({"TESTING":True,"ENABLE_LEGACY_TOOLS":False,"DATA_DIR":str(root/"data"),"SERVER_FILES_ROOT":str(root/"files"),"ADMIN_PASSWORD":"test-password"})
        self.c=self.app.test_client();token=self.c.post("/api/auth/login",json={"username":"admin","password":"test-password"}).json["token"];self.h={"Authorization":"Bearer "+token}
        self.server=HTTPServer(("127.0.0.1",0),ModelsHandler);threading.Thread(target=self.server.serve_forever,daemon=True).start()
    def tearDown(self):self.server.shutdown();self.server.server_close();self.tmp.cleanup()
    def test_crud_mask_config_and_models(self):
        base=f"http://127.0.0.1:{self.server.server_port}";r=self.c.post("/api/llm/providers",headers=self.h,json={"name":"local","base_uri":base,"api_key":"secret-api-key-123","api_style":"openai","keywords":["chat"]});self.assertEqual(r.status_code,201);pid=r.json["id"]
        listed=self.c.get("/api/llm/providers",headers=self.h).json["providers"][0];self.assertNotIn("api_key",listed);self.assertIn("…",listed["api_key_masked"])
        checked=self.c.post(f"/api/llm/providers/{pid}/check",headers=self.h);self.assertTrue(checked.json["ok"]);self.assertEqual(checked.json["models"],["model-a","model-b"])
        config=self.c.get(f"/api/llm/providers/{pid}/config",headers=self.h).json;self.assertEqual(config["api_key"],"secret-api-key-123")

if __name__=="__main__":unittest.main()

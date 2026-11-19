import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dev_control import create_app
from dev_control.db import get_db
from dev_control.words import asset_urls, validate_entry


LIBRARY = {
    "id": "library",
    "w": "library",
    "uk": "/ˈlaɪbrəri/",
    "us": "/ˈlaɪbreri/",
    "cefr": "A1",
    "topic": "education",
    "senses": [{"pos": "n.", "zh": "图书馆", "en": "A place for books."}],
    "ex": [{"en": "I went to the library.", "zh": "我去了图书馆。"}],
    "tip": "library 是图书馆。",
    "col": ["public library｜公共图书馆"],
    "fam": ["librarian (n.)"],
    "syn": [],
    "ant": [],
    "m": 31,
    "extra": {"future": True},
}


class WordDataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.app = create_app({
            "TESTING": True,
            "ENABLE_LEGACY_TOOLS": False,
            "DATA_DIR": str(root / "data"),
            "SERVER_FILES_ROOT": str(root / "files"),
            "ADMIN_PASSWORD": "test-password",
        })

    def tearDown(self):
        self.tmp.cleanup()

    def test_word_entries_table_and_validate_library(self):
        with self.app.app_context():
            self.assertIsNotNone(get_db().execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='word_entries'"
            ).fetchone())
        entry = validate_entry(LIBRARY)
        self.assertEqual(entry["id"], "library")
        self.assertEqual(entry["word"], "library")
        self.assertEqual(entry["zh"], "图书馆")
        self.assertEqual(entry["uk_ipa"], "/ˈlaɪbrəri/")
        self.assertEqual(entry["us_ipa"], "/ˈlaɪbreri/")
        self.assertEqual(entry["senses"][0]["pos"], "n.")
        self.assertEqual(entry["examples"][0]["en"], "I went to the library.")
        self.assertEqual(entry["collocations"], ["public library｜公共图书馆"])
        self.assertEqual(entry["family"], ["librarian (n.)"])
        self.assertIn("extra", entry["raw_json"])
        self.assertIsInstance(entry["content_hash"], str)

    def test_invalid_entry_is_rejected(self):
        for patch in ({"id": ""}, {"w": ""}, {"senses": "bad"}, {"ex": "bad"}, {"id": "bad id"}):
            payload = dict(LIBRARY)
            payload.update(patch)
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                validate_entry(payload)

    def test_asset_urls(self):
        urls = asset_urls("library", "https://0xlei.tech/words/atlas/")
        self.assertEqual(urls["images"]["atlas"], "https://0xlei.tech/words/atlas/img/library.webp")
        self.assertEqual(urls["images"]["classic"], "https://0xlei.tech/words/atlas/classic/img/library.webp")
        self.assertEqual(urls["audio"]["uk"], "https://0xlei.tech/words/atlas/audio/uk/library.mp3")
        self.assertEqual(urls["audio"]["us"], "https://0xlei.tech/words/atlas/audio/us/library.mp3")


if __name__ == "__main__":
    unittest.main()

class WordApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.app = create_app({
            "TESTING": True,
            "ENABLE_LEGACY_TOOLS": False,
            "DATA_DIR": str(root / "data"),
            "SERVER_FILES_ROOT": str(root / "files"),
            "ADMIN_PASSWORD": "test-password",
        })
        self.client = self.app.test_client()
        with self.app.app_context():
            from dev_control.words import upsert_entries
            upsert_entries([
                LIBRARY,
                {**LIBRARY, "id": "spring", "w": "Spring", "zh": "春天", "topic": "nature", "cefr": "A2"},
            ], source_url="test")

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_search_filters_and_pagination(self):
        response = self.client.get("/api/words?q=LIB&page_size=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["total"], 1)
        self.assertEqual(response.json["items"][0]["id"], "library")
        response = self.client.get("/api/words?q=春&topic=nature")
        self.assertEqual(response.json["items"][0]["id"], "spring")
        response = self.client.get("/api/words?page=9&page_size=1")
        self.assertEqual(response.json["items"], [])

    def test_detail_and_batch(self):
        response = self.client.get("/api/words/library")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["item"]["senses"][0]["zh"], "图书馆")
        self.assertIn("audio", response.json["item"]["assets"])
        self.assertEqual(self.client.get("/api/words/missing").status_code, 404)
        response = self.client.post("/api/words/batch", json={"ids": ["library", "library", "missing"], "words": ["Spring"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in response.json["items"]], ["library", "spring"])
        self.assertEqual(response.json["missing"], ["missing"])

    def test_batch_accepts_comma_separated_words_and_get_requests(self):
        response = self.client.get("/api/words/batch?words=library,spring")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in response.json["items"]], ["library", "spring"])
        response = self.client.post("/api/words/batch", json={"words": "library\nSpring"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in response.json["items"]], ["library", "spring"])

class WordImportSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.app = create_app({
            "TESTING": True,
            "ENABLE_LEGACY_TOOLS": False,
            "DATA_DIR": str(root / "data"),
            "SERVER_FILES_ROOT": str(root / "files"),
            "ADMIN_PASSWORD": "test-password",
        })
        self.client = self.app.test_client()
        login = self.client.post("/api/auth/login", json={"username": "admin", "password": "test-password"})
        self.auth = {"Authorization": "Bearer " + login.json["token"]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_json_and_jsonl_import_and_invalid_batch_is_atomic(self):
        import io
        response = self.client.post(
            "/api/words/import", headers=self.auth,
            data={"file": (io.BytesIO(json.dumps([LIBRARY]).encode()), "words.json")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["inserted"], 1)
        response = self.client.post(
            "/api/words/import", headers=self.auth,
            data={"file": (io.BytesIO((json.dumps({**LIBRARY, "tip": "updated"}) + "\n").encode()), "words.jsonl")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["updated"], 1)
        response = self.client.post(
            "/api/words/import", headers=self.auth,
            data={"file": (io.BytesIO(b'{"id":"broken"}\nnot-json\n'), "bad.jsonl")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/api/words/library").json["item"]["tip"], "updated")

    def test_sync_validates_all_chunks_before_replace(self):
        from unittest.mock import patch
        first = {**LIBRARY, "id": "library", "w": "library"}
        second = {**LIBRARY, "id": "spring", "w": "spring"}
        index = {"count": 2, "chunk": 1, "version": 7, "words": [["library", "library"], ["spring", "spring"]]}
        def fake_fetch(url, timeout=20):
            if url.endswith("index.json"):
                return index
            if url.endswith("entries/0.json"):
                return [first]
            if url.endswith("entries/1.json"):
                return [second]
            raise AssertionError(url)
        with patch("dev_control.words._fetch_json", side_effect=fake_fetch):
            response = self.client.post("/api/words/sync", headers=self.auth, json={"source_base": "https://0xlei.tech/words/atlas/"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["count"], 2)
        self.assertEqual(self.client.get("/api/words/library").status_code, 200)
        bad_index = {**index, "count": 3}
        with patch("dev_control.words._fetch_json", return_value=bad_index):
            response = self.client.post("/api/words/sync", headers=self.auth, json={"source_base": "https://0xlei.tech/words/atlas/"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.client.get("/api/words/spring").status_code, 200)

    def test_replace_only_removes_after_success(self):
        import io
        self.client.post("/api/words/import", headers=self.auth, data={
            "file": (io.BytesIO(json.dumps([LIBRARY, {**LIBRARY, "id": "spring", "w": "spring"}]).encode()), "words.json")
        }, content_type="multipart/form-data")
        response = self.client.post("/api/words/import?replace=true", headers=self.auth, data={
            "file": (io.BytesIO(json.dumps([LIBRARY]).encode()), "words.json")
        }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["deleted"], 1)
        self.assertEqual(self.client.get("/api/words/spring").status_code, 404)

class WordPageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.app = create_app({
            "TESTING": True,
            "ENABLE_LEGACY_TOOLS": False,
            "DATA_DIR": str(root / "data"),
            "SERVER_FILES_ROOT": str(root / "files"),
            "ADMIN_PASSWORD": "test-password",
        })
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def test_word_atlas_page_and_assets(self):
        response = self.client.get("/words/")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        response.close()
        script_response = self.client.get("/words/app.js")
        script = script_response.get_data(as_text=True)
        script_response.close()
        for marker in ("Word Atlas", "UK", "US", "SpeechSynthesis", "CC BY 4.0"):
            self.assertIn(marker, body)
        for marker in ("classic", "atlas", "data-style", "synonyms", "antonyms", "requestSeq", "图片加载失败"):
            self.assertIn(marker, script)
        css_response = self.client.get("/words/style.css")
        self.assertEqual(css_response.status_code, 200)
        css_response.close()

class WordRobustnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.app = create_app({
            "TESTING": True,
            "ENABLE_LEGACY_TOOLS": False,
            "DATA_DIR": str(root / "data"),
            "SERVER_FILES_ROOT": str(root / "files"),
            "ADMIN_PASSWORD": "test-password",
        })
        self.client = self.app.test_client()
        login = self.client.post("/api/auth/login", json={"username": "admin", "password": "test-password"})
        self.auth = {"Authorization": "Bearer " + login.json["token"]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_sync_rejects_untrusted_source_and_index_word_mismatch(self):
        response = self.client.post("/api/words/sync", headers=self.auth, json={"source_base": "http://127.0.0.1:5000/atlas/"})
        self.assertEqual(response.status_code, 400)
        from unittest.mock import patch
        index = {"count": 1, "chunk": 1, "version": 1, "words": [["library", "different"]]}
        with patch("dev_control.words._fetch_json", side_effect=[index, [LIBRARY]]):
            response = self.client.post("/api/words/sync", headers=self.auth, json={"source_base": "https://0xlei.tech/words/atlas/"})
        self.assertEqual(response.status_code, 502)

    def test_replace_is_scoped_to_source_and_search_treats_like_chars_literally(self):
        from dev_control.words import upsert_entries
        import io
        with self.app.app_context():
            upsert_entries([LIBRARY], replace=False, source_url="https://source-a/atlas")
            upsert_entries([{**LIBRARY, "id": "spring", "w": "spring"}], replace=False, source_url="https://source-b/atlas")
        response = self.client.post("/api/words/import?replace=true", headers=self.auth, data={
            "file": (io.BytesIO(json.dumps([LIBRARY]).encode()), "words.json")
        }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/words/spring").status_code, 200)
        response = self.client.get("/api/words?q=%25")
        self.assertEqual(response.json["total"], 0)

    def test_request_shape_and_batch_limits_are_rejected(self):
        import io
        self.assertEqual(self.client.post("/api/words/batch", json=[]).status_code, 400)
        self.assertEqual(self.client.post("/api/words/sync", headers=self.auth, json=[]).status_code, 400)
        self.assertEqual(self.client.post("/api/words/batch", json={"ids": ["x"] * 2001}).status_code, 400)
        response = self.client.post("/api/words/import", headers=self.auth, data={
            "file": (io.BytesIO(b"not-json"), "bad.json")
        }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 400)


class WordCreateApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.app = create_app({
            "TESTING": True,
            "ENABLE_LEGACY_TOOLS": False,
            "DATA_DIR": str(root / "data"),
            "SERVER_FILES_ROOT": str(root / "files"),
            "ADMIN_PASSWORD": "test-password",
        })
        self.client = self.app.test_client()
        login = self.client.post("/api/auth/login", json={"username": "admin", "password": "test-password"})
        self.admin_headers = {"Authorization": "Bearer " + login.json["token"]}

        user = self.client.post("/api/auth/users", headers=self.admin_headers, json={
            "username": "reader", "password": "reader-password", "role": "user",
        })
        self.assertEqual(user.status_code, 201)
        login = self.client.post("/api/auth/login", json={"username": "reader", "password": "reader-password"})
        self.user_headers = {"Authorization": "Bearer " + login.json["token"]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_requires_admin_and_creates_new_entry(self):
        self.assertEqual(self.client.post("/api/words", json=LIBRARY).status_code, 401)
        self.assertEqual(self.client.post("/api/words", headers=self.user_headers, json=LIBRARY).status_code, 403)
        response = self.client.post("/api/words", headers=self.admin_headers, json=LIBRARY)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["item"]["id"], "library")
        self.assertEqual(self.client.get("/api/words/library").status_code, 200)

    def test_invalid_create_is_atomic_and_duplicate_does_not_overwrite(self):
        response = self.client.post("/api/words", headers=self.admin_headers, json={"id": "broken"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/api/words/broken").status_code, 404)
        self.assertEqual(self.client.post("/api/words", headers=self.admin_headers, json=LIBRARY).status_code, 201)
        response = self.client.post("/api/words", headers=self.admin_headers, json={**LIBRARY, "tip": "changed"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.client.get("/api/words/library").json["item"]["tip"], LIBRARY["tip"])


class WordManagementPageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.app = create_app({
            "TESTING": True,
            "ENABLE_LEGACY_TOOLS": False,
            "DATA_DIR": str(root / "data"),
            "SERVER_FILES_ROOT": str(root / "files"),
            "ADMIN_PASSWORD": "test-password",
        })
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def test_home_and_words_page_expose_management_and_debug_tools(self):
        home = self.client.get("/").get_data(as_text=True)
        self.assertIn('data-view="words"', home)
        self.assertIn('/words/', home)
        page = self.client.get("/words/").get_data(as_text=True)
        for marker in ("新建单词", "批量导入", "单个单词接口", "单词列表接口", "多个单词接口", "library,abandon", "GET /api/words/library", "GET /api/words?q=library&amp;page=1&amp;page_size=5", "devControlToken"):
            self.assertIn(marker, page)
        script = self.client.get("/words/app.js").get_data(as_text=True)
        for marker in ("/api/words", "/api/words/batch", "/api/words/import", "Authorization", "status", "JSON.stringify", "words.jsonl"):
            self.assertIn(marker, script)


class WordAssetSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.asset_root = root / "data" / "word_assets"
        self.app = create_app({
            "TESTING": True,
            "ENABLE_LEGACY_TOOLS": False,
            "DATA_DIR": str(root / "data"),
            "SERVER_FILES_ROOT": str(root / "files"),
            "WORDS_ASSET_DIR": str(self.asset_root),
            "ADMIN_PASSWORD": "test-password",
        })
        self.client = self.app.test_client()
        login = self.client.post("/api/auth/login", json={"username": "admin", "password": "test-password"})
        self.auth = {"Authorization": "Bearer " + login.json["token"]}
        with self.app.app_context():
            from dev_control.words import upsert_entries
            upsert_entries([LIBRARY], source_url="test")

    def tearDown(self):
        self.tmp.cleanup()

    def test_asset_sync_downloads_all_variants_and_serves_local_urls(self):
        from unittest.mock import patch
        with patch("dev_control.words._fetch_binary", create=True, return_value=b"asset-bytes"):
            response = self.client.post("/api/words/assets/sync", headers=self.auth, json={"ids": ["library"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["downloaded"], 4)
        detail = self.client.get("/api/words/library").json["item"]
        self.assertEqual(detail["assets"]["images"]["atlas"], "/words/assets/img/library.webp")
        self.assertEqual(detail["assets"]["audio"]["uk"], "/words/assets/audio/uk/library.mp3")
        self.assertEqual(self.client.get("/words/assets/img/library.webp").status_code, 200)

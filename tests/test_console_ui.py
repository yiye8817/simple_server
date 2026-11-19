"""The first-party console's shared asset must render without external services."""
import re
import tempfile
import unittest
from pathlib import Path
from flask import render_template
from dev_control import create_app


class ConsoleUiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app({'TESTING':True,'ENABLE_LEGACY_TOOLS':False,'DATA_DIR':self.tmp.name+'/data',
                               'SERVER_FILES_ROOT':self.tmp.name+'/files','ADMIN_PASSWORD':'isolated-ui-test-password',
                               'HERMES_LOCAL_ENABLED':False,'HERMES_BASE_URL':''})
        self.client=self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def test_shared_stylesheet_is_served_as_css(self):
        response=self.client.get('/static/control-ui.css?v=20261025-1')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.mimetype,'text/css')
        self.assertGreater(len(response.data),100)
        response.close()

    def test_all_first_party_templates_link_same_versioned_local_stylesheet(self):
        routes=['/','/android/','/android/schedule/','/apis/','/files/','/hermes/','/llm/','/notes/','/policies/','/typing/','/updates/','/users/']
        links=[]
        for route in routes:
            response=self.client.get(route)
            self.assertEqual(response.status_code,200,route)
            html=response.get_data(as_text=True)
            link=re.search(r'<link[^>]+href="([^"]*control-ui\.css\?v=[^"]+)"',html)
            self.assertIsNotNone(link,route)
            links.append(link.group(1))
        with self.app.test_request_context():
            self.assertIn(links[0],render_template('dashboard.html'))
        self.assertEqual(len(set(links)),1)
        self.assertTrue(links[0].startswith('/static/'))

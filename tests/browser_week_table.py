"""Optional real Chromium checks; all HTTP and databases are isolated on loopback.
Run: /usr/bin/python3 tests/browser_week_table.py -v
"""
import sys
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dev_control import create_app
from werkzeug.serving import make_server, WSGIRequestHandler
from playwright.sync_api import sync_playwright, expect


class QuietHandler(WSGIRequestHandler):
    def log(self, *args, **kwargs):
        pass


class UploadResponseGate:
    """Hold an actual isolated HTTP upload response after its body is read."""
    def __init__(self, app):
        self.app = app
        self.current = None

    def hold(self, filename, status=None, body=None):
        gate = {'filename': filename, 'received': threading.Event(), 'release': threading.Event(),
                'status': status, 'body': body, 'request': None}
        self.current = gate
        return gate

    def clear(self):
        if self.current:
            self.current['release'].set()
        self.current = None

    def __call__(self, environ, start_response):
        gate = self.current
        query = parse_qs(environ.get('QUERY_STRING', ''))
        if not (gate and environ['REQUEST_METHOD'] == 'POST' and environ['PATH_INFO'].endswith('/files')
                and query.get('filename') == [gate['filename']]):
            return self.app(environ, start_response)
        gate['request'] = {'path': environ['PATH_INFO'], 'query': query,
                           'authorization': environ.get('HTTP_AUTHORIZATION'),
                           'content_type': environ.get('CONTENT_TYPE'),
                           'content_length': int(environ.get('CONTENT_LENGTH') or 0)}
        if gate['status']:
            remaining = gate['request']['content_length']
            while remaining:
                chunk = environ['wsgi.input'].read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            response = [gate['body']]
            captured = [gate['status'], [('Content-Type', 'application/json')], None]
        else:
            captured = []
            def capture(status, headers, exc_info=None):
                captured.extend([status, headers, exc_info])
            response = self.app(environ, capture)
        gate['received'].set()
        gate['release'].wait(20)
        start_response(*captured)
        return response


class WeekTableBrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='week-table-browser-')
        cls.app = create_app({'TESTING': True, 'ENABLE_LEGACY_TOOLS': False,
                              'DATA_DIR': cls.tmp.name+'/data', 'SERVER_FILES_ROOT': cls.tmp.name+'/files',
                              'ADMIN_PASSWORD': 'isolated-browser-password'})
        cls.clock = patch('dev_control.agenda.now_ms', return_value=1792900800000)
        cls.clock.start()  # 2026-10-25 12:00 Asia/Shanghai
        cls.client = cls.app.test_client()
        cls.token = cls.client.post('/api/auth/login', json={'username': 'admin', 'password': 'isolated-browser-password'}).json['token']
        cls.auth = {'Authorization': 'Bearer '+cls.token}
        cls.devices = []
        for suffix in ('A', 'B'):
            code = cls.client.post('/api/android/pairing-codes', headers=cls.auth).json['code']
            device = cls.client.post('/api/android/pair', json={'code': code, 'name': '同名平板', 'device_uid': 'browser-'+suffix}).json['device']
            agent = {'X-Device-Token': device['device_token']}
            cls.client.post('/api/android/agent/inventory', headers=agent, json={
                'name': '同名平板', 'timezone': 'Asia/Shanghai', 'capabilities': {'weekly_schedule': True, 'usage_access': True},
                'apps': [{'package_name': 'org.example.reader', 'label': '阅读器 '+suffix, 'version_name': '1', 'version_code': 1,
                          'system': False, 'launchable': True, 'protected': False}]})
            base = '/api/android/devices/'+str(device['id'])
            rule = cls.client.post(base+'/schedule/rules', headers=cls.auth, json={
                'title': '课程 '+suffix, 'notes': '学习内容 '+suffix, 'weekdays': [1, 7], 'start_minute': 780,
                'end_minute': 840, 'valid_from': '2026-10-25', 'valid_until': '2026-12-31'}).json['rule']
            cls.client.post(base+'/files?filename='+suffix+'.txt', headers=cls.auth, data=suffix.encode(), content_type='application/octet-stream')
            cls.client.post('/api/android/agent/usage', headers=agent, json={
                'timezone': 'Asia/Shanghai', 'permission_granted': True, 'generated_at': 1792900800000,
                'window_start': 1792857600000, 'window_end': 1792900800000,
                'buckets': [{'package_name': 'org.example.reader', 'bucket_start': 1792872000000, 'duration_ms': 1000 if suffix == 'A' else 9000}]})
            cls.devices.append((str(device['id']), base, rule))
        cls.upload_gate = UploadResponseGate(cls.app.wsgi_app)
        cls.app.wsgi_app = cls.upload_gate
        cls.server = make_server('127.0.0.1', 0, cls.app, threaded=True, request_handler=QuietHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = 'http://127.0.0.1:'+str(cls.server.server_port)
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)
        cls.evidence = Path('/tmp/week-table-browser-evidence')
        cls.evidence.mkdir(exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.server.shutdown()
        cls.thread.join()
        cls.clock.stop()
        cls.tmp.cleanup()

    def setUp(self):
        # Each browser case starts from the same two plans, including after a
        # previous case deliberately edited a rule through the real page.
        for suffix, (_, base, _) in zip(('A', 'B'), self.devices):
            rule = self.client.get(base+'/schedule', headers=self.auth).json['rules'][0]
            self.client.put(base+'/schedule/rules/'+str(rule['id']), headers=self.auth, json={
                **rule, 'title': '课程 '+suffix, 'notes': '学习内容 '+suffix,
                'start_minute': 780, 'end_minute': 840, 'expected_version': rule['version']})
        self.context = self.browser.new_context(viewport={'width': 1440, 'height': 1100},
                                                locale='zh-CN', timezone_id='Asia/Shanghai')
        self.context.add_init_script('localStorage.setItem("devControlToken", '+repr(self.token)+')')
        self.page = self.context.new_page()
        self.page.clock.set_fixed_time('2026-10-25T04:00:00Z')
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        # Keep actions observable in the same rendered-frame mode as the
        # diagnostic trace; it avoids a cold headless compositor stall.
        self.context.tracing.start(screenshots=True,snapshots=True)
        self.page.set_default_timeout(10000)

    def tearDown(self):
        self.upload_gate.clear()
        self.context.tracing.stop(path=str(self.evidence/(self._testMethodName+'.zip')))
        self.context.close()
        self.assertEqual(self.errors, [])

    def open(self, route):
        self.page.goto(self.origin+route)
        expect(self.page.locator('#deviceSelect')).to_have_value(self.devices[0][0])
        expect(self.page.locator('#management')).to_be_visible()
        # Initial state and the seven date requests can replace the same row;
        # start interactions after that first read finishes. Dedicated race
        # cases deliberately introduce later overlapping requests themselves.
        self.page.wait_for_load_state('networkidle')

    def hold_next(self, suffix, method='GET'):
        self.page.evaluate('''({suffix,method})=>{const original=window.fetch;let once=true;window.fetch=async(url,options={})=>{
            const response=await original(url,options);
            if(once&&String(url).endsWith(suffix)&&(options.method||'GET')===method){once=false;return await new Promise(resolve=>{window.releaseHeld=()=>{resolve(response);delete window.releaseHeld;};});}
            return response;};}''', {'suffix': suffix, 'method': method})

    def release(self):
        self.page.evaluate('window.releaseHeld()')
        self.page.wait_for_timeout(150)

    def observe_slow_upload(self):
        # Browser events come from a real File and throttled loopback transfer;
        # no fabricated ProgressEvents or production function replacements.
        self.page.evaluate('''()=>{window.nativeUploadProgress=[];window.rawUploadFile=false;
            const send=XMLHttpRequest.prototype.send;XMLHttpRequest.prototype.send=function(body){
              if(body instanceof File){window.rawUploadFile=true;this.upload.addEventListener('progress',e=>
                window.nativeUploadProgress.push({loaded:e.loaded,total:e.total,computable:e.lengthComputable}));}
              return send.call(this,body);};}''')
        cdp=self.context.new_cdp_session(self.page)
        cdp.send('Network.enable')
        cdp.send('Network.emulateNetworkConditions', {'offline':False, 'latency':0,
                 'downloadThroughput':-1, 'uploadThroughput':512*1024})
        return cdp

    def test_duplicate_device_names_are_unambiguous_and_controls_are_independent(self):
        self.open('/android/')
        expect(self.page.locator('#deviceSelect option').nth(0)).to_have_text('同名平板 · #'+self.devices[0][0]+' · 在线')
        expect(self.page.locator('#deviceSelect option').nth(1)).to_have_text('同名平板 · #'+self.devices[1][0]+' · 在线')
        expect(self.page.locator('#apps')).to_contain_text('阅读器 A')
        expect(self.page.locator('#files')).to_contain_text('A.txt')
        expect(self.page.locator('#usageApps')).to_contain_text('1 秒')
        self.page.locator('#apps input').first.uncheck()
        self.page.locator('#savePolicy').click()
        expect(self.page.locator('#policyStatus')).to_contain_text('已保存')
        self.page.locator('#deviceSelect').select_option(self.devices[1][0])
        expect(self.page.locator('#apps')).to_contain_text('阅读器 B')
        expect(self.page.locator('#apps input').first).to_be_checked()
        expect(self.page.locator('#files')).to_contain_text('B.txt')
        expect(self.page.locator('#usageApps')).to_contain_text('9 秒')
        self.assertNotIn('A.txt', self.page.locator('#files').inner_text())
        policy_a = self.client.get(self.devices[0][1], headers=self.auth).json['policy']['apps']
        policy_b = self.client.get(self.devices[1][1], headers=self.auth).json['policy']['apps']
        self.assertFalse(policy_a['org.example.reader']['allowed'])
        self.assertEqual(policy_b, {})

    def test_week_rows_edit_time_content_preserve_rule_fields_and_complete_separately(self):
        self.open('/android/schedule/')
        expect(self.page.locator('#week tbody.weekday')).to_have_count(7)
        expect(self.page.locator('#weekRange')).to_contain_text('2026-10-19')
        self.page.locator('#nextWeek').click()
        expect(self.page.locator('#weekRange')).to_contain_text('2026-10-26')
        row = self.page.locator('#week tr.course-row').first
        expect(row).to_contain_text('课程 A')
        row.locator('.course-content').click()
        expect(self.page.locator('#ruleTitle')).to_have_value('课程 A')
        self.page.locator('#ruleTitle').fill('编辑后的学习内容')
        self.page.locator('#startTime').fill('14:30')
        self.page.locator('#endTime').fill('15:30')
        self.page.locator('#saveRule').click()
        expect(self.page.locator('#message')).to_contain_text('课程已保存')
        expect(self.page.locator('#week')).to_contain_text('14:30–15:30')
        changed = self.client.get(self.devices[0][1]+'/schedule', headers=self.auth).json['rules'][0]
        self.assertEqual(changed['weekdays'], [1, 7])
        self.assertEqual(changed['valid_from'], '2026-10-25')
        self.assertEqual(changed['valid_until'], '2026-12-31')
        expect(self.page.locator('#formTitle')).to_have_text('添加课程')
        row = self.page.locator('#week tr.course-row').first
        row.locator('input[type=checkbox]').check()
        expect(self.page.locator('#week tr.course-row').first.locator('input[type=checkbox]')).to_be_checked()
        expect(self.page.locator('#formTitle')).to_have_text('添加课程')
        self.assertEqual(self.client.get(self.devices[1][1]+'/schedule', headers=self.auth).json['rules'][0]['title'], '课程 B')
        self.page.locator('#week .add-course').nth(1).click()
        expect(self.page.locator('#weekdays input[value="2"]')).to_be_checked()
        expect(self.page.locator('#ruleTitle')).to_have_value('')

    def test_old_file_callbacks_and_aba_loads_cannot_adopt_another_selection(self):
        self.open('/android/')
        expect(self.page.locator('#files')).to_contain_text('A.txt')
        self.page.evaluate('window.detachedOpen=document.querySelector("#files button")')
        self.hold_next('/files')
        self.page.locator('#refresh').click()
        self.page.wait_for_function('typeof window.releaseHeld==="function"')
        self.client.post(self.devices[0][1]+'/files?filename=new-A.txt', headers=self.auth, data=b'new', content_type='application/octet-stream')
        self.page.locator('#deviceSelect').select_option(self.devices[1][0])
        expect(self.page.locator('#files')).to_contain_text('B.txt')
        commands=[]
        self.page.on('request', lambda req: commands.append(req.url) if req.method=='POST' and '/open' in req.url else None)
        self.page.evaluate('window.detachedOpen.click()')
        self.page.wait_for_timeout(100)
        self.assertEqual(commands, [])
        self.page.locator('#deviceSelect').select_option(self.devices[0][0])
        expect(self.page.locator('#files')).to_contain_text('new-A.txt')
        self.release()
        expect(self.page.locator('#files')).to_contain_text('new-A.txt')

    def test_delayed_upload_and_rule_save_do_not_overwrite_new_device_drafts(self):
        self.open('/android/')
        gate=self.upload_gate.hold('upload-A.txt')
        self.page.locator('#fileInput').set_input_files({'name':'upload-A.txt','mimeType':'text/plain','buffer':b'A'})
        self.page.locator('#upload').click()
        self.assertTrue(gate['received'].wait(4))
        self.page.locator('#deviceSelect').select_option(self.devices[1][0])
        expect(self.page.locator('#apps')).to_contain_text('阅读器 B')
        self.page.locator('#fileInput').set_input_files({'name':'draft-B.txt','mimeType':'text/plain','buffer':b'B'})
        gate['release'].set()
        self.page.wait_for_timeout(150)
        self.assertEqual(self.page.locator('#fileInput').evaluate('(e)=>e.files[0].name'), 'draft-B.txt')
        self.assertNotIn('已上传', self.page.locator('#uploadStatus').inner_text())
        self.open('/android/schedule/')
        self.page.locator('#week .course-content').first.click()
        self.page.locator('#ruleTitle').fill('迟到的 A 保存')
        rule_id=self.client.get(self.devices[0][1]+'/schedule',headers=self.auth).json['rules'][0]['id']
        self.hold_next('/rules/'+str(rule_id), 'PUT')
        self.page.locator('#saveRule').click()
        self.page.wait_for_function('typeof window.releaseHeld==="function"')
        self.page.locator('#deviceSelect').select_option(self.devices[1][0])
        expect(self.page.locator('#week')).to_contain_text('课程 B')
        self.page.locator('#ruleTitle').fill('设备 B 的草稿')
        self.release()
        expect(self.page.locator('#ruleTitle')).to_have_value('设备 B 的草稿')
        expect(self.page.locator('#saveRule')).to_be_enabled()
        self.assertEqual(self.client.get(self.devices[1][1]+'/schedule',headers=self.auth).json['rules'][0]['title'], '课程 B')

    def test_real_upload_progress_waits_for_successful_server_response(self):
        self.open('/android/')
        expect(self.page.locator('#apps')).to_contain_text('阅读器 A')
        self.observe_slow_upload()
        filename='real-progress.bin'
        gate=self.upload_gate.hold(filename)
        size=2*1024*1024
        self.page.locator('#fileInput').set_input_files({'name':filename,'mimeType':'application/octet-stream','buffer':b'p'*size})
        self.page.locator('#upload').click()
        expect(self.page.locator('#uploadProgress')).to_be_visible()
        self.page.wait_for_function('window.nativeUploadProgress.some(e=>e.computable&&e.loaded>0&&e.loaded<e.total)',timeout=15000)
        progress=self.page.locator('#uploadProgress').evaluate('''(e)=>({value:e.value,max:e.max,
            loaded:window.nativeUploadProgress.at(-1).loaded,total:window.nativeUploadProgress.at(-1).total})''')
        self.assertEqual(progress['value'],progress['loaded'])
        self.assertEqual(progress['max'],progress['total'])
        self.assertGreater(progress['value'],0)
        self.assertLess(progress['value'],progress['max'])
        expect(self.page.locator('#uploadStatus')).to_contain_text('MiB')
        self.assertNotIn('已上传',self.page.locator('#uploadStatus').inner_text())
        self.page.wait_for_function('window.nativeUploadProgress.some(e=>e.loaded===e.total&&e.total>0)',timeout=15000)
        self.assertTrue(gate['received'].wait(4))
        expect(self.page.locator('#uploadStatus')).to_contain_text('100%')
        expect(self.page.locator('#uploadStatus')).to_contain_text('等待服务器确认')
        self.assertNotIn('已上传',self.page.locator('#uploadStatus').inner_text())
        self.assertEqual(self.page.locator('#fileInput').evaluate('(e)=>e.files[0].name'),filename)
        self.assertTrue(self.page.evaluate('window.rawUploadFile'))
        self.assertEqual(gate['request']['path'],self.devices[0][1]+'/files')
        self.assertEqual(gate['request']['query'],{'filename':[filename],'open':['1'],'install':['0']})
        self.assertEqual(gate['request']['authorization'],self.auth['Authorization'])
        self.assertEqual(gate['request']['content_type'],'application/octet-stream')
        self.assertEqual(gate['request']['content_length'],size)
        self.page.screenshot(path=str(self.evidence/'upload-awaiting-confirmation.png'),full_page=True)
        gate['release'].set()
        expect(self.page.locator('#uploadStatus')).to_contain_text('已上传')
        self.assertEqual(self.page.locator('#fileInput').evaluate('(e)=>e.files.length'),0)
        expect(self.page.locator('#files')).to_contain_text(filename)

    def test_upload_http_and_json_errors_keep_file_for_retry(self):
        self.open('/android/')
        expect(self.page.locator('#apps')).to_contain_text('阅读器 A')
        for status,body,want in [('503 Service Unavailable',json.dumps({'error':'拒绝测试上传'}).encode(),'拒绝测试上传'),
                                 ('200 OK',b'not-json','服务器返回异常'),
                                 ('200 OK',b'{"ok":true}','服务器返回异常')]:
            with self.subTest(status=status):
                gate=self.upload_gate.hold('retry-fixture.apk',status,body)
                gate['release'].set()
                self.page.locator('#fileInput').set_input_files({'name':'retry-fixture.apk','mimeType':'application/vnd.android.package-archive','buffer':b'fixture'})
                self.page.locator('#upload').click()
                expect(self.page.locator('#uploadStatus')).to_contain_text('上传失败')
                expect(self.page.locator('#message')).to_contain_text(want)
                self.assertNotIn('已上传',self.page.locator('#uploadStatus').inner_text())
                self.assertEqual(self.page.locator('#fileInput').evaluate('(e)=>e.files[0].name'),'retry-fixture.apk')
                expect(self.page.locator('#upload')).to_be_enabled()
                self.assertEqual(gate['request']['query'],{'filename':['retry-fixture.apk'],'open':['0'],'install':['1']})
        self.upload_gate.clear()
        self.page.locator('#upload').click()
        expect(self.page.locator('#uploadStatus')).to_contain_text('已上传')
        expect(self.page.locator('#files')).to_contain_text('retry-fixture.apk')

    def test_upload_progress_and_response_cannot_survive_a_b_a_selection(self):
        self.open('/android/')
        expect(self.page.locator('#apps')).to_contain_text('阅读器 A')
        self.observe_slow_upload()
        gate=self.upload_gate.hold('old-upload-A.bin')
        self.page.locator('#fileInput').set_input_files({'name':'old-upload-A.bin','mimeType':'application/octet-stream','buffer':b'a'*(2*1024*1024)})
        self.page.locator('#upload').click()
        expect(self.page.locator('#uploadProgress')).to_be_visible()
        self.page.wait_for_function('window.nativeUploadProgress.some(e=>e.loaded>0&&e.loaded<e.total)',timeout=15000)
        self.page.locator('#deviceSelect').select_option(self.devices[1][0])
        expect(self.page.locator('#apps')).to_contain_text('阅读器 B')
        expect(self.page.locator('#uploadProgress')).to_be_hidden()
        self.page.locator('#deviceSelect').select_option(self.devices[0][0])
        expect(self.page.locator('#apps')).to_contain_text('阅读器 A')
        self.page.locator('#fileInput').set_input_files({'name':'new-draft-A.txt','mimeType':'text/plain','buffer':b'draft'})
        self.page.wait_for_function('window.nativeUploadProgress.some(e=>e.loaded===e.total&&e.total>0)',timeout=15000)
        self.assertTrue(gate['received'].wait(4))
        expect(self.page.locator('#uploadStatus')).to_have_text('')
        expect(self.page.locator('#uploadProgress')).to_be_hidden()
        gate['release'].set()
        self.page.wait_for_timeout(200)
        self.assertEqual(self.page.locator('#fileInput').evaluate('(e)=>e.files[0].name'),'new-draft-A.txt')
        expect(self.page.locator('#uploadStatus')).to_have_text('')
        expect(self.page.locator('#upload')).to_be_enabled()
        self.assertNotIn('old-upload-A.bin',str(self.client.get(self.devices[1][1]+'/files',headers=self.auth).json))

    def test_confirmed_upload_stays_successful_if_file_list_refresh_fails(self):
        self.open('/android/')
        self.page.route('**'+self.devices[0][1]+'/files',lambda route:route.fulfill(
            status=503,content_type='application/json',body='{"error":"isolated list unavailable"}'))
        self.page.locator('#fileInput').set_input_files({'name':'refresh-failure.txt','mimeType':'text/plain','buffer':b'confirmed'})
        self.page.locator('#upload').click()
        expect(self.page.locator('#message')).to_contain_text('isolated list unavailable')
        expect(self.page.locator('#upload')).to_be_enabled()
        expect(self.page.locator('#uploadStatus')).to_contain_text('已上传')
        self.assertNotIn('上传失败',self.page.locator('#uploadStatus').inner_text())
        self.assertIn('refresh-failure.txt',str(self.client.get(self.devices[0][1]+'/files',headers=self.auth).json))

    def test_responsive_week_table_and_android_have_no_document_overflow(self):
        for route, name in [('/android/schedule/', 'agenda'), ('/android/', 'android')]:
            for width in (1440, 900, 390):
                with self.subTest(route=route,width=width):
                    self.page.set_viewport_size({'width':width,'height':1100})
                    self.open(route)
                    if name=='agenda':
                        expect(self.page.locator('#week tbody.weekday')).to_have_count(7)
                        expect(self.page.locator('#week .add-course').first).to_be_visible()
                    else:
                        expect(self.page.locator('#files')).to_contain_text('A.txt')
                    self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth<=innerWidth'), self.page.evaluate('({scroll:document.documentElement.scrollWidth,width:innerWidth})'))
                    self.page.screenshot(path=str(self.evidence/(name+'-'+str(width)+'.png')),full_page=True)

    def test_conflicting_rule_edit_keeps_draft_until_explicit_reload(self):
        self.open('/android/schedule/')
        self.page.locator('#week .course-content').first.click()
        self.page.locator('#ruleTitle').fill('尚未保存的草稿')
        base=self.devices[0][1]
        rule=self.client.get(base+'/schedule',headers=self.auth).json['rules'][0]
        self.client.put(base+'/schedule/rules/'+str(rule['id']),headers=self.auth,
                        json={**rule,'title':'另一端已修改','expected_version':rule['version']})
        self.page.locator('#saveRule').click()
        expect(self.page.locator('#message')).to_contain_text('课程已被修改')
        expect(self.page.locator('#ruleTitle')).to_have_value('尚未保存的草稿')
        self.assertEqual(self.client.get(base+'/schedule',headers=self.auth).json['rules'][0]['title'],'另一端已修改')
        self.page.locator('#refresh').click()
        expect(self.page.locator('#week')).to_contain_text('另一端已修改')
        self.page.locator('#week .course-content').first.click()
        expect(self.page.locator('#ruleTitle')).to_have_value('另一端已修改')


if __name__=='__main__':
    unittest.main()

"""来源服务的黑盒测试支撑，不含目标采集业务。"""
import unittest,threading,json,os,hashlib
from pathlib import Path
from urllib.request import build_opener,HTTPCookieProcessor,Request
from urllib.error import HTTPError
from http.cookiejar import CookieJar
from source.app import App
from source.common import serve
class SourceCase(unittest.TestCase):
    def setUp(self):
        self.app=App();self.mail=None
        if hasattr(self.app,'mail_port'):
            from source.mailbox import create_mailbox
            self.mail=create_mailbox(self.app,0);self.app.mail_port=self.mail.server_address[1]
            threading.Thread(target=self.mail.serve_forever,daemon=True).start()
        self.server=serve(self.app,0);threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.base='http://127.0.0.1:'+str(self.server.server_port)
        self.client=build_opener(HTTPCookieProcessor(CookieJar()))
    def tearDown(self):
        self.server.shutdown();self.server.server_close()
        if self.mail: self.mail.shutdown();self.mail.server_close()
    def request(self,path,value=None,headers=None):
        headers=dict(headers or {});data=None
        if value is not None: data=json.dumps(value).encode();headers['Content-Type']='application/json'
        try:
            with self.client.open(Request(self.base+path,data=data,headers=headers),timeout=10) as response: return response.status,dict(response.headers),response.read()
        except HTTPError as error: return error.code,dict(error.headers),error.read()
    def control(self,stage):
        status,_,data=self.request('/__control',{'stage':stage},{'X-Operator':'local-operator'});self.assertEqual(status,200)
    def value(self,path):
        status,_,data=self.request(path);self.assertEqual(status,200);return json.loads(data)
    def screenshot(self,page):
        target=Path(os.environ.get('EVIDENCE_DIR','/tmp/source-evidence'));target.mkdir(parents=True,exist_ok=True)
        data=page.screenshot(path=str(target/'source-browser.png'),full_page=True)
        print('SOURCE_BROWSER_SCREENSHOT_SHA256='+hashlib.sha256(data).hexdigest(),flush=True)
    def browser(self):
        from playwright.sync_api import sync_playwright
        runtime=sync_playwright().start();browser=runtime.chromium.launch(headless=True,args=['--no-sandbox','--disable-dev-shm-usage']);context=browser.new_context(accept_downloads=True)
        self.addCleanup(runtime.stop);self.addCleanup(browser.close);self.addCleanup(context.close)
        return context.new_page()

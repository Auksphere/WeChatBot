import contextlib
import http.cookiejar
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPCookieProcessor

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'plugins'))
from manager_app.service import Service,UserError
from manager_app.server import ManagerServer
from astrbot_plugin_personal_companion.memory import MemoryStore

class MemoryEditTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'),'Node is required for frontend checks')
    def test_editor_markup_and_payload(self):
        subprocess.run([shutil.which('node'),str(ROOT/'tests/test_memory_editor.cjs')],check=True,capture_output=True)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        (self.root/'data').mkdir();(self.root/'manage.py').write_text('# fixture')
        shutil.copyfile(ROOT/'runtime_scripts/group_manager.py',self.root/'group_manager.py')
        (self.root/'data/cmd_config.json').write_text(json.dumps({'platform':[{'id':'wechat-rolebot','account_id':'fixture-account'}]}))
        self.service=Service(self.root)
        self.service.companion.mkdir(parents=True)
        self.store=MemoryStore(self.service.companion/'people.sqlite3')
        for account in ('fixture-account','other-account'):
            self.store.touch(account,'alice','测试用户')
            self.store.merge(account,'alice',{'兴趣':'测试摄影','称呼':'测试称呼'})

    def tearDown(self):
        self.store.close();self.service.jobs.executor.shutdown(wait=True);self.temp.cleanup()

    def payload(self,**changes):
        item=self.service.memories()['items'][0]
        return dict(user=item['id'],revision=item['revision'],facts={'兴趣':'测试绘画','称呼':''},**changes)

    def test_save_and_delete_fields_are_immediately_visible_without_other_account_changes(self):
        self.service.edit_memory(self.payload())
        self.assertEqual(self.store.get('fixture-account','alice')['facts'],{'兴趣':'测试绘画'})
        self.assertEqual(self.store.get('other-account','alice')['facts'],{'兴趣':'测试摄影','称呼':'测试称呼'})
        self.assertTrue(self.store.get('fixture-account','alice')['enabled'])

    def test_conflicting_update_is_not_silently_overwritten(self):
        payload=self.payload()
        self.store.merge('fixture-account','alice',{'兴趣':'新学到的测试资料'})
        with self.assertRaises(UserError):self.service.edit_memory(payload)
        self.assertEqual(self.store.get('fixture-account','alice')['facts']['兴趣'],'新学到的测试资料')

    def test_nickname_refresh_does_not_cause_false_conflict(self):
        payload=self.payload()
        self.store.touch('fixture-account','alice','新的测试昵称')
        self.service.edit_memory(payload)
        self.assertEqual(self.store.get('fixture-account','alice')['facts']['兴趣'],'测试绘画')

    def test_disabled_missing_and_wrong_user_revision_do_not_write(self):
        payload=self.payload()
        self.store.enable('fixture-account','alice',False)
        with self.assertRaises(UserError):self.service.edit_memory(payload)
        self.assertEqual(self.store.get('fixture-account','alice')['facts'],{})
        payload['user']='absent'
        with self.assertRaises(UserError):self.service.edit_memory(payload)
        self.assertEqual(self.store.get('other-account','alice')['facts']['兴趣'],'测试摄影')

    def test_fields_types_length_and_version_are_validated(self):
        for changes in ({'facts':{'未知字段':'x'}},{'facts':{'兴趣':['x']}},{'facts':{'兴趣':'x'*241}},{'revision':'bad'}):
            payload=self.payload();payload.update(changes)
            with self.assertRaises(UserError):self.service.edit_memory(payload)
        self.assertEqual(self.store.get('fixture-account','alice')['facts']['兴趣'],'测试摄影')

    def test_old_inflight_learning_does_not_overwrite_manual_edit(self):
        before=self.store.get('fixture-account','alice')['facts']
        self.service.edit_memory(self.payload())
        saved=self.store.merge('fixture-account','alice',{'兴趣':'过期学习结果'},expected_facts=before)
        self.assertEqual(saved,{})
        self.assertEqual(self.store.get('fixture-account','alice')['facts']['兴趣'],'测试绘画')

    def test_http_edit_requires_session_and_csrf_and_saves_through_job_queue(self):
        server=ManagerServer(('127.0.0.1',0),self.service)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        opener=build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        base='http://127.0.0.1:'+str(server.server_port)
        def request(path,payload=None,csrf=None):
            headers={'Content-Type':'application/json'}
            if csrf:headers['X-Local-Token']=csrf
            req=Request(base+path,data=json.dumps(payload).encode() if payload is not None else None,headers=headers)
            with opener.open(req,timeout=3) as response:return json.load(response)
        try:
            with self.assertRaises(HTTPError):request('/api/memories/edit',self.payload())
            csrf=request('/api/session',{'token':server.launch_token})['csrf']
            with self.assertRaises(HTTPError):request('/api/memories/edit',self.payload())
            job=request('/api/memories/edit',self.payload(),csrf)
            for _ in range(30):
                result=request('/api/jobs/'+job['job_id'])
                if result['state'] in {'succeeded','failed'}:break
                time.sleep(.01)
            self.assertEqual(result['state'],'succeeded')
            self.assertEqual(self.store.get('fixture-account','alice')['facts']['兴趣'],'测试绘画')
        finally:
            server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()

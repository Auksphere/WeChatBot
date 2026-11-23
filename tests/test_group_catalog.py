"""Catalog discovery and partial upstream sync regression checks."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('group_catalog_fixture',Path(__file__).resolve().parents[1]/'runtime_scripts/group_manager.py')
m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        (self.root/'data').mkdir()
        self.config={'platform':[{'id':'wechat-rolebot','account_id':'fixture-account','allowed_groups':['old@chatroom']}]}
        (self.root/'data/cmd_config.json').write_text(json.dumps(self.config),encoding='utf-8-sig')
        self.g=m.GroupManager(self.root,alive=lambda:False)

    def test_new_group_discovered_from_threads_and_foreign_or_private_ignored(self):
        def get(path,params=None,**kwargs):
            if path=='/groups':return {'items':[{'id':'old@chatroom','displayName':'旧群'}]}
            return {'items':[
                {'id':'new@chatroom','kind':'group','accountId':'fixture-account','displayName':'测试新群'},
                {'id':'foreign@chatroom','kind':'group','accountId':'other-account','displayName':'别人的群'},
                {'id':'private-user','kind':'private','displayName':'私聊'},
                {'id':'spoof@chatroom','kind':'private','displayName':'类型不匹配'},
                {'id':'old@chatroom','kind':'group','displayName':'会话旧名称'}]}
        self.g._native_get=get
        result=self.g.catalog()
        self.assertEqual({x.id:x.name for x in result},{'old@chatroom':'旧群','new@chatroom':'测试新群'})
        self.assertEqual(result,self.g.cached_groups())
        self.assertEqual(self.g.enabled_ids(),['old@chatroom'])

    def test_partial_sync_is_recorded_and_contacts_are_synced_first(self):
        calls=[]
        def get(path,params=None,method='GET'):
            calls.append((path,method))
            if method=='POST':
                return {'accountId':'fixture-account','kind':'metadata','id':path[1:],'state':'succeeded',
                    'result':{'complete':True,'recordCount':0,'coverage':'group_metadata_partial' if path=='/group-sync' else 'contact_inventory'}}
            return {'items':[]}
        self.g._native_get=get
        self.g.refresh()
        self.assertEqual(calls[:2],[('/contact-sync','POST'),('/group-sync','POST')])
        status=self.g.sync_status()
        self.assertTrue(status['partial_group_coverage'])
        self.assertIn('只覆盖部分',status['notice'])
        self.assertEqual(status['count'],0)

    def test_running_operation_is_resumed_not_submitted_twice(self):
        (self.root/'data/contact-catalog-sync.json').write_text(json.dumps({
            'account_id':'fixture-account','id':'inflight','state':'running'}),encoding='utf-8')
        calls=[]
        def get(path,params=None,method='GET'):
            calls.append((path,method))
            if path=='/operations/inflight' or method=='POST':
                return {'accountId':'fixture-account','kind':'metadata','id':'done','state':'succeeded','result':{'complete':True}}
            return {'items':[]}
        self.g._native_get=get
        self.g.refresh()
        self.assertNotIn(('/contact-sync','POST'),calls)
        self.assertIn(('/operations/inflight','GET'),calls)

    def test_partial_failure_and_thread_failure_remain_visible(self):
        def get(path,params=None,method='GET'):
            if path in {'/contact-sync','/threads'}:raise m.GroupManagerError('fixture incomplete')
            if method=='POST':return {'accountId':'fixture-account','kind':'metadata','id':'op','state':'succeeded','result':{'complete':False}}
            return {'items':[{'id':'old@chatroom','name':'旧群'}]}
        self.g._native_get=get
        self.assertEqual(len(self.g.refresh()),1)
        self.assertIn('fixture incomplete',self.g.sync_status()['warning'])
        self.assertIn('未完整返回',self.g.sync_status()['warning'])
        self.assertIn('会话目录',self.g.sync_status()['warning'])

    def test_paginated_threads_do_not_stop_after_first_page(self):
        def get(path,params=None,**kwargs):
            if path=='/groups':return {'items':[]}
            if not params.get('cursor'):return {'items':[],'nextCursor':'second'}
            return {'items':[{'id':'new@chatroom','kind':'group','displayName':'测试新群'}]}
        self.g._native_get=get
        self.assertEqual(self.g.catalog()[0].name,'测试新群')

    def test_thread_fallback_verifies_exact_account_before_enabling(self):
        original=(self.root/'data/cmd_config.json').read_bytes()
        def get(path,params=None,**kwargs):
            if path.startswith('/groups/'):raise m.GroupManagerError('not cached')
            return {'id':'new@chatroom','accountId':'other-account','kind':'group'}
        self.g._native_get=get
        with self.assertRaises(m.GroupManagerError):self.g.change(add=['new@chatroom'])
        self.assertEqual((self.root/'data/cmd_config.json').read_bytes(),original)
        self.g._native_get=lambda path,**kwargs: get(path) if path.startswith('/groups/') else {
            'id':'new@chatroom','accountId':'fixture-account','kind':'group'}
        self.g.change(add=['new@chatroom'])
        self.assertEqual(self.g.enabled_ids(),['old@chatroom','new@chatroom'])

    def test_repeated_page_cursor_is_rejected(self):
        self.g._native_get=lambda *args,**kwargs:{'items':[],'nextCursor':'repeat'}
        with self.assertRaises(m.GroupManagerError):self.g.catalog()


if __name__=='__main__':unittest.main()

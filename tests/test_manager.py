import contextlib
import http.cookiejar
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import threading
import types
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPCookieProcessor

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from manager_app.server import ManagerServer
from manager_app.service import Service, UserError, load_module, redact


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'data').mkdir()
        (self.root / 'manage.py').write_text('# fixture', encoding='utf-8')
        shutil.copyfile(PROJECT / 'runtime_scripts/group_manager.py', self.root / 'group_manager.py')
        config = {'platform': [{'id': 'wechat-rolebot', 'account_id': 'fake-account', 'allowed_groups': ['fake@chatroom'],
            'enable_private_reply': True, 'unique_session': True}],
            'provider_sources': [{'id': 'deepseek', 'key': ['fixture-deepseek-key-for-tests-only']}],
            'provider': [{'id': 'deepseek-rolechat', 'model': 'deepseek-flash'}], 'provider_settings': {}}
        (self.root / 'data/cmd_config.json').write_text(json.dumps(config), encoding='utf-8-sig')
        with contextlib.closing(sqlite3.connect(self.root / 'data/data_v4.db')) as db:
            db.execute('CREATE TABLE personas (persona_id TEXT, system_prompt TEXT)')
            db.execute('INSERT INTO personas VALUES (?,?)', ('大肥鱼', '这是虚构的角色测试设定，不是用户真实人设。'))
            db.commit()
        self.service = Service(self.root)
        self.service.alive = lambda: False
        self.service.native = lambda *args, **kwargs: {'authenticated': True, 'ownUsername': 'fake-bot'}
        self.server = ManagerServer(('127.0.0.1', 0), self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.service.jobs.executor.shutdown(wait=True)
        self.temp.cleanup()

    def request(self, path, value=None, **headers):
        if value is not None:
            headers['Content-Type'] = 'application/json'
        req = Request(self.url + path, data=json.dumps(value).encode() if value is not None else None, headers=headers)
        with self.opener.open(req, timeout=4) as response:
            return json.load(response)

    def login(self):
        return self.request('/api/session', {'token': self.server.launch_token})['csrf']

    def test_session_is_required_and_cookie_is_http_only(self):
        with self.assertRaises(HTTPError) as raised:
            self.request('/api/settings')
        self.assertEqual(raised.exception.code, 401)
        self.login()
        self.assertIn('csrf', self.request('/api/session'))

    def test_reopening_launcher_reuses_session_and_survives_manager_restart(self):
        csrf = self.login()
        self.assertEqual(self.login(), csrf)
        replacement = ManagerServer(('127.0.0.1', 0), self.service)
        self.assertEqual(len(replacement.sessions), 1)
        self.assertEqual(next(iter(replacement.sessions.values()))['csrf'], csrf)
        replacement.server_close()

    def test_cross_origin_and_rebound_host_are_rejected(self):
        self.login()
        for headers in [{'Origin': 'https://attacker.invalid'}, {'Host': 'attacker.invalid'}]:
            with self.assertRaises(HTTPError) as raised:
                self.request('/api/status', **headers)
            self.assertEqual(raised.exception.code, 403)

    def test_writes_require_csrf_and_bad_token_cannot_login(self):
        with self.assertRaises(HTTPError):
            self.request('/api/session', {'token': 'bad'})
        self.login()
        for _ in range(8):
            with self.assertRaises(HTTPError) as raised:
                self.request('/api/settings', {'model': 'deepseek-flash', 'padding': 'x' * 4096})
            self.assertEqual(raised.exception.code, 403)

    def test_public_and_settings_apis_never_return_saved_key(self):
        self.login()
        value = self.request('/api/settings')
        self.assertTrue(value['api_key_configured'])
        self.assertNotIn('sk-', json.dumps(value))
        self.assertNotIn('fake-bot', json.dumps(self.request('/health')))
        with self.assertRaises(HTTPError) as raised:
            self.request('/data/cmd_config.json')
        self.assertEqual(raised.exception.code, 404)

    def test_settings_show_expanded_native_context_and_real_turn_limit(self):
        self.login()
        value=self.request('/api/settings')
        self.assertEqual(value['group_context_minutes'],1440)
        self.assertEqual(value['group_context_messages'],200)
        self.assertEqual(value['private_context_days'],7)
        self.assertEqual(value['private_context_messages'],100)
        config,bot=self.service.config()
        config['agent_runner']={'config':{'compression':{'max_turns':100}}}
        (self.root/'data/cmd_config.json').write_text(json.dumps(config),encoding='utf-8-sig')
        self.assertEqual(self.request('/api/settings')['conversation_max_turns'],100)

    def test_settings_preserve_groups_and_blank_key(self):
        self.service.save_settings({'model': 'deepseek-v4-pro', 'private_reply': False, 'api_key': ''})
        config, bot = self.service.config()
        self.assertEqual(bot['allowed_groups'], ['fake@chatroom'])
        self.assertFalse(bot['enable_private_reply'])
        self.assertEqual(config['provider_sources'][0]['key'], ['fixture-deepseek-key-for-tests-only'])
        self.assertTrue(list((self.root / 'data/config-backups').glob('*.json')))

    def test_config_failure_restores_original(self):
        original = (self.root / 'data/cmd_config.json').read_bytes()
        def fail(config, bot):
            bot['allowed_groups'] = []
            raise UserError('fixture failure')
        with self.assertRaises(UserError):
            self.service.change_config(fail)
        self.assertEqual((self.root / 'data/cmd_config.json').read_bytes(), original)

    def test_log_redaction_hides_keys_bearer_and_audio(self):
        result = redact('sk-abcdefghijklmnop Bearer xyz-secret password="secretpass" base64://QUJDRA==')
        for secret in ['abcdefghijklmnop', 'xyz-secret', 'secretpass', 'QUJDRA==']:
            self.assertNotIn(secret, result)

    def test_voice_requires_own_key_and_invalid_input_does_not_stop_bot(self):
        self.service.alive = lambda: True
        with patch.object(self.service, 'control') as control:
            with self.assertRaises(UserError):
                self.service.save_voice({'voice': 'hajimi'})
            control.assert_not_called()
        self.assertNotIn('companion_speech', self.service.config()[1])

    def test_voice_key_is_private_and_blank_preserves_it(self):
        self.service.save_voice({'voice': 'hajimi', 'fish_api_key': 'fish-fixture-secret-key'})
        self.login()
        public = self.request('/api/settings')
        self.assertEqual(public['voice'], 'hajimi')
        self.assertTrue(public['fish_api_key_configured'])
        self.assertNotIn('fish-fixture-secret-key', json.dumps(public))
        self.service.save_voice({'voice': 'tide', 'fish_api_key': ''})
        _, bot = self.service.config()
        self.assertEqual(bot['companion_speech']['fish_api_key'], 'fish-fixture-secret-key')
        self.assertEqual(bot['allowed_groups'], ['fake@chatroom'])
        self.assertTrue(bot['enable_private_reply'])
        self.assertEqual(self.service.config()[0]['provider'][0]['model'], 'deepseek-flash')

    def test_voice_preview_needs_auth_and_csrf_and_does_not_save(self):
        original = (self.root / 'data/cmd_config.json').read_bytes()
        with self.assertRaises(HTTPError) as raised:
            self.request('/api/voice/preview', {})
        self.assertEqual(raised.exception.code, 401)
        csrf = self.login()
        with self.assertRaises(HTTPError) as raised:
            self.request('/api/voice/preview', {})
        self.assertEqual(raised.exception.code, 403)
        with patch.object(self.service, 'tts_module') as loader:
            module = loader.return_value
            module.voice_settings.return_value = {'voice': 'stella'}
            module.synthesize = AsyncMock(return_value=b'RIFF-fixture')
            req = Request(self.url + '/api/voice/preview', data=b'{}', headers={
                'Content-Type': 'application/json', 'X-Local-Token': csrf})
            with self.opener.open(req, timeout=4) as response:
                self.assertEqual(response.headers['Content-Type'], 'audio/wav')
                self.assertEqual(response.read(), b'RIFF-fixture')
            module.synthesize.assert_awaited_once()
        self.assertEqual((self.root / 'data/cmd_config.json').read_bytes(), original)

    def test_voice_preview_rejects_overlap_and_long_text(self):
        with self.assertRaises(UserError):
            self.service.preview_voice({'text': 'a' * 121})
        self.service.voice_preview_gate.acquire()
        try:
            with self.assertRaises(UserError):
                self.service.preview_voice({})
        finally:
            self.service.voice_preview_gate.release()

    def test_friend_list_is_account_scoped_and_never_returns_ticket(self):
        def native(path,params=None):
            return {'items':[{'id':'request-1','accountId':'fake-account','displayName':'wxid_fixture','direction':'incoming','actionable':True,'isContact':False,'message':'验证消息','version':'private-ticket'},
                {'id':'foreign','accountId':'other','direction':'incoming','actionable':True,'isContact':False}]}
        self.service.native=native
        self.login()
        result=self.request('/api/friends')
        self.assertEqual(result['pending_count'],1)
        self.assertNotIn('private-ticket',json.dumps(result))
        self.assertNotIn('foreign',json.dumps(result))

    def test_friend_list_exposes_failed_attempt_without_claiming_acceptance(self):
        module=load_module('test_console_auto_friends', PROJECT / 'plugins/astrbot_plugin_personal_companion/auto_friends.py')
        self.service.companion.mkdir(parents=True)
        journal=module.Journal(self.service.companion/'auto-friends.sqlite3')
        try:
            journal.save('fake-account','request-1','private-version','fixture-key','failed','fixture-operation')
            journal.save('foreign','request-1','foreign-version','other-key','accepted')
            journal.scan('fake-account')
        finally:journal.close()
        self.service.native=lambda *args,**kwargs:{'items':[{'id':'request-1','direction':'incoming','actionable':True,'isContact':False}]}
        self.login()
        result=self.request('/api/friends')
        self.assertEqual(result['record']['failed'],1)
        self.assertEqual(result['record']['accepted'],0)
        self.assertEqual(result['items'][0]['auto_status'],'failed')
        self.assertNotIn('private-version',json.dumps(result))
        self.assertNotIn('fixture-operation',json.dumps(result))

    def test_auto_friend_toggle_preserves_private_policy_and_other_settings(self):
        token=self.root/'data/admin-token';token.write_text('fixture-only',encoding='utf-8')
        config,bot=self.service.config()
        bot['admin_token_file']='data/admin-token'
        (self.root/'data/cmd_config.json').write_text(json.dumps(config),encoding='utf-8-sig')
        self.service.save_friends({'enabled':True})
        config,bot=self.service.config()
        self.assertTrue(bot['companion_auto_friends'])
        self.assertTrue(bot['enable_private_reply'])
        self.assertEqual(bot['allowed_groups'],['fake@chatroom'])
        self.assertNotIn('enable_friend_requests',bot)
        self.service.save_friends({'enabled':False})
        self.assertFalse(self.service.config()[1]['companion_auto_friends'])

    def test_manual_accept_queues_without_changing_disabled_auto_policy(self):
        config,bot=self.service.config()
        bot['companion_auto_friends']=False
        bot['admin_token_file']='data/admin-token'
        (self.root/'data/admin-token').write_text('fixture',encoding='utf-8')
        (self.root/'data/cmd_config.json').write_text(json.dumps(config),encoding='utf-8-sig')
        self.service.alive=lambda:True
        self.service.native=lambda *args,**kwargs:{'id':'request-1','accountId':'fake-account','direction':'incoming','actionable':True,'isContact':False}
        csrf=self.login()
        created=self.request('/api/friends/accept',{'id':'request-1'},**{'X-Local-Token':csrf})
        import time
        for _ in range(30):
            job=self.service.jobs.get(created['job_id'])
            if job['state'] in {'succeeded','failed'}:break
            time.sleep(.01)
        self.assertEqual(job['state'],'succeeded',job)
        self.assertFalse(self.service.config()[1]['companion_auto_friends'])
        with contextlib.closing(sqlite3.connect(self.service.companion/'auto-friends.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT account,request_id,state FROM manual_commands').fetchone(),('fake-account','request-1','queued'))
        self.service.native=lambda *args,**kwargs:{'id':'request-1','accountId':'foreign','direction':'incoming','actionable':True,'isContact':False}
        with self.assertRaises(UserError):self.service.accept_friend({'id':'request-1'})
        with self.assertRaises(UserError):self.service.accept_friend({'id':'x'*513})

    def test_auto_friend_requires_existing_owner_admin_credentials(self):
        with self.assertRaises(UserError):self.service.save_friends({'enabled':True})
        with self.assertRaises(UserError):self.service.save_friends({'enabled':'yes'})

    def test_role_roundtrip_does_not_change_persona_binding(self):
        self.service.save_role({'prompt': '你是一个虚构的测试角色，喜欢安静简短地回答问题。'})
        self.assertEqual(self.service.role()['id'], '大肥鱼')
        self.assertIn('虚构', self.service.role()['prompt'])

    def test_unknown_control_never_executes_shell_command(self):
        with patch('subprocess.run') as run:
            with self.assertRaises(UserError):
                self.service.control('echo pwned')
            run.assert_not_called()

    def test_empty_group_list_is_not_allowed_to_open_all_groups(self):
        with self.assertRaises(Exception):
            self.service.groups.proposed(['fake@chatroom'], [], {'fake@chatroom'})

    def test_failed_apply_restarts_with_original_config(self):
        original = (self.root / 'data/cmd_config.json').read_bytes()
        self.service.alive = lambda: True
        calls = []
        def control(action):
            calls.append(action)
            if calls == ['stop', 'start']:
                raise UserError('fake startup failure')
        self.service.control = control
        with self.assertRaises(UserError):
            self.service.save_settings({'model': 'deepseek-v4-pro', 'private_reply': False})
        self.assertEqual((self.root / 'data/cmd_config.json').read_bytes(), original)
        self.assertEqual(calls, ['stop', 'start', 'start'])

    def test_memory_clear_preserves_other_accounts_and_records_forget_cutoff(self):
        module = load_module('test_console_memory', PROJECT / 'plugins/astrbot_plugin_personal_companion/memory.py')
        self.service.companion.mkdir(parents=True)
        store = module.MemoryStore(self.service.companion / 'people.sqlite3')
        try:
            for account in ['fake-account', 'other-account']:
                store.touch(account, 'fake-user', '测试用户')
                store.merge(account, 'fake-user', {'兴趣': '虚构测试兴趣'})
        finally:
            store.close()
        package = types.ModuleType('astrbot_plugin_personal_companion')
        with patch.dict(sys.modules, {'astrbot_plugin_personal_companion': package,
                'astrbot_plugin_personal_companion.memory': module}):
            self.service.memory_action({'user': 'fake-user', 'action': 'forget'})
        store = module.MemoryStore(self.service.companion / 'people.sqlite3')
        try:
            self.assertEqual(store.get('fake-account', 'fake-user')['facts'], {})
            self.assertFalse(store.get('fake-account', 'fake-user')['enabled'])
            self.assertGreater(store.reset_time('fake-account', 'fake-user'), 0)
            self.assertEqual(store.get('other-account', 'fake-user')['facts']['兴趣'], '虚构测试兴趣')
        finally:
            store.close()

    def test_qr_refresh_follows_generation_and_persists_operation_before_start(self):
        native_dir = self.root / 'native-state'
        native_dir.mkdir()
        (native_dir / 'api-token').write_text('fake-admin-token')
        calls, service = [], self.service
        def operation(state, generation=None):
            return {'id': 'fake-operation', 'kind': 'login', 'accountId': 'fake-account', 'state': state,
                'result': {'qrAvailable': generation is not None, 'qrGeneration': generation}}
        class Client:
            def __init__(self, *_args, **_kwargs):
                self.steps = [operation('awaiting_scan', 2), operation('succeeded')]
            async def __aenter__(self):return self
            async def __aexit__(self, *_args):pass
            async def start_login(self, key, *, force_qr):
                record = json.loads((service.private / 'login-operation.json').read_text())
                assert record['key'] == key and force_qr
                calls.append('start')
                return operation('awaiting_scan', 1)
            async def login_status(self, identifier):
                assert identifier == 'fake-operation'
                return self.steps.pop(0)
            async def login_qr(self, identifier, generation):
                calls.append(generation)
                return b'fake-qr-generation-' + str(generation).encode()
        cfg = types.ModuleType('astrbot_plugin_wechat_native.config')
        cfg.NativeConfig = SimpleNamespace(from_dict=lambda _: SimpleNamespace(account_id='fake-account',api_url='http://127.0.0.1:6174',admin_token_file=''))
        client = types.ModuleType('astrbot_plugin_wechat_native.native_client')
        client.NativeClient = Client
        with patch.dict(sys.modules, {'astrbot_plugin_wechat_native':types.ModuleType('astrbot_plugin_wechat_native'),
                'astrbot_plugin_wechat_native.config':cfg,'astrbot_plugin_wechat_native.native_client':client}), patch('asyncio.sleep',new=AsyncMock()):
            self.assertEqual(self.service.start_login()['message'], '微信登录已完成')
        self.assertEqual(calls, ['start', 1, 2])
        self.assertEqual((service.private / 'login-qr.png').read_bytes(), b'fake-qr-generation-2')
        self.assertFalse(service.login_status()['has_qr'])
        self.assertEqual(json.loads((service.private / 'login-operation.json').read_text())['state'], 'succeeded')


    def install_discovery_fixture(self):
        plugin = self.root / 'data/plugins/astrbot_plugin_personal_companion'
        plugin.mkdir(parents=True)
        self.service.companion.mkdir(parents=True)
        shutil.copyfile(PROJECT / 'plugins/astrbot_plugin_personal_companion/sticker_library.py', plugin / 'sticker_library.py')
        shutil.copyfile(PROJECT / 'runtime_scripts/sticker_manager.py', self.root / 'sticker_manager.py')
        policy = {'account_id': 'fake-account', 'enabled': True, 'cooldown_seconds': 30,
                  'min_replies_between': 1, 'stickers': []}
        (self.service.companion / 'auto-stickers.json').write_text(json.dumps(policy))

    def test_discovery_settings_and_queue_do_not_send_or_restart(self):
        self.install_discovery_fixture()
        self.service.control = lambda *_: self.fail('Settings must not restart the bot')
        self.service.save_stickers({'enabled': True, 'cooldown_seconds': 30, 'discovery_enabled': True})
        self.assertTrue(self.service.stickers()['discovery']['enabled'])
        settings = self.service.companion / 'sticker-discovery-settings.json'
        first = json.loads(settings.read_text())['request_id']
        self.service.alive = lambda: True
        self.service.discover_stickers()
        self.assertNotEqual(json.loads(settings.read_text())['request_id'], first)
        self.service.save_stickers({'enabled': True, 'cooldown_seconds': 30, 'discovery_enabled': False})
        self.assertFalse(self.service.stickers()['discovery']['enabled'])
        with self.assertRaises(UserError):
            self.service.discover_stickers()

    def test_invalid_discovery_settings_do_not_mutate_policy(self):
        self.install_discovery_fixture()
        path = self.service.companion / 'auto-stickers.json'
        before = path.read_bytes()
        with self.assertRaises(UserError):
            self.service.save_stickers({'enabled': False, 'cooldown_seconds': 60, 'discovery_enabled': 'true'})
        self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()

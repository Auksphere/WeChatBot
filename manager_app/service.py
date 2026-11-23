"""Scoped operations against one explicitly selected deployment directory."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.error import URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


class UserError(Exception):
    pass


def redact(text):
    text = re.sub(r'base64://[^\s\x27\x22,}]+', 'base64://[已隐藏]', str(text))
    text = re.sub(r'(?i)Bearer\s+[^\s\x22\x27,}]+', 'Bearer [已隐藏]', text)
    text = re.sub(r'\bsk-[A-Za-z0-9_-]{8,}', '[密钥已隐藏]', text)
    return re.sub(r'(?i)((?:api[_-]?key|access[_-]?token|password|authorization)[\x22\x27]?\s*[:=]\s*[\x22\x27]?)[^\s,\x22\x27}]+', r'\1[已隐藏]', text)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class Jobs:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='manager-operation')
        self.lock = threading.Lock()
        self.items = {}

    def submit(self, title, function):
        with self.lock:
            if any(j['state'] in {'queued', 'running'} for j in self.items.values()):
                raise UserError('另一个操作正在进行，请等待完成后再试。')
            identifier = secrets.token_hex(12)
            self.items[identifier] = {'id': identifier, 'title': title, 'state': 'queued', 'created': time.time()}
            if len(self.items) > 60:
                oldest = next(iter(self.items))
                self.items.pop(oldest)
        def run():
            with self.lock:
                self.items[identifier]['state'] = 'running'
            try:
                result = function()
                update = {'state': 'succeeded', 'result': result, 'message': '操作已完成'}
            except Exception as exc:
                message = str(exc) if isinstance(exc, UserError) or type(exc).__name__ == 'GroupManagerError' else '操作未完成，请查看诊断日志。'
                update = {'state': 'failed', 'message': redact(message), 'error_type': type(exc).__name__}
            with self.lock:
                self.items[identifier].update(update, finished=time.time())
        self.executor.submit(run)
        return {'job_id': identifier, 'title': title}

    def get(self, identifier):
        with self.lock:
            if identifier not in self.items:
                raise UserError('操作记录不存在，请刷新页面。')
            return dict(self.items[identifier])


class Service:
    def __init__(self, root):
        self.root = Path(root).resolve()
        if not (self.root / 'manage.py').is_file() or not (self.root / 'data/cmd_config.json').is_file():
            raise UserError('请选择含 manage.py 和 data/cmd_config.json 的机器人部署目录。')
        self.private = self.root / 'data/local-manager'
        self.private.mkdir(exist_ok=True)
        self.companion = self.root / 'data/plugin_data/astrbot_plugin_personal_companion'
        self.python = self.root / '.venv/Scripts/python.exe'
        self.group_module = load_module('local_manager_groups', self.root / 'group_manager.py')
        self.groups = self.group_module.GroupManager(self.root)
        self.jobs = Jobs()
        self.voice_preview_gate = threading.BoundedSemaphore(1)
        self.login_lock = threading.Lock()
        self.login = None
        os.environ.setdefault('ASTRBOT_ROOT', str(self.root))
        plugin_path = str(self.root / 'data/plugins')
        if plugin_path not in sys.path:
            sys.path.insert(0, plugin_path)

    def config(self):
        return self.groups.read()[1:]

    def native(self, path='', params=None):
        return self.groups._native_get(path, params)

    @staticmethod
    def alive():
        try:
            with urlopen('http://127.0.0.1:6185/api/auth/setup-status', timeout=1.5) as response:
                return response.status == 200
        except (URLError, TimeoutError, OSError):
            return False

    def control(self, action):
        if action not in {'start', 'stop', 'restart'}:
            raise UserError('未知的运行操作。')
        for step in (['stop', 'start'] if action == 'restart' else [action]):
            result = subprocess.run([str(self.python), '-X', 'utf8', str(self.root / 'manage.py'), step],
                cwd=self.root, capture_output=True, timeout=105,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode:
                raise UserError('启动或停止未完成，请在“日志与诊断”中查看原因。')
        return {'message': {'start': '机器人已启动', 'stop': '机器人已停止，管理台继续运行', 'restart': '机器人已重启'}[action]}

    def status(self):
        config, bot = self.config()
        native = False
        account = {}
        try:
            account = self.native()
            native = True
        except Exception:
            pass
        running = self.alive()
        policy = self.sticker_candidates()
        memory_count = 0
        path = self.companion / 'people.sqlite3'
        if path.exists():
            with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
                memory_count = db.execute('SELECT count(*) FROM users WHERE account=?', (bot['account_id'],)).fetchone()[0]
        with self.jobs.lock:
            recent_jobs = [dict(j) for j in list(self.jobs.items.values())[-6:]]
        return {'running': running, 'native_online': native,
            'wechat_online': bool(account.get('authenticated')),
            'connection': str(account.get('connection') or ('unknown' if native else 'offline')),
            'account': str(account.get('ownUsername') or ''), 'account_id': bot['account_id'],
            'group_count': len(bot.get('allowed_groups') or []), 'private_reply': bool(bot.get('enable_private_reply')),
            'memory_count': memory_count, 'sticker_count': len(policy.get('stickers') or []),
            'auto_stickers': policy.get('enabled') is True,
            'version': '0.2.4', 'dashboard_url': 'http://127.0.0.1:6185',
            'login': self.login_status(), 'recent_jobs': recent_jobs}

    def group_list(self, refresh=False):
        warning = ''
        before = {g.id for g in self.groups.cached_groups()}
        try:
            catalog = self.groups.refresh() if refresh else self.groups.catalog()
        except Exception as exc:
            catalog = self.groups.cached_groups()
            warning = str(exc) if type(exc).__name__ == 'GroupManagerError' else '微信目录暂不可用，显示上次缓存。'
        enabled = self.groups.enabled_ids()
        seen = {g.id for g in catalog}
        catalog += [g for g in self.groups.enabled_groups(catalog) if g.id not in seen]
        sync = self.groups.sync_status()
        warning = warning or getattr(self.groups, 'catalog_notice', '') or sync.get('warning', '')
        added = len({g.id for g in catalog} - before)
        message = f'已检查微信目录，当前发现 {len(catalog)} 个群，本次新增 {added} 个。'
        if refresh and sync.get('partial_group_coverage') and not added:
            message += '接口返回的群资料仍不完整，请按页面提示检查新群。'
        return {'items': [{'id': g.id, 'name': g.name, 'enabled': g.id in enabled} for g in catalog],
            'warning': warning, 'notice': sync.get('notice', ''), 'sync': sync,
            'enabled_count': len(enabled), 'message': message}

    def change_group(self, payload):
        identifier = payload.get('id')
        if not isinstance(identifier, str) or len(identifier) > 128 or not identifier.endswith('@chatroom'):
            raise UserError('请选择列表中的群聊。')
        if type(payload.get('enabled')) is not bool:
            raise UserError('接入状态无效。')
        return self.groups.change(add=[identifier] if payload['enabled'] else [], remove=[] if payload['enabled'] else [identifier])

    def change_config(self, mutate):
        with self.group_module.management_lock(self.root / 'data/group-management.lock'):
            running = self.alive()
            if running:
                self.control('stop')
            original = None
            try:
                original, config, bot = self.groups.read()
                mutate(config, bot)
                backup = self.root / 'data/config-backups'
                backup.mkdir(exist_ok=True)
                (backup / ('web-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.json')).write_bytes(original)
                self.groups._atomic_write(json.dumps(config, ensure_ascii=False, indent=2).encode('utf-8-sig'))
                if running:
                    self.control('start')
            except Exception:
                if original is not None:
                    self.groups._atomic_write(original)
                if running:
                    self.control('start')
                raise
        return {'message': '设置已保存并应用' if running else '设置已保存，下次启动生效', 'restarted': running}

    def settings(self):
        config, bot = self.config()
        source = next((s for s in config['provider_sources'] if s['id'] == 'deepseek'), {})
        model = next((s for s in config['provider'] if s['id'] == 'deepseek-rolechat'), {})
        key = any(k and not str(k).startswith('SET_') for k in source.get('key', []))
        limits=load_module('local_manager_context_limits',Path(__file__).resolve().parents[1]/'plugins/astrbot_plugin_personal_companion/context_limits.py')
        return {'model': model.get('model', 'deepseek-flash'), 'api_key_configured': key,
            'private_reply': bool(bot.get('enable_private_reply')), 'question_effort': 'max', 'chat_effort': 'high',
            **self.voice_public(bot.get('companion_speech') or {}), 'voice_reply': '收到语音时语音回答，普通文字仍用文字',
            'group_context_minutes': limits.GROUP_WINDOW_SECONDS//60, 'group_context_messages': limits.GROUP_MAX_MESSAGES,
            'private_context_days': limits.PRIVATE_WINDOW_SECONDS//86400, 'private_context_messages': limits.PRIVATE_MAX_MESSAGES,
            'conversation_max_turns':config.get('agent_runner',{}).get('config',{}).get('compression',{}).get('max_turns',limits.CONVERSATION_MAX_TURNS)}

    def save_settings(self, payload):
        model = payload.get('model')
        if model not in {'deepseek-flash', 'deepseek-v4-pro'}:
            raise UserError('请选择支持的 DeepSeek 模型。')
        private = payload.get('private_reply')
        if type(private) is not bool:
            raise UserError('私聊开关无效。')
        key = str(payload.get('api_key') or '').strip()
        if key and (len(key) < 12 or len(key) > 512 or re.search(r'\s', key)):
            raise UserError('API Key 格式不正确。留空可保留原密钥。')
        def mutate(config, bot):
            bot['enable_private_reply'] = private
            for provider in config['provider']:
                if provider['id'] in {'deepseek-rolechat', 'deepseek-rolechat-high', 'deepseek-rolechat-max'}:
                    provider['model'] = model
            if key:
                source = next(s for s in config['provider_sources'] if s['id'] == 'deepseek')
                source['key'] = [key]
                config['provider_settings']['websearch_deepseek_key'] = [key]
        return self.change_config(mutate)

    @staticmethod
    def tts_module():
        return load_module('local_manager_tts', Path(__file__).resolve().parents[1] / 'plugins/astrbot_plugin_personal_companion/tts.py')

    def voice_public(self, value):
        return {'voice': value.get('voice', 'stella'),
            'fish_reference_id': value.get('fish_reference_id', self.tts_module().HAJIMI_REFERENCE),
            'fish_model': value.get('fish_model', 's2.1-pro-free'),
            'fish_api_key_configured': bool(value.get('fish_api_key'))}

    def save_voice(self, payload):
        def mutate(config, bot):
            try:
                bot['companion_speech'] = self.tts_module().voice_settings(payload, bot.get('companion_speech'))
            except ValueError as exc:
                raise UserError(str(exc)) from None
        # Prevalidate before stopping the bot; validate again inside the config lock.
        _, bot = self.config()
        try:
            self.tts_module().voice_settings(payload, bot.get('companion_speech'))
        except ValueError as exc:
            raise UserError(str(exc)) from None
        return self.change_config(mutate)

    def preview_voice(self, payload):
        text = payload.get('text', '哈基米，曼波！我是大肥鱼，今天也要一起开心摸鱼。')
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 120:
            raise UserError('试听文字需为 1–120 个字符。')
        if not self.voice_preview_gate.acquire(blocking=False):
            raise UserError('另一个试听正在生成，请稍候。')
        try:
            config, bot = self.config()
            module = self.tts_module()
            settings = module.voice_settings(payload, bot.get('companion_speech'))
            source = next((s for s in config['provider_sources'] if s['id'] == 'deepseek'), {})
            key = next((k for k in source.get('key', []) if k and not k.startswith('SET_')), '')
            return asyncio.run(asyncio.wait_for(module.synthesize(settings,
                source.get('api_base', 'https://api.deepseek.com/v1'), key, text), timeout=28))
        except ValueError as exc:
            raise UserError(str(exc)) from None
        except Exception:
            raise UserError('试听未完成，请检查网络或语音服务，稍后再试。') from None
        finally:
            self.voice_preview_gate.release()

    def friends(self):
        _, bot = self.config()
        items, warning, cursor, seen = [], '', '', set()
        try:
            for _ in range(10):
                page = self.native('/contact-requests', {'direction':'incoming','actionable':'true','limit':100,
                    **({'cursor':cursor} if cursor else {})})
                for row in page.get('items', []):
                    if (row.get('accountId',bot['account_id']) != bot['account_id'] or row.get('direction') != 'incoming'
                            or row.get('actionable') is not True or row.get('isContact') is True):
                        continue
                    name = str(row.get('displayName') or '')
                    if not name or name.startswith('wxid_') or name.isdigit():
                        name = '新好友（昵称待同步）'
                    items.append({'id':row['id'],'name':name[:100],'message':str(row.get('message') or '')[:300],
                        'updated_at':row.get('updatedAt')})
                cursor = page.get('nextCursor')
                if not cursor:break
                if cursor in seen:raise ValueError('cursor')
                seen.add(cursor)
        except Exception:
            warning = '微信申请列表暂时无法读取，请确认机器人已启动且微信在线。'
        record = {'accepted':0,'pending':0,'needs_review':0,'failed':0,'last_checked':None,'error':''}
        path = self.companion / 'auto-friends.sqlite3'
        if path.exists():
            try:
                with contextlib.closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
                    for state,count in db.execute('SELECT status,count(*) FROM attempts WHERE account=? GROUP BY status',(bot['account_id'],)):
                        if state in {'accepted','contact'}:record['accepted']+=count
                        elif state in {'pending','submitting'}:record['pending']+=count
                        elif state in record:record[state]+=count
                    statuses=dict(db.execute('SELECT request_id,status FROM attempts WHERE account=?',(bot['account_id'],)))
                    for item in items:
                        item['auto_status']=statuses.get(item['id'],'waiting')
                    if db.execute("SELECT 1 FROM sqlite_master WHERE name='manual_commands'").fetchone():
                        queued=dict(db.execute("SELECT request_id,state FROM manual_commands WHERE account=? AND state IN ('queued','processing')",(bot['account_id'],)))
                        for item in items:item['manual_pending']=item['id'] in queued
                    last=db.execute('SELECT checked,error FROM scans WHERE account=?',(bot['account_id'],)).fetchone()
                    if last:record.update(last_checked=last[0],error=last[1])
            except sqlite3.Error:
                warning = warning or '自动通过记录暂时无法读取。'
        record['error_message'] = {'auth_required':'微信管理凭据无效','forbidden':'管理凭据没有通过好友的权限','rate_limited':'微信暂时限制请求，请稍后重试','wechat_offline':'微信当前未登录'}.get(record['error'], '检查未完成，请确认微信连接与管理设置' if record['error'] else '')
        return {'enabled':bot.get('companion_auto_friends',True) is True,'items':items,'pending_count':len(items),
            'warning':warning,'record':record,'private_reply':bool(bot.get('enable_private_reply'))}

    def accept_friend(self,payload):
        identifier=payload.get('id')
        if not isinstance(identifier,str) or not identifier or len(identifier)>512:
            raise UserError('好友申请无效，请刷新列表。')
        if not self.alive():raise UserError('请先启动机器人，再处理好友申请。')
        _,bot=self.config()
        if not bot.get('admin_token_file') or not (self.root/bot['admin_token_file']).is_file():
            raise UserError('请先配置本账号的 Native 管理凭据。')
        item=self.native('/contact-requests/'+quote(identifier,safe=''))
        if (item.get('id')!=identifier or item.get('accountId',bot['account_id'])!=bot['account_id']
                or item.get('direction')!='incoming' or item.get('actionable') is not True or item.get('isContact') is True):
            raise UserError('该申请已不可处理，请刷新列表。')
        module=load_module('local_manager_auto_friends',Path(__file__).resolve().parents[1]/'plugins/astrbot_plugin_personal_companion/auto_friends.py')
        self.companion.mkdir(parents=True,exist_ok=True)
        journal=module.Journal(self.companion/'auto-friends.sqlite3')
        try:journal.enqueue(bot['account_id'],identifier)
        finally:journal.close()
        return {'message':'已排队处理，约 30 秒后刷新本页查看微信实际结果。'}

    def save_friends(self, payload):
        enabled=payload.get('enabled')
        if type(enabled) is not bool:raise UserError('自动通过开关无效。')
        if enabled:
            _, bot = self.config()
            if not bot.get('admin_token_file') or not (self.root / bot['admin_token_file']).is_file():
                raise UserError('请先配置本账号的 Native 管理凭据。')
        def mutate(config, bot):
            if enabled:
                token_file=bot.get('admin_token_file')
                if not token_file or not (self.root / token_file).is_file():
                    raise UserError('请先配置本账号的 Native 管理凭据。')
            bot['companion_auto_friends']=enabled
        return self.change_config(mutate)

    def role(self):
        with contextlib.closing(sqlite3.connect(self.root / 'data/data_v4.db', timeout=4)) as db:
            row = db.execute('SELECT persona_id,system_prompt FROM personas WHERE persona_id=?', ('大肥鱼',)).fetchone()
        if not row:
            raise UserError('未找到“大肥鱼”角色，请在高级后台配置。')
        return {'id': row[0], 'prompt': row[1], 'binding': '当前机器人微信号 → 大肥鱼'}

    def save_role(self, payload):
        prompt = payload.get('prompt')
        if not isinstance(prompt, str) or not 10 <= len(prompt.strip()) <= 60000:
            raise UserError('角色设定需为 10–60000 个字符。')
        with self.group_module.management_lock(self.root / 'data/group-management.lock'):
            running = self.alive()
            if running:
                self.control('stop')
            try:
                path = self.root / 'data/data_v4.db'
                backup = self.root / 'data/config-backups' / ('role-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.sqlite3')
                backup.parent.mkdir(exist_ok=True)
                with contextlib.closing(sqlite3.connect(path)) as db, contextlib.closing(sqlite3.connect(backup)) as target:
                    db.backup(target)
                    with db:
                        if db.execute('UPDATE personas SET system_prompt=? WHERE persona_id=?', (prompt, '大肥鱼')).rowcount != 1:
                            raise UserError('角色保存失败，未找到对应角色。')
            finally:
                if running:
                    self.control('start')
        return {'message': '角色设定已保存', 'restarted': running}

    def sticker_policy(self):
        try:
            return json.loads((self.companion / 'auto-stickers.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {'enabled': False, 'cooldown_seconds': 30, 'min_replies_between': 1, 'stickers': []}

    def sticker_library(self):
        path = self.root / 'data/plugins/astrbot_plugin_personal_companion/sticker_library.py'
        return load_module('local_manager_sticker_library', path) if path.exists() else None

    def sticker_candidates(self):
        library = self.sticker_library()
        return (library.merged_policy(self.companion, self.config()[1]['account_id'])
                if library else self.sticker_policy())

    def stickers(self):
        policy = self.sticker_candidates()
        library = self.sticker_library()
        discovery = (library.discovery_status(self.companion, self.config()[1]['account_id']) if library else {'enabled': False, 'phase': 'unavailable'})
        return {k: policy.get(k, d) for k, d in [('enabled', False), ('cooldown_seconds', 30), ('min_replies_between', 1)]} | {'discovery': discovery,
            'items': [{'index': s['index'], 'description': s['description'],
                'pack_name': s.get('pack_name', ''),
                'image_url': '/api/stickers/thumbnail/' + str(s['index'])} for s in policy.get('stickers', [])]}

    def save_stickers(self, payload):
        enabled, cooldown = payload.get('enabled'), payload.get('cooldown_seconds')
        if type(enabled) is not bool or type(cooldown) is not int or not 30 <= cooldown <= 3600:
            raise UserError('自动表情间隔需为 30–3600 秒。')
        if 'discovery_enabled' in payload and type(payload['discovery_enabled']) is not bool:
            raise UserError('自动获取开关无效。')
        manager = load_module('local_manager_stickers', self.root / 'sticker_manager.py')
        with manager.library_lock(self.root / 'data/sticker-library-manager.lock'):
            policy = self.sticker_policy()
            policy.setdefault('account_id', self.config()[1]['account_id'])
            policy.setdefault('stickers', [])
            policy.setdefault('min_replies_between', 1)
            policy.update(enabled=enabled, cooldown_seconds=cooldown)
            manager.atomic_json(self.companion / 'auto-stickers.json', policy)
            if 'discovery_enabled' in payload:
                library = self.sticker_library()
                if library is None:
                    raise UserError('请先安装自动获取表情的运行插件。')
                settings_path = self.companion / 'sticker-discovery-settings.json'
                settings = library.read_json(settings_path)
                newly_enabled = payload['discovery_enabled'] and (settings.get('enabled') is not True or settings.get('account_id') != self.config()[1]['account_id'])
                settings.update(account_id=self.config()[1]['account_id'], enabled=payload['discovery_enabled'])
                if newly_enabled:
                    settings['request_id'] = secrets.token_hex(12)
                library.atomic_json(settings_path, settings)
        return {'message': '表情设置已保存，下一条聊天生效'}

    def discover_stickers(self):
        library = self.sticker_library()
        if library is None:
            raise UserError('自动获取插件尚未安装。')
        path = self.companion / 'sticker-discovery-settings.json'
        settings = library.read_json(path)
        if settings.get('account_id') != self.config()[1]['account_id'] or settings.get('enabled') is not True:
            raise UserError('请先开启并保存“自动获取免费表情包”。')
        if not self.alive():
            raise UserError('请先启动机器人，再更新表情库。')
        settings['request_id'] = secrets.token_hex(12)
        library.atomic_json(path, settings)
        return {'message': '已安排后台更新，约 15 秒内开始；稍后刷新查看进度，聊天可继续使用。'}

    def sync_stickers(self):
        manager = load_module('local_manager_stickers', self.root / 'sticker_manager.py')
        result = asyncio.run(manager.action(sync=True))
        if result is not True:
            raise UserError('表情同步尚未完成。再次同步会继续核对原任务，不会重复提交。')
        return {'message': '手机表情库已同步并缓存'}

    def thumbnail(self, index):
        item = next((s for s in self.sticker_candidates().get('stickers', []) if str(s['index']) == index), None)
        if item is None:
            raise UserError('表情不存在。')
        if item.get('pack_id'):
            preview = self.companion / 'sticker-previews' / (item['sha256'] + '.png')
            if preview.exists():
                return preview.read_bytes()
        cache = self.private / ('sticker-' + item['sha256'] + '.png')
        if cache.exists():
            return cache.read_bytes()
        bot = self.config()[1]
        if item.get('pack_id'):
            page = self.native('/sticker-packs/' + quote(item['pack_id'], safe='') + '/stickers')
            detail = next((i for i in page.get('items', []) if i.get('id') == item['id']), {})
        else:
            detail = self.native('/custom-stickers/' + quote(item['id'], safe=''))
        asset = detail.get('original') or {}
        if (asset.get('accountId') != bot['account_id'] or asset.get('sha256') != item['sha256']
                or asset.get('state') != 'available' or not isinstance(asset.get('id'), str)):
            raise UserError('表情来源校验失败。')
        base = bot['server_url'].rstrip('/')
        if urlsplit(base).hostname not in {'127.0.0.1', 'localhost'}:
            raise UserError('缩略图只读取本机微信服务。')
        if not base.endswith('/v1'):
            base += '/v1'
        token = (self.root / bot['token_file']).read_text(encoding='utf-8').strip()
        request = Request(base + '/accounts/' + quote(bot['account_id'], safe='') +
            '/media/' + quote(asset['id'], safe='') + '/content', headers={'Authorization': 'Bearer ' + token})
        with urlopen(request, timeout=8) as response:
            data = response.read(10 * 1024 * 1024 + 1)
        if len(data) != asset.get('byteLength') or hashlib.sha256(data).hexdigest() != item['sha256']:
            raise UserError('表情内容校验失败。')
        from PIL import Image
        with Image.open(io.BytesIO(data)) as original:
            image = original.convert('RGBA')
            image.thumbnail((180, 180))
            output = io.BytesIO()
            image.save(output, format='PNG')
        cache.write_bytes(output.getvalue())
        return output.getvalue()

    def memories(self, query=''):
        path = self.companion / 'people.sqlite3'
        if not path.exists():
            return {'items': []}
        account = self.config()[1]['account_id']
        with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
            rows = db.execute('SELECT user,nickname,facts,enabled,updated FROM users WHERE account=? '
                'AND (user LIKE ? OR nickname LIKE ?) ORDER BY updated DESC LIMIT 100',
                (account, '%' + query[:128] + '%', '%' + query[:128] + '%')).fetchall()
        return {'items': [{'id': r[0], 'nickname': r[1] or '昵称暂未获取', 'facts': json.loads(r[2]),
            'enabled': bool(r[3]), 'updated': r[4],
            'revision': self.memory_revision(account, r[0], json.loads(r[2]), bool(r[3]))} for r in rows]}

    @staticmethod
    def memory_revision(account, user, facts, enabled):
        value=json.dumps([account,user,facts,enabled],ensure_ascii=False,sort_keys=True,separators=(',',':'))
        return hashlib.sha256(value.encode('utf-8')).hexdigest()

    def edit_memory(self, payload):
        user, facts, revision=payload.get('user'),payload.get('facts'),payload.get('revision')
        fields={'称呼','职业或专业','兴趣','交流偏好','长期目标','相处设定'}
        if (not isinstance(user,str) or not 1<=len(user)<=256 or any(ord(c)<32 for c in user)
                or not isinstance(facts,dict) or not set(facts)<=fields
                or not isinstance(revision,str) or re.fullmatch(r'[0-9a-f]{64}',revision) is None):
            raise UserError('记忆编辑参数无效，请刷新后重试。')
        if any(not isinstance(v,str) or len(v)>240 for v in facts.values()):
            raise UserError('每项记忆需为文字，最多 240 字。')
        clean={k:v.strip() for k,v in facts.items() if v.strip()}
        path=self.companion/'people.sqlite3'
        if not path.exists():raise UserError('用户档案不存在。')
        account=self.config()[1]['account_id']
        with contextlib.closing(sqlite3.connect(path,timeout=5)) as db:
            with db:
                db.execute('BEGIN IMMEDIATE')
                row=db.execute('SELECT facts,enabled FROM users WHERE account=? AND user=?',(account,user)).fetchone()
                if row is None:raise UserError('用户档案不存在。')
                if not row[1]:raise UserError('该用户已停用记忆，请先开启记录后再编辑。')
                if self.memory_revision(account,user,json.loads(row[0]),bool(row[1]))!=revision:
                    raise UserError('该档案已有更新，请取消编辑并刷新后再修改，避免覆盖新资料。')
                db.execute('UPDATE users SET facts=?,updated=? WHERE account=? AND user=?',
                    (json.dumps(clean,ensure_ascii=False),time.time(),account,user))
        return {'message':'记忆已保存，下次聊天立即使用新资料'}

    def memory_action(self, payload):
        user, action = payload.get('user'), payload.get('action')
        if not isinstance(user, str) or not 1 <= len(user) <= 256 or action not in {'forget', 'enable'}:
            raise UserError('用户或操作无效。')
        path = self.companion / 'people.sqlite3'
        account = self.config()[1]['account_id']
        with contextlib.closing(sqlite3.connect(path, timeout=5)) as db:
            if not db.execute('SELECT 1 FROM users WHERE account=? AND user=?', (account, user)).fetchone():
                raise UserError('用户档案不存在。')
        from astrbot_plugin_personal_companion.memory import MemoryStore
        store = MemoryStore(path)
        try:
            store.enable(account, user, action == 'enable')
        finally:
            store.close()
        return {'message': '已清除该用户档案并停止记录' if action == 'forget' else '已开启该用户的记忆记录'}

    def logs(self, kind='bot'):
        paths = {'bot': self.root / 'logs/astrbot.out.log', 'error': self.root / 'logs/astrbot.err.log',
            'native': self.root / 'logs/native-control.log'}
        if kind not in paths:
            raise UserError('日志类型无效。')
        path = paths[kind]
        if not path.exists():
            return {'text': '暂无日志。'}
        with path.open('rb') as stream:
            stream.seek(max(0, path.stat().st_size - 96000))
            text = stream.read().decode('utf-8', errors='replace')
        return {'text': redact('\n'.join(text.splitlines()[-160:]))}

    def login_status(self):
        with self.login_lock:
            return dict(self.login or {'state': 'idle', 'has_qr': False})

    def start_login(self):
        # Keep the same Native operation/key across interrupted manager requests.
        with self.login_lock:
            if self.login and self.login['state'] in {'starting', 'awaiting_scan', 'awaiting_approval'}:
                raise UserError('已有登录请求，请先完成扫码。')
            self.login = {'state': 'starting', 'has_qr': False, 'message': '正在生成二维码'}
        async def run():
            from astrbot_plugin_wechat_native.config import NativeConfig
            from astrbot_plugin_wechat_native.native_client import NativeClient
            cfg = NativeConfig.from_dict(self.config()[1])
            token_file = self.root / (cfg.admin_token_file or 'native-state/api-token')
            token = token_file.read_text(encoding='utf-8').strip()
            path = self.private / 'login-operation.json'
            record = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
            active = {'starting', 'resuming', 'authenticating', 'queued', 'running', 'awaiting_scan', 'awaiting_approval'}
            if record.get('account') != cfg.account_id or record.get('state') not in active:
                record = {'account': cfg.account_id, 'key': 'rolebot-web-login-' + secrets.token_hex(16),
                    'state': 'starting', 'operation_id': None}
            def persist():
                temporary = path.with_suffix('.tmp')
                temporary.write_text(json.dumps(record), encoding='utf-8')
                os.replace(temporary, path)
            persist()  # Commit intent before requesting a new QR.
            generation = None
            async with NativeClient(cfg.api_url, cfg.account_id, token, request_timeout=6) as client:
                operation = (await client.login_status(record['operation_id']) if record.get('operation_id')
                    else await client.start_login(record['key'], force_qr=True))
                deadline = time.monotonic() + 910
                while True:
                    if (operation.get('accountId') != cfg.account_id or operation.get('kind') != 'login'
                            or (record.get('operation_id') and operation.get('id') != record['operation_id'])):
                        raise UserError('登录回执不匹配，未继续操作。')
                    current = operation['state']
                    record.update(operation_id=operation['id'], state=current)
                    persist()
                    result = operation.get('result') or {}
                    has_qr = current == 'awaiting_scan' and result.get('qrAvailable') is True
                    if has_qr and result.get('qrGeneration') != generation:
                        data = await client.login_qr(operation['id'], result['qrGeneration'])
                        temporary = self.private / 'login-qr.tmp'
                        temporary.write_bytes(data)
                        os.replace(temporary, self.private / 'login-qr.png')
                        generation = result['qrGeneration']
                    message = ('微信登录已完成' if current == 'succeeded' else
                        '请在手机微信上确认登录' if current == 'awaiting_approval' else
                        '请用手机微信扫码并确认' if has_qr else '正在读取微信登录状态')
                    with self.login_lock:
                        self.login.update(state=current, has_qr=has_qr, message=message)
                    if current == 'succeeded':
                        return {'message': message}
                    if current not in active:
                        raise UserError('微信登录未完成（' + current + '），可重新生成二维码。')
                    if time.monotonic() >= deadline:
                        raise UserError('登录请求仍在进行，再次操作会核对原登录任务。')
                    await asyncio.sleep(1)
                    operation = await client.login_status(operation['id'])
        try:
            return asyncio.run(run())
        except Exception:
            with self.login_lock:
                self.login.update(state='needs_review', has_qr=False, message='登录未完成，可再次操作核对原任务。')
            raise

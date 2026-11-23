"""Select WeChat groups by name and update only this bot's whitelist."""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class GroupManagerError(Exception):
    pass


@dataclass(frozen=True)
class Group:
    id: str
    name: str


def select_numbers(value, size):
    if not value.strip() or value.strip() == '0':
        return []
    parts = [p for p in re.split(r'[,，、;；\s]+', value.strip()) if p]
    if not parts or any(not p.isdigit() or not 1 <= int(p) <= size for p in parts):
        raise GroupManagerError('请输入列表中的编号，多个编号用逗号分开；输入0取消。')
    return list(dict.fromkeys(int(p) - 1 for p in parts))


@contextmanager
def management_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if path.stat().st_size == 0:
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise GroupManagerError('另一个窗口正在修改群聊，请等待它完成后再试。') from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class GroupManager:
    def __init__(self, root=ROOT, control=None, alive=None):
        self.root = Path(root)
        self.config_path = self.root / 'data/cmd_config.json'
        self.cache_path = self.root / 'data/group-manager-catalog.json'
        self.control = control or self._control
        self.alive = alive or self._alive

    def read(self):
        original = self.config_path.read_bytes()
        config = json.loads(original.decode('utf-8-sig'))
        bot = next((b for b in config['platform'] if b.get('id') == 'wechat-rolebot'), None)
        if bot is None:
            raise GroupManagerError('没有找到大肥鱼的微信机器人配置。')
        return original, config, bot

    def enabled_ids(self):
        return list(self.read()[2].get('allowed_groups') or [])

    def _native_get(self, path, params=None, *, method='GET'):
        bot = self.read()[2]
        token_file = bot.get('token_file')
        token = (self.root / token_file).read_text(encoding='utf-8').strip() if token_file else bot.get('token', '')
        base = bot['server_url'].rstrip('/')
        if not base.endswith('/v1'):
            base += '/v1'
        url = base + '/accounts/' + urllib.parse.quote(bot['account_id'], safe='') + path
        if params:
            url += '?' + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token}, method=method)
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                result = json.load(response)
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            raise GroupManagerError('暂时无法读取微信群。请先在控制台启动机器人，确认微信已登录，再刷新列表。') from None

    def cached_groups(self):
        try:
            value = json.loads(self.cache_path.read_text(encoding='utf-8'))
            if value.get('account_id') != self.read()[2]['account_id']:
                return []
            return [Group(x['id'], x['name']) for x in value.get('groups', [])]
        except (OSError, ValueError, KeyError, TypeError):
            return []

    def _catalog_pages(self, path):
        cursor, seen = '', set()
        account = self.read()[2]['account_id']
        for _ in range(100):
            params = {'limit': 100}
            if cursor:
                params['cursor'] = cursor
            data = self._native_get(path, params)
            if not isinstance(data.get('items'), list):
                raise GroupManagerError('微信目录返回格式异常，请稍后再同步。')
            for item in data['items']:
                if isinstance(item, dict) and item.get('accountId', account) == account:
                    yield item
            cursor = data.get('nextCursor')
            if not cursor:
                return
            if not isinstance(cursor, str) or cursor in seen:
                raise GroupManagerError('微信群列表翻页异常，请稍后同步。')
            seen.add(cursor)
        raise GroupManagerError('微信目录过大，本次读取未完成，请稍后再同步。')

    def catalog(self):
        groups = {}
        self.catalog_notice = ''
        for item in self._catalog_pages('/groups'):
            identifier = item.get('id')
            if isinstance(identifier, str) and identifier.endswith('@chatroom') and item.get('roomId', identifier) == identifier:
                groups[identifier] = Group(identifier, str(item.get('displayName') or item.get('name') or identifier))
        # A new group can already have a conversation before its roster/metadata
        # reaches /groups. Use account-scoped thread identities, never message text.
        try:
            threads = list(self._catalog_pages('/threads'))
            for item in threads:
                identifier = item.get('id')
                if (item.get('kind') == 'group' and isinstance(identifier, str)
                        and identifier.endswith('@chatroom') and identifier not in groups):
                    groups[identifier] = Group(identifier, str(item.get('displayName') or identifier))
        except GroupManagerError:
            self.catalog_notice = '群资料已读取，但会话目录读取未完成，新群可能尚未显示。'
        result = sorted(groups.values(), key=lambda g: (g.name.casefold(), g.id))
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.cache_path.parent, prefix='group-catalog-', suffix='.tmp', mode='w', encoding='utf-8', delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({'account_id': self.read()[2]['account_id'],
                    'groups': [{'id': g.id, 'name': g.name} for g in result]}, stream, ensure_ascii=False)
            os.replace(temporary, self.cache_path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        return result

    def sync_status(self):
        try:
            value = json.loads((self.root / 'data/group-catalog-status.json').read_text(encoding='utf-8'))
            return value if value.get('account_id') == self.read()[2]['account_id'] else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _sync_phase(self, endpoint, state_path, deadline, poll_interval):
        account = self.read()[2]['account_id']
        previous = {}
        try:
            previous = json.loads(state_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
        if (previous.get('account_id') == account and previous.get('id')
                and previous.get('state') in {'queued', 'running'}):
            operation = self._native_get('/operations/' + urllib.parse.quote(previous['id'], safe=''))
        else:
            operation = self._native_get(endpoint, method='POST')
        while True:
            if (not isinstance(operation, dict) or operation.get('accountId') != account
                    or operation.get('kind') != 'metadata' or not isinstance(operation.get('id'), str)):
                raise GroupManagerError('微信群同步回执不匹配，本次未更新列表。')
            record = {'account_id': account, 'id': operation['id'], 'state': operation.get('state')}
            temporary = state_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(record), encoding='utf-8')
            os.replace(temporary, state_path)
            if operation.get('state') == 'succeeded':
                return operation.get('result') or {}
            if operation.get('state') not in {'queued', 'running'}:
                raise GroupManagerError('微信端目录同步未完成，请确认机器人微信在线后再试。')
            if time.monotonic() >= deadline:
                raise GroupManagerError('微信仍在同步目录，稍后再同步会继续核对原任务。')
            time.sleep(poll_interval)
            operation = self._native_get('/operations/' + urllib.parse.quote(operation['id'], safe=''))

    def refresh(self, timeout=45, poll_interval=0.5):
        """Sync contact inventory, group metadata and merge new chat identities."""
        with management_lock(self.root / 'data/group-catalog-sync.lock'):
            deadline = time.monotonic() + timeout
            results, warnings = {}, []
            for phase, endpoint, filename in [
                    ('contacts', '/contact-sync', 'contact-catalog-sync.json'),
                    ('groups', '/group-sync', 'group-catalog-sync.json')]:
                try:
                    results[phase] = self._sync_phase(endpoint, self.root / 'data' / filename, deadline, poll_interval)
                    if results[phase].get('complete') is not True:
                        warnings.append('微信的' + ('联系人' if phase == 'contacts' else '群资料') + '同步尚未完整返回')
                except GroupManagerError as exc:
                    warnings.append(str(exc))
                if time.monotonic() >= deadline:
                    break
            groups = self.catalog()
            if self.catalog_notice:
                warnings.append(self.catalog_notice)
            partial = results.get('groups', {}).get('coverage') == 'group_metadata_partial'
            status = {'account_id': self.read()[2]['account_id'], 'checked_at': int(time.time()),
                'count': len(groups), 'partial_group_coverage': partial, 'warning': '；'.join(dict.fromkeys(warnings)),
                'notice': '微信群资料接口只覆盖部分目录；如果新群未出现，请确认机器人已入群，并在新群发一条文字消息后再次同步。' if partial else ''}
            temporary = self.root / 'data/group-catalog-status.tmp'
            temporary.write_text(json.dumps(status, ensure_ascii=False), encoding='utf-8')
            os.replace(temporary, self.root / 'data/group-catalog-status.json')
            return groups

    def enabled_groups(self, catalog=None):
        names = {g.id: g.name for g in (catalog if catalog is not None else self.cached_groups())}
        return [Group(identifier, names.get(identifier, identifier)) for identifier in self.enabled_ids()]

    def _atomic_write(self, content):
        path = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.config_path.parent, prefix='group-settings-', suffix='.tmp', delete=False) as stream:
                path = Path(stream.name)
                stream.write(content); stream.flush(); os.fsync(stream.fileno())
            os.replace(path, self.config_path)
        finally:
            if path is not None and path.exists():
                path.unlink()

    def _control(self, action):
        try:
            subprocess.run([str(self.root / '.venv/Scripts/python.exe'), '-X', 'utf8',
                str(self.root / 'manage.py'), action], cwd=self.root, check=True,
                capture_output=True, timeout=100, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (subprocess.SubprocessError, OSError):
            raise GroupManagerError('机器人' + ('启动' if action == 'start' else '关闭') + '未完成，请查看项目 logs 文件夹。') from None

    @staticmethod
    def _alive():
        try:
            with urllib.request.urlopen('http://127.0.0.1:6185/api/auth/setup-status', timeout=2) as response:
                return response.status == 200
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    @staticmethod
    def proposed(current, add, remove):
        result = list(dict.fromkeys(g for g in current if g not in remove))
        result += [g for g in add if g not in result]
        if not result:
            raise GroupManagerError('不能移除全部群聊：空白名单会开放所有群。若要停止全部回复，请关闭机器人。')
        return result

    def change(self, add=(), remove=()):
        add, remove = list(dict.fromkeys(add)), set(remove)
        if set(add) & remove:
            raise GroupManagerError('同一个群不能同时接入和移除。')
        if any(not isinstance(g, str) or not g.endswith('@chatroom') for g in add + list(remove)):
            raise GroupManagerError('选择的群聊无效，请从群列表重新选择。')
        with management_lock(self.root / 'data/group-management.lock'):
            current = self.enabled_ids()
            proposed = self.proposed(current, add, remove)
            if proposed == current:
                return {'changed': False, 'count': len(current), 'restarted': False}
            for identifier in add:
                if identifier not in current:
                    try:
                        group = self._native_get('/groups/' + urllib.parse.quote(identifier, safe=''))
                    except GroupManagerError:
                        # A group conversation is independently sufficient to
                        # verify a newly discovered ID when its roster is absent.
                        thread = self._native_get('/threads/' + urllib.parse.quote(identifier, safe=''))
                        if (thread.get('id') != identifier or thread.get('kind') != 'group'
                                or thread.get('accountId') != self.read()[2]['account_id']):
                            raise GroupManagerError('新群会话归属不匹配，请重新同步目录。')
                        group = thread
                    if group.get('id') != identifier:
                        raise GroupManagerError('微信群资料不匹配，请刷新列表后重新选择。')
            running = self.alive()
            original = None
            written = False
            try:
                if running:
                    print('正在保存并应用群聊设置，机器人将短暂重启……', flush=True)
                    self.control('stop')
                    if self.alive():
                        raise GroupManagerError('后台尚未停止，本次没有修改配置。')
                # Re-read after stopping so any latest dashboard edits are preserved.
                original, config, bot = self.read()
                proposed = self.proposed(bot.get('allowed_groups') or [], add, remove)
                backup = self.root / 'data/config-backups'
                backup.mkdir(parents=True, exist_ok=True)
                (backup / ('groups-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.json')).write_bytes(original)
                bot['allowed_groups'] = proposed
                self._atomic_write(json.dumps(config, ensure_ascii=False, indent=4).encode('utf-8-sig'))
                written = True
                if running:
                    self.control('start')
                    if not self.alive():
                        raise GroupManagerError('后台尚未恢复。')
            except Exception as exc:
                recovered = True
                if written:
                    if running:
                        try: self.control('stop')
                        except Exception: pass
                    self._atomic_write(original)
                if running:
                    try: self.control('start')
                    except Exception: recovered = False
                if written:
                    message = '新设置未能生效，已恢复原群配置。'
                    if not recovered: message += '请回控制台重新启动机器人。'
                    raise GroupManagerError(message) from None
                raise exc
            return {'changed': True, 'count': len(proposed), 'restarted': running}


def display(groups, enabled):
    names = [g.name for g in groups]
    for i, group in enumerate(groups, 1):
        suffix = (' [' + group.id + ']') if names.count(group.name) > 1 else ''
        print(f'  {i:>2}. {group.name}{suffix}' + ('  [已接入]' if group.id in enabled else ''))


def run_menu(root=ROOT):
    manager = GroupManager(root)
    try:
        try:
            catalog = manager.catalog()
        except GroupManagerError as exc:
            catalog = manager.cached_groups(); print(str(exc))
        while True:
            enabled = manager.enabled_ids()
            print(f'\n══════ 管理微信群 · 已接入 {len(enabled)} 个群 ══════')
            print('  1  添加群聊（按群名搜索）\n  2  移除已接入群\n  3  查看已接入群\n  4  刷新微信群列表\n  0  返回')
            print('新增群前，先把机器人微信号拉入那个群。')
            choice = input('\n请选择：').strip()
            if choice == '0': return
            try:
                if choice == '4':
                    print('正在从微信同步群目录……', flush=True)
                    catalog = manager.refresh(); print(f'已同步，微信中共有 {len(catalog)} 个可查询的群。'); continue
                if choice == '3':
                    display(manager.enabled_groups(catalog), enabled); continue
                if choice not in {'1', '2'}:
                    print('请输入菜单中的数字。'); continue
                if choice == '1':
                    keyword = input('输入群名关键词（回车显示所有群，0返回）：').strip()
                    if keyword == '0': continue
                    catalog = manager.catalog()
                    matches = [g for g in catalog if keyword.casefold() in g.name.casefold()]
                    if not matches:
                        print('本地目录没有匹配项，正在从微信同步新群……', flush=True)
                        catalog = manager.refresh()
                        matches = [g for g in catalog if keyword.casefold() in g.name.casefold()]
                    if len(matches) > 50:
                        print('匹配群太多，请输入更具体的群名。'); continue
                else:
                    matches = manager.enabled_groups(catalog)
                if not matches:
                    print('微信同步后仍未找到群。请确认加入的是机器人微信号，并在新群发一条消息后再刷新。'); continue
                display(matches, enabled)
                selection = input('输入要' + ('接入' if choice == '1' else '移除') + '的编号（多个用逗号分开，0取消）：')
                positions = select_numbers(selection, len(matches))
                if not positions: continue
                chosen = [matches[i] for i in positions]
                result = manager.change(add=[g.id for g in chosen] if choice == '1' else (),
                                        remove=[g.id for g in chosen] if choice == '2' else ())
                if not result['changed']:
                    print('这些群已经接入，无需修改。'); continue
                print(('已接入：' if choice == '1' else '已移除：') + '、'.join(g.name for g in chosen))
                print(f'当前接入 {result["count"]} 个群。' + ('设置已生效。' if result['restarted'] else '下次启动机器人时生效。'))
            except (GroupManagerError, OSError, ValueError) as exc:
                print('操作未完成：' + str(exc))
    except (KeyboardInterrupt, EOFError):
        return
    except PermissionError:
        print('无法读取机器人配置，请从你自己的 Windows 桌面打开本工具。')
    except (OSError, ValueError, GroupManagerError) as exc:
        print('无法打开群管理：' + str(exc))


if __name__ == '__main__':
    run_menu()

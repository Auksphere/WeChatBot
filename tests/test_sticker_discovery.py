"""Offline acquisition/receipt/identity regressions; no real WeChat sends."""
import asyncio
import copy
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'plugins'))
from plugins.astrbot_plugin_personal_companion.sticker_discovery import StickerDiscovery, DiscoveryDisabled
from plugins.astrbot_plugin_personal_companion.sticker_library import atomic_json, read_json, merged_policy
from astrbot_plugin_wechat_native.sticker_service import StickerService
from astrbot_plugin_wechat_native.tool_support import ToolError

SID = 'a' * 32
SHA = 'b' * 64


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.added = set()
        self.writes = []
        self.fail_submit = False
        self.context = SimpleNamespace()
        self.worker = StickerDiscovery(self.context, self.directory)
        atomic_json(self.worker.settings_path, {'account_id': 'fixture', 'enabled': True, 'request_id': 'one'})
        self.client = SimpleNamespace(prefix='/fixture', _request=AsyncMock(side_effect=self.request),
                                      job=AsyncMock(), sticker_packs=AsyncMock(side_effect=self.packs),
                                      pack_stickers=AsyncMock(side_effect=self.items))
        self.adapter = SimpleNamespace(native_config=SimpleNamespace(account_id='fixture', enable_stickers=True, enable_media=True),
                                       stopped=False, client=self.client, state=SimpleNamespace(upsert_sticker_label=lambda *a, **k: None),
                                       stickers=SimpleNamespace(original_gif_ready=lambda a: isinstance(a, dict) and a.get('state') == 'available'))
        self.worker.caption = AsyncMock(return_value=('小猫点头，表示同意、赞同', b'fixture-preview'))

    def tearDown(self):
        self.temp.cleanup()

    async def packs(self):
        return {'items': [self.pack(p) for p in self.added]}

    def pack(self, pid):
        return {'id': pid, 'name': pid, 'eligible': True,
                'membership': 'added' if pid in self.added else 'not_added', 'contentReadiness': 'unverified'}

    async def items(self, pid):
        return {'packId': pid, 'items': [{'id': SID, 'packId': pid, 'original':
                {'state': 'available', 'sha256': SHA}}]}

    async def request(self, method, path, **kwargs):
        if method == 'GET' and path == '/fixture/sticker-packs':
            return {'coverage': 'available_feed', 'nextCursor': None, 'items': [
                self.pack('free-pack'), dict(self.pack('paid-pack'), eligible=False),
                dict(self.pack('unknown-pack'), eligible=None)]}
        if method == 'GET':
            return self.pack(path.rsplit('/', 1)[-1])
        self.writes.append((method, path, kwargs))
        if self.fail_submit:
            raise TimeoutError()
        add = method == 'PUT'
        if add:
            self.added.add('free-pack')
        result = {'serverAcceptance': 'accepted', 'profileWriteback': 'complete'}
        if not add:
            result.update(packId='free-pack', membership='added', contentReadiness='available')
        return {'id': 'job-add' if add else 'job-download', 'accountId': 'fixture',
                'kind': 'sticker_pack' if add else 'sticker_acquire', 'state': 'available',
                'phase': 'completed', 'result': result}

    async def test_only_free_pack_added_downloaded_and_indexed_without_chat_send(self):
        await self.worker.cycle(self.adapter)
        state = read_json(self.worker.path)
        self.assertEqual(set(state['packs']), {'free-pack'})
        self.assertEqual([w[0] for w in self.writes], ['PUT', 'POST'])
        self.assertEqual(state['phase'], 'ready')
        self.assertEqual(state['items'][0]['pack_id'], 'free-pack')
        self.assertTrue((self.directory / 'sticker-previews' / (SHA + '.png')).exists())
        self.assertFalse(hasattr(self.client, 'send'))

    async def test_ambiguous_submission_reuses_persisted_key_after_new_worker(self):
        self.fail_submit = True
        state = {'account_id': 'fixture', 'operations': {}}
        self.assertEqual(await self.worker.operation(self.adapter, state, 'add:free-pack', 'free-pack'), 'needs_review')
        replacement = StickerDiscovery(self.context, self.directory)
        await replacement.operation(self.adapter, read_json(self.worker.path), 'add:free-pack', 'free-pack')
        self.assertEqual(self.writes[0][2]['headers'], self.writes[1][2]['headers'])

    async def test_disable_stops_writes(self):
        atomic_json(self.worker.settings_path, {'account_id': 'fixture', 'enabled': False})
        with self.assertRaises(DiscoveryDisabled):
            await self.worker.cycle(self.adapter)
        self.assertEqual(self.writes, [])

    async def test_foreign_account_state_rejected(self):
        atomic_json(self.worker.path, {'account_id': 'other'})
        with self.assertRaises(ValueError):
            await self.worker.cycle(self.adapter)
        self.assertEqual(self.writes, [])

    async def test_user_removed_pack_is_not_readded(self):
        await self.worker.cycle(self.adapter)
        self.added.clear()
        before = len(self.writes)
        await self.worker.cycle(self.adapter)
        state = read_json(self.worker.path)
        self.assertTrue(state['packs']['free-pack']['removed'])
        self.assertEqual(state['items'], [])
        self.assertEqual(len(self.writes), before)

    async def test_completed_receipt_from_other_account_is_not_accepted(self):
        real = self.client._request.side_effect
        async def foreign(*args, **kwargs):
            result = await real(*args, **kwargs)
            result['accountId'] = 'other'
            return result
        self.client._request.side_effect = foreign
        state = {'account_id': 'fixture', 'operations': {}}
        self.assertEqual(await self.worker.operation(self.adapter, state, 'add:free-pack', 'free-pack'), 'needs_review')


class PolicyTests(unittest.TestCase):
    def test_merge_keeps_manual_candidates_and_checks_account_and_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manual = {'index': 1, 'id': SID, 'sha256': SHA, 'description': '手动候选'}
            new = dict(manual, index=10000, id='c' * 32, pack_id='free-pack')
            atomic_json(root / 'auto-stickers.json', {'account_id': 'fixture', 'enabled': True, 'stickers': [manual]})
            atomic_json(root / 'sticker-discovery-state.json', {'account_id': 'fixture', 'items': [manual, new, dict(new, id='bad')]})
            self.assertEqual(merged_policy(root, 'fixture')['stickers'], [manual, new])
            self.assertEqual(merged_policy(root, 'other'), {})


class SelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_selection_binds_exact_source_and_rejects_changed_content(self):
        asset = {'sha256': SHA}
        adapter = SimpleNamespace()
        service = StickerService(adapter)
        service.validate_selection = AsyncMock(return_value={'original': asset})
        service.original_gif_ready = lambda a: a is asset
        extras = {}
        event = SimpleNamespace(get_extra=extras.get, set_extra=extras.__setitem__)
        ctx = SimpleNamespace(account='fixture', thread='room', message_id='msg')
        source = {'kind': 'pack', 'packId': 'free-pack', 'stickerId': SID}
        with patch('astrbot_plugin_wechat_native.sticker_service.context', return_value=ctx):
            token = await service.select_ready(event, source, SHA)
            retained = extras['wechat_native_sticker_selections'][token]
            self.assertEqual(retained['source'], source)
            self.assertEqual(retained['thread'], 'room')
            with self.assertRaises(ToolError):
                await service.select_ready(event, source, 'c' * 64)
            self.assertEqual(len(extras['wechat_native_sticker_selections']), 1)


if __name__ == '__main__':
    unittest.main()

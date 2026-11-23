"""Offline regressions for direct private stickers and leaked internal tool text."""
import asyncio
import copy
from dataclasses import replace
from datetime import datetime,timezone
import io
from pathlib import Path
import sys
import subprocess
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock,patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'plugins'))
from astrbot_plugin_wechat_native.adapter import WeChatNativeAdapter
from astrbot_plugin_wechat_native.config import NativeConfig
from astrbot_plugin_wechat_native.event import convert_message,serialize_chain,sticker_preview
from astrbot_plugin_wechat_native.native_client import MediaError
from astrbot_plugin_wechat_native.sticker_service import StickerService
from astrbot_plugin_wechat_native.output_text import has_tool_markup,visible_text,reply_fallback,clean_contexts
from astrbot_plugin_personal_companion.auto_stickers import ordinary_chat
from astrbot_plugin_personal_companion.stickers import sticker_request
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain,Image

LEAK='<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name="wechat_find_stickers">\n<｜｜DSML｜｜ parameter name="query" string="true">得意 骄傲</｜｜DSML｜｜ parameter>\n</｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls>'

def cfg(**changes):
    return NativeConfig('http://127.0.0.1:6174/v1','fixture-account','fixture','',enable_stickers=True,**changes)

def message(**changes):
    return dict(id='fixture-message',accountId='fixture-account',threadId='alice',senderId='alice',serverId='server-1',direction='incoming',type=47,appMessageType=None,body=None,recall=None,createdAt=datetime.now(timezone.utc).isoformat(),**changes)

class ClassifyTests(unittest.TestCase):
    def classify(self,raw,config=None,origin='live'):
        adapter=SimpleNamespace(native_config=config or cfg(),bot_self_id='bot',_check_timestamp=lambda r:True)
        return WeChatNativeAdapter._classify(adapter,raw,origin)

    def test_private_direct_sticker_is_media(self):
        self.assertEqual(self.classify(message()),'media')
        raw=message();raw.update(type=49,appMessageType=8)
        self.assertEqual(self.classify(raw),'media')

    def test_does_not_open_group_or_disabled_or_replay_replies(self):
        raw=message();raw['threadId']='group@chatroom'
        self.assertEqual(self.classify(raw),'ignore')
        for config in [replace(cfg(),enable_private_reply=False),replace(cfg(),enable_stickers=False),replace(cfg(),enable_media=False),replace(cfg(),allowed_users=frozenset({'bob'}))]:
            self.assertEqual(self.classify(message(),config),'ignore')
        self.assertEqual(self.classify(message(),origin='replay'),'ignore')
        raw=message();raw['recall']={}
        self.assertEqual(self.classify(raw),'ignore')
        raw=message();raw['direction']='outgoing'
        self.assertEqual(self.classify(raw),'ignore')

    def test_direct_sticker_event_contains_real_image_and_reaction_prompt(self):
        data=b'fixture-image'
        converted=convert_message(message(),cfg(),'bot',image_bytes=data,sticker=True)
        self.assertIn('表情包',converted.message_str)
        self.assertIn('静态预览',converted.message_str)
        self.assertTrue(any(isinstance(p,Image) for p in converted.message))
        extras={'wechat_native_media_kind':'sticker'}
        event=SimpleNamespace(message_obj=converted,message_str=converted.message_str,get_extra=extras.get)
        self.assertTrue(ordinary_chat(event,'哈哈，这表情很贴切。'))
        extras['companion_voice_reply']=True
        self.assertFalse(ordinary_chat(event,'哈哈。'))

class ReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.raw=message()
        self.asset={'id':'asset-1','state':'available','kind':'sticker','variant':'original','sha256':'fixture-sha','byteLength':8,'contentType':'image/gif'}
        self.client=SimpleNamespace(media_timeout=2,message=AsyncMock(side_effect=lambda _:copy.deepcopy(self.raw)),sticker_assets=AsyncMock(side_effect=lambda *args,**kwargs:[dict(self.asset)]),sticker_content=AsyncMock(return_value=b'GIF-data'))
        self.adapter=SimpleNamespace(native_config=cfg(),bot_self_id='bot',client=self.client,state=SimpleNamespace(message_status=lambda _: 'queued',is_recalled=lambda *a:False))
        self.service=StickerService(self.adapter)

    async def test_direct_read_uses_original_id_not_synthetic_quote(self):
        data,asset=await self.service.read_quote(self.raw)
        self.assertEqual(data,b'GIF-data')
        self.client.sticker_assets.assert_any_await('fixture-message',quoted=False)
        self.assertNotIn('quote',self.raw)
        self.assertEqual(self.client.message.await_count,3)

    async def test_identity_change_or_recall_rejects_before_decode(self):
        for field,value in [('accountId','foreign'),('senderId','bob'),('threadId','bob'),('serverId','foreign'),('direction','outgoing'),('recall',{})]:
            changed={**self.raw,field:value}
            self.client.message=AsyncMock(return_value=changed)
            with self.assertRaises(MediaError):await self.service.read_quote(self.raw)
        self.client.sticker_content.assert_not_called()

    async def test_changed_asset_after_download_is_rejected(self):
        self.client.sticker_assets=AsyncMock(side_effect=[[self.asset],[{**self.asset,'sha256':'different'}]])
        with self.assertRaises(MediaError):await self.service.read_quote(self.raw)

    async def test_wrong_source_or_disabled_private_read_is_rejected(self):
        with self.assertRaises(MediaError):await self.service.check_quote(self.raw,source_message_id='foreign')
        self.adapter.native_config=replace(cfg(),enable_private_reply=False)
        with self.assertRaises(MediaError):await self.service.read_quote(self.raw)
        self.client.sticker_content.assert_not_called()

class OutputTests(unittest.TestCase):
    def test_real_leak_and_truncated_ascii_variant_never_show_arguments(self):
        self.assertTrue(has_tool_markup(LEAK))
        self.assertNotIn('得意',visible_text(LEAK))
        self.assertEqual(visible_text('可以。\n'+LEAK),'可以。')
        self.assertEqual(visible_text('<||DSML|| invoke name="unknown">secret','fallback'),'fallback')
        self.assertIn('表情',visible_text(LEAK,reply_fallback('你能发表情包嘛')))

    def test_context_cleanup_preserves_user_and_structured_tool_calls(self):
        original=[{'role':'user','content':LEAK},{'role':'assistant','content':LEAK},{'role':'assistant','content':[{'type':'text','text':LEAK}],'tool_calls':[{'id':'real-tool','type':'function'}]}]
        cleaned=clean_contexts(original)
        self.assertEqual(cleaned[0],original[0])
        self.assertEqual(original[1]['content'],LEAK)
        self.assertFalse(has_tool_markup(cleaned[1]['content']))
        self.assertEqual(cleaned[2]['tool_calls'],original[2]['tool_calls'])
        self.assertFalse(has_tool_markup(cleaned[2]['content'][0]['text']))

    def test_fragmented_native_stream_is_combined_before_guard(self):
        chain=MessageChain();chain.chain=[Plain(LEAK[:9]),Plain(LEAK[9:])]
        parts,_,_=serialize_chain(chain,group=False)
        self.assertEqual(len(parts),1)
        self.assertTrue(has_tool_markup(parts[0]))
        self.assertEqual(visible_text('普通回复：你好！'),'普通回复：你好！')

    def test_companion_imports_under_astrbot_namespace(self):
        script="import sys,types; p=types.ModuleType('data'); p.__path__=[sys.argv[1]];sys.modules['data']=p; import data.plugins.astrbot_plugin_personal_companion.main; print('companion_import_ok')"
        result=subprocess.run([sys.executable,'-X','utf8','-c',script,str(ROOT)],cwd=ROOT,capture_output=True,text=True,timeout=30,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
        self.assertEqual(result.returncode,0,result.stderr[-2500:])
        self.assertIn('companion_import_ok',result.stdout)

    def test_explicit_named_sticker_request_stays_deterministic(self):
        self.assertEqual(sticker_request('发一个表情包'),'')
        self.assertEqual(sticker_request('发一个得意的表情包'),'得意')
        self.assertIsNone(sticker_request('为什么不回我的表情包'))
        self.assertIsNone(sticker_request('你能发表情包嘛'))

if __name__=='__main__':unittest.main()

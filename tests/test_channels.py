"""Offline Channels identity, download and evidence checks."""
import ast
import asyncio
import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
PACKAGE='_channels_fixture'
package=types.ModuleType(PACKAGE);package.__path__=[]
typed=types.ModuleType(PACKAGE+'.typed_trigger');typed.addressed=lambda *args:True;typed.private_request=lambda *args:True
sys.modules[PACKAGE]=package;sys.modules[typed.__name__]=typed

def load(name):
    spec=importlib.util.spec_from_file_location(PACKAGE+'.'+name,ROOT/'plugins/astrbot_plugin_personal_companion'/f'{name}.py')
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module);return module
media=load('media');channels=load('channels')
NOW=datetime.now(timezone.utc).isoformat()
def message(identifier='card',**changes):
    return {'id':identifier,'accountId':'account','threadId':'group@chatroom','senderId':'alice','direction':'incoming',
        'serverId':'server-'+identifier,'type':49,'appMessageType':51,'createdAt':NOW,'recall':None,
        'link':{'sourceName':'创作者','description':'卡片描述'},**changes}

class ChannelsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source=message()
        self.raw=message('question',type=49,appMessageType=57,senderId='bob',quote={
            'serverId':'server-card','senderId':'alice','type':49,'appMessageType':51,'state':'unsupported'})
        self.payload=b'fixture-video-bytes'
        self.asset={'id':'asset','accountId':'account','messageId':'card','kind':'video','variant':'normal',
            'source':'message','state':'remote','byteLength':len(self.payload),'sha256':hashlib.sha256(self.payload).hexdigest()}
        self.client=types.SimpleNamespace(messages=AsyncMock(return_value={'items':[self.source]}),
            media_assets=AsyncMock(side_effect=[{'items':[self.asset]},{'items':[{**self.asset,'state':'available'}]}]),
            transfer_media=AsyncMock(return_value={'id':'job','state':'succeeded'}),job=AsyncMock(),
            media_content=AsyncMock(return_value=(self.payload,'video/mp4')),
            message=AsyncMock(side_effect=lambda key:copy.deepcopy(self.source if key=='card' else self.raw)))
        cfg=types.SimpleNamespace(account_id='account',allowed_groups={'group@chatroom'})
        self.extras={}
        self.event=types.SimpleNamespace(adapter=types.SimpleNamespace(client=self.client,native_config=cfg),
            message_obj=types.SimpleNamespace(raw_message=self.raw),message_str='@大肥鱼 总结这个视频',
            get_sender_id=lambda:self.raw['senderId'],set_extra=self.extras.__setitem__)

    async def test_quote_of_other_member_resolves_exact_source(self):
        source=await channels.locate(self.event)
        self.assertEqual(source['id'],'card')
        self.assertEqual(source['senderId'],'alice')

    def test_recent_followup_only_selects_current_sender_in_same_chat(self):
        raw=message('q',type=1,appMessageType=None,senderId='bob')
        own=message('own',senderId='bob')
        foreign=message('foreign',senderId='bob',threadId='other@chatroom')
        self.assertIsNone(channels.select_source(raw,'总结视频号',[self.source,foreign]))
        self.assertEqual(channels.select_source(raw,'总结视频号',[self.source,foreign,own])['id'],'own')

    def test_no_borrowing_older_channel_when_latest_upload_is_regular_video(self):
        raw=message('q',type=1,appMessageType=None)
        old=message(createdAt='2026-11-22T00:00:00+00:00')
        newer=message('ordinary',type=43,appMessageType=None,createdAt='2026-11-22T00:01:00+00:00')
        now=datetime.fromisoformat('2026-11-22T00:02:00+00:00').timestamp()
        self.assertIsNone(channels.select_source(raw,'总结这个视频',[old,newer],now=now))

    def test_foreign_account_recalled_or_wrong_quote_author_rejected(self):
        for patch_fields in [{'accountId':'other'},{'threadId':'other@chatroom'},{'recall':{}},{'senderId':'eve'}]:
            value={**self.source,**patch_fields}
            if patch_fields=={'recall':{}}:value['recall']={'state':'recalled'}
            self.assertIsNone(channels.select_source(self.raw,'总结',[value]))

    async def test_direct_regular_video_and_quoted_video_use_full_pipeline(self):
        self.event.message_obj.raw_message={**self.raw,'type':43,'appMessageType':None,'quote':None}
        source=await channels.locate_video(self.event)
        self.assertEqual(source['type'],43)
        self.event.message_obj.raw_message={**self.raw,'quote':{**self.raw['quote'],'type':43,'appMessageType':None}}
        self.client.messages.return_value={'items':[{**self.source,'type':43,'appMessageType':None}]}
        source=await channels.locate_video(self.event)
        self.assertEqual(source['type'],43)

    async def test_direct_card_is_supported(self):
        self.event.message_obj.raw_message=self.source
        self.event.get_sender_id=lambda:'alice'
        self.assertEqual((await channels.locate(self.event))['id'],'card')
        self.client.messages.assert_not_called()

    async def test_download_uses_normal_quality_and_checks_integrity(self):
        data,name,video,identifier=await media.attachment_bytes(self.event,'video',source_id='card')
        self.assertEqual((data,name,video,identifier),(self.payload,'video.mp4',True,'card'))
        self.assertEqual(self.client.transfer_media.call_args.args,('card','normal'))

    async def test_cross_account_asset_never_downloaded(self):
        self.client.media_assets=AsyncMock(return_value={'items':[{**self.asset,'accountId':'other'}]})
        with self.assertRaises(ValueError):await media.attachment_bytes(self.event,'video',source_id='card')
        self.client.transfer_media.assert_not_called()

    async def test_bad_video_hash_rejected(self):
        self.client.media_content=AsyncMock(return_value=(b'bad-bytes','video/mp4'))
        with self.assertRaisesRegex(ValueError,'校验失败'):await media.attachment_bytes(self.event,'video',source_id='card')

    async def test_download_failure_does_not_claim_success(self):
        self.client.transfer_media=AsyncMock(return_value={'id':'job','state':'failed'})
        with self.assertRaisesRegex(ValueError,'下载未完成'):await media.attachment_bytes(self.event,'video',source_id='card')

    async def test_channel_delegates_whole_video_and_preserves_card_metadata(self):
        analyze=AsyncMock(return_value={'frames':[],'note':'全片覆盖0:00–10:00','segment_count':6})
        with patch.object(channels,'attachment_bytes',new=AsyncMock(return_value=(self.payload,'video.mp4',True,'card'))),patch.object(channels,'verify_attachment',new=AsyncMock(return_value=self.source)):
            result=await channels.read_video(self.event,self.source,analyze)
        analyze.assert_awaited_once_with(self.payload,'video.mp4','card')
        self.assertIn('全片覆盖',result['note'])
        self.assertIn('创作者',result['note'])

    async def test_channel_rechecks_source_after_full_analysis(self):
        with patch.object(channels,'attachment_bytes',new=AsyncMock(return_value=(self.payload,'video.mp4',True,'card'))),patch.object(channels,'verify_attachment',new=AsyncMock(side_effect=ValueError('已撤回'))):
            with self.assertRaisesRegex(ValueError,'撤回'):
                await channels.read_video(self.event,self.source,AsyncMock(return_value={'frames':[],'note':'全片'}))

    def test_native_card_metadata_without_url_is_readable(self):
        tree=ast.parse((ROOT/'plugins/astrbot_plugin_wechat_native/event.py').read_text(encoding='utf-8'))
        function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='link_card_text')
        scope={'urlsplit':urlsplit};exec(compile(ast.Module(body=[function],type_ignores=[]),'card-function','exec'),scope)
        text=scope['link_card_text'](self.source)
        self.assertIn('视频号卡片',text)
        self.assertIn('创作者',text)
        self.assertEqual(scope['link_card_text']({**self.source,'recall':{'state':'recalled'}}),'')

    def test_native_private_card_accepted_and_group_whitelist_preserved(self):
        from plugins.astrbot_plugin_wechat_native.peer_filter import blocked_private_peer
        tree=ast.parse((ROOT/'plugins/astrbot_plugin_wechat_native/adapter.py').read_text(encoding='utf-8'))
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and any(isinstance(f,ast.FunctionDef) and f.name=='_classify' for f in n.body))
        function=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='_classify')
        scope={'logger':Mock(),'RED_PACKET_APP_TYPES':set(),'quoted_sticker_candidate':lambda raw:False,'is_sticker':lambda raw:False,
            'is_pdf_filename':lambda name:False,'link_card_text':lambda raw:'[视频号卡片]' if raw.get('appMessageType')==51 else '',
            'is_summary_command':lambda text:False,'re':__import__('re'),'blocked_private_peer':blocked_private_peer}
        exec(compile(ast.Module(body=[function],type_ignores=[]),'classify-function','exec'),scope)
        cfg=types.SimpleNamespace(enable_media=True,enable_stickers=True,enable_private_reply=True,allowed_users=set(),group_reply_mode='mention',allowed_groups={'group@chatroom'})
        adapter=types.SimpleNamespace(bot_self_id='bot',native_config=cfg,_check_timestamp=lambda raw:True)
        fn=scope['_classify']
        self.assertEqual(fn(adapter,{**self.source,'threadId':'alice'},'live'),'text')
        self.assertEqual(fn(adapter,self.source,'live'),'ignore')
        self.assertEqual(fn(adapter,{**self.source,'mentions':{'state':'known','self':True}},'live'),'text')
        self.assertEqual(fn(adapter,{**self.source,'threadId':'other@chatroom','mentions':{'state':'known','self':True}},'live'),'ignore')

class RequestHookTests(unittest.IsolatedAsyncioTestCase):
    def load_hook(self,locate,read):
        tree=ast.parse((ROOT/'plugins/astrbot_plugin_personal_companion/main.py').read_text(encoding='utf-8'))
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='PersonalCompanion')
        fn=next(n for n in cls.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='prepare_channels')
        fn.decorator_list=[]
        class Part:
            def __init__(self,text):self.text=text
            def mark_as_temp(self):return self
        scope={'AstrMessageEvent':object,'ProviderRequest':object,'asyncio':asyncio,'TextPart':Part,
            'locate_channels':locate,'read_channels_video':read,'logger':Mock()}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'channels-hook','exec'),scope)
        return scope['prepare_channels']

    async def test_evidence_attached_to_main_request_without_second_model_call(self):
        extras={}
        event=types.SimpleNamespace(get_extra=extras.get,set_extra=extras.__setitem__,send=AsyncMock(),plain_result=lambda x:x,stop_event=Mock())
        req=types.SimpleNamespace(image_urls=[],extra_user_content_parts=[],system_prompt='角色')
        owner=types.SimpleNamespace(identity=lambda event:True,media_gate=asyncio.Semaphore(1),directory=Path('.'),transcribe=AsyncMock())
        hook=self.load_hook(AsyncMock(return_value={'id':'card'}),AsyncMock(return_value={'frames':['base64://frame'],'note':'前7秒'}))
        await hook(owner,event,req)
        self.assertEqual(req.image_urls,['base64://frame'])
        self.assertIn('前7秒',req.extra_user_content_parts[0].text)
        self.assertIn('发布者、转发者',req.system_prompt)
        self.assertTrue(extras['companion_channels_prepared'])
        event.send.assert_not_awaited()

    async def test_read_failure_sends_explicit_answer_and_stops(self):
        extras={}
        event=types.SimpleNamespace(get_extra=extras.get,set_extra=extras.__setitem__,send=AsyncMock(),plain_result=lambda x:x,stop_event=Mock())
        req=types.SimpleNamespace(image_urls=[],extra_user_content_parts=[],system_prompt='')
        owner=types.SimpleNamespace(identity=lambda event:True,media_gate=asyncio.Semaphore(1),directory=Path('.'),transcribe=AsyncMock())
        hook=self.load_hook(AsyncMock(return_value={'id':'card'}),AsyncMock(side_effect=ValueError('视频号已失效')))
        await hook(owner,event,req)
        self.assertIn('视频读取未完成',event.send.call_args.args[0])
        self.assertIn('视频号已失效',event.send.call_args.args[0])
        event.stop_event.assert_called_once()
        self.assertEqual(req.image_urls,[])

class FullVideoToolTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_video_timeout_is_per_request_not_shared_registry(self):
        tree=ast.parse((ROOT/'plugins/astrbot_plugin_personal_companion/main.py').read_text(encoding='utf-8'))
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='PersonalCompanion')
        fn=next(n for n in cls.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='allow_full_video_tool_time')
        fn.decorator_list=[]
        fake=types.ModuleType('astrbot.core.agent.tool')
        fake.ToolSet=lambda tools:types.SimpleNamespace(tools=tools)
        import copy
        scope={'AstrMessageEvent':object,'ProviderRequest':object,'copy':copy}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'video-tool-timeout','exec'),scope)
        original=types.SimpleNamespace(name='wechat_read_attachment',execution_timeout=None)
        other=types.SimpleNamespace(name='web_search',execution_timeout=20)
        req=types.SimpleNamespace(func_tool=types.SimpleNamespace(tools=[original,other]))
        with patch.dict(sys.modules,{'astrbot.core.agent.tool':fake}):
            await scope['allow_full_video_tool_time'](types.SimpleNamespace(identity=lambda e:True),object(),req)
        self.assertIsNone(original.execution_timeout)
        self.assertEqual(req.func_tool.tools[0].execution_timeout,3600)
        self.assertIs(req.func_tool.tools[1],other)
        self.assertEqual(other.execution_timeout,20)

class AttachmentSendGuardTests(unittest.IsolatedAsyncioTestCase):
    def guard(self,source):
        tree=ast.parse((ROOT/'plugins/astrbot_plugin_wechat_native/adapter.py').read_text(encoding='utf-8'))
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and any(isinstance(f,ast.AsyncFunctionDef) and f.name=='_check_quote_original' for f in n.body))
        fn=next(n for n in cls.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='_check_quote_original')
        class MediaError(Exception):pass
        scope={'WeChatNativeEvent':object,'MediaError':MediaError}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'send-quote-guard','exec'),scope)
        extras={'wechat_native_verified_attachment':{k:source.get(k) for k in ['id','accountId','threadId','serverId','senderId','type','appMessageType']}}
        event=types.SimpleNamespace(get_extra=extras.get,set_extra=extras.__setitem__)
        adapter=types.SimpleNamespace(client=types.SimpleNamespace(message=AsyncMock(return_value=copy.deepcopy(source))),
            native_config=types.SimpleNamespace(account_id='account'),state=types.SimpleNamespace(message_status=lambda identifier:'queued'))
        return scope['_check_quote_original'],adapter,event,extras,MediaError

    async def test_quoted_channel_is_not_checked_as_an_image(self):
        source=message()
        fn,adapter,event,extras,error=self.guard(source)
        await fn(adapter,message('q',quote={'serverId':source['serverId'],'senderId':'alice'}),event,source['id'])
        self.assertFalse(extras.get('wechat_native_quote_invalidated'))

    async def test_direct_private_channel_does_not_require_a_quote(self):
        source=message(threadId='alice')
        fn,adapter,event,extras,error=self.guard(source)
        await fn(adapter,source,event,source['id'])

    async def test_recalled_source_still_blocks_send(self):
        source=message()
        fn,adapter,event,extras,error=self.guard(source)
        adapter.client.message.return_value['recall']={'state':'recalled'}
        with self.assertRaises(error):await fn(adapter,message('q',quote={'serverId':source['serverId'],'senderId':'alice'}),event,source['id'])
        self.assertTrue(extras['wechat_native_quote_invalidated'])

    async def test_cross_account_or_wrong_quote_never_passes(self):
        source=message()
        fn,adapter,event,extras,error=self.guard(source)
        adapter.client.message.return_value['accountId']='other'
        with self.assertRaises(error):await fn(adapter,source,event,source['id'])
        adapter.client.message.return_value=source
        with self.assertRaises(error):await fn(adapter,message('q',quote={'serverId':'different','senderId':'alice'}),event,source['id'])

if __name__=='__main__':unittest.main()

"""Offline platform URL, selection, source guard and request integration checks."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
import tempfile
import urllib.request
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from plugins.astrbot_plugin_personal_companion import platform_video as p
from plugins.astrbot_plugin_personal_companion.platform_video_worker import canonical_url,check_url,PlatformVideoError,make_downloader,Quiet

BILI='https://www.bilibili.com/video/BV1Aa1111111/'
XHS='https://www.xiaohongshu.com/discovery/item/000000000000000000000001?xsec_token=fixture&xsec_source=pc_share'
NOW=datetime.now(timezone.utc)

def message(identifier='current',**changes):
    return {'id':identifier,'accountId':'account','threadId':'group@chatroom','serverId':'server-'+identifier,'senderId':'alice','type':1,'appMessageType':None,'direction':'incoming','recall':None,'createdAt':NOW.isoformat(),'body':'总结这个视频',**changes}

class Event:
    def __init__(self,raw,source=None):
        self.message_obj=SimpleNamespace(raw_message=raw);self.message_str=raw.get('body') or '';self.extras={}
        async def fetch(identifier):return source if source is not None and identifier==source['id'] else raw
        self.adapter=SimpleNamespace(stopped=False,client=SimpleNamespace(message=AsyncMock(side_effect=fetch)))
    def is_stopped(self):return False
    def get_extra(self,k):return self.extras.get(k)
    def set_extra(self,k,v):self.extras[k]=v

class UrlTests(unittest.TestCase):
    def test_bilibili_language_prefix_and_page_are_normalized(self):
        self.assertEqual(canonical_url('https://www.bilibili.com/zh-Hans/video/BV1Aa1111111/?p=2&track=fixture'),BILI+'?p=2')
        self.assertEqual(canonical_url(BILI),BILI+'?p=1')
        self.assertIsNone(canonical_url('https://b23.tv/fixture'))

    def test_xhs_signature_is_preserved_and_short_urls_are_supported(self):
        value=canonical_url(XHS)
        self.assertIn('/explore/000000000000000000000001?',value)
        self.assertIn('xsec_token=fixture',value)
        self.assertIsNone(canonical_url('http://xhslink.com/o/fixture'))
        self.assertIsNone(canonical_url('https://xhslink.cn/fixture'))

    def test_only_supported_sites_and_web_protocols(self):
        for url in ['file:///D:/secret','http://127.0.0.1/video','https://bilibili.com.evil.test/video/x','https://user:pass@www.bilibili.com/video/BV1Aa1111111/','https://www.bilibili.com:8181/video/BV1Aa1111111/','https://www.bilibili.com/\\evil/video/x']:
            with self.assertRaises(PlatformVideoError):check_url(url,share=True)
        for url in ['https://www.bilibili.com/list/123','https://www.xiaohongshu.com/user/profile/123',BILI+'?p=0']:
            with self.assertRaises(PlatformVideoError):canonical_url(url)
        check_url('https://upos-sz-mirrorcos.bilivideo.com/media.mp4')
        check_url('https://sns-video-bd.xhscdn.com/media.mp4')
        with self.assertRaises(PlatformVideoError):check_url('https://cdn.evil.test/file.mp4')

    def test_network_redirect_is_guarded_before_request(self):
        with make_downloader({'quiet':True,'logger':Quiet(),'cachedir':False}) as ydl:
            self.assertEqual(len(ydl._request_director.handlers),1)
            handler=next(iter(ydl._request_director.handlers.values()))
            opener=handler._create_instance(proxies={},cookiejar=ydl.cookiejar)
            redirect=next(h for h in opener.handlers if type(h).__name__=='CheckedRedirect')
            with self.assertRaises(PlatformVideoError):redirect.redirect_request(urllib.request.Request('https://b23.tv/fixture'),None,302,'',{},'http://127.0.0.1/private')

class SelectionTests(unittest.TestCase):
    def test_plain_and_card_shares_and_decline(self):
        raw=message(body='总结 '+BILI)
        self.assertEqual(p.select_source(raw,raw['body'],[]),(raw,BILI))
        raw=message(type=49,appMessageType=5,link={'url':XHS},body='小红书分享')
        self.assertEqual(p.select_source(raw,'',[]),(raw,XHS))
        self.assertIsNone(p.select_source(raw,'不要分析这个视频',[]))

    def test_exact_quote_can_read_other_members_but_never_guess_them(self):
        source=message('shared',senderId='bob',type=49,appMessageType=5,link={'url':XHS})
        raw=message(quote={'serverId':source['serverId'],'senderId':'bob','state':'unsupported','type':49})
        self.assertEqual(p.select_source(raw,'总结',[source]),(source,XHS))
        self.assertIsNone(p.select_source(message(),'总结',[source]))
        self.assertIsNone(p.select_source(raw,'总结',[{**source,'threadId':'other@chatroom'}]))
        self.assertIsNone(p.select_source(raw,'总结',[{**source,'accountId':'other'}]))

    def test_recent_share_is_own_and_newer_attachment_blocks_old_link(self):
        source=message('shared',body=BILI,createdAt=(NOW-timedelta(seconds=20)).isoformat())
        self.assertEqual(p.select_source(message(),'总结',[source]),(source,BILI))
        newer=message('newer-video',type=43,createdAt=(NOW-timedelta(seconds=10)).isoformat())
        self.assertIsNone(p.select_source(message(),'总结',[source,newer]))
        self.assertIsNone(p.select_source(message(),'1',[source]))
        self.assertIsNone(p.select_source(message(),'分析今天的AUBL赛况',[source]))
        self.assertIsNone(p.select_source(message(),'分析这道数学题',[source]))
        source['createdAt']=(NOW-timedelta(seconds=601)).isoformat()
        self.assertIsNone(p.select_source(message(),'总结',[source]))

    def test_multiple_platform_links_need_specific_target(self):
        with self.assertRaises(ValueError):p.select_source(message(body=BILI+' '+XHS),'总结',[])

class GuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source=message('shared',type=49,appMessageType=5,link={'url':XHS})
        self.raw=message(quote={'serverId':self.source['serverId'],'senderId':'alice','type':49,'state':'unsupported'})
        self.event=Event(self.raw,self.source);self.snapshot={k:self.source.get(k) for k in p.IDENTITY}

    async def test_source_and_trigger_are_revalidated(self):
        value=await p.verify_source(self.event,self.snapshot,XHS)
        self.assertEqual(value,self.source)
        self.source['recall']={}
        with self.assertRaises(ValueError):await p.verify_source(self.event,self.snapshot,XHS)

    async def test_changed_url_or_sender_or_quote_is_rejected(self):
        self.source['link']={'url':BILI}
        with self.assertRaises(ValueError):await p.verify_source(self.event,self.snapshot,XHS)
        self.source['link']={'url':XHS};self.source['senderId']='bob'
        with self.assertRaises(ValueError):await p.verify_source(self.event,self.snapshot,XHS)
        self.source['senderId']='alice';self.raw['quote']['senderId']='bob'
        with self.assertRaises(ValueError):await p.verify_source(self.event,self.snapshot,XHS)

    async def test_prefetch_uses_full_video_analyzer_and_appends_evidence(self):
        request=SimpleNamespace(image_urls=[],extra_user_content_parts=[],system_prompt='角色')
        metadata={'platform':'xiaohongshu','title':'测试','uploader':'作者'}
        evidence={'frames':['frame-fixture'],'note':'0:00-1:31 全片资料'}
        analyze=AsyncMock(return_value=evidence)
        with tempfile.TemporaryDirectory() as directory,patch.object(p,'download_video',AsyncMock(return_value=(b'video','media.mp4',metadata))):
            await p.prefetch(self.event,request,(self.source,XHS),directory,analyze)
        self.assertEqual(request.image_urls,['frame-fixture'])
        self.assertTrue(self.event.get_extra('companion_platform_video_handled'))
        self.assertTrue(self.event.get_extra('companion_channels_handled'))
        self.assertTrue(callable(self.event.get_extra('wechat_native_platform_video_guard')))
        self.assertIn('全片资料',request.extra_user_content_parts[0].text)
        self.assertEqual(analyze.call_args.args[2],self.source['id'])
        await analyze.call_args.args[3]()

    async def test_failed_download_never_generates_fake_video_evidence(self):
        request=SimpleNamespace(image_urls=[],extra_user_content_parts=[],system_prompt='角色');analyze=AsyncMock()
        with tempfile.TemporaryDirectory() as directory,patch.object(p,'download_video',AsyncMock(side_effect=ValueError('访问受限'))):
            with self.assertRaises(ValueError):await p.prefetch(self.event,request,(self.source,XHS),directory,analyze)
        analyze.assert_not_called();self.assertEqual(request.extra_user_content_parts,[])

if __name__=='__main__':unittest.main()

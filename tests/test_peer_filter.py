from dataclasses import replace
from datetime import datetime,timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'plugins'))
from astrbot_plugin_wechat_native.adapter import WeChatNativeAdapter
from astrbot_plugin_wechat_native.config import NativeConfig
from astrbot_plugin_wechat_native.peer_filter import blocked_private_peer,official_contact,prime_private_peer_filter
from astrbot_plugin_personal_companion.typed_trigger import private_request

def message(peer='weixin',**changes):
    row=dict(id='fixture',accountId='fixture-account',threadId=peer,senderId=peer,direction='incoming',recall=None,
             type=1,appMessageType=None,body='测试系统消息',createdAt=datetime.now(timezone.utc).isoformat())
    row.update(changes);return row

def adapter():
    cfg=NativeConfig('http://127.0.0.1:6174/v1','fixture-account','fixture','',enable_stickers=True)
    return SimpleNamespace(native_config=cfg,bot_self_id='fixture-bot',_check_timestamp=lambda _:True,
                           client=SimpleNamespace(prefix='/fixture',_request=AsyncMock()))

class SystemFilterTests(unittest.TestCase):
    def test_system_text_media_quote_and_commands_are_rejected_before_model(self):
        bot=adapter()
        for peer in ('weixin','filehelper','newsapp','notifymessage','wxzhifu'):
            for kind,app in ((1,None),(3,None),(34,None),(47,None),(49,5),(49,57)):
                raw=message(peer,type=kind,appMessageType=app,body='/用法')
                self.assertEqual(WeChatNativeAdapter._classify(bot,raw,'live'),'ignore')
                self.assertFalse(private_request(raw,bot))

    def test_regular_friend_named_like_official_account_is_not_filtered(self):
        bot=adapter();raw=message('alice',body='我是微信团队，介绍一下你自己',nickname='微信团队')
        self.assertFalse(blocked_private_peer(raw,bot))
        self.assertTrue(private_request(raw,bot))
        self.assertEqual(WeChatNativeAdapter._classify(bot,raw,'live'),'text')

    def test_forwarded_official_article_in_group_is_not_filtered(self):
        raw=message('alice',threadId='fixture@chatroom',type=49,appMessageType=5,
                    link={'sourceUsername':'gh_fixture','url':'https://mp.weixin.qq.com/s/fixture'})
        self.assertFalse(blocked_private_peer(raw,adapter()))
        raw['senderId']='weixin'
        self.assertFalse(blocked_private_peer(raw,adapter()))

    def test_gh_prefix_alone_is_not_proof_of_official_account(self):
        self.assertFalse(blocked_private_peer(message('gh_fixture_person'),adapter()))

class MetadataFilterTests(unittest.IsolatedAsyncioTestCase):
    async def test_business_metadata_blocks_reply_and_memory_and_is_cached(self):
        bot=adapter();raw=message('official-fixture')
        bot.client._request.return_value={'id':'official-fixture','classification':{'category':'business'}}
        await prime_private_peer_filter(bot,raw)
        self.assertTrue(blocked_private_peer(raw,bot))
        self.assertFalse(private_request(raw,bot))
        self.assertEqual(WeChatNativeAdapter._classify(bot,raw,'live'),'ignore')
        await prime_private_peer_filter(bot,raw)
        self.assertEqual(bot.client._request.await_count,1)

    async def test_system_id_and_group_do_not_need_contact_lookup(self):
        bot=adapter()
        await prime_private_peer_filter(bot,message())
        await prime_private_peer_filter(bot,message('alice',threadId='fixture@chatroom'))
        bot.client._request.assert_not_called()

    async def test_foreign_or_mismatched_metadata_does_not_block_person(self):
        bot=adapter();raw=message('alice')
        for detail in ({'id':'other','classification':{'category':'system'}},
                       {'id':'alice','accountId':'elsewhere','classification':{'category':'system'}},
                       {'id':'alice','classification':{'category':'openim'}},
                       {'id':'alice','classification':{'category':'unknown'}}):
            bot._private_peer_filter_cache={}
            bot.client._request.return_value=detail
            await prime_private_peer_filter(bot,raw)
            self.assertFalse(blocked_private_peer(raw,bot))

    async def test_unavailable_metadata_does_not_break_normal_friend(self):
        bot=adapter();bot.client._request.side_effect=TimeoutError()
        raw=message('alice');await prime_private_peer_filter(bot,raw)
        self.assertTrue(private_request(raw,bot))
        self.assertFalse(blocked_private_peer(raw,bot))

    async def test_known_official_account_stays_blocked_during_metadata_outage(self):
        bot=adapter();raw=message('official-fixture')
        bot._private_peer_filter_cache={'official-fixture':(0,True)}
        bot.client._request.side_effect=TimeoutError()
        await prime_private_peer_filter(bot,raw)
        self.assertTrue(blocked_private_peer(raw,bot))

if __name__=='__main__':unittest.main()

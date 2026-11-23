"""Synthetic same-group cards; no private chats, network, or WeChat sends."""
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'plugins'))
from astrbot_plugin_personal_companion.article_context import select_article, resolve_article, verify_article
from astrbot_plugin_personal_companion.search import SearchSupport, REQUEST, link_request

NOW=datetime(2026,11,23,12,tzinfo=timezone.utc)
URL='https://mp.weixin.qq.com/s/fixture-article'

def row(identifier,age=1,**changes):
    return dict(id=identifier,accountId='fixture-account',threadId='fixture@chatroom',senderId='alice',
                createdAt=(NOW-timedelta(seconds=age)).isoformat(),direction='incoming',recall=None,
                type=1,appMessageType=None,body='',serverId='server-'+identifier,**changes)

def card(identifier='article',age=1,title='示例芯片公司：发布新产品',url=URL):
    value=row(identifier,age);value.update(type=49,appMessageType=5,link={'title':title,'url':url})
    return value

class SelectionTests(unittest.TestCase):
    def setUp(self):self.raw=row('trigger',0)

    def test_named_comment_and_paper_question_both_select_recent_article(self):
        source=card()
        choice,status=select_article(self.raw,'锐评示例芯片公司',[self.raw,source])
        self.assertEqual(choice['url'],URL);self.assertEqual(status,'recent_self')
        choice,_=select_article(self.raw,'这篇论文研究问题是什么',[self.raw,source])
        self.assertEqual(choice['url'],URL)

    def test_same_second_transport_order_not_random_id_order(self):
        self.raw['createdAt']=card(age=0)['createdAt']
        before=card('zz-before',age=0)
        after=card('aa-after',age=0,url='https://mp.weixin.qq.com/s/future')
        choice,_=select_article(self.raw,'总结一下',[after,self.raw,before])
        self.assertEqual(choice['url'],URL)

    def test_unique_article_can_be_discussed_by_another_member(self):
        source=card();source['senderId']='bob'
        choice,status=select_article(self.raw,'这篇文章的结论是什么',[self.raw,source])
        self.assertEqual(choice['url'],URL);self.assertEqual(status,'recent_group')

    def test_cross_scope_recall_old_and_outgoing_cannot_be_selected(self):
        for field,value in [('accountId','other'),('threadId','elsewhere@chatroom'),('recall',{}),('direction','outgoing')]:
            source=card();source[field]=value
            self.assertIsNone(select_article(self.raw,'总结一下',[self.raw,source])[0])
        self.assertIsNone(select_article(self.raw,'总结一下',[self.raw,card(age=601)])[0])

    def test_quote_never_falls_back_to_an_unrelated_article(self):
        self.raw['quote']={'type':3,'state':'embedded'}
        self.assertIsNone(select_article(self.raw,'评价一下',[self.raw,card()])[0])

    def test_unrelated_questions_and_negative_requests_do_not_fetch(self):
        for text in ('你是谁','我的昵称是什么','锐评今天的足球比赛','不要读这篇文章','这个表情是什么意思'):
            self.assertIsNone(select_article(self.raw,text,[self.raw,card()])[0],text)

    def test_multiple_shared_articles_need_clear_target(self):
        one=card('one',title='测试甲文章');one['senderId']='bob'
        two=card('two',age=2,title='测试乙文章',url='https://mp.weixin.qq.com/s/fixture-second');two['senderId']='carol'
        choice,status=select_article(self.raw,'这篇文章说了什么',[self.raw,one,two])
        self.assertIsNone(choice);self.assertEqual(status,'ambiguous')

class PrefetchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source=card();self.raw=row('trigger',0);self.raw['body']='@大肥鱼 锐评示例芯片公司'
        self.extras={'companion_group_native_messages':[self.raw,self.source]}
        self.event=SimpleNamespace(message_obj=SimpleNamespace(raw_message=self.raw),get_extra=self.extras.get,
            set_extra=self.extras.__setitem__,adapter=SimpleNamespace(client=SimpleNamespace(message=AsyncMock(return_value=self.source))))
        self.request=SimpleNamespace(contexts=[{'role':'user','content':'旧链接 https://mp.weixin.qq.com/s/stale'}],
            image_urls=[],extra_user_content_parts=[],system_prompt='角色')
        self.extras[REQUEST]=self.request
        self.support=SearchSupport(SimpleNamespace())
        async def execute(event,original,runtime,args):
            self.support.state(event)['evidence'].append('已实际读取的测试正文')
            return '已实际读取的测试正文'
        self.support.execute=AsyncMock(side_effect=execute)

    async def test_prefetch_prefers_new_card_to_stale_model_context(self):
        self.assertTrue(await self.support.prefetch_link(self.event,self.request))
        self.assertEqual(self.support.execute.call_args.args[-1]['url'],URL)
        self.assertEqual(self.event.adapter.client.message.await_count,2)
        self.assertIn('测试正文',self.request.extra_user_content_parts[0].text)

    async def test_changed_source_does_not_use_page_evidence(self):
        changed=dict(self.source,recall={})
        self.event.adapter.client.message=AsyncMock(side_effect=[self.source,changed])
        self.assertFalse(await self.support.prefetch_link(self.event,self.request))
        self.assertEqual(self.support.state(self.event)['error'],'source_changed')
        self.assertEqual(self.support.state(self.event)['evidence'],[])
        self.assertEqual(self.request.extra_user_content_parts,[])

    async def test_unrelated_text_does_not_read_native_history(self):
        self.extras.pop('companion_group_native_messages')
        self.event.adapter.client.messages=AsyncMock()
        self.assertIsNone(await resolve_article(self.event,'晚上好'))
        self.event.adapter.client.messages.assert_not_called()

    def test_comment_intent_is_recognized_for_explicit_links_too(self):
        self.assertTrue(link_request('锐评一下 '+URL,[URL]))

if __name__=='__main__':unittest.main()

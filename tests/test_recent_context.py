"""Offline context paging, isolation, reset and usage regressions."""
import copy
from datetime import datetime,timedelta,timezone
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'plugins'))
from astrbot_plugin_personal_companion.recent_context import recent_messages,bounded_rows
from astrbot_plugin_personal_companion.group_context import add_group_context
from astrbot_plugin_personal_companion.personal_history import seed_own_history
from astrbot_plugin_personal_companion.usage import USAGE

NOW=datetime(2026,11,22,12,tzinfo=timezone.utc)

def row(i=1,thread='alice',sender='alice',age=1,**changes):
    value=dict(id='m'+str(i),accountId='account',threadId=thread,senderId=sender,type=1,body='普通内容'+str(i),direction='outgoing' if sender=='bot' else 'incoming',createdAt=(NOW-timedelta(seconds=age)).isoformat(),recall=None)
    return {**value,**changes}

class Event:
    def __init__(self,items,group=False):
        self.extras={};thread='group@chatroom' if group else 'alice'
        self.message_obj=SimpleNamespace(raw_message=row('current',thread=thread,age=0))
        config=SimpleNamespace(account_id='account',enable_private_reply=True,allowed_users=(),allowed_groups=('group@chatroom',),group_reply_mode='mention')
        async def messages(thread,limit,cursor=''):
            start=int(cursor or 0);page=items[start:start+limit]
            return {'items':page,'nextCursor':str(start+limit) if start+limit<len(items) else None}
        self.adapter=SimpleNamespace(client=SimpleNamespace(messages=AsyncMock(side_effect=messages)),bot_self_id='bot',native_config=config)
    def get_extra(self,key):return self.extras.get(key)
    def set_extra(self,key,value):self.extras[key]=value

class Memory:
    def __init__(self,cutoff=0,enabled=True):
        self.cutoff=cutoff;self.enabled=enabled
        self.db=sqlite3.connect(':memory:');self.db.execute('CREATE TABLE privacy_resets(account TEXT,user TEXT,forgot_at REAL)')
    def reset_time(self,account,user):return self.cutoff if user=='alice' else 0
    def get(self,*args):return {'enabled':self.enabled}

class RecentContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.memory=Memory();self.addCleanup(self.memory.db.close)
        self.names=SimpleNamespace(member=AsyncMock(return_value={'display':'测试昵称'}))

    async def test_group_uses_multiple_pages_and_older_than_two_hours(self):
        items=[row(i,thread='group@chatroom',age=i*100) for i in range(1,251)]
        event=Event(items,group=True);request=SimpleNamespace(contexts=[],extra_user_content_parts=[])
        await add_group_context(event,request,self.memory,self.names)
        self.assertEqual(event.get_extra('companion_group_context_count'),200)
        self.assertGreaterEqual(event.adapter.client.messages.await_count,3)
        text=request.extra_user_content_parts[0].text
        self.assertIn('普通内容200',text)
        self.assertNotIn('普通内容201"',text)
        self.assertIn('sender_id',text)

    async def test_group_rejects_foreign_threads_old_recalled_and_forgotten(self):
        items=[row('good',thread='group@chatroom',age=100),row('foreign',thread='private',body='OTHER_CHAT'),row('account',thread='group@chatroom',accountId='other',body='OTHER_ACCOUNT'),row('old',thread='group@chatroom',age=86401,body='TOO_OLD'),row('recall',thread='group@chatroom',recall={},body='RECALLED'),row('forgot',thread='group@chatroom',age=1000,body='FORGOTTEN')]
        self.memory.cutoff=(NOW-timedelta(seconds=500)).timestamp()
        event=Event(items,group=True);request=SimpleNamespace(contexts=[],extra_user_content_parts=[])
        await add_group_context(event,request,self.memory,self.names)
        text=request.extra_user_content_parts[0].text
        self.assertNotIn('OTHER_',text);self.assertNotIn('TOO_OLD',text);self.assertNotIn('RECALLED',text);self.assertNotIn('FORGOTTEN',text)

    async def test_private_history_enriches_existing_context_with_both_sides(self):
        items=[row('yesterday',age=86400,body='昨天计划去海边'),row('answer',sender='bot',age=86390,body='机器人建议上午出发'),row('foreign',thread='bob',sender='bob',body='OTHER_FRIEND'),row('spoof',sender='bob',body='SPOOFED_SENDER'),row('old',age=7*86400+1,body='OLDER_THAN_WEEK'),row('current',age=0,body='CURRENT_DUPLICATE')]
        event=Event(items);request=SimpleNamespace(contexts=[{'role':'user','content':'已有会话'}],extra_user_content_parts=[])
        before=copy.deepcopy(request.contexts)
        await seed_own_history(event,request,self.memory)
        self.assertEqual(request.contexts,before)
        self.assertEqual(event.get_extra('companion_private_context_count'),2)
        text=request.extra_user_content_parts[0].text
        self.assertIn('昨天计划去海边',text);self.assertIn('机器人建议上午出发',text)
        self.assertIn('用户',text);self.assertIn('机器人',text)
        for excluded in ['OTHER_FRIEND','SPOOFED_SENDER','OLDER_THAN_WEEK','CURRENT_DUPLICATE']:self.assertNotIn(excluded,text)

    async def test_private_reset_removes_both_sides_and_disabled_memory_does_not_reseed(self):
        self.memory.cutoff=(NOW-timedelta(seconds=50)).timestamp()
        items=[row('olduser',age=100,body='OLD_USER'),row('oldbot',sender='bot',age=99,body='OLD_BOT'),row('new',age=10,body='NEW_TEXT')]
        event=Event(items);request=SimpleNamespace(contexts=[],extra_user_content_parts=[])
        await seed_own_history(event,request,self.memory)
        self.assertEqual(event.get_extra('companion_private_context_count'),1)
        self.assertNotIn('OLD_',request.extra_user_content_parts[0].text)
        self.memory.enabled=False;event=Event(items);request=SimpleNamespace(contexts=[],extra_user_content_parts=[])
        await seed_own_history(event,request,self.memory)
        self.assertEqual(request.extra_user_content_parts,[])
        event.adapter.client.messages.assert_not_called()

    async def test_private_cap_and_commands_exclusion(self):
        items=[row(i,age=i) for i in range(1,151)]+[row('guide',body='大肥鱼使用指南\nprivate'),row('command',body='/我的记忆'),row('recalled',recall={})]
        event=Event(items);request=SimpleNamespace(contexts=[],extra_user_content_parts=[])
        await seed_own_history(event,request,self.memory)
        self.assertEqual(event.get_extra('companion_private_context_count'),100)
        text=request.extra_user_content_parts[0].text
        self.assertNotIn('普通内容101"',text);self.assertNotIn('private',text);self.assertNotIn('/我的记忆',text)

    async def test_paging_cache_and_duplicate_cursor_are_bounded(self):
        event=Event([]);event.adapter.client.messages=AsyncMock(return_value={'items':[row(1)],'nextCursor':'repeat'})
        items=await recent_messages(event,window_seconds=86400,max_items=400)
        self.assertEqual(len(items),1);self.assertEqual(event.adapter.client.messages.await_count,2)
        await recent_messages(event,window_seconds=86400,max_items=400)
        self.assertEqual(event.adapter.client.messages.await_count,2)

    async def test_second_page_failure_preserves_available_context(self):
        event=Event([]);event.adapter.client.messages=AsyncMock(side_effect=[{'items':[row(1)],'nextCursor':'next'},TimeoutError()])
        items=await recent_messages(event,window_seconds=86400,max_items=400)
        self.assertEqual(len(items),1)

    def test_budget_keeps_latest_and_copies_input(self):
        rows=[{'id':str(i),'stamp':i,'text':'x'*8} for i in range(5)]
        before=copy.deepcopy(rows)
        chosen=bounded_rows(rows,4,18)
        self.assertEqual(sum(len(r['text']) for r in chosen),18)
        self.assertEqual(chosen[-1]['id'],'4')
        self.assertEqual(rows,before)

    def test_usage_has_actual_examples_and_limits(self):
        for text in ['24小时','200条','7天','100条','100轮','群聊例子','私聊例子','看图例子','表情例子','读文件例子','视频号例子','搜索例子','文章例子','强制语音例子','不同好友']:
            self.assertIn(text,USAGE)
        self.assertNotIn('6.',USAGE)
        self.assertLess(len(USAGE.encode()),16000)

if __name__=='__main__':unittest.main()

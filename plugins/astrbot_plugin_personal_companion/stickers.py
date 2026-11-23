"""Fulfill explicit sticker requests through Native's existing journal and receipts."""
import asyncio
import importlib
import re

from astrbot.api import logger
from astrbot.core.agent.message import TextPart
from .typed_trigger import strip_text_at


def sticker_request(text):
    text=strip_text_at(re.sub(r'^(?:@[^\u2005\r\n]+\u2005[ \t]*)+','',str(text or ''))).strip()
    if re.search(r'别|不要|不用|停止|怎么|如何|为什么|不能|不会',text):return None
    match=re.fullmatch(r'(?:请|麻烦|帮我|帮忙|给我|给大家|你)?\s*(?:发|来|整|丢|甩|回|送)(?:送)?'
        r'(?:一个|个|一张|张|一下)?\s*(.{0,12}?)(?:微信)?(?:表情包|表情|贴纸)'
        r'(?:给我|给大家|看看|一下|吧|呗|呀|啊|嘛|谢谢|[。！!，,？?\s])*',text)
    if not match:return None
    query=match.group(1).strip().strip('的')
    if query in {'','任意','随机','随便','随便一个','你喜欢','合适'}:query=''
    return query


def pending_sticker(text):
    text=str(text or '')
    return len(text)<220 and not re.search(r'不能|无法|没找到|没有可用|发送失败',text) and bool(
        re.search(r'(?:稍等|等一下|去|这就|马上|翻|找|挑|拿|准备).{0,28}(?:表情|贴纸)',text))


class StickerSupport:
    async def fulfill(self,event,query=''):
        previous=event.get_extra('companion_sticker_outcome')
        if previous is not None:return previous
        adapter=event.adapter
        tracker=importlib.import_module(type(adapter).__module__).track_send_task
        answer=None;status='failed'
        try:
            async with tracker(adapter):
                choices=await asyncio.wait_for(adapter.stickers.find(event,query=query,limit=12),timeout=12)
                candidates=choices.get('items') or []
                if not candidates:
                    answer=('本地表情库暂时没有与“'+query+'”匹配的可发送表情。可以让我发一个任意表情。'
                            if query else '本地表情库暂时没有可发送的表情，请在控制台同步表情库后再试。')
                    status='empty'
                else:
                    # Generic requests explicitly allow any ready candidate. Named requests
                    # are already filtered by Native's verified captions, never by opaque ID.
                    result=await asyncio.wait_for(adapter.stickers.send(event,candidates[0]['selection_id']),timeout=45)
                    status=result.get('status','failed')
                    if status not in {'success','pending','needs_review'}:
                        answer=result.get('message') or '这次表情没有发送成功，请稍后再试。'
        except TimeoutError:
            operation=adapter.state.sticker_operation(adapter.native_config.account_id,
                event.message_obj.raw_message['threadId'],event.message_obj.message_id,'send')
            if operation and operation['state'] in {'pending','submitting','needs_review','success'}:
                status='needs_review'
                event.set_extra('wechat_native_sticker_suppress_followup',True)
            else:answer='这次读取表情库超时了，没有发送表情，请稍后再试。'
        except Exception as exc:
            logger.warning('Companion sticker fulfillment failed: %s',type(exc).__name__)
            answer='这次表情没有发送成功，请稍后再试。'
        outcome={'status':status,'answer':answer}
        event.set_extra('companion_sticker_outcome',outcome)
        logger.info('Companion explicit sticker request settled: %s',status)
        return outcome

    async def finish(self,event,run_context,response):
        if response.tools_call_name or response.role=='err':return False
        query=sticker_request(event.message_str)
        if query is None or not pending_sticker(response.completion_text):return False
        outcome=await self.fulfill(event,query)
        answer=outcome['answer'] or ('已发送表情。' if outcome['status']=='success' else '微信尚未确认表情发送结果，未重复提交。')
        response.completion_text=answer
        if run_context.messages:
            last=run_context.messages[-1]
            if getattr(last,'role',None)=='assistant' and not getattr(last,'tool_calls',None):
                last.content=[TextPart(text=answer)]
        return True

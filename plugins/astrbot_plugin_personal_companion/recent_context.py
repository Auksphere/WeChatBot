"""Bounded Native history paging, scoped to the current account and thread."""
import asyncio
import re
from datetime import datetime


def timestamp(value):
    try:
        date=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return date.timestamp() if date.tzinfo is not None else None
    except (TypeError,ValueError):return None


async def recent_messages(event,*,window_seconds,max_items):
    raw=event.message_obj.raw_message
    account,thread=raw.get('accountId'),raw.get('threadId')
    current=timestamp(raw.get('createdAt'))
    client=getattr(event.adapter,'client',None)
    if not account or not thread or current is None or not hasattr(client,'messages'):return []
    cached=event.get_extra('companion_native_context_cache')
    key=(account,thread,raw.get('id'),window_seconds,max_items)
    if cached and cached.get('key')==key:return cached['items']
    rows,seen_ids,seen_cursors=[],set(),set()
    cursor=''
    async def fetch():
        nonlocal cursor
        for _ in range(6):
            try:
                page=await client.messages(thread,limit=min(100,max_items-len(rows)),**({'cursor':cursor} if cursor else {}))
            except Exception:return
            if not isinstance(page,dict) or not isinstance(page.get('items'),list):return
            page_times=[]
            for item in page['items']:
                if not isinstance(item,dict):continue
                when=timestamp(item.get('createdAt'))
                if item.get('accountId')!=account or item.get('threadId')!=thread or when is None:continue
                page_times.append(when)
                identifier=item.get('id')
                if (not isinstance(identifier,str) or not identifier or identifier in seen_ids
                        or when>current or when<current-window_seconds):continue
                seen_ids.add(identifier);rows.append(item)
                if len(rows)>=max_items:return
            next_cursor=page.get('nextCursor')
            if not isinstance(next_cursor,str) or not next_cursor or next_cursor in seen_cursors:return
            # Native pages are newest first. Stop once the entire page is older than the window.
            if page_times and max(page_times)<current-window_seconds:return
            seen_cursors.add(next_cursor);cursor=next_cursor
    try:await asyncio.wait_for(fetch(),timeout=4)
    except TimeoutError:pass
    event.set_extra('companion_native_context_cache',{'key':key,'items':rows})
    return rows


def text_body(item,max_text):
    text=item.get('body') or ''
    if not isinstance(text,str):return ''
    if item.get('direction')=='outgoing' and text.startswith(('这条回复生成异常','这次模型返回了无效内容')):return ''
    if text.startswith(('大肥鱼使用指南','跨群记忆：','跨群记忆已关闭','已清除你的跨群个人档案','已开启你的跨群记忆')):return ''
    if re.match(r'^(?:@[^\u2005\r\n]+\u2005\s*)?[／/]\S+',text):return ''
    if item.get('type')==1:pass
    elif item.get('type')==49 and item.get('appMessageType')==57:
        quote=item.get('quote') or {}
        if quote.get('state')=='embedded' and isinstance(quote.get('embeddedText'),str):
            text+='\n引用自 '+str(quote.get('senderId') or '未知成员')+'：'+quote['embeddedText'][:320]
    elif item.get('type')==49 and item.get('appMessageType') in {3,4,5,51}:
        link=item.get('link') or {}
        label='视频号卡片' if item.get('appMessageType')==51 else '链接卡片'
        text='['+label+'] '+str(link.get('title') or '')+' '+str(link.get('url') or '')
    else:return ''
    text=re.sub(r'(?:sk-|Bearer\s+)[A-Za-z0-9_-]{12,}','[密钥已隐藏]',text)
    # Assistant protocol markup is not useful historical discussion.
    if item.get('direction')=='outgoing':text=re.split(r'<\s*/?\s*[｜|]{1,2}\s*DSML\s*[｜|]{1,2}',text,maxsplit=1)[0]
    return text.strip()[:max_text]


def bounded_rows(rows,max_messages,max_total):
    rows.sort(key=lambda x:(x['stamp'],x['id']))
    total=0;chosen=[]
    for row in reversed(rows[-max_messages:]):
        remaining=max_total-total
        if remaining<=0:break
        row=dict(row);row['text']=row['text'][:remaining]
        chosen.append(row);total+=len(row['text'])
    return list(reversed(chosen))

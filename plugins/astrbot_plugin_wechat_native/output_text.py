"""Keep internal model tool markup out of user-visible messages; never execute it."""
import copy
import re

TOOL_MARKER=re.compile(r'<\s*/?\s*[｜|]{1,2}\s*DSML\s*[｜|]{1,2}\s*(?:calls|invoke|parameter)\b',re.I)


def has_tool_markup(text):
    return isinstance(text,str) and TOOL_MARKER.search(text) is not None


def visible_text(text,fallback='这条回复生成异常，请再说一次，我重新回答。'):
    if not isinstance(text,str):return text
    found=TOOL_MARKER.search(text)
    if found is None:return text
    # Truncated/malformed blocks are also rejected. Their arguments never become prose.
    return text[:found.start()].rstrip('` \t\r\n') or fallback


def reply_fallback(question):
    return ('可以发表情包，也会按聊天内容挑合适的表情。你可以直接说“发一个表情包”。'
            if re.search(r'表情|贴纸',str(question or '')) else '这条回复生成异常，请再说一次，我重新回答。')


def clean_contexts(contexts):
    cleaned=copy.deepcopy(contexts or [])
    for item in cleaned:
        if not isinstance(item,dict) or item.get('role')!='assistant':continue
        content=item.get('content')
        invalid=False
        if isinstance(content,str):
            invalid=has_tool_markup(content) or content.startswith(('这条回复生成异常','这次模型返回了无效内容'))
            item['content']='' if content.startswith(('这条回复生成异常','这次模型返回了无效内容')) else visible_text(content,'')
        elif isinstance(content,list):
            for part in content:
                if isinstance(part,dict) and part.get('type')=='text':
                    text=part.get('text')
                    placeholder=isinstance(text,str) and text.startswith(('这条回复生成异常','这次模型返回了无效内容'))
                    invalid=invalid or placeholder or has_tool_markup(text)
                    part['text']='' if placeholder else visible_text(text,'')
        if not item.get('tool_calls') and (invalid or has_tool_markup(item.get('reasoning_content'))):
            item.pop('reasoning_content',None)
    return cleaned

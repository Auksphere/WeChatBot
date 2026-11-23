"""Resolve unquoted public-account articles from verified same-thread messages."""
import re
from urllib.parse import urlsplit

from .recent_context import recent_messages, timestamp

WINDOW = 600
URLS = re.compile(r'https?://[^\s<>\x22]+', re.I)
ARTICLE_WORDS = re.compile(r'这篇|那篇|该文|文章|公众号|论文|链接|刚才.{0,6}(?:发|转|分享)')
ACTION = re.compile(r'总结|概括|分析|解读|锐评|评价|点评|评一评|怎么看|看法|靠谱|可信|合理|研究问题|创新|贡献|结论|讲解|介绍|读|看看')
VAGUE = re.compile(r'^(?:请|帮我|麻烦|再|继续|简单|详细|给我|来|好好)*\s*(?:总结|概括|分析|解读|锐评|评价|点评|读|看看|讲解|介绍)(?:一下|下|这个|这篇|那篇|吧|呗|看看|内容|[，。！？!?\s])*$')


def article_urls(raw):
    link = raw.get('link') or {}
    text = str(link.get('url') or '') + ' ' + str(raw.get('body') or '')
    urls = []
    for match in URLS.finditer(text):
        url = match.group().rstrip('.,;!?)）]}>')
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme in {'http', 'https'} and parsed.hostname == 'mp.weixin.qq.com'
                     and not parsed.username and not parsed.password)
        except ValueError:
            valid = False
        if valid and url not in urls:
            urls.append(url)
    return urls


def refers_to_title(text, title):
    text, title = text.casefold(), str(title or '').casefold()
    if any(word in title for word in re.findall(r'[a-z][a-z0-9_-]{2,}', text)):
        return True
    stop = {'公众号', '是什么', '为什么', '怎么样', '评价一下', '总结一下', '分析一下', '研究问题'}
    return any(part[i:i+3] in title and part[i:i+3] not in stop
               for part in re.findall(r'[\u4e00-\u9fff]{3,}', text)
               for i in range(len(part)-2))


def select_article(raw, text, messages):
    """Pure selection: no model guesses, old LLM contexts, or cross-thread URLs."""
    if raw.get('quote') or article_urls(raw):
        return None, 'explicit'
    if (not ACTION.search(text) and not ARTICLE_WORDS.search(text)) or re.search(
            r'(?:别|不要|不用|停止).{0,8}(?:读|看|打开|分析|总结)|我的记忆|我的档案', text):
        return None, 'unrelated'
    if re.search(r'图片|照片|截图|表情|语音|视频|文件|附件', text) and not ARTICLE_WORDS.search(text):
        return None, 'unrelated'
    current = timestamp(raw.get('createdAt'))
    if current is None:
        return None, 'missing'
    account, thread, user = raw.get('accountId'), raw.get('threadId'), raw.get('senderId')
    current_pos = next((i for i, m in enumerate(messages) if m.get('id') == raw.get('id')), None)
    candidates = []
    for pos, item in enumerate(messages):
        when = timestamp(item.get('createdAt'))
        if (item.get('accountId') != account or item.get('threadId') != thread
                or item.get('direction') != 'incoming' or not item.get('senderId')
                or item.get('id') == raw.get('id') or item.get('recall') is not None
                or when is None or not 0 <= current - when <= WINDOW):
            continue
        # Native pages are newest first: exclude messages following this trigger, even within the same second.
        if current_pos is not None and pos < current_pos:
            continue
        urls = article_urls(item)
        if len(urls) != 1:
            continue
        if item.get('type') != 1 and not (item.get('type') == 49 and item.get('appMessageType') in {3, 4, 5}):
            continue
        title = str((item.get('link') or {}).get('title') or '')
        candidates.append({'raw': item, 'url': urls[0], 'title': title, 'stamp': when, 'order': pos})
    candidates.sort(key=lambda c: (-c['stamp'], c['order']))
    unique = []
    for item in candidates:
        if not any(c['url'] == item['url'] for c in unique):
            unique.append(item)
    matching = [c for c in unique if refers_to_title(text, c['title'])]
    own = [c for c in unique if c['raw']['senderId'] == user]
    if re.search(r'我.{0,5}(?:发|分享|转发)|我这篇', text):
        selected = own[:1]
    elif len(matching) == 1:
        selected = matching
    elif re.search(r'最新|最后|刚发的|刚转的|刚分享的', text) and unique:
        selected = unique[:1]
    elif len(unique) == 1 and (ARTICLE_WORDS.search(text) or VAGUE.fullmatch(text)):
        selected = unique
    elif unique and own and unique[0] is own[0] and current-own[0]['stamp'] <= 120 and VAGUE.fullmatch(text):
        selected = own[:1]
    else:
        selected = []
    if selected:
        return selected[0], 'recent_self' if selected[0]['raw']['senderId'] == user else 'recent_group'
    return None, 'ambiguous' if unique and (ARTICLE_WORDS.search(text) or VAGUE.fullmatch(text)) else 'missing'


async def resolve_article(event, text):
    event.set_extra('companion_article_context_checked', True)
    if not ACTION.search(text) and not ARTICLE_WORDS.search(text):
        event.set_extra('companion_article_selection_status', 'unrelated')
        return None
    messages = event.get_extra('companion_group_native_messages')
    if messages is None:
        messages = await recent_messages(event, window_seconds=WINDOW, max_items=160)
    target, status = select_article(event.message_obj.raw_message, text, messages)
    event.set_extra('companion_article_selection_status', status)
    if target:
        event.set_extra('companion_article_target', target)
    return target


async def verify_article(event, target):
    original = target['raw']
    current = await event.adapter.client.message(original['id'])
    fields = ('id', 'accountId', 'threadId', 'senderId', 'serverId', 'direction', 'type', 'appMessageType')
    if (not isinstance(current, dict) or any(current.get(k) != original.get(k) for k in fields)
            or current.get('recall') is not None or article_urls(current) != [target['url']]):
        raise ValueError('article_source_changed')

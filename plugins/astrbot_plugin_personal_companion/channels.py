"""Select same-chat Channels shares and prepare real video evidence."""
import asyncio
from datetime import datetime, timezone
import re

from .media import attachment_bytes, verify_attachment


def is_channels(message):
    return isinstance(message, dict) and message.get('type') == 49 and message.get('appMessageType') == 51


def is_video(message):
    return isinstance(message, dict) and (is_channels(message) or message.get('type') in {43,62})


def candidate(raw, text):
    quote = raw.get('quote') or {}
    if re.search(r'(?:别|不要|不用|停止).{0,8}(?:读|看|分析|总结)', text):
        return False
    return bool(is_channels(raw) or is_channels(quote) or
        (quote.get('type') == 49 and str(quote.get('embeddedText') or '').startswith(('[Channels', '[视频号'))) or
        re.search(r'视频号|(?:这个|这条|刚才的|刚发的)视频|(?:总结|分析|看看|读).{0,6}视频', text))


def select_source(raw, text, recent, now=None, *, channels_only=True):
    """Never guess another member's target without an exact quote."""
    account, thread, sender = raw.get('accountId'), raw.get('threadId'), raw.get('senderId')
    quote = raw.get('quote') or {}
    if quote.get('state') == 'recalled':
        raise ValueError('引用的视频号消息已撤回')
    accepts = is_channels if channels_only else is_video
    if accepts(raw):
        sources = [raw]
    elif quote:
        if not isinstance(quote.get('serverId'), str) or not quote['serverId'] or not quote.get('senderId'):
            raise ValueError('视频号引用信息不完整，请重新转发卡片后引用提问')
        sources = [m for m in recent if m.get('serverId') == quote['serverId'] and m.get('senderId') == quote['senderId']]
    else:
        now = datetime.now(timezone.utc).timestamp() if now is None else now
        sources = []
        for message in recent:
            try:
                age = now - datetime.fromisoformat(message['createdAt'].replace('Z', '+00:00')).timestamp()
            except (KeyError, ValueError, TypeError):
                continue
            if (message.get('senderId') == sender and message.get('id') != raw.get('id') and -60 <= age <= 600
                    and (is_channels(message) or message.get('type') in {43, 62} or
                         (message.get('type') == 49 and message.get('appMessageType') == 6))):
                sources.append(message)
        sources.sort(key=lambda m:m.get('createdAt',''), reverse=True)
        if '视频号' not in text:
            sources = sources[:1]
    sources = [m for m in sources if accepts(m) and m.get('id') and not m.get('recall')
        and m.get('accountId') == account and m.get('threadId') == thread]
    sources.sort(key=lambda m:m.get('createdAt',''), reverse=True)
    if not sources:
        return None
    if quote and len(sources) != 1:
        raise ValueError('引用目标不明确，请重新转发目标视频号')
    return sources[0]


async def locate(event):
    raw = event.message_obj.raw_message
    text = str(event.message_str or '')
    if not candidate(raw, text):
        return None
    recent = [] if is_channels(raw) else (await asyncio.wait_for(event.adapter.client.messages(raw['threadId'], limit=100), timeout=5)).get('items', [])
    source = select_source(raw, text, recent)
    if source is None and (is_channels(raw) or is_channels(raw.get('quote'))):
        raise ValueError('暂时找不到引用的视频号原消息，请重新转发卡片后提问')
    return source


async def locate_video(event):
    """Receive direct private videos and quoted/recent regular videos as well."""
    raw = event.message_obj.raw_message
    text = str(event.message_str or '')
    quote = raw.get('quote') or {}
    if not (candidate(raw, text) or is_video(raw) or is_video(quote)):
        return None
    recent = [] if is_video(raw) else (await asyncio.wait_for(event.adapter.client.messages(raw['threadId'], limit=100), timeout=5)).get('items', [])
    source = select_source(raw, text, recent, channels_only=False)
    if source is None and (is_video(raw) or is_video(quote)):
        raise ValueError('找不到引用的视频原消息，请重新发送后提问')
    return source


async def read_video(event, source, analyze):
    data, name, video, message_id = await asyncio.wait_for(attachment_bytes(event, 'video', source_id=source['id']), timeout=55)
    if not video or message_id != source['id']:
        raise ValueError('收到的媒体不是所选视频号视频')
    result = await analyze(data, name, message_id)
    current = await verify_attachment(event, message_id)
    link = current.get('link') or source.get('link') or {}
    metadata = '\n'.join(label + str(link[key])[:1500] for key, label in
        [('sourceName','视频发布者：'),('title','卡片标题：'),('description','卡片描述：'),('durationSeconds','卡片标注时长（秒）：')]
        if link.get(key) is not None)
    result['note'] = metadata + '\n' + result['note']
    return result

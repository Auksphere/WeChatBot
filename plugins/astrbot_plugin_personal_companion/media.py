"""Bounded document extraction and sampled video preparation."""

import hashlib
import io
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from .typed_trigger import addressed,private_request

DOCUMENTS = {'.pdf', '.docx', '.xlsx', '.xls', '.pptx', '.txt', '.md', '.csv', '.tsv', '.json', '.log', '.doc', '.ppt'}
VIDEOS = {'.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v'}
MAX_BYTES = 100 * 1024 * 1024
MAX_TEXT = 24000


def extract_document(data: bytes, name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix not in DOCUMENTS:
        raise ValueError('暂不支持这种文档格式')
    if len(data) > 25 * 1024 * 1024:
        raise ValueError('文档超过25MB，请拆分后发送')
    if suffix == '.pdf':
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ValueError('PDF已加密，请先解密')
        text = '\n'.join(p.extract_text() or '' for p in reader.pages[:50])
    elif suffix in {'.docx', '.xlsx', '.pptx'}:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if sum(x.file_size for x in entries) > 64 * 1024 * 1024 or len(entries) > 5000:
                raise ValueError('文档解压后过大')
            if suffix == '.docx':
                paths = [x.filename for x in entries if x.filename == 'word/document.xml']
            elif suffix == '.pptx':
                paths = sorted(x.filename for x in entries if re.fullmatch(r'ppt/slides/slide\d+\.xml', x.filename))[:100]
            else:
                paths = [x.filename for x in entries if x.filename == 'xl/sharedStrings.xml']
                paths += sorted(x.filename for x in entries if re.fullmatch(r'xl/worksheets/sheet\d+\.xml', x.filename))[:30]
            chunks = []
            for path in paths:
                element = ET.fromstring(archive.read(path))
                chunks.append(' '.join(x.text for x in element.iter() if x.text and x.tag.rsplit('}', 1)[-1] in {'t','v'}))
            text = '\n'.join(chunks)
            if suffix == '.xlsx':
                from openpyxl import load_workbook
                book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
                rows = []
                for sheet in list(book)[:20]:
                    rows.append('工作表：' + sheet.title)
                    for row in sheet.iter_rows(max_row=500, max_col=30, values_only=True):
                        rows.append('\t'.join('' if x is None else str(x) for x in row))
                        if sum(map(len, rows)) > MAX_TEXT:
                            break
                book.close()
                text = '\n'.join(rows)
    elif suffix == '.xls':
        import xlrd
        book = xlrd.open_workbook(file_contents=data)
        rows = []
        for sheet in book.sheets()[:20]:
            rows.append('工作表：' + sheet.name)
            rows += ['\t'.join(map(str, sheet.row_values(i)[:30])) for i in range(min(500, sheet.nrows))]
        text = '\n'.join(rows)
    elif suffix in {'.doc', '.ppt'}:
        raise ValueError('旧版DOC/PPT请转为DOCX/PPTX，或使用云端文档解析')
    else:
        encodings = ('utf-16',) if data.startswith((b'\xff\xfe', b'\xfe\xff')) else ('utf-8-sig', 'gb18030')
        for encoding in encodings:
            try:
                text = data.decode(encoding)
                break
            except UnicodeError:
                continue
        else:
            raise ValueError('无法识别文本编码')
    if not text.strip():
        raise ValueError('未提取到文字；扫描文档可使用云端图文解析')
    return text[:MAX_TEXT] + ('\n[内容较长，已截取前24000字]' if len(text) > MAX_TEXT else '')


async def attachment_bytes(event, kind='auto', *, source_id=None):
    """Read only an explicitly quoted attachment or the sender's recent same-chat file."""
    import asyncio
    from datetime import datetime, timezone
    adapter = event.adapter
    raw = event.message_obj.raw_message
    cfg = adapter.native_config
    if not getattr(cfg, 'enable_media', True):
        raise ValueError('当前未启用附件读取')
    if raw.get('accountId') != cfg.account_id or raw.get('senderId') != event.get_sender_id():
        raise ValueError('账号或发送者不匹配')
    if raw.get('direction') != 'incoming' or raw.get('recall'):
        raise ValueError('只能读取当前收到的有效消息')
    thread = raw['threadId']
    if thread.endswith('@chatroom') and not addressed(raw,adapter):
        raise ValueError('请@机器人或输入“@大肥鱼”再请求读取')
    if thread.endswith('@chatroom') and cfg.allowed_groups and thread not in cfg.allowed_groups:
        raise ValueError('当前群未启用')
    if not thread.endswith('@chatroom') and not private_request(raw,adapter):
        raise ValueError('当前私聊未启用')
    client = adapter.client
    recent = (await client.messages(thread, limit=100)).get('items', [])
    quote = raw.get('quote') or {}
    if quote.get('state') == 'recalled':
        raise ValueError('引用消息已撤回')
    if (raw.get('type') in {43,62} or (raw.get('type') == 49 and raw.get('appMessageType') == 51)) and not quote:
        sources = [raw]
    elif quote.get('serverId'):
        sources = [m for m in recent if m.get('serverId') == quote['serverId'] and
                   (not quote.get('senderId') or m.get('senderId') == quote['senderId'])]
    else:
        now = datetime.now(timezone.utc).timestamp()
        sources = []
        for m in recent:
            try:
                age = now - datetime.fromisoformat(m['createdAt'].replace('Z','+00:00')).timestamp()
            except (KeyError, ValueError, TypeError):
                continue
            if m.get('senderId') == raw['senderId'] and m.get('id') != raw['id'] and -60 <= age <= 600:
                sources.append(m)
    sources = [m for m in sources if m.get('threadId') == thread and m.get('accountId') == cfg.account_id
               and not m.get('recall') and (m.get('type') in {43,62} or
               (m.get('type') == 49 and m.get('appMessageType') in {6,51}))]
    if source_id is not None:
        sources = [m for m in sources if m.get('id') == source_id]
    if kind == 'video':
        sources = [m for m in sources if m.get('type') in {43,62} or m.get('type') == 49]
    if not sources:
        raise ValueError('请引用要读取的文件/视频后请求读取，也可先发送附件再在10分钟内提问；群聊还需@大肥鱼')
    sources.sort(key=lambda m: m.get('createdAt', ''), reverse=True)
    source = sources[0]
    assets = (await client.media_assets(source['id'])).get('items', [])
    assets = [a for a in assets if a.get('accountId') == cfg.account_id and a.get('messageId') == source['id']
              and a.get('kind') in {'file','video'} and a.get('source') in {None,'message'}]
    if not assets:
        raise ValueError('附件元数据暂不可用，请在微信打开附件后重试')
    asset = next((a for a in assets if a.get('variant') == 'original'), assets[0])
    if kind == 'video' and asset.get('kind') != 'video' and Path(asset.get('filename') or '').suffix.lower() not in VIDEOS:
        raise ValueError('最近附件不是视频，请引用目标视频重试')
    size = asset.get('byteLength') or asset.get('advertisedByteLength')
    if size and int(size) > MAX_BYTES:
        raise ValueError('附件超过100MB，请拆分后重试')
    if asset.get('state') != 'available':
        job = await client.transfer_media(source['id'], 'normal' if source.get('appMessageType') == 51 else 'original', asset_id=asset['id'],
            key=hashlib.sha256(('companion-read:' + cfg.account_id + ':' + asset['id']).encode()).hexdigest())
        for _ in range(45):
            state = job.get('state')
            if state in {'success','succeeded','completed','available'}:
                break
            if state in {'failed','cancelled','needs_review'}:
                if source.get('appMessageType') == 51:
                    reason = job.get('reason')
                    if reason == 'channels_download_failed':
                        import socket, ipaddress
                        try:
                            addresses = await asyncio.wait_for(asyncio.to_thread(socket.getaddrinfo, 'finder.video.qq.com', 443, type=socket.SOCK_STREAM), timeout=2)
                            if addresses and all(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
                                raise ValueError('视频号下载域名被代理 DNS 解析为虚拟或内网 IP，安全检查阻止了下载；请让 finder.video.qq.com 使用真实 DNS 后重试')
                        except (OSError, asyncio.TimeoutError):
                            pass
                    reasons = {'channels_post_unavailable':'视频号已删除、设为私密或当前账号无法访问',
                        'channels_unsupported_media':'这类视频号内容暂不支持下载',
                        'channels_integrity_failed':'视频号文件未通过完整性校验',
                        'channels_download_failed':'微信视频号文件下载失败，请稍后重新转发重试',
                        'channels_route_unsupported':'当前微信接入层不支持这条视频号的下载路线'}
                    raise ValueError(reasons.get(reason, '视频号视频下载未完成，请稍后重试'))
                raise ValueError('微信附件下载未完成，请在微信打开文件后重试')
            await asyncio.sleep(1)
            job = await client.job(job['id'])
        assets = (await client.media_assets(source['id'])).get('items', [])
        asset = next((a for a in assets if a.get('id') == asset['id'] and a.get('state') == 'available'
                      and a.get('accountId') == cfg.account_id and a.get('messageId') == source['id']
                      and a.get('kind') in {'video','file'} and a.get('source') in {None,'message'}), None)
        if not asset:
            raise ValueError('附件下载超时，请在微信打开后重试')
    current = await client.message(source['id'])
    if current.get('recall') or current.get('threadId') != thread:
        raise ValueError('附件已撤回或会话不匹配')
    data, content_type = await client.media_content(asset['id'], preview=False, max_bytes=MAX_BYTES)
    if asset.get('sha256') and hashlib.sha256(data).hexdigest().lower() != asset['sha256'].lower():
        raise ValueError('附件校验失败')
    if asset.get('byteLength') and len(data) != asset['byteLength']:
        raise ValueError('附件长度不匹配')
    current = await client.message(source['id'])
    trigger = await client.message(raw['id'])
    if (current.get('recall') or trigger.get('recall') or current.get('threadId') != thread
            or current.get('accountId') != cfg.account_id or trigger.get('threadId') != thread
            or trigger.get('accountId') != cfg.account_id or trigger.get('senderId') != raw['senderId']):
        raise ValueError('消息已撤回')
    event.set_extra('wechat_native_verified_attachment', {k:source.get(k) for k in
        ['id','accountId','threadId','serverId','senderId','type','appMessageType']})
    event.set_extra('wechat_native_quote_original_id', source['id'])
    filename = Path(asset.get('filename') or ('video.mp4' if source.get('type') in {43,62} or source.get('appMessageType') == 51 else 'document.bin')).name
    return data, filename, asset.get('kind') == 'video' or Path(filename).suffix.lower() in VIDEOS, source['id']


async def verify_attachment(event, source_id):
    """Revalidate the exact selected attachment between long-running stages."""
    import asyncio
    raw = event.message_obj.raw_message
    snapshot = event.get_extra('wechat_native_verified_attachment') or {}
    if snapshot.get('id') != source_id:
        raise ValueError('附件来源尚未验证')
    source, trigger = await asyncio.wait_for(asyncio.gather(event.adapter.client.message(source_id),
        event.adapter.client.message(raw['id'])), timeout=5)
    if (any(m.get('accountId') != raw['accountId'] or m.get('threadId') != raw['threadId'] or m.get('recall') is not None for m in [source,trigger])
            or any(source.get(k) != snapshot.get(k) for k in ['id','accountId','threadId','serverId','senderId','type','appMessageType'])
            or trigger.get('id') != raw['id'] or trigger.get('senderId') != raw['senderId']):
        raise ValueError('视频或提问已撤回，或消息身份改变')
    if raw.get('quote'):
        expected, actual = raw['quote'], trigger.get('quote') or {}
        if any(actual.get(k) != expected.get(k) for k in ['serverId','senderId','type']) or actual.get('state') == 'recalled':
            raise ValueError('引用已改变或撤回')
    return source

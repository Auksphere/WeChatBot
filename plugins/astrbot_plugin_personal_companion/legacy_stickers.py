"""Read legacy Tencent sticker references without modifying Native messages or profiles."""
import asyncio,base64,hashlib,hmac,importlib,io,json,os,re,sqlite3,time
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit
from xml.etree import ElementTree as ET

from Crypto.Cipher import AES
from PIL import Image
import zstandard

MAX_BYTES=10*1024*1024

def canonical_descriptor(content):
    if len(content)>128*1024 or '<!DOCTYPE' in content.upper() or '<!ENTITY' in content.upper():return None
    if '<msg' in content:content=content[content.index('<msg'):]
    doc=ET.fromstring(content)
    if doc.tag!='msg':return None
    emoji=doc.find('emoji')
    if emoji is None:return None
    source=emoji.get('cdnurl') or ''
    if len(source)>8192:return None
    url=urlsplit(source)
    if (url.hostname!='vweixinf.tc.qq.com' or url.scheme not in {'http','https'} or url.username
            or url.password or url.fragment or url.port not in {None,80,443}):return None
    size=int(emoji.get('len') or '0');md5=(emoji.get('md5') or '').lower()
    if not 0<size<=MAX_BYTES or not re.fullmatch('[a-f0-9]{32}',md5):return None
    expiry=emoji.get('expiretime')
    if expiry and int(expiry)<=time.time():return None
    return dict(url=urlunsplit(('https','wxapp.tc.qq.com',url.path,url.query,'')),size=size,md5=md5)

def _database_snapshot(path,key,salt):
    """Authenticated SQLCipher-4 snapshot in memory, including committed WAL pages."""
    if path.stat().st_size>64*1024*1024:raise ValueError('profile_database_too_large')
    hk=hashlib.pbkdf2_hmac('sha512',key,bytes(b^0x3a for b in salt),2,32)
    def decrypt(data,number):
        if len(data)!=4096:raise ValueError('profile_page_size')
        offset=16 if number==1 else 0
        digest=hmac.new(hk,data[offset:4032]+number.to_bytes(4,'little'),'sha512').digest()
        if not hmac.compare_digest(digest,data[4032:]):raise ValueError('profile_page_integrity')
        plain=AES.new(key,AES.MODE_CBC,data[4016:4032]).decrypt(data[offset:4016])
        return (b'SQLite format 3\x00' if number==1 else b'')+plain+b'\0'*80
    data=path.read_bytes()
    if data[:16]!=salt or len(data)%4096:raise ValueError('profile_database_identity')
    pages={i//4096+1:decrypt(data[i:i+4096],i//4096+1) for i in range(0,len(data),4096)}
    size=len(pages);wal=path.with_name(path.name+'-wal')
    if wal.exists():
        if wal.stat().st_size>64*1024*1024:raise ValueError('profile_wal_too_large')
        data=wal.read_bytes();pending={}
        if len(data)>=32 and int.from_bytes(data[8:12],'big')==4096:
            for offset in range(32,len(data)-4119,4120):
                header=data[offset:offset+24]
                if header[8:16]!=data[16:24]:break
                number=int.from_bytes(header[:4],'big');commit=int.from_bytes(header[4:8],'big')
                if not 0<number<=16384 or commit>16384:raise ValueError('profile_wal_page')
                pending[number]=decrypt(data[offset+24:offset+4120],number)
                if commit:pages.update(pending);pending={};size=commit
    plain=bytearray(b''.join(pages[i] for i in range(1,size+1)));plain[18:20]=b'\x01\x01'
    db=sqlite3.connect(':memory:');db.deserialize(bytes(plain));return db

def read_descriptor(root,account,own,group,original):
    if not re.fullmatch(r'account-[A-Za-z0-9_-]+',account) or not re.fullmatch(r'wxid_[A-Za-z0-9]+',own):
        return None
    source=original['id']
    if not source.startswith('msg_'):return None
    decoded=json.loads(base64.urlsafe_b64decode(source[4:]+'='*((-len(source[4:]))%4)))
    relative=decoded.get('database','');table=decoded.get('table','')
    if not re.fullmatch(r'message/message_\d+\.db',relative):return None
    if table!='Msg_'+hashlib.md5(group.encode()).hexdigest():return None
    location=Path(root)/'native-state/accounts'/account
    keys=json.loads((location/'agent/profile-keys.json').read_text(encoding='utf-8'))
    entry=next(k for k in keys['databases'] if k['relativePath']==relative)
    profile=next((location/'profile/xwechat_files').glob(own+'_*/db_storage'))
    path=(profile/relative).resolve()
    if not path.is_relative_to(location.resolve()):return None
    key=base64.b64decode(entry['rawPageKeyBase64']);salt=bytes.fromhex(entry['saltHex'])
    if len(key)!=32 or len(salt)!=16:return None
    with closing(_database_snapshot(path,key,salt)) as db:
        row=db.execute('SELECT server_id,local_type,message_content FROM '+table+' WHERE local_id=?',
            (int(decoded['localId']),)).fetchone()
        if not row or row[1]&2147483647!=47 or str(int(row[0])&((1<<64)-1))!=original.get('serverId'):return None
        content=row[2]
        if isinstance(content,bytes):
            if content.startswith(b'\x28\xb5\x2f\xfd'):
                content=zstandard.ZstdDecompressor().decompress(content,max_output_size=128*1024)
            content=content.decode('utf-8')
        return canonical_descriptor(content)

class LegacyStickerBridge:
    def __init__(self):self.installed={}

    def install(self,adapter):
        service=adapter.stickers
        previous=self.installed.get(adapter)
        if previous and previous[0] is service:return
        if previous:self.remove(adapter)
        original=service.read_quote
        async def read(raw,*,original=False,guard=None):
            try:return await self.installed[adapter][1](raw,original=original,guard=guard)
            except Exception as error:
                if getattr(error,'reason',None) not in {'unavailable','unsupported_format'}:raise
                try:
                    recovered=await asyncio.wait_for(self.recover(adapter,raw,guard),timeout=22)
                except Exception as recovery_error:
                    if getattr(recovery_error,'reason',None)=='recalled':raise
                    recovered=None
                if recovered is None:raise error
                return recovered
        self.installed[adapter]=(service,original,read)
        service.read_quote=read

    async def recover(self,adapter,raw,guard=None):
        cfg=adapter.native_config
        from .typed_trigger import private_request
        if (raw.get('accountId')!=cfg.account_id or (raw.get('threadId') not in cfg.allowed_groups and not private_request(raw,adapter))
                or raw.get('direction')!='incoming' or raw.get('recall') is not None):return None
        url=urlsplit(cfg.api_url)
        if url.hostname not in {'127.0.0.1','localhost'}:return None
        service=adapter.stickers
        if guard:guard()
        source,embedded=await service._resolve_quote_source(raw)
        if embedded:return None
        await service.check_quote(raw,source_message_id=source)
        original=await adapter.client.message(source)
        if original.get('type')!=47 or original.get('recall') is not None:return None
        descriptor=await asyncio.to_thread(read_descriptor,Path(os.environ['ASTRBOT_ROOT']),cfg.account_id,
            adapter.bot_self_id,raw['threadId'],original)
        if descriptor is None:return None
        native=importlib.import_module(type(adapter.client).__module__)
        data=await native._download_public_media(descriptor['url'],max_bytes=MAX_BYTES)
        if len(data)!=descriptor['size'] or hashlib.md5(data).hexdigest()!=descriptor['md5']:
            raise ValueError('legacy_sticker_integrity')
        with Image.open(io.BytesIO(data)) as image:
            if image.width*image.height>16_000_000:raise ValueError('legacy_sticker_pixels')
            content_type=Image.MIME.get(image.format,'application/octet-stream');image.verify()
        await service.check_quote(raw,source_message_id=source)
        if guard:guard()
        return data,dict(kind='sticker',state='available',contentType=content_type,
            byteLength=len(data),sha256=hashlib.sha256(data).hexdigest())

    def remove(self,adapter):
        entry=self.installed.pop(adapter,None)
        if entry and entry[0].read_quote is entry[2]:entry[0].read_quote=entry[1]

    def close(self):
        for adapter in list(self.installed):self.remove(adapter)

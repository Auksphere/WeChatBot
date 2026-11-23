"""Owner-configured automatic friend acceptance, separate from LLM tools."""
import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import secrets
import time


def digest(*values):
    return hashlib.sha256(json.dumps(values,separators=(',',':')).encode()).hexdigest()


def eligible(item, account):
    return (isinstance(item,dict) and item.get('accountId',account)==account
        and isinstance(item.get('id'),str) and bool(item['id']) and len(item['id'])<=512
        and item.get('direction')=='incoming' and item.get('actionable') is True and item.get('isContact') is not True
        and isinstance(item.get('version'),str) and 0<len(item['version'])<=8192)


class Journal:
    def __init__(self,path):
        self.db=sqlite3.connect(path)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS attempts (account TEXT, request_hash TEXT, version_hash TEXT, mutation_key TEXT, operation_id TEXT, status TEXT, updated REAL, request_id TEXT, PRIMARY KEY(account,request_hash))')
        self.db.execute('CREATE TABLE IF NOT EXISTS scans (account TEXT PRIMARY KEY, checked REAL, error TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS manual_commands (account TEXT, request_id TEXT, command_key TEXT, state TEXT, result TEXT, updated REAL, PRIMARY KEY(account,request_id))')
        self.db.commit()

    def get(self,account,request_id):
        row=self.db.execute('SELECT * FROM attempts WHERE account=? AND request_hash=?',(account,digest(request_id))).fetchone()
        return dict(row) if row else None

    def save(self,account,request_id,version,key,status,operation_id=None):
        with self.db:
            self.db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(account,request_hash) DO UPDATE SET version_hash=excluded.version_hash,mutation_key=excluded.mutation_key,operation_id=excluded.operation_id,status=excluded.status,updated=excluded.updated',
                (account,digest(request_id),digest(version),key,operation_id,status,time.time(),request_id))

    def update_status(self,account,request_id,status,operation_id=None):
        with self.db:self.db.execute('UPDATE attempts SET status=?,operation_id=COALESCE(?,operation_id),updated=? WHERE account=? AND request_hash=?',
            (status,operation_id,time.time(),account,digest(request_id)))

    def unfinished(self,account):
        return [dict(r) for r in self.db.execute("SELECT * FROM attempts WHERE account=? AND status IN ('submitting','pending','needs_review') ORDER BY updated LIMIT 20",(account,))]

    def scan(self,account,error=''):
        with self.db:self.db.execute('INSERT INTO scans VALUES(?,?,?) ON CONFLICT(account) DO UPDATE SET checked=excluded.checked,error=excluded.error',(account,time.time(),error))

    def enqueue(self,account,request_id):
        key='companion-manual-friend-'+secrets.token_hex(24)
        with self.db:
            self.db.execute("INSERT INTO manual_commands VALUES(?,?,?,'queued','',?) ON CONFLICT(account,request_id) DO UPDATE SET command_key=excluded.command_key,state='queued',result='',updated=excluded.updated WHERE manual_commands.state='done'",(account,request_id,key,time.time()))
        return self.db.execute('SELECT command_key FROM manual_commands WHERE account=? AND request_id=?',(account,request_id)).fetchone()[0]

    def commands(self,account):
        return [dict(r) for r in self.db.execute("SELECT * FROM manual_commands WHERE account=? AND state IN ('queued','processing') ORDER BY updated LIMIT 20",(account,))]

    def command_status(self,account,request_id,key,state,result=''):
        with self.db:self.db.execute('UPDATE manual_commands SET state=?,result=?,updated=? WHERE account=? AND request_id=? AND command_key=?',(state,result,time.time(),account,request_id,key))

    def close(self):self.db.close()


class AutoAcceptor:
    def __init__(self,account,reader,admin,journal,enabled):
        if reader.account_id!=account or admin.account_id!=account:
            raise ValueError('好友管理账号不匹配')
        self.account,self.reader,self.admin,self.journal,self.enabled=account,reader,admin,journal,enabled

    async def inspect(self,op,request_id):
        result=op.get('result')
        if (op.get('accountId')!=self.account or op.get('kind')!='contact_mutation' or not isinstance(op.get('id'),str)
                or not isinstance(result,dict) or result.get('action')!='accept_request'
                or result.get('requestId',request_id)!=request_id):
            raise ValueError('好友申请操作回执不匹配')
        state,remote=op.get('state'),result.get('remoteOutcome')
        if state=='succeeded' and remote=='accepted':
            return 'contact' if result.get('relationship')=='contact' else 'accepted'
        if state=='failed' and remote in {'not_attempted','rejected'}:return 'failed'
        if state in {'queued','running','projecting','submitting','received'}:return 'pending'
        return 'needs_review'

    async def accept(self,request_id,retry_key=None):
        if not self.enabled():return 'disabled'
        existing=self.journal.get(self.account,request_id)
        recovered_key=None
        if existing and existing['status'] in {'submitting','pending','needs_review'}:
            try:
                op=await (self.admin.operation(existing['operation_id']) if existing['operation_id']
                    else self.admin.contact_mutation_by_key(existing['mutation_key']))
            except Exception as exc:
                if getattr(exc,'status',None)!=404:return 'needs_review'
                recovered_key=existing['mutation_key']
            else:
                if existing['operation_id'] and op.get('id')!=existing['operation_id']:
                    raise ValueError('好友申请操作身份不匹配')
                status=await self.inspect(op,request_id)
                self.journal.update_status(self.account,request_id,status,op['id'])
                return status
        item=await self.reader.contact_request(request_id)
        if item.get('id')!=request_id or item.get('accountId',self.account)!=self.account:
            raise ValueError('好友申请身份不匹配')
        if item.get('isContact') is True:
            if existing:self.journal.update_status(self.account,request_id,'contact')
            return 'contact'
        if not eligible(item,self.account):return 'skipped'
        version=item['version']
        key=retry_key or 'companion-auto-friend-'+digest(self.account,request_id,version)
        if recovered_key is not None:
            if existing['version_hash']!=digest(version):
                self.journal.update_status(self.account,request_id,'needs_review')
                return 'needs_review'
            key=recovered_key
        elif existing and existing['version_hash']==digest(version):
            if not (retry_key and existing['status']=='failed' and existing['mutation_key']!=retry_key):
                return existing['status']
        if not self.enabled():return 'disabled'
        # Persist before sending. The server key and expectedVersion remain stable
        # across restarts and transport timeouts; no greetings are ever sent here.
        self.journal.save(self.account,request_id,version,key,'submitting')
        try:
            op=await self.admin.accept_contact_request(request_id,version,key)
        except Exception as exc:
            code=getattr(exc,'code','')
            if getattr(exc,'status',None) in {401,403,429}:raise
            if code in {'stale_contact_request','contact_request_not_actionable'}:
                self.journal.save(self.account,request_id,version,key,'failed')
                return 'failed'
            try:op=await self.admin.contact_mutation_by_key(key)
            except Exception:return 'needs_review'
        status=await self.inspect(op,request_id)
        self.journal.save(self.account,request_id,version,key,status,op['id'])
        # Most accepts finish quickly. Bound this observation; later scans resume.
        for _ in range(3):
            if status!='pending':break
            await asyncio.sleep(.4)
            op=await self.admin.operation(op['id'])
            status=await self.inspect(op,request_id)
            self.journal.save(self.account,request_id,version,key,status,op['id'])
        return status

    async def process_manual(self):
        for command in self.journal.commands(self.account):
            if not self.enabled():return
            rid,key=command['request_id'],command['command_key']
            self.journal.command_status(self.account,rid,key,'processing')
            status=await self.accept(rid,retry_key=key)
            done=status in {'accepted','contact','failed','skipped'}
            self.journal.command_status(self.account,rid,key,'done' if done else 'processing',status)

    async def scan(self):
        cursor,seen='',set()
        count=0
        for record in self.journal.unfinished(self.account):
            if not self.enabled():return count
            await self.accept(record['request_id'])
        for _ in range(10):
            if not self.enabled():return count
            page=await self.reader.contact_requests(cursor=cursor)
            if not isinstance(page.get('items'),list):raise ValueError('好友申请列表格式异常')
            for row in page['items']:
                # List responses can omit version. Always inspect the exact row
                # again before accepting, instead of trusting names/messages.
                if (row.get('accountId',self.account)==self.account and row.get('direction')=='incoming'
                        and row.get('actionable') is True and row.get('isContact') is not True
                        and isinstance(row.get('id'),str)):
                    before=self.journal.get(self.account,row['id'])
                    await self.accept(row['id'])
                    after=self.journal.get(self.account,row['id'])
                    if after and (not before or before['mutation_key']!=after['mutation_key']):count+=1
                if count>=20:return count
            cursor=page.get('nextCursor')
            if not cursor:return count
            if not isinstance(cursor,str) or cursor in seen:raise ValueError('好友申请列表翻页异常')
            seen.add(cursor)
        return count


class FriendSupervisor:
    def __init__(self,context,directory):
        self.context=context
        self.journal=Journal(Path(directory)/'auto-friends.sqlite3')
        self.tasks={}

    def policy(self,adapter):
        platforms=self.context.get_config().get('platform',[])
        bot=next((p for p in platforms if p.get('id')==adapter.meta().id
            and p.get('account_id')==adapter.native_config.account_id),{})
        return bool(bot) and bot.get('companion_auto_friends',True) is True and bot.get('enable',True) is not False

    def sync(self,adapters):
        active={a for a in adapters if a.meta().name=='wechat_native' and a.meta().id=='wechat-rolebot'}
        for adapter,task in list(self.tasks.items()):
            if adapter not in active:task.cancel();self.tasks.pop(adapter)
        try:loop=asyncio.get_running_loop()
        except RuntimeError:return
        for adapter in active:
            if adapter not in self.tasks or self.tasks[adapter].done():
                self.tasks[adapter]=loop.create_task(self.run(adapter))

    async def run(self,adapter):
        own_admin=None
        account=adapter.native_config.account_id
        try:
            while not adapter.stopped:
                if (not self.policy(adapter) and not self.journal.commands(account)) or adapter.client.closed:
                    await asyncio.sleep(3);continue
                try:
                    if (await adapter.client.account()).get('authenticated') is not True:
                        self.journal.scan(account,'wechat_offline')
                        await asyncio.sleep(30)
                        continue
                    admin=adapter.admin_client
                    if admin is None:
                        if own_admin is None:
                            cfg=adapter.native_config
                            if not cfg.admin_token_file:raise ValueError('未配置好友管理凭据')
                            token=replace(cfg,token='',token_file=cfg.admin_token_file).load_token()
                            own_admin=type(adapter.client)(cfg.api_url,account,token)
                        admin=own_admin
                    await admin.open()
                    manual=AutoAcceptor(account,adapter.client,admin,self.journal,lambda:not adapter.stopped)
                    await asyncio.wait_for(manual.process_manual(),timeout=65)
                    worker=AutoAcceptor(account,adapter.client,admin,self.journal,lambda:self.policy(adapter) and not adapter.stopped)
                    await asyncio.wait_for(worker.scan(),timeout=65)
                    self.journal.scan(account)
                except asyncio.CancelledError:raise
                except Exception as exc:
                    code=getattr(exc,'code','')
                    safe=code if code in {'auth_required','forbidden','rate_limited','stale_contact_request'} else type(exc).__name__
                    self.journal.scan(account,safe)
                await asyncio.sleep(30)
        finally:
            if own_admin is not None:await own_admin.close()

    async def close(self):
        tasks=list(self.tasks.values())
        for task in tasks:task.cancel()
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)
        self.tasks.clear();self.journal.close()

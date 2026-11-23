"""Private system/business senders are not conversational users."""
import asyncio
from collections import OrderedDict
import time
from urllib.parse import quote

# Mirrors Native's stable built-in peer IDs, not display names.
SYSTEM_PEERS=frozenset({
    'filehelper','weixin','weibo','fmessage','floatbottle','medianote','qmessage','tmessage',
    'masssendapp','newsapp','blogapp','qqmail','qqsync','lbsapp','shakeapp',
    'brandsessionholder','brandservicesessionholder','appbrandcustomerservicemsg',
    'service_officialaccounts','notifymessage','notification_messages',
    '@placeholder_foldgroup','@opencustomerservicemsg','wa_app_kefu_message','wxzhifu',
    'gh_22b87fa7cb3c','gh_43f2581f6fd6','gh_3dfda90e39d6','gh_25d9ac85a4bc','gh_579db1f2cf89',
})
BLOCKED_CATEGORIES=frozenset({'system','business','openim_customer_service'})


def private_peer(raw):
    if not isinstance(raw,dict):return None
    thread=raw.get('threadId')
    sender=raw.get('senderId')
    if not isinstance(thread,str) or not isinstance(sender,str) or not sender:return None
    if thread.endswith(('@chatroom','@im.chatroom','@qy_g')):return None
    return sender


def blocked_private_peer(raw,adapter=None):
    peer=private_peer(raw)
    if peer is None:return False
    if peer in SYSTEM_PEERS or raw.get('threadId') in SYSTEM_PEERS:return True
    cached=getattr(adapter,'_private_peer_filter_cache',{}).get(peer)
    return bool(cached and cached[0]>time.monotonic() and cached[1])


def official_contact(detail,peer,account):
    return bool(isinstance(detail,dict) and detail.get('id')==peer
                and detail.get('accountId',account)==account
                and isinstance(detail.get('classification'),dict)
                and detail['classification'].get('category') in BLOCKED_CATEGORIES)


async def prime_private_peer_filter(adapter,raw):
    """One scoped metadata read, cached; never inspect message text or user nicknames."""
    peer=private_peer(raw)
    if peer is None or blocked_private_peer(raw,adapter):return
    cfg=adapter.native_config
    if (raw.get('accountId')!=cfg.account_id or raw.get('direction')!='incoming'
            or peer==adapter.bot_self_id or not cfg.enable_private_reply):return
    if cfg.allowed_users and peer not in cfg.allowed_users:return
    cache=getattr(adapter,'_private_peer_filter_cache',None)
    if not isinstance(cache,OrderedDict):
        cache=adapter._private_peer_filter_cache=OrderedDict(cache or {})
    old=cache.get(peer)
    if old and old[0]>time.monotonic():return
    client=adapter.client
    try:
        detail=await asyncio.wait_for(client._request('GET',client.prefix+'/contacts/'+quote(peer,safe='')),timeout=1.5)
        if not isinstance(detail,dict) or detail.get('id')!=peer or detail.get('accountId',cfg.account_id)!=cfg.account_id:
            raise ValueError('contact_identity_mismatch')
        blocked=official_contact(detail,peer,cfg.account_id)
        if (detail.get('classification') or {}).get('category')=='unknown' and old:
            blocked=old[1]
        duration=300
    except Exception:
        # Unknown classification is not proof of being an official account.
        blocked,duration=bool(old and old[1]),30
    cache[peer]=(time.monotonic()+duration,blocked)
    cache.move_to_end(peer)
    while len(cache)>1024:cache.popitem(last=False)

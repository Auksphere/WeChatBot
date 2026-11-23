"""Local text wake policy; preserve Native metadata and its existing receipt flow."""
import re

from astrbot.api.message_components import At, Plain
try:
    from ..astrbot_plugin_wechat_native.peer_filter import blocked_private_peer
except ImportError:
    from astrbot_plugin_wechat_native.peer_filter import blocked_private_peer

# Exact alias, with a word boundary, accepts spaces, WeChat U+2005 and punctuation.
TEXT_AT = re.compile(r'(?<![A-Za-z0-9_@＠])[@＠]大肥鱼(?![\w\u3400-\u9fff])')
VOICE_COMMAND = re.compile(r'^[/／]语音(?:\s|$)')


def real_mention(raw):
    mentions=raw.get('mentions') or {}
    return mentions.get('state')=='known' and mentions.get('self') is True


def text_request(raw,adapter):
    cfg=adapter.native_config
    thread=raw.get('threadId','')
    return bool(raw.get('accountId')==cfg.account_id and isinstance(thread,str)
        and thread.endswith('@chatroom') and cfg.allowed_groups and thread in cfg.allowed_groups
        and cfg.group_reply_mode!='disabled' and raw.get('direction')=='incoming'
        and raw.get('recall') is None and isinstance(raw.get('senderId'),str)
        and raw['senderId'] and raw['senderId']!=adapter.bot_self_id
        and (raw.get('type')==1 or (raw.get('type')==49 and raw.get('appMessageType')==57))
        and isinstance(raw.get('body'),str) and (TEXT_AT.search(raw['body'])
            or (raw.get('type')==1 and raw['body'].strip() in {'/用法','／用法'})
            or VOICE_COMMAND.match(raw['body'].strip())))


def addressed(raw,adapter):
    return real_mention(raw) or text_request(raw,adapter) or private_request(raw,adapter)


def private_request(raw,adapter):
    cfg=adapter.native_config
    sender,thread=raw.get('senderId'),raw.get('threadId')
    return bool(getattr(cfg,'enable_private_reply',False)
        and not blocked_private_peer(raw,adapter)
        and raw.get('accountId')==cfg.account_id and raw.get('direction')=='incoming'
        and raw.get('recall') is None and isinstance(sender,str) and sender
        and sender!=adapter.bot_self_id and thread==sender and not thread.endswith('@chatroom')
        and (not getattr(cfg,'allowed_users',()) or sender in cfg.allowed_users))


def strip_text_at(text):
    return TEXT_AT.sub('',text).strip()


class TextTriggerBridge:
    """Extend only an existing adapter instance; restore its methods on unload."""
    def __init__(self):
        self.installed={}

    def accepts_sticker_event(self,event):
        adapter=getattr(event,'adapter',None)
        return bool(adapter in self.installed
            and getattr(adapter,'_companion_text_trigger_owner',None) is self
            and adapter._events.get(event.message_obj.message_id) is event
            and event.get_extra('companion_text_trigger') is True
            and text_request(event.message_obj.raw_message,adapter))

    def install(self,adapter):
        previous=getattr(adapter,'_companion_text_trigger_owner',None)
        if previous is self:return False
        if previous is not None:previous.remove(adapter)
        classify=adapter._classify
        commit=adapter.commit_event

        def classify_with_text(raw,origin):
            kind=classify(raw,origin)
            if kind!='ignore' or not text_request(raw,adapter):return kind
            # This transient policy input is used ONLY for the adapter's existing
            # admission checks (timestamp, origin, whitelist and media kind).
            # Never store it, emit it, or pass it to Native sends. The original
            # message and its real mention target list remain unchanged.
            policy_input=dict(raw)
            policy_input['mentions']={**(raw.get('mentions') or {}),'state':'known','self':True}
            return classify(policy_input,origin)

        def commit_with_text(event):
            raw=event.message_obj.raw_message
            if isinstance(raw,dict) and not real_mention(raw) and text_request(raw,adapter):
                text=strip_text_at(event.message_str) or '在吗？'
                event.message_str=text
                event.message_obj.message_str=text
                chain=event.message_obj.message
                for component in chain:
                    if isinstance(component,Plain):component.text=strip_text_at(component.text) or '在吗？'
                if not any(isinstance(c,At) and str(c.qq)==str(event.get_self_id()) for c in chain):
                    # A policy wake marker is not a native WeChat mention.
                    chain.insert(0,At(qq=event.get_self_id()))
                event.set_extra('companion_text_trigger',True)
            return commit(event)

        self.installed[adapter]=(classify,commit,classify_with_text,commit_with_text)
        adapter._classify=classify_with_text
        adapter.commit_event=commit_with_text
        adapter._companion_text_trigger_owner=self
        return True

    def remove(self,adapter):
        entry=self.installed.pop(adapter,None)
        if entry is None:return
        classify,commit,wrapped_classify,wrapped_commit=entry
        if adapter._classify is wrapped_classify:adapter._classify=classify
        if adapter.commit_event is wrapped_commit:adapter.commit_event=commit
        if getattr(adapter,'_companion_text_trigger_owner',None) is self:
            del adapter._companion_text_trigger_owner

    def close(self):
        for adapter in list(self.installed):self.remove(adapter)

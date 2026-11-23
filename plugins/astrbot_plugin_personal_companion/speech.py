"""Bounded DeepSeek ASR/TTS for Native voice messages, using existing credentials."""
import asyncio,base64,re
import httpx
from astrbot.api.message_components import Plain,Record
from .typed_trigger import strip_text_at
from .tts import MAX_AUDIO, pcm_wav, wechat_wav, synthesize as synthesize_voice


def voice_command(raw):
    """Read the original command; AstrBot strips its slash before event handlers."""
    text=raw.get('body')
    if not isinstance(text,str):return None
    text=re.sub(r'^(?:@[^\u2005\r\n]+\u2005[ \t]*)+','',text.strip())
    text=strip_text_at(text)
    match=re.fullmatch(r'[/／]语音(?:\s+(.*))?',text,re.S)
    return (match.group(1) or '').strip() if match else None

class SpeechSupport:
    def __init__(self,context):
        self.context=context;self.gate=asyncio.Semaphore(2)

    def credentials(self,event):
        config=self.context.get_config(event.unified_msg_origin)
        source=next(p for p in config['provider_sources'] if p['id']=='deepseek')
        key=next(k for k in source.get('key',[]) if k and not k.startswith('SET_'))
        return source.get('api_base','https://api.deepseek.com/v1').rstrip('/'),key

    async def recognize(self,event,data):
        data=pcm_wav(data);base,key=self.credentials(event)
        async with self.gate,httpx.AsyncClient(timeout=20) as client:
            response=await client.post(base+'/tools/asr',headers={'Authorization':'Bearer '+key},
                files={'file':('audio.wav',data,'audio/wav')})
            response.raise_for_status();result=response.json()
            if not isinstance(result,dict) or not isinstance(result.get('text'),str):raise ValueError('语音识别没有返回文字')
            return result['text'].strip()[:8000]

    async def input(self,event):
        records=[c for c in event.message_obj.message if isinstance(c,Record)]
        if not records:return False
        if event.get_extra('companion_speech_transcript') is not None:return True
        record=records[0];source=record.file or ''
        if not source.startswith('base64://'):raise ValueError('没有收到原生语音数据')
        data=base64.b64decode(source[9:],validate=True)
        transcript=await asyncio.wait_for(self.recognize(event,data),timeout=22)
        if not transcript:raise ValueError('这条语音没有识别到清晰的话语')
        event.set_extra('companion_speech_transcript',transcript)
        event.set_extra('companion_voice_reply',True)
        original=str(event.message_str or '').strip()
        quote=event.message_obj.raw_message.get('quote') or {}
        label='被引用语音的转写' if quote else '当前发送者的语音转写'
        text=(original+'\n' if original else '')+'【'+label+'】'+transcript
        event.message_str=text;event.message_obj.message_str=text
        event.message_obj.message=[c for c in event.message_obj.message if not isinstance(c,Record)]
        event.message_obj.message.append(Plain('【'+label+'】'+transcript))
        return True

    async def synthesize(self,event,text):
        config=self.context.get_config(event.unified_msg_origin)
        account=(event.message_obj.raw_message or {}).get('accountId')
        bot=next((p for p in config.get('platform',[]) if p.get('account_id')==account),{}) if account else {}
        settings=bot.get('companion_speech') or {}
        # Fish credentials are independent; a Fish reply never needs a DeepSeek TTS key.
        base,key=('', '') if settings.get('voice')=='hajimi' else self.credentials(event)
        async with self.gate:
            return await synthesize_voice(settings,base,key,text)

    async def decorate(self,event):
        if not event.get_extra('companion_voice_reply'):return
        result=event.get_result()
        if result is None or not result.is_llm_result() or event.get_extra('companion_speech_output_ready'):return
        text=result.get_plain_text().strip()
        if not text:return
        try:
            data=await asyncio.wait_for(self.synthesize(event,text),timeout=28)
            record=Record.fromBase64(base64.b64encode(data).decode('ascii'),text=text)
            result.chain=[record]
            event.set_extra('companion_speech_output_ready',True)
        except Exception as exc:
            # Keep the complete existing text if synthesis fails; never promise a later voice.
            result.chain.append(Plain('\n（这次语音合成未完成，先用文字回复。）'))
            event.set_extra('companion_speech_output_error',type(exc).__name__)

"""Shared TTS transport: no AstrBot imports, no audio or credentials persisted."""
import asyncio
import io
import os
import re
import subprocess
import wave

MAX_AUDIO = 8 * 1024 * 1024
HAJIMI_REFERENCE = 'ae5adc6778ac459e8d6106b82f88fa2b'
FISH_MODELS = {'s2.1-pro-free', 's2.1-pro'}


def voice_settings(payload, current=None):
    """Blank keys preserve the saved key. Validate before any config mutation."""
    value = dict(current or {})
    voice = payload.get('voice', value.get('voice', 'stella'))
    if voice not in {'stella', 'tide', 'hajimi'}:
        raise ValueError('请选择支持的语音音色。')
    reference = str(payload.get('fish_reference_id', value.get('fish_reference_id', HAJIMI_REFERENCE))).strip()
    model = payload.get('fish_model', value.get('fish_model', 's2.1-pro-free'))
    key = str(payload.get('fish_api_key') or '').strip() or str(value.get('fish_api_key') or '')
    if not re.fullmatch(r'[a-fA-F0-9]{32}', reference):
        raise ValueError('Fish 音色 ID 需为 32 位十六进制字符，可从音色页面链接复制。')
    if model not in FISH_MODELS:
        raise ValueError('请选择支持的 Fish Audio 模型。')
    if key and (not 12 <= len(key) <= 512 or re.search(r'\s', key)):
        raise ValueError('Fish API Key 格式无效，留空可保留原密钥。')
    if voice == 'hajimi' and not key:
        raise ValueError('哈基米音色需要单独的 Fish Audio API Key，请在本页填写。')
    return {'voice': voice, 'fish_reference_id': reference.lower(), 'fish_model': model, 'fish_api_key': key}


def request_spec(settings, deepseek_base, deepseek_key, text):
    if not isinstance(text, str) or not text.strip() or len(text) > 600:
        raise ValueError('语音文字需为 1–600 个字符。')
    value = voice_settings({}, settings)
    if value['voice'] == 'hajimi':
        # Strip common Markdown while keeping the content. No instruction tags
        # are added to the user's answer; reference_id selects the timbre.
        text = re.sub(r'\[([^\]]+)\]\(https?://[^)]+\)', r'\1', text)
        text = re.sub(r'(?m)^\s{0,3}#{1,6}\s+', '', text)
        text = text.replace('**', '').replace('`', '')
        return ('https://api.fish.audio/v1/tts',
            {'Authorization': 'Bearer ' + value['fish_api_key'], 'model': value['fish_model']},
            {'text': text, 'reference_id': value['fish_reference_id'], 'format': 'wav',
             'sample_rate': 16000, 'latency': 'balanced', 'normalize': True})
    if not deepseek_key:
        raise ValueError('请先配置 DeepSeek API Key。')
    return (deepseek_base.rstrip('/') + '/tools/tts', {'Authorization': 'Bearer ' + deepseek_key},
        {'text': text, 'voice_id': value['voice'], 'format': 'wav', 'filter_markdown': True})


def pcm_wav(data):
    if not isinstance(data,bytes) or not data or len(data)>MAX_AUDIO:raise ValueError('语音大小无效或超过8MiB')
    try:
        with wave.open(io.BytesIO(data),'rb') as source:
            rate,channels,width=source.getframerate(),source.getnchannels(),source.getsampwidth()
            if source.getcomptype()!='NONE' or width!=2 or channels not in {1,2} or rate not in {8000,16000,22050,24000,32000,44100,48000}:
                raise ValueError('语音不是兼容的16位PCM WAV')
            audio=source.readframes(MAX_AUDIO)
    except (wave.Error,EOFError) as exc:raise ValueError('语音文件不完整') from exc
    if not audio or len(audio)%(channels*width):raise ValueError('语音采样不完整')
    duration=len(audio)/(rate*channels*width)
    if duration>120:raise ValueError('语音超过2分钟，请分段发送')
    output=io.BytesIO()
    with wave.open(output,'wb') as target:
        target.setnchannels(channels);target.setsampwidth(width);target.setframerate(rate);target.writeframes(audio)
    return output.getvalue()

def wechat_wav(data):
    """Produce the PCM format Native can encode without an external decoder."""
    data=pcm_wav(data)
    with wave.open(io.BytesIO(data),'rb') as source:
        if source.getframerate()==16000 and source.getnchannels()==1:return data
    import imageio_ffmpeg
    try:
        result=subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-v','error','-nostdin',
            '-protocol_whitelist','file,pipe','-format_whitelist','wav','-f','wav','-i','pipe:0',
            '-vn','-ac','1','-ar','16000','-c:a','pcm_s16le','-f','wav','pipe:1'],
            input=data,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=12,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0,check=True)
    except (OSError,subprocess.SubprocessError) as exc:
        raise ValueError('语音格式转换未完成') from exc
    return pcm_wav(result.stdout)

async def synthesize(settings, base, key, text):
    import httpx
    url, headers, payload = request_spec(settings, base, key, text)
    async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
        async with client.stream('POST', url, headers=headers, json=payload) as response:
            if response.status_code != 200:
                # Never include upstream bodies, request headers or tokens in errors.
                messages = {400: '音色 ID 或请求参数无效', 401: 'API Key 无效',
                    402: '当前语音模型的 API 余额不足', 403: '当前音色没有访问权限',
                    404: '音色或接口不可用', 429: '语音请求过于频繁，请稍后再试'}
                raise ValueError(messages.get(response.status_code, '语音服务暂时不可用'))
            data = bytearray()
            async for chunk in response.aiter_bytes(65536):
                data.extend(chunk)
                if len(data) > MAX_AUDIO:
                    raise ValueError('语音回复超过大小限制')
    return await asyncio.to_thread(wechat_wav, bytes(data))

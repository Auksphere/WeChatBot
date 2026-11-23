"""Offline transport tests; no AstrBot, external API calls or real credentials."""
import asyncio
import importlib.util
import io
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
import wave

SPEC = importlib.util.spec_from_file_location('tts_fixture', Path(__file__).resolve().parents[1] / 'plugins/astrbot_plugin_personal_companion/tts.py')
tts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tts)


def wav():
    result = io.BytesIO()
    with wave.open(result, 'wb') as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes(b'\0\0' * 1600)
    return result.getvalue()


class TransportTests(unittest.TestCase):
    def test_default_and_tide_keep_deepseek_contract(self):
        for settings, expected in [({}, 'stella'), ({'voice': 'tide'}, 'tide')]:
            url, headers, body = tts.request_spec(settings, 'https://api.deepseek.com/v1/', 'deepseek-fixture', '你好')
            self.assertEqual(url, 'https://api.deepseek.com/v1/tools/tts')
            self.assertEqual(body['voice_id'], expected)
            self.assertEqual(body['format'], 'wav')
            self.assertEqual(headers['Authorization'], 'Bearer deepseek-fixture')

    def test_hajimi_uses_separate_key_fixed_host_and_reference(self):
        settings = {'voice': 'hajimi', 'fish_api_key': 'fish-fixture-key'}
        url, headers, body = tts.request_spec(settings, 'https://deepseek.invalid', 'never-send-this', '**你好**')
        self.assertEqual(url, 'https://api.fish.audio/v1/tts')
        self.assertEqual(headers['model'], 's2.1-pro-free')
        self.assertEqual(body['reference_id'], tts.HAJIMI_REFERENCE)
        self.assertEqual(body['text'], '你好')
        self.assertEqual(body['sample_rate'], 16000)
        self.assertNotIn('never-send-this', json.dumps([headers, body]))

    def test_invalid_voice_model_reference_key_and_long_text_rejected(self):
        for payload in [{'voice': 'unknown'}, {'fish_model': 'unknown'}, {'fish_reference_id': '../secret'},
                {'voice': 'hajimi'}, {'fish_api_key': 'has spaces not valid'}]:
            with self.assertRaises(ValueError):
                tts.voice_settings(payload)
        with self.assertRaises(ValueError):
            tts.request_spec({}, '', '', 'a' * 601)

    def test_stream_bounds_errors_and_wav_compatibility(self):
        for status, chunks, success in [(200, [wav()], True), (401, [b'upstream-secret'], False),
                (200, [b'x' * (tts.MAX_AUDIO + 1)], False), (200, [b'not audio'], False)]:
            class Response:
                status_code = status
                async def __aenter__(self): return self
                async def __aexit__(self, *args): pass
                async def aiter_bytes(self, size):
                    for chunk in chunks: yield chunk
            class Client:
                def __init__(self, **kw): pass
                async def __aenter__(self): return self
                async def __aexit__(self, *args): pass
                def stream(self, *args, **kw): return Response()
            with patch.dict(sys.modules, {'httpx': types.SimpleNamespace(AsyncClient=Client)}):
                call = tts.synthesize({}, 'https://deepseek.invalid', 'fixture-key', '测试')
                if success:
                    with wave.open(io.BytesIO(asyncio.run(call)), 'rb') as audio:
                        self.assertEqual((audio.getnchannels(), audio.getframerate()), (1, 16000))
                else:
                    with self.assertRaises(ValueError) as raised:
                        asyncio.run(call)
                    self.assertNotIn('upstream-secret', str(raised.exception))


class SpeechRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_account_voice_selection_and_text_fallback(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        class Plain:
            def __init__(self, text): self.text=text
        class Record:
            @classmethod
            def fromBase64(cls, *args, **kwargs): return cls()
        class Event:
            unified_msg_origin = 'fixture-origin'
            message_obj = SimpleNamespace(raw_message={'accountId':'second-account'})
            def __init__(self):
                self.extras={'companion_voice_reply':True}
                self.result=SimpleNamespace(chain=[Plain('完整文字')], is_llm_result=lambda:True, get_plain_text=lambda:'完整文字')
            def get_extra(self,key): return self.extras.get(key)
            def set_extra(self,key,value): self.extras[key]=value
            def get_result(self): return self.result
        package = '_speech_fixture'
        fake_package = types.ModuleType(package)
        fake_package.__path__ = []
        components = types.ModuleType('astrbot.api.message_components')
        components.Plain, components.Record = Plain, Record
        typed = types.ModuleType(package+'.typed_trigger')
        typed.strip_text_at=lambda text:text
        with patch.dict(sys.modules, {package:fake_package,'astrbot.api.message_components':components,
                package+'.typed_trigger':typed, package+'.tts':tts}):
            spec=importlib.util.spec_from_file_location(package+'.speech',Path(SPEC.origin).with_name('speech.py'))
            speech=importlib.util.module_from_spec(spec)
            spec.loader.exec_module(speech)
            settings={'voice':'hajimi','fish_api_key':'second-fish-fixture-key'}
            config={'platform':[{'account_id':'first-account','companion_speech':{'voice':'tide'}},
                {'account_id':'second-account','companion_speech':settings}]}
            support=speech.SpeechSupport(SimpleNamespace(get_config=lambda origin:config))
            event=Event()
            with patch.object(speech,'synthesize_voice',new=AsyncMock(return_value=wav())) as call:
                await support.synthesize(event,'测试')
                call.assert_awaited_once_with(settings,'','','测试')
            with patch.object(support,'synthesize',new=AsyncMock(side_effect=TimeoutError)):
                await support.decorate(event)
                self.assertEqual(event.result.chain[0].text,'完整文字')
                self.assertIn('先用文字回复',event.result.chain[-1].text)
                self.assertFalse(event.extras.get('companion_speech_output_ready',False))

    async def test_synthesis_cancels_on_timeout(self):
        started=asyncio.Event()
        cancelled=asyncio.Event()
        class Response:
            status_code=200
            async def __aenter__(self): return self
            async def __aexit__(self,*args): cancelled.set()
            async def aiter_bytes(self,size):
                started.set()
                await asyncio.Event().wait()
                yield b''
        class Client:
            def __init__(self,**kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
            def stream(self,*args,**kw): return Response()
        with patch.dict(sys.modules,{'httpx':types.SimpleNamespace(AsyncClient=Client)}):
            task=asyncio.create_task(tts.synthesize({},'https://deepseek.invalid','fixture','测试'))
            await started.wait()
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(task,timeout=0.02)
            self.assertTrue(cancelled.is_set())


if __name__ == '__main__':
    unittest.main()

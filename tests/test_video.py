"""Full timeline, tail inclusion, failure coverage and bounded concurrency."""
import asyncio
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock,patch
import wave

spec=importlib.util.spec_from_file_location('video_fixture',Path(__file__).resolve().parents[1]/'plugins/astrbot_plugin_personal_companion/video.py')
v=importlib.util.module_from_spec(spec);sys.modules[spec.name]=v;spec.loader.exec_module(v)

class PlanningTests(unittest.TestCase):
    def test_no_gaps_and_last_second_is_included(self):
        for duration in [7.5,119.99,120,120.01,230,250,3600,14400]:
            plan=v.segment_plan(duration)
            self.assertEqual(plan[0]['start'],0)
            self.assertEqual(plan[-1]['end'],duration)
            for before,after in zip(plan,plan[1:]):self.assertEqual(before['end'],after['start'])
            self.assertTrue(all(s['audio_end']-s['audio_start']<=120 for s in plan))
            self.assertTrue(all(s['audio_start']<=s['start'] and s['audio_end']>=s['end'] for s in plan))

    def test_unknown_or_unreasonable_duration_is_rejected_not_truncated(self):
        for duration in [0,-1,float('inf'),float('nan'),14401,'120']:
            with self.assertRaises(ValueError):v.segment_plan(duration)

    def test_note_groups_include_tail_instead_of_slicing_head(self):
        notes=[str(i)*2500 for i in range(25)]
        groups=v.text_groups(notes)
        self.assertEqual([n for g in groups for n in g],notes)
        self.assertEqual(groups[-1][-1],notes[-1])

class AnalysisTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name)

    def info(self,duration=250,audio=True):
        return {'source':'fixture.mp4','duration_seconds':duration,'has_audio':audio,'segments':v.segment_plan(duration)}

    def extract(self,source,segment,directory,has_audio):
        directory.mkdir(parents=True,exist_ok=True)
        audio=directory/'audio.wav'
        audio.write_bytes(str(segment['index']).encode())
        return {'frames':['base64://frame-'+str(segment['index'])], 'frame_times':[segment['start']+.5],
            'missed_frame_times':[], 'audio':str(audio) if has_audio else None,'audio_error':None}

    async def test_all_segments_transcribed_analyzed_and_tail_in_final_notes(self):
        seen=[];active=0;peak=0
        async def asr(path):
            nonlocal active,peak
            active+=1;peak=max(peak,active)
            index=Path(path).read_text()
            await asyncio.sleep(.005)
            active-=1;seen.append(index)
            return ['开头苹果','中间月亮','结尾火星'][int(index)]
        async def describe(frames,note):return note
        with patch.object(v,'probe_video',return_value=self.info()),patch.object(v,'extract_segment',side_effect=self.extract):
            result=await v.analyze_video(b'fixture','video.mp4',self.directory,asr,describe,AsyncMock())
        self.assertEqual(set(seen),{'0','1','2'})
        self.assertLessEqual(peak,2)
        self.assertIn('结尾火星',result['note'])
        self.assertEqual(result['coverage'][-1]['end'],250)
        self.assertEqual(result['failures'],[])
        self.assertEqual(result['frames'],[])
        self.assertEqual(list(self.directory.iterdir()),[])

    async def test_asr_failure_marks_middle_gap_and_still_processes_tail(self):
        async def asr(path):
            i=int(Path(path).read_text())
            if i==1:raise TimeoutError()
            return '结尾火星' if i==2 else '开头苹果'
        with patch.object(v,'probe_video',return_value=self.info()),patch.object(v,'extract_segment',side_effect=self.extract):
            result=await v.analyze_video(b'fixture','video.mp4',self.directory,asr,AsyncMock(side_effect=lambda frames,note:note))
        self.assertFalse(result['coverage'][1]['audio_ok'])
        self.assertTrue(result['coverage'][2]['audio_ok'])
        self.assertIn('结尾火星',result['note'])
        self.assertTrue(any('1:55–3:50' in x and '音频转写' in x for x in result['failures']))

    async def test_segment_model_failure_keeps_audio_evidence_and_tail(self):
        async def describe(frames,note):
            if '1:55–3:50' in note:raise TimeoutError()
            return note
        with patch.object(v,'probe_video',return_value=self.info()),patch.object(v,'extract_segment',side_effect=self.extract):
            result=await v.analyze_video(b'fixture','video.mp4',self.directory,AsyncMock(return_value='音频证据'),describe)
        self.assertFalse(result['coverage'][1]['analysis_ok'])
        self.assertEqual(result['coverage'][-1]['end'],250)
        self.assertIn('仅保留已取得资料',result['note'])

    async def test_short_video_has_full_audio_and_no_extra_model_round(self):
        describe=AsyncMock()
        with patch.object(v,'probe_video',return_value=self.info(119.9)),patch.object(v,'extract_segment',side_effect=self.extract):
            result=await v.analyze_video(b'fixture','video.mp4',self.directory,AsyncMock(return_value='结尾'),describe)
        describe.assert_not_called()
        self.assertEqual(result['segment_count'],1)
        self.assertTrue(result['frames'])
        self.assertIn('结尾',result['note'])
        self.assertEqual(result['coverage'][0]['end'],119.9)

    async def test_regular_attachment_uses_segment_summary_even_if_short(self):
        describe=AsyncMock(return_value='视觉和音频证据')
        with patch.object(v,'probe_video',return_value=self.info(20)),patch.object(v,'extract_segment',side_effect=self.extract):
            result=await v.analyze_video(b'fixture','video.mp4',self.directory,AsyncMock(return_value='转写'),describe,inline_frames=False)
        describe.assert_awaited_once()
        self.assertEqual(result['frames'],[])
        self.assertIn('视觉和音频证据',result['note'])

    async def test_recall_stops_further_external_processing(self):
        calls=0
        async def guard():
            nonlocal calls
            calls+=1
            if calls>=3:raise ValueError('已撤回')
        asr=AsyncMock(return_value='不得再转写')
        with patch.object(v,'probe_video',return_value=self.info()),patch.object(v,'extract_segment',side_effect=self.extract):
            with self.assertRaises(v.VideoSourceInvalid):
                await v.analyze_video(b'fixture','video.mp4',self.directory,asr,AsyncMock(),guard=guard)
        self.assertEqual(list(self.directory.iterdir()),[])

    async def test_hierarchical_reduction_receives_tail(self):
        notes=['段'+str(i)+':'+('x'*3000) for i in range(20)]
        notes[-1]+='TAIL_MARKER'
        seen=[]
        async def reduce(text):
            seen.append(text)
            return '合并笔记 '+('TAIL_MARKER' if 'TAIL_MARKER' in text else '前面段落')
        result=await v.compact_notes(notes,reduce,AsyncMock())
        self.assertIn('TAIL_MARKER',result)
        self.assertTrue(any('TAIL_MARKER' in text for text in seen))

    async def test_missing_reducer_never_silently_truncates(self):
        with self.assertRaisesRegex(ValueError,'未丢弃后半段'):
            await v.compact_notes(['x'*30000],None,AsyncMock())

    async def test_incomplete_visuals_are_reported(self):
        def extract(*args):
            result=self.extract(*args)
            result['missed_frame_times']=[100]
            return result
        with patch.object(v,'probe_video',return_value=self.info(115)),patch.object(v,'extract_segment',side_effect=extract):
            result=await v.analyze_video(b'fixture','video.mp4',self.directory,AsyncMock(return_value='音频'))
        self.assertFalse(result['coverage'][0]['frames_ok'])
        self.assertIn('画面抽取',result['failures'][0])

    async def test_real_extraction_uses_absolute_frame_and_audio_ranges(self):
        segment=v.segment_plan(250)[-1]
        commands=[]
        def run(command):
            commands.append(command)
            target=Path(command[-1])
            if target.suffix=='.jpg':target.write_bytes(b'jpeg')
            else:
                with wave.open(str(target),'wb') as output:
                    output.setnchannels(1);output.setsampwidth(2);output.setframerate(16000);output.writeframes(b'\0\0'*16000)
            return type('Result',(),{'returncode':0})()
        import types
        with patch.dict(sys.modules,{'imageio_ffmpeg':types.SimpleNamespace(get_ffmpeg_exe=lambda:'ffmpeg-fixture')}),patch.object(v,'runner',return_value=run):
            result=v.extract_segment('fixture.mp4',segment,self.directory,True)
        frame_times=[float(c[c.index('-ss')+1]) for c in commands if c[-1].endswith('.jpg')]
        self.assertTrue(all(230<t<250 for t in frame_times))
        self.assertGreater(max(frame_times),248)
        audio=commands[-1]
        self.assertEqual(float(audio[audio.index('-ss')+1]),229)
        self.assertEqual(float(audio[audio.index('-t')+1]),21)
        self.assertTrue(result['audio'])

if __name__=='__main__':unittest.main()

"""Video content presentation and bounded extraction retry regressions."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from plugins.astrbot_plugin_personal_companion.video_reply import content_only,asks_processing
from plugins.astrbot_plugin_personal_companion import platform_video_worker as worker

class PresentationTests(unittest.TestCase):
    def test_real_duration_intro_is_removed_and_content_kept(self):
        text='主人这份德军口粮我看完了，全片 22 分半没有缺段。纸盒是单兵口粮，里面有黑面包和炖菜。'
        self.assertEqual(content_only(text),'纸盒是单兵口粮，里面有黑面包和炖菜。')

    def test_real_frame_count_prologue_is_removed(self):
        text='主人，这个视频我看了全片，大概3分半，从头到尾都处理了，有12张抽样画面，不是逐帧。\n\n内容是这样的：卖的是五元盒饭。'
        result=content_only(text)
        for phrase in ['3分半','12张','从头到尾','不是逐帧','看了全片']:self.assertNotIn(phrase,result)
        self.assertIn('卖的是五元盒饭',result)

    def test_content_frame_rate_and_time_markers_are_kept(self):
        text='作者展示每秒60帧的实时生成。1:20之后开始讲架构，2:00给出结论。'
        self.assertEqual(content_only(text),text)
        self.assertEqual(content_only('大约3分钟后，主角离开现场。'),'大约3分钟后，主角离开现场。')

    def test_real_missing_intervals_and_uncertainty_remain(self):
        text='全片2分钟。1:00到1:20的音频转写失败，不能确认这一段说了什么。其余部分介绍步骤。'
        result=content_only(text)
        self.assertNotIn('全片2分钟',result)
        self.assertIn('转写失败',result);self.assertIn('不能确认',result);self.assertIn('其余部分',result)

    def test_explicit_processing_question_can_ask_for_statistics(self):
        text='总时长3分钟，有12张抽样画面。'
        for question in ['这个视频多长？','你怎么分析这个视频的？','抽了多少帧？']:
            self.assertTrue(asks_processing(question))
            self.assertEqual(content_only(text,question),text)
        self.assertFalse(asks_processing('MIT开源新作：60帧实时驾驶AI'))

    def test_mixed_sentence_keeps_substantive_clauses(self):
        result=content_only('视频时长 2 分钟，核心观点是降低延迟。基于12幅抽样画面，界面包含三个旋钮。')
        self.assertNotIn('视频时长',result);self.assertNotIn('抽样画面',result)
        self.assertIn('核心观点是降低延迟',result);self.assertIn('三个旋钮',result)

class RetryTests(unittest.TestCase):
    def test_temporary_no_stream_gets_one_fresh_attempt_and_cleans_own_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory);partial=path/'media.mp4.part';partial.write_bytes(b'partial')
            unrelated=path/'unrelated.txt';unrelated.write_text('keep')
            with patch.object(worker,'download',side_effect=[worker.PlatformVideoError('no_video'),{'ok':True}]) as download,patch.object(worker.time,'sleep'):
                result=worker.download_with_retry('https://b23.tv/fixture',path,'fixture-ffmpeg')
            self.assertEqual(result['attempts'],2);self.assertEqual(download.call_count,2)
            self.assertFalse(partial.exists());self.assertTrue(unrelated.exists())

    def test_permanent_errors_are_not_retried(self):
        for code in ['invalid_url','access_restricted','too_large','invalid_duration']:
            with tempfile.TemporaryDirectory() as directory,patch.object(worker,'download',side_effect=worker.PlatformVideoError(code)) as download,patch.object(worker.time,'sleep'):
                with self.assertRaisesRegex(worker.PlatformVideoError,code):worker.download_with_retry('fixture',directory,'fixture')
                self.assertEqual(download.call_count,1)

    def test_failure_stops_after_second_attempt(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(worker,'download',side_effect=worker.PlatformVideoError('no_video')) as download,patch.object(worker.time,'sleep') as sleep:
            with self.assertRaisesRegex(worker.PlatformVideoError,'no_video'):worker.download_with_retry('fixture',directory,'fixture')
            self.assertEqual(download.call_count,2);sleep.assert_called_once_with(1)

    def test_success_does_not_retry(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(worker,'download',return_value={'ok':True}) as download:
            self.assertEqual(worker.download_with_retry('fixture',directory,'fixture')['attempts'],1)
            self.assertEqual(download.call_count,1)

if __name__=='__main__':unittest.main()

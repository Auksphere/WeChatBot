"""Full-timeline video evidence, bounded workers and hierarchical notes.

No framework dependency: credentials and model calls remain with the caller.
"""
import asyncio
import base64
import math
from pathlib import Path
import re
import subprocess
import tempfile
import time
import wave

MAX_BYTES = 100 * 1024 * 1024
SEGMENT_SECONDS = 115
MAX_DURATION = 4 * 60 * 60
FRAME_COUNT = 6
NOTE_BUDGET = 24000
FORMATS = 'mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi'


class VideoSourceInvalid(ValueError):
    pass


def stamp(seconds):
    seconds = max(0, int(seconds))
    return f'{seconds // 3600}:{seconds // 60 % 60:02}:{seconds % 60:02}' if seconds >= 3600 else f'{seconds // 60}:{seconds % 60:02}'


def segment_plan(seconds):
    if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 < seconds <= MAX_DURATION:
        raise ValueError('无法确定完整时长，或视频超过4小时；请分段发送，不会只读取开头')
    size = seconds if seconds <= 120 else SEGMENT_SECONDS
    result = []
    for index in range(math.ceil(seconds / size)):
        start, end = index * size, min(seconds, (index + 1) * size)
        result.append({'index': index, 'start': start, 'end': end,
            'audio_start': max(0, start - 1), 'audio_end': min(seconds, end + 1)})
    return result


def runner(timeout):
    deadline = time.monotonic() + timeout
    def run(command):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError('本段视频解码超时')
        try:
            return subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=remaining, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except subprocess.TimeoutExpired:
            raise ValueError('本段视频解码超时') from None
    return run


def probe_video(data, name, directory):
    import imageio_ffmpeg
    if not isinstance(data, bytes) or not data or len(data) > MAX_BYTES:
        raise ValueError('视频为空或超过100MB，请压缩后发送')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    suffix = Path(name).suffix.lower()
    suffix = suffix if suffix in {'.mp4','.mov','.mkv','.avi','.webm','.m4v'} else '.mp4'
    source = directory / ('video' + suffix)
    source.write_bytes(data)
    executable = imageio_ffmpeg.get_ffmpeg_exe()
    result = runner(15)([executable, '-hide_banner', '-protocol_whitelist', 'file,pipe',
        '-format_whitelist', FORMATS, '-i', str(source)])
    duration = re.search(rb'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', result.stderr)
    if duration is None or b'Video:' not in result.stderr:
        raise ValueError('无法确认视频的完整时长或视频轨道，未截取开头冒充全片')
    seconds = sum(float(v) * scale for v, scale in zip(duration.groups(), (3600,60,1)))
    plan = segment_plan(seconds)
    return {'source': str(source), 'duration_seconds': seconds,
        'has_audio': b'Audio:' in result.stderr, 'segments': plan}


def extract_segment(source, segment, directory, has_audio, timeout=45):
    import imageio_ffmpeg
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    executable = imageio_ffmpeg.get_ffmpeg_exe()
    run = runner(timeout)
    frames, times, missed = [], [], []
    start, end = segment['start'], segment['end']
    for i in range(FRAME_COUNT):
        # Each segment is sampled across its own whole interval, including the tail.
        offset = start + (i + .5) * (end - start) / FRAME_COUNT
        target = directory / f'frame-{i}.jpg'
        try:
            result = run([executable, '-hide_banner', '-loglevel', 'error', '-threads', '2', '-ss', str(offset),
                '-protocol_whitelist', 'file,pipe', '-format_whitelist', FORMATS, '-i', str(source),
                '-frames:v', '1', '-vf', 'scale=640:640:force_original_aspect_ratio=decrease', '-q:v', '4', '-y', str(target)])
            if result.returncode == 0 and target.exists() and target.stat().st_size:
                frames.append('base64://' + base64.b64encode(target.read_bytes()).decode('ascii'))
                times.append(round(offset, 3))
            else:
                missed.append(round(offset, 3))
        except ValueError:
            missed.extend(round(start + (j + .5) * (end - start) / FRAME_COUNT, 3) for j in range(i, FRAME_COUNT))
            break
    audio, audio_error = None, None
    audio_start, audio_end = segment['audio_start'], segment['audio_end']
    if has_audio:
        target = directory / 'audio.wav'
        try:
            result = run([executable, '-hide_banner', '-loglevel', 'error', '-threads', '2', '-ss', str(audio_start),
                '-protocol_whitelist', 'file,pipe', '-format_whitelist', FORMATS, '-i', str(source),
                '-t', str(audio_end-audio_start), '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', '-y', str(target)])
            if result.returncode != 0 or not target.exists():
                raise ValueError('本段音轨提取失败')
            with wave.open(str(target), 'rb') as stream:
                duration = stream.getnframes() / stream.getframerate()
            if duration > 120.1:
                raise ValueError('本段音轨超过识别时长限制')
            if duration:
                audio = str(target)
        except (ValueError, wave.Error, EOFError):
            audio_error = '本段音轨提取失败或超时'
    return {'frames': frames, 'frame_times': times, 'missed_frame_times': missed,
        'audio': audio, 'audio_error': audio_error}


async def local_call(function, *args):
    # Do not remove temporary files while a cancelled FFmpeg worker still uses them.
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise


def text_groups(notes, limit=12000):
    groups, current, size = [], [], 0
    for note in notes:
        if current and (len(current) >= 8 or size + len(note) > limit):
            groups.append(current); current, size = [], 0
        current.append(note); size += len(note)
    if current:
        groups.append(current)
    return groups


async def compact_notes(notes, reduce_notes, check):
    for _ in range(4):
        if sum(map(len, notes)) <= NOTE_BUDGET:
            return '\n\n'.join(notes)
        if reduce_notes is None:
            raise ValueError('全片资料已取得，但缺少长视频汇总服务，未丢弃后半段')
        compressed = []
        for group in text_groups(notes):
            await check()
            result = await asyncio.wait_for(reduce_notes('\n\n'.join(group)), timeout=60)
            if not isinstance(result, str) or not result.strip():
                raise ValueError('全片分段资料汇总失败，请稍后重试')
            compressed.append(result.strip())
        if sum(map(len, compressed)) >= sum(map(len, notes)):
            raise ValueError('长视频笔记未能压缩到汇总范围，未截断后半段')
        notes = compressed
    raise ValueError('视频汇总资料过长，请将视频分段发送')


async def analyze_video(data, name, directory, transcribe, describe=None, reduce_notes=None,
                        *, guard=None, progress=None, inline_frames=True):
    async def check():
        if guard is not None:
            try:
                await guard()
            except Exception:
                raise VideoSourceInvalid('原视频或提问已撤回、改变或不再属于当前会话，已停止处理') from None
    async def notify(done, total, seconds, stage):
        if progress is not None:
            await progress(done, total, seconds, stage)
    await check()
    with tempfile.TemporaryDirectory(prefix='full-video-', dir=directory) as folder:
        info = await local_call(probe_video, data, name, Path(folder))
        plan, total = info['segments'], len(info['segments'])
        long = total > 1
        if (long or not inline_frames) and describe is None:
            raise ValueError('缺少逐段视频分析服务，未只读取前两分钟')
        if long:
            await notify(0, total, info['duration_seconds'], 'segments')
        gate = asyncio.Semaphore(2)
        done, results = 0, []
        async def one(segment):
            nonlocal done
            async with gate:
                await check()
                label = stamp(segment['start']) + '–' + stamp(segment['end'])
                try:
                    clip = await local_call(extract_segment, info['source'], segment,
                        Path(folder) / f'segment-{segment["index"]}', info['has_audio'])
                except Exception as exc:
                    clip = {'frames': [], 'frame_times': [], 'missed_frame_times': ['解码失败'],
                        'audio': None, 'audio_error': type(exc).__name__}
                transcript = ''
                audio_ok = not info['has_audio']
                if not info['has_audio']:
                    audio_note = '原视频没有音轨。'
                elif clip['audio_error']:
                    audio_note = clip['audio_error'] + '，不能猜测本段话语。'
                elif clip['audio']:
                    try:
                        await check()
                        transcript = await asyncio.wait_for(transcribe(clip['audio']), timeout=45)
                        if not isinstance(transcript, str):
                            raise ValueError('音频转写结果无效')
                        audio_note = (f'音频范围{stamp(segment["audio_start"])}–{stamp(segment["audio_end"])}（边界含约1秒重叠），音频转写：'
                            + (transcript.strip() or '未识别到清晰语言。'))
                        audio_ok = True
                    except VideoSourceInvalid:
                        raise
                    except Exception:
                        audio_note = '本段音频转写失败或超时，不能猜测本段话语。'
                else:
                    audio_note = '本段没有可提取的音频采样。'
                    audio_ok = True
                evidence = f'时间段：{label}\n实际抽帧时间（秒）：{clip["frame_times"]}\n{audio_note}'
                if clip['missed_frame_times']:
                    evidence += '\n未成功取得的画面位置：' + str(clip['missed_frame_times'])
                analysis_ok = True
                if long or not inline_frames:
                    try:
                        if not clip['frames'] and not transcript.strip():
                            raise ValueError('没有取得本段资料')
                        await check()
                        summary = await asyncio.wait_for(describe(clip['frames'], evidence), timeout=75)
                        if not isinstance(summary, str) or not summary.strip():
                            raise ValueError('本段分析为空')
                        note = f'【{label}】\n{summary.strip()}'
                    except VideoSourceInvalid:
                        raise
                    except Exception:
                        analysis_ok = False
                        note = f'【{label}，分段模型分析失败；仅保留已取得资料】\n{evidence}'
                else:
                    note = evidence
                done += 1
                if long and done < total:
                    await notify(done, total, info['duration_seconds'], 'segments')
                return {'segment': segment, 'label': label, 'note': note, 'audio_ok': audio_ok,
                    'frames_ok': bool(clip['frames']) and not clip['missed_frame_times'],
                    'analysis_ok': analysis_ok, 'frame_count': len(clip['frames']),
                    'frames': clip['frames'] if not long and inline_frames else []}
        tasks = [asyncio.create_task(one(segment)) for segment in plan]
        try:
            results = await asyncio.gather(*tasks)
            await check()
            if long:
                await notify(total, total, info['duration_seconds'], 'reduce')
            notes = await compact_notes([r['note'] for r in results], reduce_notes, check)
            await check()
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        failures = [r['label'] + '（' + '、'.join(name for name, ok in
            [('画面抽取',r['frames_ok']),('音频转写',r['audio_ok']),('分段分析',r['analysis_ok'])] if not ok) + '未完成）'
            for r in results if not all(r[k] for k in ['frames_ok','audio_ok','analysis_ok'])]
        frames = [frame for r in results for frame in r['frames']]
        header = (f'实际视频总长约{info["duration_seconds"]:.2f}秒；处理范围0:00–{stamp(info["duration_seconds"])}，共{total}段。'
            f'\n全片时间轴均已分段处理，取得{sum(r["frame_count"] for r in results)}幅抽样画面；不是逐帧观看。'
            f'\n音频转写完成{sum(r["audio_ok"] for r in results)}/{total}段；分段分析完成{sum(r["analysis_ok"] for r in results)}/{total}段。')
        if not info['has_audio']:
            header += '\n原视频没有音轨，音频指标表示无需转写。'
        header += '\n缺失区间：' + ('；'.join(failures) if failures else '无已知处理失败区间。画面始终为抽样，不保证捕获所有瞬间。')
        return {'frames': frames, 'note': header + '\n\n' + notes, 'long_video': long,
            'duration_seconds': info['duration_seconds'], 'segment_count': total, 'failures': failures,
            'coverage': [{'start': r['segment']['start'], 'end': r['segment']['end'],
                'audio_ok':r['audio_ok'],'frames_ok':r['frames_ok'],'analysis_ok':r['analysis_ok']} for r in results]}

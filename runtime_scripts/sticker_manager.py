"""Synchronize only the account's already-added sticker packs; never send chats."""
import asyncio
import hashlib
import io
import json
import os
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent


@contextmanager
def library_lock(path):
    import msvcrt
    with open(path, 'a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError('已有另一个表情同步任务运行中') from None
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def atomic_json(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


async def operate(client, path, state, action, submit, *, pack_id=None, saved_id=None,
                  job_kind=None, wait_seconds=45):
    row = state['operations'].get(action)
    if row and row.get('state') in {'available', 'failed', 'cancelled', 'expired', 'unsupported'}:
        row = None
    if row is None:
        row = {'key': 'rolebot-sticker-library-' + uuid.uuid4().hex, 'state': 'submitting'}
        state['operations'][action] = row
        atomic_json(path, state)
        try:
            job = await submit(row['key'])
        except Exception as exc:
            code = getattr(exc, 'status', 0)
            row['state'] = 'failed' if code and 400 <= code < 500 and code not in {408, 409, 429} else 'needs_review'
            atomic_json(path, state)
            return False, row['state']
        if not isinstance(job, dict) or not isinstance(job.get('id'), str):
            row['state'] = 'needs_review'
            atomic_json(path, state)
            return False, row['state']
        row['job_id'] = job['id']
        atomic_json(path, state)
    elif row.get('job_id'):
        job = await client.job(row['job_id'])
    else:
        return False, 'needs_review'

    expected_kind = job_kind or ('download' if saved_id else 'sticker_acquire' if pack_id else 'sticker_pack')
    async def inspect():
        nonlocal job
        if (job.get('id') != row['job_id'] or job.get('accountId') != state['account']
                or job.get('kind') != expected_kind):
            raise ValueError('invalid_library_receipt')
        row['state'] = job.get('state', 'needs_review')
        atomic_json(path, state)
        return row['state']

    try:
        async with asyncio.timeout(wait_seconds):
            while await inspect() in {'queued', 'running'}:
                await asyncio.sleep(1)
                job = await client.job(row['job_id'])
    except TimeoutError:
        return False, 'pending'
    except Exception:
        row['state'] = 'needs_review'
        atomic_json(path, state)
        return False, 'needs_review'
    payload = job.get('result') or {}
    complete = row['state'] == 'available' and job.get('phase') == 'completed'
    if job_kind == 'sticker_prepare':
        detail = await client._request('GET', client.prefix + '/sticker-preparations/' + row['job_id'])
        original = (detail.get('result') or {}).get('original') or {}
        complete = (complete and original.get('accountId') == state['account']
                    and original.get('state') == 'available' and original.get('contentType') == 'image/gif')
    elif saved_id:
        detail = await client.custom_sticker(saved_id)
        original = detail.get('original') or {}
        complete = (complete and detail.get('id') == saved_id and original.get('kind') == 'sticker'
                    and original.get('state') == 'available' and original.get('accountId') == state['account'])
    else:
        complete = (complete and payload.get('serverAcceptance') in {'accepted', 'reconciled_noop'}
                    and payload.get('profileWriteback') == 'complete')
    if pack_id:
        complete = complete and payload.get('packId') == pack_id and payload.get('contentReadiness') == 'available'
    if not complete and row['state'] == 'available':
        row['state'] = 'needs_review'
        atomic_json(path, state)
    return complete, row['state']


def compatible_gif(data):
    from PIL import Image, ImageSequence
    frames = []
    durations = []
    with Image.open(io.BytesIO(data)) as image:
        if image.width * image.height > 16_000_000 or getattr(image, 'n_frames', 1) > 200:
            raise ValueError('sticker_too_large')
        for frame in ImageSequence.Iterator(image):
            converted = frame.convert('RGBA')
            converted.thumbnail((1024, 1024))
            frames.append(converted)
            durations.append(max(20, int(frame.info.get('duration') or 100)))
    if len(frames) > 1 and sum(durations) > 10000:
        raise ValueError('sticker_animation_too_long')
    output = io.BytesIO()
    frames[0].save(output, format='GIF', save_all=len(frames) > 1, append_images=frames[1:],
                   duration=durations if len(frames) > 1 else durations[0], loop=0, disposal=2)
    result = output.getvalue()
    if len(result) > 1024 * 1024:
        raise ValueError('sticker_gif_too_large')
    return result


async def ensure_gif_copy(client, path, state, item):
    """Keep originals; add one idempotent GIF copy when the original is not GIF."""
    import aiohttp
    sid = item['id']
    detail = await client.custom_sticker(sid)
    original = detail.get('original') or {}
    if original.get('contentType') == 'image/gif':
        return True
    copies = state.setdefault('gif_copies', {})
    row = copies.get(sid)
    if row and row.get('saved'):
        return True
    data = await client.sticker_content(original, original=True)
    gif = await asyncio.to_thread(compatible_gif, data)
    gid = hashlib.md5(gif).hexdigest()
    row = copies.setdefault(sid, {'gif_id': gid})
    if row['gif_id'] != gid:
        raise ValueError('conversion_identity_changed')
    atomic_json(path, state)
    if not row.get('upload_id'):
        form = aiohttp.FormData()
        form.add_field('file', gif, filename=gid + '.gif', content_type='image/gif')
        upload = await client._request('POST', client.prefix + '/uploads', data=form)
        if upload.get('byteLength') != len(gif) or upload.get('sha256') != hashlib.sha256(gif).hexdigest():
            raise ValueError('upload_identity_mismatch')
        row['upload_id'] = upload['id']
        atomic_json(path, state)
    async def prepare(key):
        return await client._request('POST', client.prefix + '/sticker-preparations',
            json={'sourceId': row['upload_id']}, headers={'Idempotency-Key': key}, expected_status=202)
    okay, status = await operate(client, path, state, 'prepare:' + sid, prepare, job_kind='sticker_prepare')
    if not okay:
        print('兼容副本准备尚未完成：' + status, flush=True)
        return False
    preparation = state['operations']['prepare:' + sid]['job_id']
    async def save(key):
        return await client._request('POST', client.prefix + '/custom-stickers',
            json={'source': {'kind': 'prepared', 'preparationId': preparation}},
            headers={'Idempotency-Key': key}, expected_status=202)
    okay, status = await operate(client, path, state, 'save-gif:' + sid, save, job_kind='sticker_saved')
    if not okay:
        print('兼容副本保存尚未完成：' + status, flush=True)
        return False
    saved = await client.custom_sticker(gid)
    asset = saved.get('original') or {}
    if saved.get('id') != gid or asset.get('state') != 'available' or asset.get('contentType') != 'image/gif':
        raise ValueError('gif_copy_not_ready')
    row['saved'] = True
    atomic_json(path, state)
    # Release only this manager's completed upload; retain it if still referenced.
    try:await client._request('DELETE', client.prefix + '/uploads/' + row['upload_id'])
    except Exception:pass
    return True


async def synchronize(client, account, path):
    state = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'account': account, 'operations': {}}
    if state.get('account') != account:
        raise ValueError('account_mismatch')
    print('正在同步手机微信已添加的表情库……', flush=True)
    okay, status = await operate(client, path, state, 'sync', client.sync_stickers)
    if not okay:
        print('同步尚未完成：' + status + '。稍后再次选择同步，将核对原任务。', flush=True)
        return False
    saved = (await client.custom_stickers()).get('items', [])
    derived = {r['gif_id'] for r in state.get('gif_copies', {}).values()}
    saved = [item for item in saved if item.get('id') not in derived]
    for index, item in enumerate(saved):
        saved_id = item['id']
        original = (await client.custom_sticker(saved_id)).get('original') or {}
        if original.get('state') != 'available':
            print(f'正在缓存收藏表情 {index + 1}/{len(saved)}……', flush=True)
            async def download_saved(key):
                return await client._request('POST', client.prefix + '/sticker-downloads',
                    json={'stickerId': saved_id}, headers={'Idempotency-Key': key}, expected_status=202)
            okay, status = await operate(client, path, state, 'saved:' + saved_id, download_saved, saved_id=saved_id)
            if not okay:
                print('收藏表情缓存尚未完成：' + status + '。下次同步时核对原任务。', flush=True)
                return False
        print(f'正在检查发送格式 {index + 1}/{len(saved)}（原图保留，必要时生成 GIF 副本）……', flush=True)
        if not await ensure_gif_copy(client, path, state, item):
            return False
    packs = (await client.sticker_packs()).get('items', [])
    packs = [p for p in packs if p.get('membership') == 'added']
    for pack in packs:
        if pack.get('contentReadiness') == 'available':
            continue
        pack_id = pack['id']
        print('正在缓存表情包：' + str(pack.get('name') or '未命名表情包'), flush=True)
        async def download(key):
            return await client.download_sticker_pack(pack_id, key)
        okay, status = await operate(client, path, state, 'download:' + pack_id, download, pack_id=pack_id)
        if not okay:
            print('该包尚未完成：' + status + '。保留原任务，下次同步时核对。', flush=True)
            return False
    await show_catalog(client)
    return True


async def show_catalog(client):
    saved = (await client.custom_stickers()).get('items', [])
    packs = (await client.sticker_packs()).get('items', [])
    count = 0
    saved_ready = 0
    for item in saved:
        original = (await client.custom_sticker(item['id'])).get('original') or {}
        saved_ready += original.get('state') == 'available' and original.get('contentType') == 'image/gif'
    for pack in packs:
        if pack.get('membership') != 'added':
            continue
        items = (await client.pack_stickers(pack['id'])).get('items', [])
        ready = sum(isinstance(i.get('original'), dict) and i['original'].get('state') == 'available'
                    and i['original'].get('contentType') == 'image/gif' for i in items)
        count += ready
        print(str(pack.get('name') or '未命名表情包') + '：本地 GIF ' + str(ready) + ' 个', flush=True)
    print(f'收藏表情 {len(saved)} 个（可用 GIF {saved_ready} 个），已添加官方包 {len(packs)} 个，本地包内 GIF {count} 个。', flush=True)
    if not saved and not packs:
        print('请先在手机上用机器人微信号添加表情包，再同步。', flush=True)
    return {'saved': len(saved), 'packs': len(packs), 'ready_saved_gifs': saved_ready, 'ready_pack_gifs': count}


async def action(sync=True):
    os.environ.setdefault('ASTRBOT_ROOT', str(ROOT))
    plugin_path = str(ROOT / 'data/plugins')
    if plugin_path not in sys.path:
        sys.path.insert(0, plugin_path)
    from astrbot_plugin_wechat_native.config import NativeConfig
    from astrbot_plugin_wechat_native.native_client import NativeClient
    config = json.loads((ROOT / 'data/cmd_config.json').read_text(encoding='utf-8-sig'))
    native = NativeConfig.from_dict(next(p for p in config['platform'] if p['id'] == 'wechat-rolebot'))
    async with NativeClient(native.api_url, native.account_id, native.load_token()) as client:
        if sync:
            with library_lock(ROOT / 'data/sticker-library-manager.lock'):
                return await synchronize(client, native.account_id, ROOT / 'data/sticker-library-manager.json')
        return await show_catalog(client)


def contextual_settings():
    path = ROOT / 'data/plugin_data/astrbot_plugin_personal_companion/auto-stickers.json'
    if not path.exists():
        print('自动配表情尚未配置。')
        return
    while True:
        value = json.loads(path.read_text(encoding='utf-8'))
        print('\n普通聊天自动配表情：' + ('已开启' if value.get('enabled') else '已关闭') +
              '；同群间隔至少 ' + str(value.get('cooldown_seconds', 30)) + ' 秒。')
        choice = input('1 开启；2 关闭；3 修改间隔；0 返回：').strip()
        if choice == '0':
            return
        if choice not in {'1', '2', '3'}:
            continue
        cooldown = None
        if choice == '3':
            text = input('请输入间隔秒数（30–3600）：').strip()
            if not text.isdigit() or not 30 <= int(text) <= 3600:
                print('请输入30到3600之间的整数。')
                continue
            cooldown = int(text)
        with library_lock(ROOT / 'data/sticker-library-manager.lock'):
            value = json.loads(path.read_text(encoding='utf-8'))
            if cooldown is not None:
                value['cooldown_seconds'] = cooldown
            else:
                value['enabled'] = choice == '1'
            atomic_json(path, value)
        print('设置已保存，下一条聊天生效。')


def run_menu():
    while True:
        print('\n表情库管理：1 同步并缓存手机已添加的表情包；2 查看表情库；3 设置聊天自动配表情；0 返回')
        try:
            choice = input('请选择：').strip()
            if choice == '0':
                return
            if choice in {'1', '2'}:
                asyncio.run(action(sync=choice == '1'))
            elif choice == '3':
                contextual_settings()
        except (EOFError, KeyboardInterrupt):
            return
        except Exception as exc:
            print('操作未完成（' + type(exc).__name__ + '）。请确认机器人微信服务已启动并已登录。')


if __name__ == '__main__':
    if '--sync' in sys.argv:
        raise SystemExit(0 if asyncio.run(action()) else 1)
    run_menu()

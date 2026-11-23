"""Start, stop and inspect this isolated local test deployment."""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent
PRIVATE = ROOT / 'native-state'
STATE = ROOT / 'processes.json'
NATIVE = ROOT / 'native' / 'wx.exe'
PYTHON = ROOT / '.venv' / 'Scripts' / 'python.exe'
WX = [str(NATIVE), '--data-dir', str(PRIVATE), '--server-config', str(PRIVATE / 'server.toml'), '--config', str(PRIVATE / 'client.toml'),
      '--listen', '127.0.0.1:6174']


def owned_astrbot_processes():
    """Windows venv launchers may exit while their base-Python child keeps running."""
    owner = psutil.Process().username()
    executables = {PYTHON.resolve(), Path(sys._base_executable).resolve()}
    result = []
    for process in psutil.process_iter(['name']):
        if str(process.info['name']).lower() not in {'python.exe', 'pythonw.exe'}:
            continue
        try:
            args = process.cmdline()
            if (len(args) < 4 or args[1:4] != ['-m', 'astrbot.cli', 'run']
                    or process.username() != owner or Path(process.exe()).resolve() not in executables):
                continue
            configured_root = process.environ().get('ASTRBOT_ROOT')
            if (configured_root and Path(configured_root).resolve() == ROOT
                    and Path(process.cwd()).resolve() == ROOT):
                result.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    return result


def native_environment():
    """Make this project's media encoder available only to its child processes."""
    env = os.environ.copy()
    env['PATH'] = str(ROOT / 'native') + os.pathsep + env.get('PATH', '')
    temporary = ROOT / 'data/temp'
    cache = ROOT / 'data/cache'
    temporary.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    env.update(TEMP=str(temporary), TMP=str(temporary), TMPDIR=str(temporary),
               XDG_CACHE_HOME=str(cache), PIP_CACHE_DIR=str(cache / 'pip'),
               UV_CACHE_DIR=str(cache / 'uv'), HF_HOME=str(cache / 'huggingface'),
               MPLCONFIGDIR=str(cache / 'matplotlib'), IMAGEIO_USERDIR=str(cache / 'imageio'),
               IMAGEIO_FFMPEG_EXE=str(ROOT / 'native/ffmpeg.exe'))
    return env


def main():
    """Run one local deployment management operation."""
    action = sys.argv[1] if len(sys.argv) > 1 else 'status'
    state = json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {}
    if action == 'open':
        webbrowser.open('http://127.0.0.1:6185')
        return
    if action == 'login':
        command = WX + ['--server', 'http://127.0.0.1:6174', '--token-file', str(PRIVATE / 'api-token'),
                        '--account', 'rolebot', 'auth', 'login', '--qr-output', str(ROOT / '微信登录.png')]
        subprocess.run(command, cwd=ROOT, check=True)
        return
    if action == 'groups':
        subprocess.run(WX + ['--server', 'http://127.0.0.1:6174', '--token-file', str(PRIVATE / 'api-token'),
                             '--account', 'rolebot', 'groups', 'list'], cwd=ROOT, check=True)
        return
    if action == 'stop':
        processes = owned_astrbot_processes()
        for process in reversed(processes):
            try: process.terminate()
            except psutil.NoSuchProcess: pass
        _, remaining = psutil.wait_procs(processes, timeout=15)
        for process in remaining:
            # Recheck ownership before terminating a process that survived shutdown.
            if process.pid in {p.pid for p in owned_astrbot_processes()}:
                process.kill()
        psutil.wait_procs(remaining, timeout=5)
        if owned_astrbot_processes():
            raise SystemExit('This deployment has not stopped; its process record was retained.')
        result = subprocess.run(WX + ['daemon', 'stop'], cwd=ROOT, capture_output=True)
        if result.returncode:
            print('Native daemon stop failed. Inspect logs before retrying.')
            raise SystemExit(1)
        STATE.write_text('{}', encoding='utf-8')
        print('Stopped this test deployment. Login and conversations are retained.')
        return
    if action == 'start':
        with (ROOT / 'logs' / 'native-control.log').open('ab') as native_log:
            result = subprocess.run(WX + ['daemon', 'start'], cwd=ROOT, env=native_environment(), stdout=native_log, stderr=native_log,
                                    timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise SystemExit('Native daemon did not start. Check logs/native-control.log.')
        owned = owned_astrbot_processes()
        running = bool(owned)
        if owned:
            process = max(owned, key=lambda p: p.create_time())
            state['astrbot'] = {'pid': process.pid, 'created': process.create_time()}
            STATE.write_text(json.dumps(state, indent=2), encoding='utf-8')
        if not running:
            with socket.socket() as connection:
                if connection.connect_ex(('127.0.0.1', 6185)) == 0:
                    raise SystemExit('Port 6185 is already occupied. No existing process was changed.')
            env = native_environment()
            env.update(ASTRBOT_ROOT=str(ROOT), PYTHONUTF8='1', DASHBOARD_HOST='127.0.0.1',
                       XDG_CONFIG_HOME=str(ROOT / 'local-config'), XDG_DATA_HOME=str(ROOT / 'local-data'))
            (ROOT / 'logs').mkdir(exist_ok=True)
            with (ROOT / 'logs' / 'astrbot.out.log').open('ab') as stdout, (ROOT / 'logs' / 'astrbot.err.log').open('ab') as stderr:
                process = subprocess.Popen([str(PYTHON), '-m', 'astrbot.cli', 'run'], cwd=ROOT,
                                           env=env, stdout=stdout, stderr=stderr,
                                           creationflags=subprocess.CREATE_NO_WINDOW)
            state['astrbot'] = {'pid': process.pid, 'created': psutil.Process(process.pid).create_time()}
            STATE.write_text(json.dumps(state, indent=2), encoding='utf-8')
        for _ in range(45):
            try:
                with urllib.request.urlopen('http://127.0.0.1:6185/api/auth/setup-status', timeout=2) as response:
                    if response.status == 200:
                        print('AstrBot ready: http://127.0.0.1:6185')
                        return
            except (urllib.error.URLError, TimeoutError):
                time.sleep(1)
        raise SystemExit('Startup has not completed. Check logs/astrbot.err.log and astrbot.out.log.')
    if action != 'status':
        raise SystemExit('Use start, stop, status, open, login or groups.')
    for label, url in [('AstrBot', 'http://127.0.0.1:6185/api/auth/setup-status'),
                       ('WeChat Native', 'http://127.0.0.1:6174/v1/status')]:
        headers = {}
        if label == 'WeChat Native':
            headers['Authorization'] = 'Bearer ' + (PRIVATE / 'api-token').read_text(encoding='utf-8').strip()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=3) as response:
                print(f'{label}: responding ({response.status})')
        except (urllib.error.URLError, TimeoutError):
            print(f'{label}: unavailable')


if __name__ == '__main__':
    main()

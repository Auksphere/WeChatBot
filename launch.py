"""Start only this manager if necessary, then open an authenticated local tab."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.request import urlopen
from urllib.error import URLError
import webbrowser

HERE = Path(__file__).resolve().parent


def launch(root, port=6186, open_browser=True):
    root = Path(root).resolve()
    expected = hashlib.sha256(str(root).encode()).hexdigest()[:16]
    base = 'http://127.0.0.1:' + str(port)
    def health():
        try:
            with urlopen(base + '/health', timeout=1) as response:
                result = json.load(response)
            if result.get('app') != 'wechat-rolebot-console' or result.get('root_id') != expected:
                raise RuntimeError('管理端口已被其他程序占用，未更改任何进程。')
            return True
        except (URLError, TimeoutError, OSError):
            return False
    if not health():
        private = root / 'data/local-manager'
        private.mkdir(parents=True, exist_ok=True)
        temporary = root / 'data/temp'
        cache = root / 'data/cache'
        temporary.mkdir(parents=True, exist_ok=True)
        cache.mkdir(parents=True, exist_ok=True)
        python = root / '.venv/Scripts/pythonw.exe'
        if not python.exists():
            python = root / '.venv/Scripts/python.exe'
        with (private / 'manager.log').open('ab') as log:
            subprocess.Popen([str(python), '-m', 'manager_app.server', '--root', str(root), '--port', str(port)],
                cwd=HERE, stdout=log, stderr=log,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                env={**os.environ, 'PYTHONUTF8': '1', 'ASTRBOT_ROOT': str(root), 'TEMP': str(temporary), 'TMP': str(temporary), 'TMPDIR': str(temporary), 'XDG_CACHE_HOME': str(cache)})
        for _ in range(50):
            if health():
                break
            time.sleep(.2)
        else:
            raise RuntimeError('管理台未启动，请检查部署目录 data/local-manager/manager.log。')
    if open_browser:
        token = (root / 'data/local-manager/access-token').read_text(encoding='ascii').strip()
        webbrowser.open(base + '/#token=' + token)
    return base


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root')
    parser.add_argument('--port', type=int, default=6186)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    local = HERE / 'local.config.json'
    root = args.root or (json.loads(local.read_text(encoding='utf-8-sig')).get('deployment_root') if local.exists() else None)
    if not root:
        raise SystemExit('请先运行 setup.ps1 -DeploymentRoot <机器人部署目录>。')
    launch(root, args.port, not args.no_browser)


if __name__ == '__main__':
    main()

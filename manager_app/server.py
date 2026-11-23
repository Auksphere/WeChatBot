"""Loopback-only HTTP server with local-launch authentication and CSRF checks."""
from __future__ import annotations

import argparse
import hashlib
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import parse_qs, urlsplit

from . import VERSION
from .service import Service, UserError


class ManagerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, service):
        if address[0] != '127.0.0.1':
            raise ValueError('The manager only binds to 127.0.0.1')
        self.service = service
        self.sessions = {}
        self.sessions_lock = threading.Lock()
        self.session_file = service.private / 'sessions.json'
        try:
            saved = json.loads(self.session_file.read_text(encoding='utf-8'))
            self.sessions = {k: v for k, v in saved.items() if isinstance(k, str) and isinstance(v, dict)
                and isinstance(v.get('csrf'), str) and isinstance(v.get('expires'), (int, float)) and v['expires'] > time.time()}
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        self.root_id = hashlib.sha256(str(service.root).encode()).hexdigest()[:16]
        token_path = service.private / 'access-token'
        if not token_path.exists():
            token_path.write_text(secrets.token_urlsafe(48), encoding='ascii')
        self.launch_token = token_path.read_text(encoding='ascii').strip()
        if len(self.launch_token) < 48:
            raise ValueError('Invalid local access token')
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = 'LocalRolebotManager'

    def log_message(self, *_args):
        pass  # No URL, authentication token, query or request-body logging.

    def respond(self, status, data, mime='application/json; charset=utf-8', headers=None):
        # Drain bounded POST bodies even on rejected requests. Closing a Windows
        # socket with unread JSON can reset it before the client sees the 403.
        if self.command == 'POST' and not hasattr(self, '_request_body'):
            try:
                self.read_request_body()
            except (UserError, OSError):
                pass
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; media-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def check_host(self):
        return self.client_address[0] == '127.0.0.1' and self.headers.get('Host') == '127.0.0.1:' + str(self.server.server_port)

    def check_origin(self):
        origin = self.headers.get('Origin')
        return origin is None or origin == 'http://127.0.0.1:' + str(self.server.server_port)

    def session(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
            identifier = cookie['rolebot_manager'].value
        except (KeyError, ValueError):
            return None
        with self.server.sessions_lock:
            record = self.server.sessions.get(identifier)
            return record if record and record['expires'] > time.time() else None

    def read_request_body(self):
        if hasattr(self, '_request_body'):
            return self._request_body
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 262144:
                raise ValueError()
            self.connection.settimeout(5)
            self._request_body = self.rfile.read(length)
            return self._request_body
        except ValueError:
            raise UserError('请求数据无效或过大。') from None

    def body(self):
        if not self.headers.get('Content-Type', '').startswith('application/json'):
            raise UserError('请求需要使用 JSON。')
        try:
            value = json.loads(self.read_request_body())
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeError):
            raise UserError('请求数据无效或过大。') from None

    def do_GET(self):
        self.dispatch('GET')

    def do_POST(self):
        self.dispatch('POST')

    def dispatch(self, method):
        if not self.check_host() or not self.check_origin():
            self.respond(403, {'error': '仅允许本机管理台访问。'})
            return
        try:
            self.route(method)
        except UserError as exc:
            self.respond(400, {'error': str(exc)})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception:
            try:
                self.respond(500, {'error': '读取或操作失败，请查看机器人状态及日志。'})
            except OSError:
                pass

    def route(self, method):
        url = urlsplit(self.path)
        path = url.path
        service = self.server.service
        if method == 'GET' and path == '/health':
            self.respond(200, {'app': 'wechat-rolebot-console', 'version': VERSION, 'root_id': self.server.root_id})
            return
        static = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
            '/style.css': ('style.css', 'text/css; charset=utf-8'), '/fish.svg': ('fish.svg', 'image/svg+xml')}
        if method == 'GET' and path in static:
            name, mime = static[path]
            self.respond(200, (Path(__file__).parent / 'static' / name).read_bytes(), mime)
            return
        if method == 'POST' and path == '/api/session':
            body = self.body()
            token = body.get('token')
            if not isinstance(token, str) or not secrets.compare_digest(token, self.server.launch_token):
                self.respond(401, {'error': '请通过桌面“大肥鱼管理台”快捷方式打开页面。'})
                return
            existing = self.session()
            if existing is not None:
                self.respond(200, {'csrf': existing['csrf']})
                return
            identifier, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            with self.server.sessions_lock:
                self.server.sessions = {k: v for k, v in self.server.sessions.items() if v['expires'] > time.time()}
                self.server.sessions[identifier] = {'csrf': csrf, 'expires': time.time() + 43200}
                temporary = self.server.session_file.with_suffix('.tmp')
                temporary.write_text(json.dumps(self.server.sessions), encoding='utf-8')
                temporary.replace(self.server.session_file)
            self.respond(200, {'csrf': csrf}, headers={'Set-Cookie': 'rolebot_manager=' + identifier + '; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200'})
            return
        session = self.session()
        if session is None:
            self.respond(401, {'error': '请通过桌面快捷方式打开管理台，或重新打开以恢复登录。'})
            return
        if method == 'POST' and not secrets.compare_digest(self.headers.get('X-Local-Token', ''), session['csrf']):
            self.respond(403, {'error': '管理会话校验失败，请重新打开页面。'})
            return
        if method == 'GET':
            if path == '/api/session':
                self.respond(200, {'csrf': session['csrf']})
                return
            if path.startswith('/api/jobs/'):
                self.respond(200, service.jobs.get(path.removeprefix('/api/jobs/')))
                return
            if path.startswith('/api/stickers/thumbnail/'):
                self.respond(200, service.thumbnail(path.removeprefix('/api/stickers/thumbnail/')), 'image/png')
                return
            if path == '/api/login/qr':
                if not service.login_status().get('has_qr'):
                    self.respond(404, {'error': '暂时没有有效二维码。'})
                else:
                    self.respond(200, (service.private / 'login-qr.png').read_bytes(), 'image/png')
                return
            query = parse_qs(url.query)
            routes = {'/api/status': service.status, '/api/groups': service.group_list,
                '/api/settings': service.settings, '/api/friends': service.friends, '/api/role': service.role,
                '/api/stickers': service.stickers, '/api/login': service.login_status,
                '/api/memories': lambda: service.memories(query.get('q', [''])[0]),
                '/api/logs': lambda: service.logs(query.get('kind', ['bot'])[0])}
            if path in routes:
                self.respond(200, routes[path]())
                return
        elif method == 'POST':
            payload = self.body()
            if path == '/api/voice/preview':
                self.respond(200, service.preview_voice(payload), 'audio/wav')
                return
            routes = {'/api/control': ({'start':'启动机器人','stop':'停止机器人','restart':'重启机器人'}.get(payload.get('action'),'运行控制'), lambda: service.control(payload.get('action'))),
                '/api/groups/change': ('应用群聊设置', lambda: service.change_group(payload)),
                '/api/groups/refresh': ('同步微信群目录', lambda: service.group_list(refresh=True)),
                '/api/settings': ('保存模型与私聊设置', lambda: service.save_settings(payload)),
                '/api/friends/settings': ('保存自动通过好友设置', lambda: service.save_friends(payload)),
                '/api/friends/accept': ('处理好友申请', lambda: service.accept_friend(payload)),
                '/api/voice/settings': ('保存语音音色', lambda: service.save_voice(payload)),
                '/api/role': ('保存角色设定', lambda: service.save_role(payload)),
                '/api/stickers/settings': ('保存表情设置', lambda: service.save_stickers(payload)),
                '/api/stickers/sync': ('同步手机表情库', service.sync_stickers),
                '/api/stickers/discover': ('更新免费表情包', service.discover_stickers),
                '/api/memories/action': ('更新用户记忆', lambda: service.memory_action(payload)),
                '/api/login/start': ('微信扫码登录', service.start_login)}
            if path in routes:
                title, function = routes[path]
                self.respond(202, service.jobs.submit(title, function))
                return
        self.respond(404, {'error': '页面或接口不存在。'})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--port', type=int, default=6186)
    args = parser.parse_args()
    service = Service(args.root)
    server = ManagerServer(('127.0.0.1', args.port), service)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()

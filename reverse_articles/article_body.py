"""Bounded, credential-free retrieval of an explicitly selected Weixin article.

Network access is opt-in by calling fetch_article(). It never reads browser or
Weixin cookies and does not follow links or fetch article media. Returned page
text is untrusted data, not an instruction or an assurance of completeness.
"""
from __future__ import annotations

import html.parser
import fnmatch
import http.client
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import time
import uuid
from urllib.parse import urljoin, urlsplit


def _load_local_card_validation():
    """Load the sibling validator without changing the caller's import path.

    The AstrBot plugin executes this module repeatedly using runpy, while the
    worker runs it with Python's isolated ``-I`` flag. Importing from a fixed
    sibling path works in both cases and avoids exposing a new global search
    directory to unrelated threads. A unique temporary module name allows the
    sibling's dataclass to resolve its module during execution without races.
    """
    name = '_weixin_article_cards_' + uuid.uuid4().hex
    path = Path(__file__).resolve().with_name('article_cards.py')
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError('article card validator unavailable')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        return module.MAX_URL, module.canonical_article_url
    finally:
        sys.modules.pop(name, None)


MAX_URL, canonical_article_url = _load_local_card_validation()

MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_TEXT = 60_000
MAX_REDIRECTS = 3
WORKER_TIMEOUT = 25
SOCKET_TIMEOUT = 7
_HOST = 'mp.weixin.qq.com'
_VOID = frozenset(('area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
                   'link', 'meta', 'param', 'source', 'track', 'wbr'))
_BREAK = frozenset(('p', 'div', 'section', 'article', 'br', 'li', 'h1', 'h2', 'h3', 'blockquote'))
_IGNORE = frozenset(('script', 'style', 'noscript', 'template', 'iframe', 'svg'))
_LOCAL_PROXY = re.compile(r'^(?:http://)?(127\.0\.0\.1|localhost|\[::1\]):([0-9]{1,5})$', re.I)
_VERIFICATION_PATHS = frozenset(('/mp/wappoc_appmsgcaptcha',))


class ArticleError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _ArticleParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.skip_depth = 0
        self.found = False
        self.ambiguous = False
        self.chunks: list[str] = []
        self.length = 0
        self.truncated = False
        self.has_images = False

    def _add(self, text):
        remaining = MAX_TEXT - self.length
        if len(text) > remaining:
            self.truncated = True
        if remaining > 0:
            item = text[:remaining]
            self.chunks.append(item)
            self.length += len(item)

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if not self.depth:
            ids = [value for name, value in attrs if name.lower() == 'id']
            if tag in ('div', 'section', 'article') and ids == ['js_content']:
                if self.found:
                    self.ambiguous = True
                self.found = True
                self.depth = 1
            return
        if not self.skip_depth and tag == 'img':
            self.has_images = True
        if not self.skip_depth and tag in _BREAK:
            self._add('\n')
        if tag not in _VOID:
            self.depth += 1
            if not self.skip_depth and tag in _IGNORE:
                self.skip_depth = self.depth

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if not self.depth or tag in _VOID:
            return
        if self.skip_depth == self.depth:
            self.skip_depth = 0
        self.depth -= 1
        if self.depth and not self.skip_depth and tag in _BREAK:
            self._add('\n')

    def handle_data(self, data):
        if self.depth and not self.skip_depth:
            self._add(data)

    def result(self):
        if not self.found or self.ambiguous:
            raise ArticleError('body_unavailable')
        text = re.sub(r'\n[ \t]*\n+', '\n\n', ''.join(self.chunks)).strip()
        if not text:
            raise ArticleError('no_readable_text')
        return {'status': 'ok', 'text': text, 'truncated': self.truncated,
                'has_images': self.has_images, 'body_is_untrusted': True}


def extract_article_html(body: bytes) -> dict:
    if not isinstance(body, bytes) or not 0 < len(body) <= MAX_BODY_BYTES:
        raise ArticleError('invalid_body_size')
    try:
        page = body.decode('utf-8-sig', errors='strict')
        parser = _ArticleParser()
        parser.feed(page)
        parser.close()
        return parser.result()
    except UnicodeError:
        raise ArticleError('invalid_encoding') from None
    except (ValueError, RecursionError):
        raise ArticleError('invalid_html') from None


def _public_addresses(records):
    if not records or len(records) > 64:
        raise ArticleError('dns_unavailable')
    addresses = []
    for family, socktype, proto, _, address in records:
        if family not in (socket.AF_INET, socket.AF_INET6) or socktype != socket.SOCK_STREAM:
            continue
        try:
            parsed = ipaddress.ip_address(address[0])
        except (ValueError, IndexError, TypeError):
            raise ArticleError('dns_invalid') from None
        mapped = getattr(parsed, 'ipv4_mapped', None)
        if not parsed.is_global or (mapped is not None and not mapped.is_global):
            raise ArticleError('dns_not_public')
        addresses.append((family, socktype, proto, address))
    if not addresses:
        raise ArticleError('dns_unavailable')
    return addresses


def _parse_local_proxy(value: object, bypass: object = '') -> tuple[str, int]:
    """Accept only an explicit loopback HTTP proxy from trusted OS settings."""
    if not isinstance(value, str) or not value or len(value) > 4096 or not isinstance(bypass, str):
        raise ArticleError('proxy_unavailable')
    for pattern in bypass.split(';'):
        pattern = pattern.strip().lower()
        if pattern and pattern != '<local>' and fnmatch.fnmatchcase(_HOST, pattern):
            raise ArticleError('proxy_bypassed')
    if '=' in value:
        mappings = {}
        for item in value.split(';'):
            key, separator, endpoint = item.strip().partition('=')
            key = key.strip().lower()
            if not separator or not key or key in mappings:
                raise ArticleError('proxy_unavailable')
            mappings[key] = endpoint.strip()
        value = mappings.get('https', '')
    match = _LOCAL_PROXY.fullmatch(value.strip())
    if not match:
        raise ArticleError('proxy_unavailable')
    port = int(match.group(2))
    if not 1 <= port <= 65535:
        raise ArticleError('proxy_unavailable')
    return ('::1' if match.group(1).lower() == '[::1]' else '127.0.0.1', port)


def _system_loopback_proxy() -> tuple[str, int]:
    if os.name != 'nt':
        raise ArticleError('proxy_unavailable')
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r'Software\Microsoft\Windows\CurrentVersion\Internet Settings') as key:
            def get(name, default=None):
                try:
                    return winreg.QueryValueEx(key, name)[0]
                except FileNotFoundError:
                    return default
            if get('ProxyEnable', 0) != 1 or get('AutoConfigURL', ''):
                raise ArticleError('proxy_unavailable')
            return _parse_local_proxy(get('ProxyServer', ''), get('ProxyOverride', ''))
    except OSError:
        raise ArticleError('proxy_unavailable') from None


def _request_once(url: str, *, local_proxy: tuple[str, int] | None = None):
    """Pin direct DNS or use only a prevalidated OS-configured local proxy."""
    parts = urlsplit(url)
    if canonical_article_url(url) != url:
        raise ArticleError('invalid_url')
    addresses = []
    if local_proxy is None:
        try:
            records = socket.getaddrinfo(_HOST, 443, type=socket.SOCK_STREAM)
            addresses = _public_addresses(records)
        except socket.gaierror:
            raise ArticleError('dns_unavailable') from None
    elif (not isinstance(local_proxy, tuple) or len(local_proxy) != 2 or
          local_proxy[0] not in ('127.0.0.1', '::1') or type(local_proxy[1]) is not int or
          not 1 <= local_proxy[1] <= 65535):
        raise ArticleError('proxy_unavailable')
    context = ssl.create_default_context()
    connection = http.client.HTTPSConnection(_HOST, port=443, timeout=SOCKET_TIMEOUT, context=context)
    connected = None
    try:
        if local_proxy is not None:
            # The endpoint comes only from the Windows user's trusted system
            # configuration. CONNECT is pinned to the official hostname;
            # TLS still authenticates that hostname end-to-end. No proxy
            # authentication, Weixin cookie or URL query is sent to the proxy
            # until the TLS tunnel is established.
            proxy = http.client.HTTPConnection(local_proxy[0], local_proxy[1], timeout=SOCKET_TIMEOUT)
            try:
                proxy.set_tunnel(_HOST, 443, headers={'Host': _HOST + ':443'})
                proxy.connect()
                raw, proxy.sock = proxy.sock, None
                if raw is None:
                    raise ArticleError('proxy_unavailable')
                try:
                    connected = context.wrap_socket(raw, server_hostname=_HOST)
                except BaseException:
                    raw.close()
                    raise
            finally:
                proxy.close()
        else:
            for family, socktype, proto, address in addresses:
                sock = socket.socket(family, socktype, proto)
                try:
                    sock.settimeout(SOCKET_TIMEOUT)
                    sock.connect(address)
                    connected = context.wrap_socket(sock, server_hostname=_HOST)
                    break
                except (OSError, ssl.SSLError):
                    sock.close()
        if connected is None:
            raise ArticleError('connection_failed')
        connection.sock = connected
        target = parts.path + (('?' + parts.query) if parts.query else '')
        connection.request('GET', target, headers={'Host': _HOST, 'User-Agent': 'WeixinArticleReader/1.0',
            'Accept': 'text/html,application/xhtml+xml', 'Accept-Encoding': 'identity', 'Connection': 'close'})
        response = connection.getresponse()
        headers = response.getheaders()
        locations = [v for k, v in headers if k.lower() == 'location']
        if len(locations) > 1:
            raise ArticleError('invalid_redirect')
        if response.status in (301, 302, 303, 307, 308):
            return response.status, locations[0] if locations else '', b'', ''
        if response.status != 200:
            raise ArticleError('http_unavailable')
        content_type = response.getheader('Content-Type', '').split(';', 1)[0].strip().lower()
        if content_type not in ('text/html', 'application/xhtml+xml'):
            raise ArticleError('unsupported_content_type')
        if response.getheader('Content-Encoding', 'identity').strip().lower() not in ('', 'identity'):
            raise ArticleError('unsupported_encoding')
        length = response.getheader('Content-Length')
        if length is not None:
            if not length.isascii() or not length.isdigit() or int(length) > MAX_BODY_BYTES:
                raise ArticleError('invalid_body_size')
        data = response.read(MAX_BODY_BYTES + 1)
        if len(data) > MAX_BODY_BYTES:
            raise ArticleError('invalid_body_size')
        return response.status, '', data, content_type
    except (TimeoutError, OSError, ssl.SSLError, http.client.HTTPException):
        raise ArticleError('network_error') from None
    finally:
        connection.close()


def _fetch_with_transport(url: str, request_once=_request_once) -> dict:
    current = canonical_article_url(url)
    if current is None:
        raise ArticleError('invalid_url')
    seen = set()
    for index in range(MAX_REDIRECTS + 1):
        if current in seen:
            raise ArticleError('redirect_loop')
        seen.add(current)
        status, location, body, _ = request_once(current)
        if status == 200:
            return extract_article_html(body)
        if status not in (301, 302, 303, 307, 308) or index == MAX_REDIRECTS:
            raise ArticleError('redirect_limit')
        if not isinstance(location, str) or not location or len(location) > MAX_URL or re.search(r'[\x00-\x20\x7f\\]', location):
            raise ArticleError('invalid_redirect')
        resolved = urljoin(current, location)
        # Weixin can redirect an article to its own verification endpoint.
        # Report that condition without visiting the endpoint or attempting to
        # solve it. Validate the complete authority before recognizing it.
        try:
            parts = urlsplit(resolved)
            if (parts.scheme.lower() == 'https' and parts.hostname == _HOST and
                    parts.username is None and parts.password is None and
                    parts.port in (None, 443) and parts.path in _VERIFICATION_PATHS):
                raise ArticleError('verification_required')
        except ValueError:
            raise ArticleError('invalid_redirect') from None
        target = canonical_article_url(resolved)
        if target is None:
            raise ArticleError('invalid_redirect')
        current = target
    raise ArticleError('redirect_limit')


def fetch_article(url: str, *, use_system_proxy: bool = False) -> dict:
    """Read one selected article in a time-limited isolated helper process.

    Returns a bounded status dictionary; URLs and credentials are never written
    to command-line arguments or logs. The caller must have a user's authority
    to fetch the selected link. It sends the article URL to mp.weixin.qq.com.
    """
    if type(use_system_proxy) is not bool:
        return {'status': 'unavailable', 'reason': 'invalid_request'}
    safe_url = canonical_article_url(url)
    if safe_url is None:
        return {'status': 'unavailable', 'reason': 'invalid_url'}
    env = dict(os.environ)
    for name in tuple(env):
        if name.upper() in ('PYTHONPATH', 'PYTHONHOME', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY'):
            env.pop(name, None)
    env['PYTHONNOUSERSITE'] = '1'
    try:
        result = subprocess.run([sys.executable, '-I', str(Path(__file__).resolve()), '--worker'],
            input=json.dumps({'url': safe_url, 'use_system_proxy': use_system_proxy}, ensure_ascii=True).encode('ascii'),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
            timeout=WORKER_TIMEOUT, check=False,
            creationflags=0x08000000 if os.name == 'nt' else 0)
        if result.returncode != 0 or len(result.stdout) > 1_000_000:
            return {'status': 'unavailable', 'reason': 'worker_failed'}
        value = json.loads(result.stdout.decode('utf-8'))
        if not isinstance(value, dict) or value.get('status') not in ('ok', 'unavailable'):
            return {'status': 'unavailable', 'reason': 'worker_failed'}
        if value['status'] == 'ok':
            if (set(value) != {'status', 'text', 'truncated', 'has_images', 'body_is_untrusted'} or
                    not isinstance(value['text'], str) or len(value['text']) > MAX_TEXT or
                    type(value['truncated']) is not bool or type(value['has_images']) is not bool or
                    value['body_is_untrusted'] is not True):
                return {'status': 'unavailable', 'reason': 'worker_failed'}
        elif (set(value) != {'status', 'reason'} or not isinstance(value['reason'], str) or
              not re.fullmatch(r'[a-z_]{1,32}', value['reason'])):
            return {'status': 'unavailable', 'reason': 'worker_failed'}
        return value
    except subprocess.TimeoutExpired:
        return {'status': 'unavailable', 'reason': 'timeout'}
    except (OSError, ValueError, UnicodeError):
        return {'status': 'unavailable', 'reason': 'worker_failed'}


def _worker():
    try:
        raw = sys.stdin.buffer.read(MAX_URL * 6 + 1024)
        payload = json.loads(raw.decode('utf-8'))
        if (not isinstance(payload, dict) or set(payload) not in ({'url'}, {'url', 'use_system_proxy'}) or
                type(payload.get('use_system_proxy', False)) is not bool):
            raise ArticleError('invalid_request')
        if payload.get('use_system_proxy', False):
            proxy = _system_loopback_proxy()
            def through_proxy(url):
                if _system_loopback_proxy() != proxy:
                    raise ArticleError('proxy_changed')
                return _request_once(url, local_proxy=proxy)
            result = _fetch_with_transport(payload['url'], through_proxy)
        else:
            result = _fetch_with_transport(payload['url'])
    except ArticleError as exc:
        result = {'status': 'unavailable', 'reason': exc.code}
    except Exception:
        result = {'status': 'unavailable', 'reason': 'worker_failed'}
    sys.stdout.write(json.dumps(result, ensure_ascii=True, separators=(',', ':')))


if __name__ == '__main__' and sys.argv[1:] == ['--worker']:
    _worker()

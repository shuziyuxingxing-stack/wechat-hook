import json
import os
import runpy
import socket
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from pathlib import Path

import article_body as body

URL = 'https://mp.weixin.qq.com/s?__biz=example&mid=123'
PAGE = b'<html><body><div id="js_content"><p>Hello <b>world</b></p><script>ignore me</script><img src="https://evil.invalid/a"></div></body></html>'


class HtmlTests(unittest.TestCase):
    def test_extracts_only_article_text_and_tracks_media(self):
        result = body.extract_article_html(PAGE)
        self.assertEqual(result['status'], 'ok')
        self.assertIn('Hello world', result['text'])
        self.assertNotIn('ignore me', result['text'])
        self.assertTrue(result['has_images'])
        self.assertTrue(result['body_is_untrusted'])
        self.assertFalse(result['truncated'])

    def test_body_missing_empty_duplicate_or_invalid(self):
        cases = [
            (b'<html>login required</html>', 'body_unavailable'),
            (b'<div id="js_content"><img></div>', 'no_readable_text'),
            (b'<div id="js_content">a</div><div id="js_content">b</div>', 'body_unavailable'),
            (b'\xff', 'invalid_encoding'),
            (b'x' * (body.MAX_BODY_BYTES + 1), 'invalid_body_size'),
        ]
        for value, reason in cases:
            with self.subTest(reason=reason), self.assertRaises(body.ArticleError) as caught:
                body.extract_article_html(value)
            self.assertEqual(caught.exception.code, reason)

    def test_long_text_is_bounded_and_labeled(self):
        page = b'<div id="js_content">' + b'a' * (body.MAX_TEXT + 100) + b'</div>'
        result = body.extract_article_html(page)
        self.assertEqual(len(result['text']), body.MAX_TEXT)
        self.assertTrue(result['truncated'])


class RedirectTests(unittest.TestCase):
    def test_valid_relative_redirect_is_revalidated_and_fragment_removed(self):
        calls = []
        def request(url):
            calls.append(url)
            return ((302, '/s/second#private', b'', '') if len(calls) == 1
                    else (200, '', PAGE, 'text/html'))
        self.assertEqual(body._fetch_with_transport(URL + '#rd', request)['status'], 'ok')
        self.assertEqual(calls, [URL, 'https://mp.weixin.qq.com/s/second'])

    def test_cross_host_redirect_and_http_downgrade_are_rejected(self):
        for location in ('https://evil.invalid/s/a', 'http://mp.weixin.qq.com/s/a',
                         '//mp.weixin.qq.com.evil.invalid/s/a', '/mp/profile_ext', '/s/a%0dfoo'):
            with self.subTest(location=location), self.assertRaises(body.ArticleError) as caught:
                body._fetch_with_transport(URL, lambda _: (302, location, b'', ''))
            self.assertEqual(caught.exception.code, 'invalid_redirect')

    def test_weixin_verification_is_reported_without_following_redirect(self):
        calls = []
        def request(url):
            calls.append(url)
            return (302, '/mp/wappoc_appmsgcaptcha?token=opaque', b'', '')
        with self.assertRaises(body.ArticleError) as caught:
            body._fetch_with_transport(URL, request)
        self.assertEqual(caught.exception.code, 'verification_required')
        self.assertEqual(calls, [URL])

    def test_lookalike_verification_destinations_remain_invalid(self):
        destinations = (
            'https://mp.weixin.qq.com.evil.invalid/mp/wappoc_appmsgcaptcha',
            'http://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha',
            'https://user@mp.weixin.qq.com/mp/wappoc_appmsgcaptcha',
            'https://mp.weixin.qq.com:444/mp/wappoc_appmsgcaptcha',
        )
        for location in destinations:
            with self.subTest(location=location), self.assertRaises(body.ArticleError) as caught:
                body._fetch_with_transport(URL, lambda _: (302, location, b'', ''))
            self.assertEqual(caught.exception.code, 'invalid_redirect')

    def test_redirect_loop_and_limit(self):
        with self.assertRaises(body.ArticleError) as caught:
            body._fetch_with_transport(URL, lambda _: (302, URL, b'', ''))
        self.assertEqual(caught.exception.code, 'redirect_loop')
        counter = iter(range(100))
        with self.assertRaises(body.ArticleError) as caught:
            body._fetch_with_transport(URL, lambda _: (302, '/s/' + str(next(counter)), b'', ''))
        self.assertEqual(caught.exception.code, 'redirect_limit')


class DnsAndTransportTests(unittest.TestCase):
    @staticmethod
    def record(ip):
        return socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443)

    def test_all_dns_addresses_must_be_public(self):
        self.assertEqual(len(body._public_addresses([self.record('8.8.8.8')])), 1)
        for records in ([self.record('127.0.0.1')], [self.record('8.8.8.8'), self.record('10.0.0.1')], []):
            with self.subTest(records=records), self.assertRaises(body.ArticleError):
                body._public_addresses(records)

    def test_dns_is_fixed_on_socket_with_official_sni_and_no_credentials(self):
        sent = {}
        class FakeSocket:
            def settimeout(self, value): sent['timeout'] = value
            def connect(self, address): sent['address'] = address
            def close(self): pass
        class FakeContext:
            def wrap_socket(self, sock, server_hostname):
                sent['sni'] = server_hostname
                return sock
        class FakeResponse:
            status = 200
            def getheaders(self): return [('Content-Type', 'text/html; charset=utf-8')]
            def getheader(self, name, default=None):
                return {'Content-Type': 'text/html; charset=utf-8', 'Content-Encoding': 'identity'}.get(name, default)
            def read(self, size): return PAGE
        class FakeConnection:
            def __init__(self, host, port, timeout, context): sent['host'] = host
            def request(self, method, target, headers): sent.update(method=method, target=target, headers=headers)
            def getresponse(self): return FakeResponse()
            def close(self): pass
        with patch.object(body.socket, 'getaddrinfo', return_value=[self.record('8.8.8.8')]) as lookup, \
             patch.object(body.socket, 'socket', return_value=FakeSocket()), \
             patch.object(body.ssl, 'create_default_context', return_value=FakeContext()), \
             patch.object(body.http.client, 'HTTPSConnection', FakeConnection):
            status, _, value, _ = body._request_once(URL)
        self.assertEqual((status, value), (200, PAGE))
        self.assertEqual(lookup.call_args.args[:2], ('mp.weixin.qq.com', 443))
        self.assertEqual(sent['address'], ('8.8.8.8', 443))
        self.assertEqual(sent['sni'], 'mp.weixin.qq.com')
        self.assertEqual(sent['target'], '/s?__biz=example&mid=123')
        self.assertFalse({'Cookie', 'Authorization', 'Proxy-Authorization', 'Referer'} & set(sent['headers']))

    def test_private_dns_rejected_before_any_socket_creation(self):
        with patch.object(body.socket, 'getaddrinfo', return_value=[self.record('127.0.0.1')]), \
             patch.object(body.socket, 'socket') as make_socket:
            with self.assertRaises(body.ArticleError) as caught:
                body._request_once(URL)
        self.assertEqual(caught.exception.code, 'dns_not_public')
        make_socket.assert_not_called()


class WorkerTests(unittest.TestCase):
    def test_repeated_and_concurrent_plugin_loads_leave_import_path_unchanged(self):
        path_before = list(sys.path)
        def load(_):
            namespace = runpy.run_path(body.__file__)
            self.assertEqual(namespace['canonical_article_url'](URL + '#rd'), URL)
        for index in range(3):
            load(index)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(load, range(32)))
        self.assertEqual(sys.path, path_before)
        self.assertFalse(any(name.startswith('_weixin_article_cards_') for name in sys.modules))

    def test_worker_receives_url_via_stdin_not_argv_and_scrubs_environment(self):
        output = {'status': 'ok', 'text': 'hello', 'truncated': False,
                  'has_images': False, 'body_is_untrusted': True}
        completed = subprocess.CompletedProcess([], 0, json.dumps(output).encode(), b'')
        with patch.dict(os.environ, {'HTTPS_PROXY': 'http://example.invalid', 'PYTHONPATH': 'untrusted'}), \
             patch.object(body.subprocess, 'run', return_value=completed) as run:
            self.assertEqual(body.fetch_article(URL + '#rd'), output)
        args, kwargs = run.call_args
        self.assertFalse(any('example&mid' in str(arg) for arg in args[0]))
        self.assertEqual(json.loads(kwargs['input']), {'url': URL, 'use_system_proxy': False})
        self.assertNotIn('HTTPS_PROXY', kwargs['env'])
        self.assertNotIn('PYTHONPATH', kwargs['env'])
        self.assertEqual(kwargs['timeout'], body.WORKER_TIMEOUT)

    def test_timeout_invalid_worker_output_and_invalid_url_are_explicit(self):
        with patch.object(body.subprocess, 'run', side_effect=subprocess.TimeoutExpired('worker', 1)):
            self.assertEqual(body.fetch_article(URL)['reason'], 'timeout')
        with patch.object(body.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'{"status":"ok","text":7}', b'')):
            self.assertEqual(body.fetch_article(URL)['reason'], 'worker_failed')
        with patch.object(body.subprocess, 'run') as run:
            self.assertEqual(body.fetch_article('https://evil.invalid/s/a')['reason'], 'invalid_url')
            run.assert_not_called()

    def test_proxy_is_explicit_and_proxy_target_not_user_controlled(self):
        output = {'status': 'unavailable', 'reason': 'body_unavailable'}
        completed = subprocess.CompletedProcess([], 0, json.dumps(output).encode(), b'')
        with patch.object(body.subprocess, 'run', return_value=completed) as run:
            self.assertEqual(body.fetch_article(URL, use_system_proxy=True), output)
        self.assertEqual(json.loads(run.call_args.kwargs['input']), {'url': URL, 'use_system_proxy': True})
        self.assertEqual(body.fetch_article(URL, use_system_proxy='127.0.0.1:5')['reason'], 'invalid_request')

    def test_isolated_worker_starts_without_network_for_invalid_url(self):
        script = Path(body.__file__).resolve()
        result = subprocess.run([sys.executable, '-I', str(script), '--worker'],
                                input=b'{"url":"https://evil.invalid/s/x"}',
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {'status': 'unavailable', 'reason': 'invalid_url'})


class LoopbackProxyTests(unittest.TestCase):
    def test_system_proxy_parser_accepts_only_local_http_endpoints(self):
        valid = {
            '127.0.0.1:7890': ('127.0.0.1', 7890),
            'localhost:7890': ('127.0.0.1', 7890),
            '[::1]:7890': ('::1', 7890),
            'http=127.0.0.1:1080;https=127.0.0.1:7890': ('127.0.0.1', 7890),
        }
        for value, expected in valid.items():
            with self.subTest(value=value):
                self.assertEqual(body._parse_local_proxy(value), expected)
        for value in ('10.0.0.1:7890', 'example.com:7890', '127.0.0.1:0',
                      'user@127.0.0.1:7890', 'socks=127.0.0.1:7890',
                      'https=127.0.0.1:1;https=127.0.0.1:2'):
            with self.subTest(value=value), self.assertRaises(body.ArticleError):
                body._parse_local_proxy(value)
        with self.assertRaises(body.ArticleError) as caught:
            body._parse_local_proxy('127.0.0.1:7890', '*.qq.com;<local>')
        self.assertEqual(caught.exception.code, 'proxy_bypassed')

    def test_proxy_connects_fixed_official_target_and_tls_without_credentials(self):
        sent = {}
        class FakeSocket:
            def close(self): sent['socket_closed'] = True
        class FakeProxy:
            def __init__(self, host, port, timeout): sent['proxy_endpoint'] = (host, port); self.sock = FakeSocket()
            def set_tunnel(self, host, port, headers): sent['tunnel'] = (host, port, headers)
            def connect(self): sent['connected'] = True
            def close(self): sent['proxy_closed'] = True
        class FakeContext:
            def wrap_socket(self, sock, server_hostname): sent['sni'] = server_hostname; return sock
        class FakeResponse:
            status = 200
            def getheaders(self): return [('Content-Type', 'text/html')]
            def getheader(self, name, default=None): return {'Content-Type': 'text/html'}.get(name, default)
            def read(self, size): return PAGE
        class FakeConnection:
            def __init__(self, host, port, timeout, context): pass
            def request(self, method, target, headers): sent['target'] = target; sent['headers'] = headers
            def getresponse(self): return FakeResponse()
            def close(self): pass
        with patch.object(body.socket, 'getaddrinfo') as lookup, \
             patch.object(body.http.client, 'HTTPConnection', FakeProxy), \
             patch.object(body.http.client, 'HTTPSConnection', FakeConnection), \
             patch.object(body.ssl, 'create_default_context', return_value=FakeContext()):
            self.assertEqual(body._request_once(URL, local_proxy=('127.0.0.1', 7890))[0], 200)
        lookup.assert_not_called()
        self.assertEqual(sent['proxy_endpoint'], ('127.0.0.1', 7890))
        self.assertEqual(sent['tunnel'][:2], ('mp.weixin.qq.com', 443))
        self.assertEqual(sent['sni'], 'mp.weixin.qq.com')
        self.assertFalse({'Cookie', 'Authorization', 'Proxy-Authorization', 'Referer'} & set(sent['headers']))

    def test_rejects_arbitrary_or_nonloopback_proxy(self):
        for endpoint in (('10.0.0.1', 80), ('example.invalid', 80), ('127.0.0.1', 0), ('127.0.0.1', True)):
            with self.subTest(endpoint=endpoint), self.assertRaises(body.ArticleError):
                body._request_once(URL, local_proxy=endpoint)


if __name__ == '__main__':
    unittest.main()

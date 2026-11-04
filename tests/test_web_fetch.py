from email.message import Message
import unittest
from unittest.mock import patch

from src.tools.web_fetch import WebFetchTool, _PublicHTTP, _PublicHTTPS


PUBLIC = [(2, 1, 6, '', ('93.184.216.34', 443))]


class Response:
    def __init__(self, body=b'', mime='text/plain; charset=utf-8', status=200, **headers):
        self.body = body; self.offset = 0; self.status = status
        self.headers = Message(); self.headers['Content-Type'] = mime
        for key, value in headers.items(): self.headers[key] = value

    def getheader(self, name, default=None): return self.headers.get(name, default)

    def read(self, size):
        value = self.body[self.offset:self.offset+size]; self.offset += len(value); return value


class Connection:
    sock = None
    def __init__(self, response): self.response = response; self.closed = False
    def request(self, *args, **kwargs): self.request_args = (args, kwargs)
    def getresponse(self): return self.response
    def close(self): self.closed = True


class WebFetchTests(unittest.TestCase):
    def fetch(self, response, **kwargs):
        connection = Connection(response)
        with patch('src.tools.web_fetch.socket.getaddrinfo', return_value=PUBLIC), patch('src.tools.web_fetch._PublicHTTPS', return_value=connection):
            result = WebFetchTool().run('https://example.com/source', **kwargs)
        self.assertTrue(connection.closed)
        return result

    def test_plain_source_pagination_and_hash(self):
        response = self.fetch(Response(('中'*500).encode()), start_char=200, max_chars=200)
        self.assertEqual(response['content'], '中'*200)
        self.assertEqual(response['next_start_char'], 400)
        self.assertTrue(response['truncated'])
        self.assertEqual(len(response['read_sha256']), 64)

    def test_html_extracts_title_body_and_discards_scripts(self):
        result = self.fetch(Response(b'<html><title>Official &amp; docs</title><nav>Menu</nav><h1>Design</h1><script>secret()</script><p>Evidence</p></html>', mime='text/html'))
        self.assertEqual(result['title'], 'Official & docs')
        self.assertIn('Design', result['content']); self.assertIn('Evidence', result['content'])
        self.assertNotIn('secret', result['content']); self.assertNotIn('Menu', result['content'])

    def test_byte_limit_is_explicit_and_never_read_unbounded(self):
        tool = WebFetchTool(); tool.byte_limit = 100
        connection = Connection(Response(b'x'*500))
        with patch('src.tools.web_fetch.socket.getaddrinfo', return_value=PUBLIC), patch('src.tools.web_fetch._PublicHTTPS', return_value=connection):
            result = tool.run('https://example.com')
        self.assertTrue(result['download_truncated'])
        self.assertEqual(len(result['content']), 100)
        self.assertIsNone(result['next_start_char'])
        self.assertEqual(connection.response.offset, 101)

    def test_private_address_and_authenticated_or_non_http_urls_never_connect(self):
        for url in ('file:///etc/passwd', 'https://name:password@example.com', 'https://127.0.0.1', 'http://169.254.169.254'):
            with self.subTest(url=url), patch('src.tools.web_fetch.socket.getaddrinfo', return_value=[(2,1,6,'',('127.0.0.1',80))]), patch('src.tools.web_fetch._PublicHTTPS') as connection:
                self.assertIn('error', WebFetchTool().run(url))
                connection.assert_not_called()

    def test_multicast_dns_answer_is_not_a_public_web_target(self):
        with patch('src.tools.web_fetch.socket.getaddrinfo', return_value=[(2,1,6,'',('224.0.0.1',443))]), patch('src.tools.web_fetch._PublicHTTPS') as connection:
            result=WebFetchTool().run('https://example.com')
        self.assertEqual(result['error_code'],'blocked_address');connection.assert_not_called()

    def test_redirect_to_private_address_is_blocked(self):
        connection = Connection(Response(status=302, Location='http://127.0.0.1/internal'))
        with patch('src.tools.web_fetch.socket.getaddrinfo', side_effect=[PUBLIC, [(2,1,6,'',('127.0.0.1',80))]]), patch('src.tools.web_fetch._PublicHTTPS', return_value=connection), patch('src.tools.web_fetch._PublicHTTP') as http:
            result = WebFetchTool().run('https://example.com')
        self.assertEqual(result['error_code'], 'blocked_address'); http.assert_not_called()
        self.assertTrue(connection.closed)

    def test_redirect_count_is_bounded(self):
        connections = [Connection(Response(status=302, Location='/loop')) for _ in range(4)]
        with patch('src.tools.web_fetch.socket.getaddrinfo', return_value=PUBLIC), patch('src.tools.web_fetch._PublicHTTPS', side_effect=connections):
            result = WebFetchTool().run('https://example.com')
        self.assertEqual(result['error_code'], 'redirect_limit')
        self.assertTrue(all(c.closed for c in connections))

    def test_binary_and_http_error_categories(self):
        self.assertEqual(self.fetch(Response(mime='application/pdf'))['error_code'], 'unsupported_content')
        self.assertFalse(self.fetch(Response(status=404))['retryable'])
        self.assertTrue(self.fetch(Response(status=503))['retryable'])

    def test_connection_uses_validated_ip_not_a_second_dns_lookup(self):
        with patch('src.tools.web_fetch.socket.create_connection') as create:
            connection = _PublicHTTP('example.com', 80, '93.184.216.34', 3)
            connection.connect()
            create.assert_called_once_with(('93.184.216.34', 80), 3)
        with patch('src.tools.web_fetch.socket.create_connection'), patch('src.tools.web_fetch.ssl.create_default_context') as context:
            connection = _PublicHTTPS('example.com', 443, '93.184.216.34', 3); connection.connect()
            self.assertEqual(context.return_value.wrap_socket.call_args.kwargs['server_hostname'], 'example.com')

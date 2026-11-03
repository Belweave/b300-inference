import sys, unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import guard_weights, node_tunnel, s3_mirror


class Response:
    def __init__(self): self.left = [b'x']
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self, n=-1): return self.left.pop() if self.left else b''


class UserAgent(unittest.TestCase):
    """Cloudflare 403s urllib's default agent, so every mirror request names its own."""

    def sent(self, call):
        seen = []
        opener = mock.Mock()
        opener.open.side_effect = lambda request, timeout: seen.append(request) or Response()
        with mock.patch('urllib.request.build_opener', return_value=opener), TemporaryDirectory() as d:
            call(Path(d)/'out')
        return seen[0]

    def test_guard_fetch(self):
        f = {'size': 1, 'sha256': '2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881'}
        request = self.sent(lambda t: guard_weights.fetch('https://m/x', {'Authorization': 'Bearer t'}, False, t, f, 5))
        self.assertEqual(request.get_header('User-agent'), node_tunnel.USER_AGENT)
        self.assertEqual(request.get_header('Authorization'), 'Bearer t')

    def test_tool_download(self):
        request = self.sent(lambda t: node_tunnel.download('https://m/x', {}, t, 5, False))
        self.assertEqual(request.get_header('User-agent'), node_tunnel.USER_AGENT)

    def test_s3_client(self):
        # boto3 is a node dependency only; a stand-in records what the client is built with.
        boto3, config = mock.Mock(), mock.Mock()
        modules = {'boto3': boto3, 'botocore': mock.Mock(config=config), 'botocore.config': config}
        env = {'AWS_ENDPOINT_URL': 'https://s3.example.invalid', 'AWS_ACCESS_KEY_ID': 'id', 'AWS_SECRET_ACCESS_KEY': 'secret'}
        with mock.patch.dict(sys.modules, modules):
            s3_mirror.client(env)
        self.assertEqual(config.Config.call_args.kwargs['user_agent'], node_tunnel.USER_AGENT)
        self.assertIs(boto3.client.call_args.kwargs['config'], config.Config.return_value)


if __name__ == '__main__':
    unittest.main()

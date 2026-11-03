"""Guard weight staging against a loopback mirror and a loopback stand-in for HF."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import guard_weights
import guards
import serve_plan

REPO, REV = 'mistralai/Shieldstral-1.0-3B', 'b'*40
HEADER = 'Authorization: Bearer fixture-mirror-credential'
FILES = {'config.json': b'{"fixture": true}\n', 'sub/model.safetensors': b'w'*300_000}


class Origin:
    """One loopback server. mode decides what it sends; every request is recorded."""

    def __init__(self, files):
        self.files, self.mode, self.requests = files, 'ok', []
        origin = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                origin.requests.append((self.path, dict(self.headers)))
                name = self.path.split(f'/{REV}/', 1)[-1]
                if origin.mode == 'redirect':
                    self.send_response(302)
                    self.send_header('Location', 'http://127.0.0.1:9/elsewhere')
                    self.end_headers()
                    return
                if origin.mode == '401' or name not in origin.files:
                    self.send_response(401 if origin.mode == '401' else 404)
                    self.end_headers()
                    return
                body = origin.files[name]
                if origin.mode == 'corrupt':
                    body = body[:-1]+b'!'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class GuardWeightsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        manifest = self.folder/'guards.json'
        manifest.write_text(json.dumps({'guards': [{'repo': REPO, 'revision': REV, 'files': [
            {'filename': n, 'size': len(b), 'sha256': hashlib.sha256(b).hexdigest()} for n, b in FILES.items()]}]}))
        self.mirror, self.hf = Origin(FILES), Origin(FILES)
        self.addCleanup(self.mirror.close)
        self.addCleanup(self.hf.close)
        self.env = {'NODE_RUN_TESTING': '1', 'GUARD_MANIFEST': str(manifest), 'GUARD_HF_URL': self.hf.url,
                    'MIRROR_HTTP': '1', 'MIRROR_URL': self.mirror.url+'/models/main-model', 'MIRROR_AUTH_HEADER': HEADER}
        self.root = self.folder/'guards'

    def stage(self):
        return guard_weights.stage(self.env, REPO, REV, self.root, timeout=5)

    def assert_staged(self, result):
        dest = self.root/REPO/REV
        self.assertEqual(result['dir'], str(dest.absolute()))
        for name, body in FILES.items():
            self.assertEqual((dest/name).read_bytes(), body)
        self.assertEqual(list(dest.rglob('*.part')), [])

    def assert_no_credential_to_hf(self):
        for _, headers in self.hf.requests:
            self.assertNotIn('Authorization', headers)

    def test_mirror_ok_sends_header_to_mirror_path_only(self):
        result = self.stage()
        self.assert_staged(result)
        self.assertEqual(result['sources'], {'mirror': 2})
        self.assertEqual(self.hf.requests, [])
        for path, headers in self.mirror.requests:
            self.assertTrue(path.startswith(f'/models/{REPO}/{REV}/'))
            self.assertEqual(headers['Authorization'], HEADER.split(': ', 1)[1])

    def test_mirror_checksum_mismatch_falls_back_to_hf(self):
        self.mirror.mode, self.env['MIRROR_ONLY'] = 'corrupt', '0'
        result = self.stage()
        self.assert_staged(result)
        self.assertEqual(result['sources'], {'hf': 2})
        self.assertIn('mirror: config.json checksum mismatch', result['failed'])
        self.assert_no_credential_to_hf()

    def test_mirror_401_or_redirect_falls_back_without_following(self):
        self.env['MIRROR_ONLY'] = '0'
        for mode in ('401', 'redirect'):
            with self.subTest(mode=mode):
                self.mirror.mode, self.mirror.requests, self.hf.requests = mode, [], []
                self.root = self.folder/mode
                result = self.stage()
                self.assert_staged(result)
                self.assertEqual(result['sources'], {'hf': 2})
                # A followed redirect would show up as a request for /elsewhere.
                self.assertFalse(any('elsewhere' in p for p, _ in self.mirror.requests+self.hf.requests))
                self.assert_no_credential_to_hf()

    def test_mirror_only_is_the_default_and_never_asks_hf(self):
        self.mirror.mode = 'corrupt'
        with self.assertRaises(ValueError):
            self.stage()
        self.assertEqual(self.hf.requests, [])
        del self.env['MIRROR_URL']
        with self.assertRaisesRegex(ValueError, 'no mirror source'):
            self.stage()
        self.assertEqual(self.hf.requests, [])

    def test_both_bad_fails_closed_without_url_or_header(self):
        self.mirror.mode, self.hf.mode = 'corrupt', '401'
        with self.assertRaises(ValueError) as caught:
            self.stage()
        message = str(caught.exception)
        for leak in (self.mirror.url, self.hf.url, '127.0.0.1', 'fixture-mirror-credential', 'Bearer'):
            self.assertNotIn(leak, message)
        self.assertIsNone(caught.exception.__cause__)
        dest = self.root/REPO/REV
        self.assertFalse(any(p.is_file() for p in dest.rglob('*')))

    def test_verified_local_copy_is_reused_and_a_bad_one_replaced(self):
        self.stage()
        self.mirror.requests = []
        result = self.stage()
        self.assertEqual(result['sources'], {'verified-local': 2})
        self.assertEqual(self.mirror.requests, [])
        (self.root/REPO/REV/'config.json').write_bytes(b'{"tampered": 1}\n')
        result = self.stage()
        self.assertEqual(result['sources'], {'verified-local': 1, 'mirror': 1})
        self.assert_staged(result)

    def test_markers_make_relaunch_cheap_and_still_catch_edits(self):
        total = sum(len(b) for b in FILES.values())
        self.assertEqual(guard_weights.missing_bytes(self.env, REPO, REV, self.root), total)
        # Markers written "later" than the files, as on a node where warm sits between the two checks.
        later = guard_weights.time_ns()+10_000_000_000
        with patch('guard_weights.time_ns', return_value=later):
            self.stage()
        self.assertEqual(guard_weights.missing_bytes(self.env, REPO, REV, self.root), 0)
        with patch('guard_weights.hashlib.sha256') as digest:
            guard_weights.verify(self.env, REPO, REV, self.root)
        digest.assert_not_called()
        # Same size, mtime put back: the marker no longer matches, so it hashes and refuses.
        path = self.root/REPO/REV/'config.json'
        # File times come from a coarse kernel clock; let it tick past the parallel download.
        time.sleep(0.05)
        before = path.stat()
        path.write_bytes(b'{"fixture": 1234}\n')
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaises(ValueError):
            guard_weights.verify(self.env, REPO, REV, self.root)
        # A fresh marker never vouches for a file changed in the same ctime tick.
        path.write_bytes(FILES['config.json'])
        guard_weights.verify(self.env, REPO, REV, self.root)
        path.write_bytes(b'{"fixture": 1234}\n')
        with self.assertRaises(ValueError):
            guard_weights.verify(self.env, REPO, REV, self.root)
        self.assertEqual(guard_weights.missing_bytes(self.env, REPO, REV, self.root), len(FILES['config.json']))

    def test_no_mirror_uses_hf_and_unpinned_revision_is_refused(self):
        del self.env['MIRROR_URL']
        self.env['MIRROR_ONLY'] = '0'
        self.assertEqual(self.stage()['sources'], {'hf': 2})
        with self.assertRaises(ValueError):
            guard_weights.stage(self.env, REPO, 'c'*40, self.root)

    def test_overrides_and_plain_http_mirror_need_testing_mode(self):
        env = {**self.env, 'NODE_RUN_TESTING': '0'}
        self.assertEqual(guard_weights.testing_value(env, 'GUARD_HF_URL', 'x'), 'x')
        with self.assertRaises(ValueError):
            guard_weights.mirror_base(env)

    def test_files_download_in_parallel_and_land_in_the_log(self):
        log = self.folder/'guards.log'
        result = guard_weights.stage(dict(self.env, GUARD_WORKERS='2'), REPO, REV, self.root, timeout=5, log=log)
        self.assert_staged(result)
        lines = log.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(f'{REPO} mirror ' in line for line in lines))
        self.assertNotIn('fixture-mirror-credential', log.read_text())
        with self.assertRaises(ValueError):
            guard_weights.stage(dict(self.env, GUARD_WORKERS='0'), REPO, REV, self.root)

    def test_committed_manifest_covers_both_pinned_guards(self):
        raw = json.loads(guard_weights.MANIFEST.read_text())
        pins = {(g['repo'], g['revision']) for g in raw['guards']}
        self.assertEqual(pins, {('mistralai/Shieldstral-1.0-3B', '003ec7e2b0bab5f0e6307edbaf186fa5822b76f5'),
                                ('Qwen/Qwen3Guard-Gen-0.6B', 'fada3b2f655b89601929198343c94cd2f64d93cc')})
        for repo, revision in pins:
            self.assertTrue(guard_weights.pinned_files({}, repo, revision))

    def test_plan_mounts_the_staged_dir_read_only(self):
        env = {'SAFETY': '1', 'SAFETY_MODEL_REVISION': REV, 'SAFETY_QWEN_REVISION': REV,
               'WEIGHTS_DIR': '/tmp/weights'}
        plan = guards.plans(env, serve_plan.image_reference({}), 'rehearsal')[0]
        self.assertEqual(plan['weights_dir'], f'/tmp/guards/{REPO}/{REV}')
        self.assertIn(f"type=bind,src={plan['weights_dir']},dst=/guard-model,readonly", plan['argv'])
        self.assertEqual(plan['argv'][plan['argv'].index('--served-model-name')+1], REPO)
        self.assertIn('HF_HUB_OFFLINE=1', plan['argv'])


if __name__ == '__main__':
    unittest.main()

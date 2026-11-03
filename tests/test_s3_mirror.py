"""Direct S3 (R2) weight pulls against a mocked client only; never real credentials."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import s3_mirror
import warm_download
import guard_weights

ENV = {'R2_BUCKET': 'fixture-bucket', 'AWS_ACCESS_KEY_ID': 'fixture-id', 'AWS_SECRET_ACCESS_KEY': 'fixture-secret',
       'AWS_ENDPOINT_URL': 'https://fixture.invalid', 'AWS_REGION': 'auto'}
GOOD = b'good'
FILE = {'size': 4, 'sha256': hashlib.sha256(GOOD).hexdigest()}


class FakeS3:
    def __init__(self, bodies=None, fail=False):
        self.calls, self.bodies, self.fail = [], bodies or {}, fail

    def download_file(self, bucket, key, filename, Config=None):
        self.calls.append((bucket, key))
        if self.fail:
            raise RuntimeError('https://fixture.invalid/fixture-bucket boom')
        Path(filename).write_bytes(self.bodies.get(key, GOOD))


class Response:
    def __init__(self, body=GOOD): self.body, self.status, self.content = body, 200, self
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    def raise_for_status(self): pass
    async def iter_chunked(self, size): yield self.body


class Session:
    def __init__(self): self.calls = []
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    def get(self, url, **options):
        self.calls.append(url)
        return Response()


REAL = json.loads((ROOT/'config/manifests/uncensored-c19ca5c.json').read_text())
NAME = 'model-00001-of-00002.safetensors'
DATA = {'model': {'repo': REAL['repo'], 'revision': REAL['revision']}, 'files': {NAME: FILE}, 'differs_from_base': [NAME]}


class KeyTests(unittest.TestCase):
    def test_exact_main_and_guard_keys(self):
        rev = REAL['revision']
        self.assertEqual(len(rev), 40)
        self.assertEqual(s3_mirror.main_key(rev, NAME),
                         f'models/DeepSeek-V4.1-Flash-1017-UNCENSORED-FP8/{rev}/{NAME}')
        self.assertNotIn('dealignai', s3_mirror.main_key(rev, NAME))
        guards = json.loads((ROOT/'config/manifests/guards.json').read_text())['guards']
        for g in guards:
            self.assertIn('/', g['repo'])
            self.assertEqual(s3_mirror.guard_key(g['repo'], g['revision'], 'config.json'),
                             f"models/{g['repo']}/{g['revision']}/config.json")

    def test_enabled_needs_all_creds_and_can_be_turned_off(self):
        self.assertTrue(s3_mirror.enabled(ENV))
        self.assertFalse(s3_mirror.enabled({**ENV, 'MIRROR_S3': '0'}))
        self.assertFalse(s3_mirror.enabled({k: v for k, v in ENV.items() if k != 'R2_BUCKET'}))
        with self.assertRaises(ValueError):
            s3_mirror.enabled({**ENV, 'MIRROR_S3': 'yes'})

    def test_bad_bytes_and_errors_never_leak_endpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)/'x'
            with self.assertRaises(s3_mirror.S3MirrorError) as bad:
                s3_mirror.download(FakeS3({'k': b'bad!'}), ENV, 'k', target, FILE, config=object())
            with self.assertRaises(s3_mirror.S3MirrorError) as err:
                s3_mirror.download(FakeS3(fail=True), ENV, 'k', target, FILE, config=object())
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(folder).iterdir()), [])
        for e in (bad.exception, err.exception):
            self.assertNotIn('fixture', str(e))
            self.assertIsNone(e.__cause__)
            self.assertIsNone(e.__context__)


class MainModelTests(unittest.IsolatedAsyncioTestCase):
    async def run_acquire(self, fake, env):
        session = Session()
        with tempfile.TemporaryDirectory() as folder, \
                patch('warm_download.aiohttp.ClientSession', return_value=session), \
                patch('s3_mirror.client', return_value=fake), \
                patch('s3_mirror.transfer_config', return_value=object()):
            log = Path(folder)/'weights.log'
            rows = await warm_download.acquire(DATA, Path(folder)/'t', Path(folder)/'b', time.time()+30, env, log=log)
            self.assertEqual((Path(folder)/'t'/NAME).read_bytes(), GOOD)
            return rows, session.calls, log.read_text()

    async def test_s3_is_first_and_the_worker_mirror_is_not_touched(self):
        fake = FakeS3()
        env = {**ENV, 'MIRROR_URL': 'https://mirror.invalid/models/x', 'MIRROR_AUTH_HEADER': 'Authorization: Bearer f'}
        rows, http, log = await self.run_acquire(fake, env)
        self.assertEqual(fake.calls, [('fixture-bucket', s3_mirror.main_key(REAL['revision'], NAME))])
        self.assertEqual([r['source'] for r in rows], ['s3'])
        self.assertEqual(http, [])
        self.assertIn('s3 first', log)

    async def test_s3_failure_falls_back_to_hf_loudly(self):
        rows, http, log = await self.run_acquire(FakeS3(fail=True), dict(ENV))
        self.assertEqual([r['source'] for r in rows], ['hf'])
        self.assertEqual(len(http), 1)
        self.assertIn('huggingface.co', http[0])
        self.assertIn('S3 FAILED', log)
        self.assertIn('FALLING BACK TO HF', log)
        self.assertNotIn('fixture', log)

    async def test_mirror_only_with_s3_never_contacts_hf(self):
        with self.assertRaises(s3_mirror.S3MirrorError):
            await self.run_acquire(FakeS3(fail=True), {**ENV, 'MIRROR_ONLY': '1'})


class GuardTests(unittest.TestCase):
    def test_guards_come_from_s3_with_their_org_key(self):
        repo, rev = 'org/guard', 'b'*40
        with tempfile.TemporaryDirectory() as folder:
            manifest = Path(folder)/'guards.json'
            manifest.write_text(json.dumps({'guards': [{'repo': repo, 'revision': rev, 'files': [
                {'filename': 'config.json', **FILE}]}]}))
            fake = FakeS3()
            env = {**ENV, 'NODE_RUN_TESTING': '1', 'GUARD_MANIFEST': str(manifest), 'GUARD_HF_URL': 'http://127.0.0.1:9'}
            with patch('s3_mirror.client', return_value=fake), patch('s3_mirror.transfer_config', return_value=object()):
                result = guard_weights.stage(env, repo, rev, Path(folder)/'g', timeout=1)
            self.assertEqual(result['sources'], {'s3': 1})
            self.assertEqual(fake.calls, [('fixture-bucket', f'models/{repo}/{rev}/config.json')])


if __name__ == '__main__':
    unittest.main()

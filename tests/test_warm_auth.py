"""Offline credentials/ready-contract regression tests; credentials are fixtures."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import mirror_auth
from redact import redact, secret_values
import warm
import warm_download
import warm_network

VALUE = 'fixture-only-private-value'
HEADER = 'X-Mirror-Access: '+VALUE
FILE = {'size': 4, 'sha256': hashlib.sha256(b'good').hexdigest()}
MANIFEST = {'model': {'repo': 'fixture/public', 'revision': 'a'*40},
            'files': {'x.safetensors': FILE}, 'differs_from_base': ['x.safetensors']}
MIRROR = {'revision': 'a'*40, 'files': MANIFEST['files']}


class Response:
    def __init__(self, body=b'good', obj=None, status=200):
        self.body, self.obj, self.status, self.content = body, obj, status, self

    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    def raise_for_status(self): pass
    async def json(self): return self.obj
    async def iter_chunked(self, size): yield self.body


class AuthTests(unittest.TestCase):
    def test_parse_bearer_custom_and_unset_without_exposing_repr(self):
        for line, name, value in ((HEADER, 'X-Mirror-Access', VALUE),
                                  ('Authorization: Bearer '+VALUE, 'Authorization', 'Bearer '+VALUE),
                                  ('X-Foo:  fixture:with:colons\t', 'X-Foo', 'fixture:with:colons')):
            auth = mirror_auth.parse_mirror_auth(line)
            self.assertEqual(auth.headers(), {name: value})
            for exposed in (auth, auth.headers(), auth.headers()[name]):
                self.assertNotIn(value, repr(exposed))
        self.assertIsNone(mirror_auth.parse_mirror_auth(None))
        self.assertIsNone(mirror_auth.parse_mirror_auth(''))

    def test_malformed_lines_fail_with_safe_clear_errors(self):
        bad_lines = [VALUE, ': '+VALUE, 'Bad Name: '+VALUE, ' X-Foo: '+VALUE,
                     'X-Foo : '+VALUE, 'X-Foo:', 'X-Foo: \t',
                     HEADER+'\r\nX-Extra: injected', HEADER+'\n', HEADER+'\0',
                     HEADER+'\x1f', HEADER+'\x7f', HEADER+'\u2603']
        for line in bad_lines:
            with self.subTest(case=bad_lines.index(line)):
                try:
                    mirror_auth.parse_mirror_auth(line)
                except ValueError as exc:
                    self.assertIn('MIRROR_AUTH_HEADER', str(exc))
                    self.assertNotIn(VALUE, str(exc)+repr(exc)+traceback.format_exc())
                else:
                    self.fail('Malformed header was accepted')

    def test_redaction_handles_plain_json_and_repr_forms(self):
        for value in (VALUE, 'fixture-quote"-slash\\-tab\t-value'):
            header = 'X-Foo: '+value
            secrets = secret_values({'MIRROR_AUTH_HEADER': header})
            self.assertNotIn(value, repr(secrets))
            for form in (header, value, json.dumps({'header': header, 'value': value}),
                         repr({'X-Foo': value}), repr(RuntimeError(value))):
                clean = redact(form, secrets)
                self.assertNotIn(value, clean)
                self.assertNotIn(json.dumps(value)[1:-1], clean)
                self.assertNotIn(repr(value)[1:-1], clean)
                self.assertIn('[REDACTED]', clean)

    def test_timing_and_command_logs_redact_header_and_value(self):
        with tempfile.TemporaryDirectory() as folder, patch('warm.ROOT', Path(folder)):
            obj = warm.Warm({'TUNNEL_SLOT':'a', 'WINDOW_END':str(int(time.time())+3600),
                             'MIRROR_AUTH_HEADER':HEADER})
            obj.record['diagnostic'] = {'header':HEADER, 'reflected_value':VALUE}
            obj.save()
            result = subprocess.CompletedProcess(['fixture'], 1, stdout=(HEADER+'\n'+VALUE).encode())
            with patch('warm.subprocess.run', return_value=result):
                with self.assertRaises(RuntimeError) as caught:
                    obj.command(['fixture'])
            for text in (obj.path.read_text(), (obj.run/'operations.log').read_text(), repr(caught.exception)):
                self.assertNotIn(VALUE, text)
            self.assertEqual(json.loads(obj.path.read_text())['diagnostic']['reflected_value'], '[REDACTED]')


class MirrorRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_mirror_only_never_contacts_hf_even_if_mirror_fails(self):
        for broken in (False, True):
            calls = []
            class Session:
                async def __aenter__(self): return self
                async def __aexit__(self, *args): pass
                def get(self, url, **options):
                    calls.append(url)
                    if 'huggingface.co' in url:
                        raise AssertionError('mirror-only leaked a request to HF')
                    if url.endswith('manifest.json'): return Response(obj=MIRROR)
                    return Response(body=b'bad!' if broken else b'good')
            with tempfile.TemporaryDirectory() as folder, patch('warm_download.aiohttp.ClientSession', return_value=Session()):
                task = warm_download.acquire(MANIFEST, Path(folder)/'target', Path(folder)/'base',
                                             time.time()+30, {'MIRROR_HTTP':'1','MIRROR_URL':'https://mirror.invalid','MIRROR_ONLY':'1'})
                if broken:
                    with self.assertRaises(mirror_auth.MirrorRequestError): await task
                else:
                    rows = await task
                    self.assertEqual(rows[0]['source'], 'mirror')
            self.assertEqual(len(calls), 2)

    async def test_mirror_only_requires_mirror_before_network(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                await warm_download.acquire(MANIFEST, Path(folder), Path(folder)/'base',
                                            time.time()+30, {'MIRROR_ONLY':'1'})

    async def test_manifest_and_shards_get_auth_but_hf_does_not(self):
        for header in (None, '', HEADER, 'Authorization: Bearer '+VALUE):
            calls = []
            class Session:
                async def __aenter__(self): return self
                async def __aexit__(self, *args): pass
                def get(self, url, **options):
                    calls.append((url, options))
                    if url.endswith('manifest.json'): return Response(obj=MIRROR)
                    return Response(body=b'bad!' if 'huggingface.co' in url else b'good')
            env = {'MIRROR_HTTP':'1','MIRROR_URL':'https://mirror.invalid'}
            if header is not None: env['MIRROR_AUTH_HEADER'] = header
            with tempfile.TemporaryDirectory() as folder, patch('warm_download.aiohttp.ClientSession', return_value=Session()):
                rows = await warm_download.acquire(MANIFEST, Path(folder)/'target', Path(folder)/'base', time.time()+30, env)
                self.assertEqual((Path(folder)/'target/x.safetensors').read_bytes(), b'good')
            self.assertEqual([r['source'] for r in rows], ['mirror'])
            # With a credential the mirror goes first and HF is never asked.
            self.assertEqual(len(calls), 2 if header else 3)
            for url, options in calls:
                self.assertNotIn(VALUE, url)
                if 'huggingface.co' in url or not header:
                    self.assertNotIn('headers', options)
                else:
                    self.assertEqual(options['headers'], mirror_auth.parse_mirror_auth(header).headers())
                    self.assertFalse(options['allow_redirects'])
                    self.assertNotIn(VALUE, repr(options))

    async def test_authenticated_mirror_goes_first_and_hf_only_covers_its_failures(self):
        for mirror_good in (True, False):
            calls = []
            class Session:
                async def __aenter__(self): return self
                async def __aexit__(self, *args): pass
                def get(self, url, **options):
                    calls.append(url)
                    if url.endswith('manifest.json'): return Response(obj=MIRROR)
                    if 'huggingface.co' in url: return Response(body=b'good')
                    return Response(body=b'good' if mirror_good else b'bad!')
            env = {'MIRROR_HTTP': '1', 'MIRROR_URL': 'https://mirror.invalid', 'MIRROR_AUTH_HEADER': HEADER, 'MIN_MBPS': '1e9'}
            with tempfile.TemporaryDirectory() as folder, patch('warm_download.aiohttp.ClientSession', return_value=Session()):
                log = Path(folder)/'weights.log'
                rows = await warm_download.acquire(MANIFEST, Path(folder)/'target', Path(folder)/'base',
                                                   time.time()+30, env, log=log)
                self.assertEqual((Path(folder)/'target/x.safetensors').read_bytes(), b'good')
                text = log.read_text()
            self.assertEqual([r['source'] for r in rows], ['mirror' if mirror_good else 'hf'])
            self.assertFalse('huggingface.co' in calls[1])
            self.assertEqual(len(calls), 2 if mirror_good else 3)
            self.assertIn('start 1 files 4 bytes, mirror first', text)
            self.assertIn(('mirror' if mirror_good else 'hf')+' x.safetensors 4 bytes', text)
            self.assertNotIn(VALUE, text)

    async def test_main_model_mirror_keys_have_no_org_segment(self):
        # MIRROR_URL is the model base, <origin>/models/<name> with no HF org, as staged
        # in R2; guards use <origin>/models/<org>/<name>/<rev>/ (test_guard_weights).
        real = json.loads((Path(__file__).resolve().parents[1]/'config/manifests/uncensored-c19ca5c.json').read_text())
        revision = real['revision']
        self.assertEqual(real['repo'], 'dealignai/DeepSeek-V4.1-Flash-1017-UNCENSORED-FP8')
        data = {'model': {'repo': real['repo'], 'revision': revision},
                'files': {'model-00001-of-00002.safetensors': FILE}, 'differs_from_base': ['model-00001-of-00002.safetensors']}
        calls = []
        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def get(self, url, **options):
                calls.append(url)
                if url.endswith('manifest.json'): return Response(obj={'revision': revision, 'files': data['files']})
                return Response(body=b'good')
        base = 'https://mirror.invalid/models/DeepSeek-V4.1-Flash-1017-UNCENSORED-FP8'
        env = {'MIRROR_HTTP': '1', 'MIRROR_URL': base+'/', 'MIRROR_AUTH_HEADER': HEADER}
        with tempfile.TemporaryDirectory() as folder, patch('warm_download.aiohttp.ClientSession', return_value=Session()):
            rows = await warm_download.acquire(data, Path(folder)/'target', Path(folder)/'base', time.time()+30, env)
        self.assertEqual(calls, [f'{base}/{revision}/manifest.json', f'{base}/{revision}/model-00001-of-00002.safetensors'])
        self.assertTrue(all('dealignai' not in url for url in calls))
        self.assertEqual([r['source'] for r in rows], ['mirror'])

    async def test_mirror_errors_drop_raw_exception_context_and_headers(self):
        http_error = warm_download.aiohttp.ClientResponseError(
            SimpleNamespace(real_url='https://mirror.invalid', headers={'X-Mirror-Access':VALUE}),
            (), status=403, message=HEADER, headers={'X-Reflected':VALUE})
        for error in (RuntimeError('HTTP error with reflected '+HEADER), http_error):
            session = Mock(); session.get.side_effect = error
            try:
                await mirror_auth.mirror_request(session, 'https://mirror.invalid/file', mirror_auth.parse_mirror_auth(HEADER),
                                                 lambda response: response.json())
            except mirror_auth.MirrorRequestError as exc:
                self.assertNotIn(VALUE, str(exc)+repr(exc)+traceback.format_exc())
                self.assertIsNone(exc.__cause__)
                self.assertIsNone(exc.__context__)
            else:
                self.fail('Expected sanitized request failure')

    async def test_reflected_response_body_error_has_no_exception_chain(self):
        class BadResponse(Response):
            async def json(self): raise ValueError('Reflected body '+HEADER)
        session = Mock(); session.get.return_value = BadResponse()
        try:
            await mirror_auth.mirror_request(session, 'https://mirror.invalid/manifest.json', mirror_auth.parse_mirror_auth(HEADER),
                                             lambda response: response.json())
        except mirror_auth.MirrorRequestError as exc:
            self.assertNotIn(VALUE, str(exc)+repr(exc)+traceback.format_exc())
            self.assertIsNone(exc.__context__)
        else:
            self.fail('Expected sanitized response failure')

    async def test_authenticated_mirror_redirect_is_not_followed(self):
        session = Mock()
        session.get.return_value = Response(status=302)
        with self.assertRaises(mirror_auth.MirrorRequestError):
            await mirror_auth.mirror_request(session, 'https://mirror.invalid/file', mirror_auth.parse_mirror_auth(HEADER),
                                             lambda response: response.json())
        self.assertEqual(session.get.call_count, 1)
        self.assertFalse(session.get.call_args.kwargs['allow_redirects'])


class ProbeTests(unittest.TestCase):
    def test_r2_probe_gets_auth_but_hf_and_location_do_not(self):
        for header in (None, '', HEADER, 'Authorization: Bearer '+VALUE):
            calls = []
            def open_request(request, **options):
                url = request if isinstance(request, str) else request.full_url
                headers = {} if isinstance(request, str) else dict(request.header_items())
                calls.append((url, {k.lower():v for k,v in headers.items()}))
                return io.BytesIO(b'colo=EWR\nloc=US\nip=not-recorded\n' if isinstance(request,str) else b'fixture')
            opener = Mock(); opener.open.side_effect = open_request
            with patch('warm_network.urllib.request.urlopen', side_effect=open_request), \
                 patch('warm_network.urllib.request.build_opener', return_value=opener) as build:
                report = warm_network.probe(MANIFEST, 'https://mirror.invalid', mirror_auth_header=header)
            self.assertEqual(len(calls), 3)
            self.assertTrue(report['hf']['ok'] and report['mirror']['ok'])
            self.assertNotIn(VALUE, json.dumps(report))
            for url, headers in calls:
                self.assertNotIn(VALUE, url)
                if header:
                    name, _, value = header.partition(':')
                    if 'mirror.invalid' in url: self.assertEqual(headers[name.lower()], value.strip())
                    else: self.assertNotIn(name.lower(), headers)
                else: self.assertEqual(set(headers) - {'range'}, set())
            self.assertEqual(build.call_count, int(bool(header)))
            if header:
                handler = build.call_args.args[0]
                self.assertIsInstance(handler, warm_network.NoMirrorRedirect)
                self.assertIsNone(handler.redirect_request(None,None,302,'',{},'https://huggingface.co/'))

    def test_probe_error_records_only_type_not_reflected_header(self):
        opener = Mock(); opener.open.side_effect = RuntimeError(HEADER)
        with patch('warm_network.urllib.request.build_opener', return_value=opener):
            result = warm_network.speed('https://mirror.invalid/test', mirror_auth.parse_mirror_auth(HEADER))
        self.assertEqual(result, {'ok':False, 'error':'RuntimeError'})
        self.assertNotIn(VALUE, repr(result))


class TunnelSlotTests(unittest.TestCase):
    def test_missing_invalid_slot_and_bad_header_fail_before_any_work(self):
        for slot in (None, '', 'c', 'A', ' a', 'b\n', VALUE):
            env = {} if slot is None else {'TUNNEL_SLOT':slot}
            with patch('warm.load_manifest') as load, patch('warm.acquire') as acquire:
                with self.assertRaisesRegex(ValueError, 'TUNNEL_SLOT') as caught:
                    warm.Warm(env)
                self.assertNotIn(VALUE, str(caught.exception))
                load.assert_not_called(); acquire.assert_not_called()
        with patch('warm.load_manifest') as load:
            with self.assertRaisesRegex(ValueError, 'MIRROR_AUTH_HEADER'):
                warm.Warm({'TUNNEL_SLOT':'a','MIRROR_AUTH_HEADER':'Bad '+VALUE})
            load.assert_not_called()

    def test_a_and_b_are_in_ready_file_stdout_and_timing_with_schema_one(self):
        for slot in ('a', 'b'):
            with tempfile.TemporaryDirectory() as folder, patch('warm.ROOT',Path(folder)):
                obj = warm.Warm({'TUNNEL_SLOT':slot,'WINDOW_END':str(int(time.time())+3600)})
                output = io.StringIO()
                with redirect_stdout(output): obj.publish_ready()
                signal = json.loads(output.getvalue().splitlines()[0].removeprefix('READY '))
                self.assertEqual(signal, json.loads(obj.ready.read_text()))
                self.assertEqual(signal['schema'], 1)
                self.assertEqual(signal['tunnel_slot'], slot)
                self.assertEqual(json.loads(obj.path.read_text())['tunnel_slot'], slot)

    def test_cli_rejects_missing_slot_with_clear_error_before_run(self):
        with tempfile.TemporaryDirectory() as folder, patch('warm.ROOT',Path(folder)), \
             patch.dict(os.environ, {}, clear=True), patch.object(warm.Warm,'run_all') as run:
            output = io.StringIO()
            with redirect_stderr(output): result = warm.main([])
            self.assertEqual(result,1)
            self.assertIn("TUNNEL_SLOT is required and must be exactly 'a' or 'b'", output.getvalue())
            self.assertNotIn('Traceback', output.getvalue())
            run.assert_not_called()

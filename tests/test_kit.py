import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('validate', ROOT/'claim/validate.py')
validate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validate)


class KitTests(unittest.TestCase):
    def test_claim_rejects_injection_inline_keys_and_expired_windows(self):
        with tempfile.NamedTemporaryFile() as key:
            claim = {'host':'node.example.invalid','port':22,'user':'ubuntu',
                     'key':{'path':key.name},'expires_at':int(time.time())+3600}
            self.assertEqual(validate.validate(claim)[0], claim['host'])
            for change in ({'host':'host;touch /tmp/no'}, {'key':{'inline':'secret'}}, {'expires_at':0}, {'port':True}):
                with self.assertRaises((ValueError, KeyError)):
                    validate.validate(dict(claim, **change))

    def test_dry_runs_cannot_invoke_provider_hook(self):
        env = dict(os.environ, DRY_RUN='1', PROVIDER_HOOKS='/nonexistent', INFER_UPSTREAM_KEY='fake-secret')
        for script, args in [('bootstrap.sh', []), ('watcher/watch.sh', ['--once'])]:
            result = subprocess.run(['bash', str(ROOT/script), *args], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('fake-secret', result.stdout+result.stderr)

    def test_parser_is_explicit_stub(self):
        result = subprocess.run(['python3', str(ROOT/'claim/parse.py')], input='test', capture_output=True, text=True)
        self.assertEqual(result.returncode,78)
        self.assertIn('not configured',result.stderr)

    def test_engine_binding_and_native_format(self):
        env = dict(os.environ, ENGINE='sglang', ENGINE_PORT='8000', MODEL='model', CONTEXT_LENGTH='32768',
                   MEM_FRACTION='.8', MAX_RUNNING_REQUESTS='256', CHUNKED_PREFILL_SIZE='4096',
                   CUDA_GRAPH_MAX_BS='256', PARALLELISM='tp', SPECULATION='none')
        out = subprocess.check_output(['python3',str(ROOT/'scripts/engine.py')],env=env).decode().split('\0')
        self.assertEqual(out[out.index('--host')+1],'127.0.0.1')
        self.assertNotIn('--kv-cache-dtype',out)
        self.assertNotIn('--speculative-algorithm',out)
        env['ENGINE_EXTRA_ARGS_JSON']='["--host=0.0.0.0"]'
        self.assertNotEqual(subprocess.run(['python3',str(ROOT/'scripts/engine.py')],env=env,capture_output=True).returncode,0)

    def test_redaction(self):
        result = subprocess.run(['python3',str(ROOT/'scripts/redact.py')],input='key=example-secret\n',
                                env=dict(os.environ, INFER_UPSTREAM_KEY='example-secret'), capture_output=True,text=True)
        self.assertEqual(result.stdout,'key=[REDACTED]\n')

    def test_status_atomic_history(self):
        with tempfile.TemporaryDirectory() as directory:
            env=dict(os.environ,RUN_DIR=directory,RUN_ID='test',MODEL='m',MODEL_REVISION='r',ENGINE='sglang',ENDPOINT='http://127.0.0.1:8000')
            for phase,ok in [('pull','running'),('health','running'),('health','error')]:
                subprocess.run(['python3',str(ROOT/'scripts/status.py'),phase,ok],env=env,check=True)
            status=json.loads(Path(directory,'status.json').read_text())
            self.assertEqual(status['ok'],'error')
            self.assertEqual([x['phase'] for x in status['timings']],['pull','health'])

    def test_status_public_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            env=dict(os.environ,RUN_DIR=directory,RUN_ID='test',MODEL='m',MODEL_REVISION='r',ENGINE='sglang',ENDPOINT='http://127.0.0.1:8000')
            for network,phase,expected in [
                ('cloudflared','ready',None),
                ('cloudflared','serving','https://api.infer.belweave.ai'),
                ('cloudflared','draining',None),
                ('ssh','serving',None),
            ]:
                with self.subTest(network=network,phase=phase):
                    Path(directory,'network.json').write_text(json.dumps({'selected':network}))
                    subprocess.run(['python3',str(ROOT/'scripts/status.py'),phase,'ok'],env=env,check=True)
                    status=json.loads(Path(directory,'status.json').read_text())
                    self.assertEqual(status['public_endpoint'],expected)

    def test_volume_reuse_requires_matching_checksum(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            files={'model-00001.safetensors':b'fixture weights',
                   'model.safetensors.index.json':json.dumps({'weight_map':{'w':'model-00001.safetensors'}}).encode()}
            manifest={'model':'fixture','revision':'a'*40,'files':[]}
            for name,body in files.items():
                (root/name).write_bytes(body)
                manifest['files'].append({'path':name,'size':len(body),'sha256':hashlib.sha256(body).hexdigest(),'git_sha1':None})
            (root/'metadata.json').write_text(json.dumps({'variants':{'fp8':manifest}}))
            env=dict(os.environ,WEIGHTS_DIR=directory,WEIGHTS_SOURCE='volume',MODEL='fixture',MODEL_REVISION='a'*40,MODEL_METADATA=str(root/'metadata.json'))
            command=['python3',str(ROOT/'scripts/weights.py')]
            good=subprocess.run(command,env=env,capture_output=True,text=True)
            self.assertEqual(good.returncode,0,good.stderr)
            (root/'model-00001.safetensors').write_bytes(b'corrupt weights')
            bad=subprocess.run(command,env=env,capture_output=True,text=True)
            self.assertNotEqual(bad.returncode,0)
            self.assertIn('checksum failed',bad.stderr)

import importlib.util
import io
import json
import hashlib
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
sys.path.insert(0, str(ROOT/'watcher'))
import network
import images
import readiness
import relay
import push


class NetworkTests(unittest.TestCase):
    def test_vendor_is_exact_netops_copy(self):
        self.assertEqual(hashlib.sha256((ROOT/'vendor/infer-gateway/node-tunnel.sh').read_bytes()).hexdigest(),
                         '845edd39cb7c91bf5dbcff06b09516833aafbd893ec9ddc42710cbd140fca498')

    def test_auto_success_and_each_probe_failure(self):
        success = lambda url: {'ok': True, 'http_status': 200}
        data=network.select({'NETWORK_MODE':'auto'},success)
        self.assertEqual(data['selected'],'cloudflared')
        self.assertEqual(len(data['probes']),3)
        for host in ('api.cloudflare.com','github.com','huggingface.co'):
            data=network.select({'NETWORK_MODE':'auto'},lambda url: {'ok':host not in url,'http_status':0 if host in url else 200})
            self.assertEqual(data['selected'],'ssh')

    def test_explicit_modes_never_probe(self):
        for mode,direction in [('cloudflared','forward'),('ssh','forward'),('ssh','reverse')]:
            probe=Mock(side_effect=AssertionError('Unexpected outbound probe'))
            data=network.select({'NETWORK_MODE':mode,'SSH_DIRECTION':direction},probe)
            self.assertEqual((data['selected'],data['ssh_direction']),(mode,direction))
            probe.assert_not_called()

    def test_failed_curl_timeout_is_a_failed_probe(self):
        with patch('network.subprocess.run',side_effect=subprocess.TimeoutExpired('curl',6)):
            self.assertFalse(network.probe('https://example.invalid')['ok'])

    def test_dry_run_modes_and_prestage(self):
        for mode,direction in [('cloudflared','forward'),('ssh','forward'),('ssh','reverse'),('auto','forward')]:
            env=dict(os.environ,DRY_RUN='1',NETWORK_MODE=mode,SSH_DIRECTION=direction,WEIGHTS_SOURCE='push')
            result=subprocess.run([str(ROOT/'bootstrap.sh')],env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn(f'NETWORK_MODE={mode}; SSH_DIRECTION={direction}',result.stdout)
            self.assertIn('IMAGE_SOURCE=push',result.stdout)
        for script in ('scripts/prestage.sh','scripts/reverse-tunnel.sh'):
            result=subprocess.run([str(ROOT/script)],env=dict(os.environ,DRY_RUN='1'),capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('DRY RUN',result.stdout)


class HealthTests(unittest.TestCase):
    def test_both_engine_commands_bind_main_port(self):
        for engine in ('sglang','vllm'):
            env=dict(os.environ,ENGINE=engine,ENGINE_PORT='8000',MODEL='fixture',CONTEXT_LENGTH='32768',MEM_FRACTION='.8',
                     MAX_RUNNING_REQUESTS='256',CHUNKED_PREFILL_SIZE='4096',CUDA_GRAPH_MAX_BS='256',PARALLELISM='tp',SPECULATION='none')
            args=subprocess.check_output([sys.executable,str(ROOT/'scripts/engine.py')],env=env).decode().strip('\0').split('\0')
            self.assertEqual(args[args.index('--host')+1],'127.0.0.1')
            self.assertEqual(args[args.index('--port')+1],'8000')

    def test_bootstrap_health_is_unauthenticated_on_8000(self):
        self.assertIn('scripts/origin_request.py" /health', (ROOT/'bootstrap.sh').read_text())
        request_seen=[]
        def open_request(req, **kwargs):
            request_seen.append(req)
            return io.BytesIO(b'')
        with patch.dict(os.environ,ENDPOINT='http://127.0.0.1:8000',INFER_UPSTREAM_KEY='test-only'), \
             patch.object(sys,'argv',['origin_request.py','/health']), \
             patch('urllib.request.urlopen',side_effect=open_request):
            runpy.run_path(str(ROOT/'scripts/origin_request.py'),run_name='__main__')
        self.assertEqual(request_seen[0].full_url,'http://127.0.0.1:8000/health')
        self.assertFalse(request_seen[0].has_header('Authorization'))

    def test_readiness_requires_public_health_protected_models_and_correct_model(self):
        calls=[]
        def open_request(req, **kwargs):
            url=req if isinstance(req,str) else req.full_url
            calls.append(url)
            if url.endswith('/health'):
                resp=io.BytesIO(b''); resp.status=200; return resp
            if isinstance(req,str):
                raise urllib.error.HTTPError(url,401,'Unauthorized',{},None)
            self.assertEqual(req.get_header('Authorization'),'Bearer test-only')
            return io.BytesIO(b'{"data":[{"id":"fixture"}]}')
        with patch('readiness.urllib.request.urlopen',side_effect=open_request):
            readiness.check('http://127.0.0.1:8000','test-only','fixture')
        self.assertEqual(calls,['http://127.0.0.1:8000/health','http://127.0.0.1:8000/v1/models','http://127.0.0.1:8000/v1/models'])


class TransferTests(unittest.TestCase):
    def make_stage(self, directory):
        stage=Path(directory)/'fp8'
        (stage/'weights').mkdir(parents=True)
        (stage/'images').mkdir()
        (stage/'weights'/'a.safetensors').write_bytes(b'fixture')
        (stage/'weights'/'b.json').write_bytes(b'{}')
        (stage/'images'/'engine.tar.zst').write_bytes(b'fake image')
        (stage/'images'/'engine.json').write_text('{}')
        files=[{'path':p.name,'size':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'git_sha1':None} for p in (stage/'weights').iterdir()]
        meta=Path(directory)/'metadata.json'
        meta.write_text(json.dumps({'variants':{'fp8':{'model':'fixture','revision':'a'*40,'files':files}}}))
        return dict(os.environ,STAGING_DIR=directory,WEIGHTS_DIR='/data/weights/fixture',REMOTE_PUSH_DIR='kit/runs/w/incoming',
                    MODEL='fixture',MODEL_REVISION='a'*40,MODEL_VARIANT='fp8',MODEL_METADATA=str(meta),WEIGHTS_SOURCE='push',
                    NODE_SSH_PORT='22',NODE_SSH_KEY_PATH='/keys/node',PUSH_WORKERS='2')

    def test_external_relay_exact_up_and_down_argv(self):
        run=Mock(return_value=subprocess.CompletedProcess([],0,stdout='window window-abc serving via relay port 18042, auto-down at 2000000000\n'))
        env={'RELAY_SH':'/netops/relay.sh'}
        args=['up','window-abc','ubuntu@node','--port','2222','--key','/keys/node','--expires','2000000000']
        self.assertEqual(relay.call(args,env,run),18042)
        self.assertEqual(run.call_args.args[0],['/netops/relay.sh',*args])
        relay.call(['down','window-abc'],env,run)
        self.assertEqual(run.call_args.args[0],['/netops/relay.sh','down','window-abc'])

    def test_remote_relay_passes_secret_only_through_stdin(self):
        run=Mock(return_value=subprocess.CompletedProcess([],0,stdout='RELAY_PORT=18043\n'))
        env={'RELAY_SH':'/netops/relay.sh','RELAY_HOST':'user@relay','CF_TUNNEL_TOKEN':'test-only-token'}
        args=['up','window-abc','ubuntu@node','--port','2200','--key','/relay/key','--expires','2000000000']
        self.assertEqual(relay.call(args,env,run),18043)
        self.assertEqual(run.call_args.args[0][0],'ssh')
        self.assertNotIn('test-only-token',' '.join(run.call_args.args[0]))
        payload=json.loads(run.call_args.kwargs['input'])
        self.assertEqual(payload['argv'],['/netops/relay.sh',*args])
        self.assertEqual(payload['env']['CF_TUNNEL_TOKEN'],'test-only-token')

    def test_push_calls_netops_interface_then_rsyncs_image(self):
        with tempfile.TemporaryDirectory() as directory:
            env=self.make_stage(directory); env['RELAY_SH']='/netops/relay.sh'; env['NODE_SSH_PORT']='2222'
            run=Mock(return_value=subprocess.CompletedProcess([],0))
            push.push('ubuntu@node',env,run)
            commands=[c.args[0] for c in run.call_args_list]
            self.assertEqual(commands[0][0],'ssh')
            self.assertEqual(commands[1],['/netops/relay.sh','push-weights','ubuntu@node',directory+'/fp8/weights','/data/weights/fixture','--port','2222','--key','/keys/node'])
            self.assertEqual(commands[2][0],'rsync')
            self.assertIn(directory+'/fp8/images/engine.tar.zst',commands[2])

    def test_fallback_uses_disjoint_parallel_resumable_file_lists(self):
        with tempfile.TemporaryDirectory() as directory:
            env=self.make_stage(directory); env.pop('RELAY_SH',None)
            run=Mock(return_value=subprocess.CompletedProcess([],0))
            push.push('ubuntu@node',env,run)
            copies=[c for c in run.call_args_list if '--files-from=-' in c.args[0]]
            self.assertEqual(len(copies),2)
            paths=[]
            for c in copies:
                self.assertIn('--partial',c.args[0]); paths+=c.kwargs['input'].rstrip(b'\0').split(b'\0')
            self.assertEqual(sorted(paths),[b'a.safetensors',b'b.json'])
            self.assertEqual(len(paths),len(set(paths)))

    def test_image_push_hashes_before_docker_load_and_checks_image_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); archive=root/'engine.tar.zst'; archive.write_bytes(b'fixture archive')
            meta={'source_image':'engine:test','id':'sha256:abc','architecture':'amd64','os':'linux',
                  'archive_bytes':archive.stat().st_size,'archive_sha256':images.sha256(archive),'repo_digests':['engine@sha256:registry']}
            (root/'engine.json').write_text(json.dumps(meta))
            env={'IMAGE_SOURCE':'push','PUSH_DIR':directory,'RUN_DIR':directory,'ENGINE_IMAGE':'engine:test'}
            proc=Mock(stdout=io.BytesIO(b'fake tar')); proc.wait.return_value=0; proc.poll.return_value=0
            with patch('images.subprocess.Popen',return_value=proc) as popen, \
                 patch('images.subprocess.run',return_value=subprocess.CompletedProcess([],0)) as run, \
                 patch('images.inspect',return_value={'id':'sha256:abc','architecture':'amd64','os':'linux'}):
                images.acquire(env)
                self.assertEqual(popen.call_args.args[0],['zstd','-dc',str(archive)])
                self.assertEqual(run.call_args.args[0],['docker','load'])
            self.assertEqual(json.loads((root/'loaded-image.json').read_text())['effective_ref'],'sha256:abc')
            archive.write_bytes(b'corrupted')
            with patch('images.subprocess.Popen') as popen:
                with self.assertRaisesRegex(ValueError,'checksum mismatch'): images.acquire(env)
                popen.assert_not_called()

class ReverseAndIdentityTests(unittest.TestCase):
    def test_reverse_script_uses_loopback_R_and_keepalive_with_fake_ssh(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); binary=root/'bin'; binary.mkdir()
            ssh=binary/'ssh'
            ssh.write_text('#!/bin/bash\nset -eu\nprintf "%s\\n" "$@" > "$MOCK_ARGS"\ntouch "$RUN_DIR/stop"\n')
            ssh.chmod(0o755)
            sleep=binary/'sleep'; sleep.write_text('#!/bin/bash\nexit 0\n'); sleep.chmod(0o755)
            import time
            env=dict(os.environ,PATH=str(binary)+':'+os.environ['PATH'],RUN_DIR=directory,MOCK_ARGS=str(root/'args'),
                     REVERSE_RELAY_TARGET='user@relay.invalid',REVERSE_RELAY_PORT='18234',WINDOW_END=str(int(time.time())+3600))
            env.pop('DRY_RUN',None)
            result=subprocess.run(['/bin/bash',str(ROOT/'scripts/reverse-tunnel.sh')],env=env,capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            args=(root/'args').read_text().splitlines()
            self.assertEqual(args[args.index('-R')+1],'127.0.0.1:18234:127.0.0.1:8000')
            self.assertIn('ServerAliveInterval=15',args)
            self.assertIn('ExitOnForwardFailure=yes',args)
            self.assertEqual(args[-1],'user@relay.invalid')

    def test_origin_alias_warns_once_and_never_prints_value(self):
        env=dict(os.environ,ORIGIN_KEY='test-only-value')
        env.pop('INFER_UPSTREAM_KEY',None); env.pop('ORIGIN_ALIAS_WARNED',None)
        result=subprocess.run(['/bin/bash','-c','source "$1"; source "$1"','bash',str(ROOT/'scripts/common.sh')],
                              env=env,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stderr.count('ORIGIN_KEY is deprecated'),1)
        self.assertNotIn('test-only-value',result.stdout+result.stderr)
        self.assertNotIn('TUNNEL_TOKEN=',(ROOT/'bootstrap.sh').read_text())

    def test_both_committed_variants_have_all_weight_sha256s(self):
        variants=json.loads((ROOT/'docs/model-metadata.json').read_text())['variants']
        self.assertEqual(set(variants),{'fp8','nvfp4'})
        for data in variants.values():
            self.assertEqual(len(data['revision']),40)
            self.assertTrue(data['files'])
            for f in data['files']:
                if f['path'].endswith('.safetensors'):
                    self.assertEqual(len(f['sha256']),64)

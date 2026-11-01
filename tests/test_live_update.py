"""Regression checks for the live-window defaults, auth and operator handoff."""
import io
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'watcher'))
import relay


class LiveUpdateTests(unittest.TestCase):
    def test_profiles_generate_both_engine_commands(self):
        for engine in ('sglang', 'vllm'):
            for profile, context, running in (('longctx','409600','64'), ('throughput','32768','256')):
                env = dict(os.environ, ENGINE=engine, PROFILE=profile, DRY_RUN='1')
                for name in ('CONTEXT_LENGTH','MAX_RUNNING_REQUESTS','CUDA_GRAPH_MAX_BS','ENGINE_EXTRA_ARGS_JSON'):
                    env.pop(name, None)
                result = subprocess.run([str(ROOT/'bootstrap.sh')], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'Context {context}; max running {running}; graph max {running}; SYS_NICE', result.stdout)
                script = '''set -a
if [[ $PROFILE == throughput ]]; then source config/throughput.env; fi
source "config/$ENGINE.env"
ENGINE_PORT=8000; MODEL=fixture
python3 scripts/engine.py
'''
                args = subprocess.check_output(['bash','-c',script], cwd=ROOT, env=env).decode().strip('\0').split('\0')
                ctxflag, maxflag = ('--context-length','--max-running-requests') if engine == 'sglang' else ('--max-model-len','--max-num-seqs')
                self.assertEqual(args[args.index(ctxflag)+1], context)
                self.assertEqual(args[args.index(maxflag)+1], running)
        result = subprocess.run([str(ROOT/'bootstrap.sh')],env=dict(os.environ,DRY_RUN='1',PROFILE='unknown'),capture_output=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('--cap-add SYS_NICE', (ROOT/'bootstrap.sh').read_text())

    def test_vllm_env_auth_and_sglang_narrow_fallback(self):
        fake = 'sk-test-FAKE-inner-auth'
        for engine in ('sglang','vllm'):
            payload = {'kind':'engine','env':{'ENGINE':engine,'INFER_UPSTREAM_KEY':fake}}
            argv = ['python3','-m','sglang.launch_server'] if engine == 'sglang' else ['vllm','serve','/weights']
            observed = []
            def execute(binary, args):
                observed.append((args.copy(),os.environ.copy()))
            with patch.dict(os.environ, {}, clear=True), patch('sys.stdin', io.StringIO(json.dumps(payload))), \
                 patch('subprocess.check_output', return_value=('\0'.join(argv)+'\0').encode()), patch('os.execvp', side_effect=execute):
                runpy.run_path(str(ROOT/'scripts/serve.py'),run_name='__main__')
            args, env = observed[0]
            if engine == 'vllm':
                self.assertNotIn(fake,' '.join(args))
                self.assertNotIn('--api-key',args)
                self.assertEqual(env['VLLM_API_KEY'],fake)
            else:
                self.assertEqual(args[-2:],['--api-key',fake])
        self.assertNotIn(fake,(ROOT/'bootstrap.sh').read_text())

    def test_fake_keys_never_reach_engine_log_or_teardown_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory); run=base/'run-test'; run.mkdir()
            fake='sk-test-FAKE-exact-secret'
            envfile=base/'node.env'; envfile.write_text('INFER_UPSTREAM_KEY='+shlex.quote(fake)+'\n'); envfile.chmod(0o600)
            env=dict(os.environ, HOME=str(base), NODE_ENV_FILE=str(envfile))
            env.pop('DRY_RUN',None)
            engine=base/'fake_engine.py'
            engine.write_text('import os,sys\nprint("startup key="+os.environ["INFER_UPSTREAM_KEY"])\nprint("api_key=\\\'sk-test-FAKE-old\\\'",file=sys.stderr)\nprint(\'{"api_key": "sk-test-FAKE-json"}\')\nprint("Bearer sk-test-FAKE-bearer")\n')
            result=subprocess.run([str(ROOT/'scripts/capture.sh'),str(run/'engine.log'),sys.executable,str(engine)],env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertNotIn('sk-test-FAKE', (run/'engine.log').read_text())
            # Re-filter collected/legacy logs too, even when they bypassed capture.
            (run/'collected.log').write_text(fake+'\napi_key="sk-test-FAKE-other"\nBearer sk-test-FAKE-unknown\n')
            (run/'node.env').write_text('SHOULD_NOT_BE_ARCHIVED=1\n')
            (run/'incoming').mkdir(); (run/'incoming/image.tar.zst').write_bytes(b'large-image-fixture')
            archive=base/'result.tar.gz'
            subprocess.run([str(ROOT/'scripts/capture.sh'),str(base/'archive.log'),sys.executable,
                            str(ROOT/'scripts/archive.py'),str(run),str(archive)],env=env,check=True)
            with tarfile.open(archive) as tf:
                self.assertEqual(tf.getnames(),['run-test/collected.log','run-test/engine.log'])
                for member in tf.getmembers():
                    self.assertNotIn(b'sk-test-FAKE',tf.extractfile(member).read())
            self.assertIn('scripts/archive.py', (ROOT/'teardown.sh').read_text())
            self.assertIn('--log-driver none', (ROOT/'bootstrap.sh').read_text())

    def test_node_env_is_sourced_only_when_present_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory); envfile=base/'node.env'
            env=dict(os.environ,HOME=directory,NODE_ENV_FILE='~/node.env',DRY_RUN='0')
            command=['bash','-ec','source "$1"; test "$INFER_UPSTREAM_KEY" = fixture-node-value','bash',str(ROOT/'scripts/node-env.sh')]
            envfile.write_text('INFER_UPSTREAM_KEY=fixture-node-value\n'); envfile.chmod(0o600)
            self.assertEqual(subprocess.run(command,env=env,capture_output=True).returncode,0)
            envfile.chmod(0o644)
            self.assertNotEqual(subprocess.run(command,env=env,capture_output=True).returncode,0)

    def test_relay_reads_chosen_port_file_and_rejects_bad_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory); (p/'window-a.port').write_text('18999\n')
            env={'RELAY_SH':'/external/relay.sh','RELAY_STATE':directory,'RUN_DIR':directory}
            runner=Mock(return_value=subprocess.CompletedProcess([],0,stdout=''))
            self.assertEqual(relay.call(['up','window-a','user@node','--port','2222','--expires','2000000000'],env,runner),18999)
            self.assertEqual(json.loads((p/'relay.status.json').read_text())['relay_port'],18999)
            for args in (['up','window-a','u@n','/old/key'],['up','window-a','u@n','--port','0'],['down','window-a','--key','x']):
                with self.assertRaises(ValueError): relay.call(args,env,runner)

    def test_live_doc_is_interactive_safe_key_free_and_redacts_in_container(self):
        doc=(ROOT/'docs/live-node.md').read_text()
        self.assertNotIn('~/.infer_key',doc)
        self.assertNotIn('set -euo pipefail',doc)
        self.assertNotIn('docker attach',doc)
        self.assertNotRegex(doc,r'(?:INFER_UPSTREAM_KEY|CF_TUNNEL_TOKEN)=[^\s]')
        blocks=re.findall(r'```bash\n(.*?)```',doc,re.S)
        self.assertGreaterEqual(len(blocks),7)
        for block in blocks:
            # Validate shell syntax after replacing documentation placeholders.
            runnable=block.replace('<port>','2222').replace('<user@node>','user@node')
            result=subprocess.run(['bash','-n'],input=runnable,text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            if 'tail -f' in block:
                self.assertEqual(len([l for l in block.splitlines() if l and not l.startswith('#')]),1)
        launch=next(b for b in blocks if 'docker run' in b)
        self.assertIn('-e INFER_UPSTREAM_KEY',launch)
        self.assertIn('--log-driver none',launch)
        self.assertIn('| python3 -u /redact.py >> /logs/dsv41.log',launch)
        # Exercise the exact embedded redactor using fake startup output.
        redactor=launch.split("<<'PY'\n",1)[1].split('\nPY\n',1)[0]
        result=subprocess.run([sys.executable,'-c',redactor],input="api_key='fake-echo'\nBearer fake-bearer\nfixture-key\n",env=dict(os.environ,INFER_UPSTREAM_KEY='fixture-key'),capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        for value in ('fake-echo','fake-bearer','fixture-key'):self.assertNotIn(value,result.stdout)

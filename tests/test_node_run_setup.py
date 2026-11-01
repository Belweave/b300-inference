"""Offline first-boot checks: fake interpreters, dependencies and hardware."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import guards
import node_run


FAKE_PYTHON = r'''import json
import os
from pathlib import Path
import sys
args = sys.argv[1:]
with Path(os.environ['CALLS']).open('a') as stream:
    stream.write(json.dumps({'python': sys.argv[0], 'args': args})+'\n')
if args[:2] == ['-m', 'venv']:
    assert sys.stdin.read() == ''
    if os.environ.get('FAIL_VENV') == '1':
        sys.exit(1)
    target = Path(args[2])/'bin/python3'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(Path(__file__).read_text())
    target.chmod(0o700)
    target.with_name('pip').symlink_to(target.name)
elif args[:2] == ['-m', 'pip']:
    assert sys.stdin.read() == ''
    if 'install' in args:
        print('fake pip install')
        sys.exit(int(os.environ.get('FAIL_PIP', '0')))
else:
    print('fake runner')
'''


class LauncherTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)/'kit with spaces'
        (self.root/'scripts').mkdir(parents=True)
        for name in ('node-run.sh', 'warm.sh'):
            shutil.copy2(ROOT/'scripts'/name, self.root/'scripts'/name)
        shutil.copy2(ROOT/'requirements-node.txt', self.root/'requirements-node.txt')
        self.bin = Path(temp.name)/'bin'
        self.bin.mkdir()
        self.python = self.bin/'python3'
        self.python.write_text('#!'+sys.executable+'\n'+FAKE_PYTHON)
        self.python.chmod(0o700)
        self.calls = Path(temp.name)/'calls.jsonl'
        self.env = {'PATH': str(self.bin)+os.pathsep+os.environ['PATH'], 'HOME': temp.name,
                    'CALLS': str(self.calls)}

    def invoke(self, script='node-run.sh', **env):
        # The key pipe belongs to the runner; setup must leave it untouched.
        with tempfile.TemporaryFile() as stdin:
            stdin.write(b'unread stdin fixture\n')
            stdin.seek(0)
            result = subprocess.run([str(self.root/'scripts'/script), '--fixture'],
                                    env={**self.env, **env}, stdin=stdin,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(stdin.tell(), 0)
        return result

    def history(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def test_missing_venv_support_fails_before_stdin_or_runner(self):
        result = self.invoke(FAIL_VENV='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('sudo apt-get install -y python3-venv', result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual([call['args'] for call in self.history()],
                         [['-m', 'venv', str(self.root/'.venv')]])
        self.assertFalse((self.root/'runs').exists())

    def test_stamp_skips_install_until_requirements_change(self):
        first = self.invoke()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, 'fake runner\n')
        self.assertIn('fake pip install', first.stderr)
        second = self.invoke()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertNotIn('fake pip install', second.stderr)
        self.assertEqual(sum(call['args'][:2] == ['-m', 'pip'] for call in self.history()), 1)
        installs = lambda: [call for call in self.history() if call['args'][:3] == ['-m', 'pip', 'install']]
        self.assertEqual(len(installs()), 1)
        with (self.root/'requirements-node.txt').open('a') as stream:
            stream.write('# changed\n')
        third = self.invoke()
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertEqual(len(installs()), 2)
        self.assertEqual(sum(call['args'][:2] == ['-m', 'venv'] for call in self.history()), 1)
        last = self.history()[-1]
        self.assertEqual(last['python'], str(self.root/'.venv/bin/python3'))
        self.assertEqual(last['args'], ['-B', str(self.root/'scripts/node_run.py'), '--fixture'])

    def test_failed_install_does_not_stamp_success(self):
        self.assertNotEqual(self.invoke(FAIL_PIP='1').returncode, 0)
        self.assertFalse((self.root/'.venv/requirements-node.sha256').exists())
        self.assertEqual(self.invoke().returncode, 0)
        self.assertEqual(sum(call['args'][:3] == ['-m', 'pip', 'install'] for call in self.history()), 2)

    def test_python_override_skips_venv_management(self):
        result = self.invoke(NODE_RUN_PYTHON=str(self.python), FAIL_VENV='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root/'.venv').exists())
        self.assertEqual(len(self.history()), 1)
        self.assertEqual(self.history()[0]['python'], str(self.python))

    def test_dry_run_skips_venv_management(self):
        result = self.invoke(DRY_RUN='1', FAIL_VENV='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root/'.venv').exists())
        self.assertEqual(len(self.history()), 1)
        self.assertEqual(self.history()[0]['python'], str(self.python))

    def test_warm_uses_override_then_venv_then_system_python(self):
        self.assertEqual(self.invoke(script='warm.sh').returncode, 0)
        self.assertEqual(self.history()[-1]['python'], str(self.python))
        self.assertEqual(self.invoke().returncode, 0)
        self.assertEqual(self.invoke(script='warm.sh').returncode, 0)
        self.assertEqual(self.history()[-1]['python'], str(self.root/'.venv/bin/python3'))
        self.assertEqual(self.invoke(script='warm.sh', NODE_RUN_PYTHON=str(self.python)).returncode, 0)
        self.assertEqual(self.history()[-1]['python'], str(self.python))


class PreflightSetupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for mocker in (patch('node_run.ROOT', self.root), patch.dict(os.environ, {}, clear=True),
                       patch('node_run.signal.setitimer'), patch('node_run.socket.socket'),
                       patch('node_run.shutil.which', return_value='/fixture/tool'),
                       patch.dict(sys.modules, huggingface_hub=Mock())):
            mocker.start()
            self.addCleanup(mocker.stop)
        self.runner = node_run.NodeRun()
        self.addCleanup(lambda: self.runner.lock.close() if self.runner.lock else None)
        self.weights = self.root/'models/weights'
        self.runner.env = {'WEIGHTS_DIR': str(self.weights), 'INFER_UPSTREAM_KEY': 'fixture',
                           'CREDIT_CAP_SECONDS': '11250', 'PROVISIONED_AT': str(time.time()-1),
                           'TUNNEL_SLOT': 'a', 'SAFETY_MODEL_REVISION': 'a'*40, 'SAFETY_QWEN_REVISION': 'a'*40}
        self.runner.source_node_env = Mock()
        self.runner.tunnel_preflight = Mock()
        self.runner.command = Mock(side_effect=lambda argv: (0, 'B300, 288000 MiB\n'*8
                                                            if argv[0] == 'nvidia-smi' else 'a'*40))
        self.runner.disk_check = Mock()
        self.runner.clean_stale = Mock()
        self.runner.ports_free = Mock()
        self.runner.stage_guards = Mock()
        self.runner.fingerprint = Mock(return_value={})

    def assert_no_docker(self):
        self.assertFalse(any(call.args[0][0] == 'docker' for call in self.runner.command.call_args_list))
        self.runner.disk_check.assert_not_called()

    def test_missing_dependencies_include_exact_repair_command(self):
        for missing in ('aiohttp', 'huggingface_hub', 'boto3'):
            with self.subTest(module=missing):
                def dependency(name):
                    if name == missing:
                        raise ModuleNotFoundError(name)
                    return Mock()

                output = io.StringIO()
                with patch('node_run.importlib.import_module', side_effect=dependency), \
                     patch('s3_mirror.enabled', return_value=True), \
                     redirect_stdout(output), self.assertRaises(node_run.GateFailure) as caught:
                    with self.runner.gate('preflight'):
                        self.runner.preflight()
                fix = shlex.join([sys.executable, '-m', 'pip', 'install', '-r', str(self.root/'requirements-node.txt')])
                expected = f'python dependency {missing} is missing; run: {fix}'
                self.assertEqual(str(caught.exception), expected)
                self.assertIn('FAIL preflight: '+expected, output.getvalue())
                self.assert_no_docker()
                self.assertFalse(self.weights.exists())
                self.runner.lock.close()

    def test_unwritable_weights_or_guard_cache_fails_before_docker(self):
        for target in (self.weights, self.root/'models/guards'):
            for operation in ('mkdir', 'tempfile'):
                with self.subTest(path=target, operation=operation):
                    real_mkdir, real_tempfile = Path.mkdir, tempfile.TemporaryFile

                    def mkdir(path, *args, **kwargs):
                        if path == target and operation == 'mkdir':
                            raise PermissionError()
                        return real_mkdir(path, *args, **kwargs)

                    def probe(*args, **kwargs):
                        if kwargs['dir'] == target:
                            raise PermissionError()
                        return real_tempfile(*args, **kwargs)

                    output = io.StringIO()
                    with patch.object(Path, 'mkdir', mkdir), patch('node_run.tempfile.TemporaryFile', side_effect=probe), \
                         redirect_stdout(output), self.assertRaises(node_run.GateFailure) as caught:
                        with self.runner.gate('preflight'):
                            self.runner.preflight()
                    expected = (f'weights dir not writable: {target}; fix: '
                                f'sudo mkdir -p {target} && sudo chown $USER {target}')
                    self.assertEqual(str(caught.exception), expected)
                    self.assertIn('FAIL preflight: '+expected, output.getvalue())
                    report = json.loads(self.runner.path.read_text())
                    self.assertEqual(report['gates'][-1]['reason'], expected)
                    self.assert_no_docker()
                    self.runner.lock.close()

    def test_writable_dirs_created_and_probes_removed_before_docker(self):
        for custom in (False, True):
            with self.subTest(custom=custom):
                if custom:
                    self.runner.env['GUARD_WEIGHTS_DIR'] = str(self.root/'custom/cache')
                cache = guards.cache_dir(self.runner.env)
                command = self.runner.command.side_effect

                def checked(argv):
                    if argv[0] == 'docker':
                        for path in (self.weights, cache):
                            self.assertTrue(path.is_dir())
                            self.assertEqual(list(path.iterdir()), [])
                    return command(argv)

                self.runner.command.side_effect = checked
                self.runner.preflight()
                self.assertEqual(self.runner.guard_plans[0]['cache_dir'], str(cache))
                self.runner.command.side_effect = command
                self.runner.lock.close()

    def test_subprocess_helpers_receive_running_interpreter(self):
        process = Mock(returncode=0, communicate=Mock(return_value=('', '')))
        with patch('node_run.subprocess.Popen', return_value=process) as spawn:
            node_run.NodeRun.command(self.runner, ['helper'], env={'NODE_RUN_PYTHON': '/wrong/python'})
        self.assertEqual(spawn.call_args.kwargs['env']['NODE_RUN_PYTHON'], sys.executable)

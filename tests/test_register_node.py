"""Mac-side node registration: nodes.conf gets b300-<node_id>, the address stays in a local ssh file."""
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'watcher'))
import register_node  # noqa: E402

# A fake ssh. FAKE_NODE picks how the node answers: json (status.sh --json works),
# old (no --json yet, so the direct hash), down (ssh fails), junk (no usable id).
FAKE_SSH = '''#!PYTHON
import json,os,sys
args=sys.argv[1:]
with open(os.environ['FAKE_LOG'],'a') as out: out.write(json.dumps(args)+'\\n')
if args[0]=='-G':
    target=args[-1]
    known=os.environ.get('FAKE_ALIAS_OK')=='1' or not target.startswith('b300-')
    host=os.environ.get('FAKE_HOSTNAME','node.example.invalid')
    port=args[args.index('-p')+1] if '-p' in args else '22'
    print('hostname '+(host if known else target)); print('user ubuntu'); print('port '+port)
    print('proxyjump none')
    sys.exit(0)
mode=os.environ.get('FAKE_NODE','json'); cmd=args[-1]; nid=os.environ.get('FAKE_ID','a1b2c3d4')
if mode=='down': sys.exit(255)
if 'status.sh --json' in cmd:
    if mode=='json': print(json.dumps({'schema':1,'node_id':nid,'phase':'no_run'})); sys.exit(0)
    print('status.sh: error: unrecognized arguments: --json', file=sys.stderr); sys.exit(2)
if cmd=='python3 -':
    code=sys.stdin.read(); assert 'machine-id' in code
    print('not-an-id' if mode=='junk' else nid); sys.exit(0)
sys.exit(1)
'''


class RegisterNodeTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.ssh = self.dir/'ssh'
        self.ssh.write_text(FAKE_SSH.replace('PYTHON', sys.executable))
        self.ssh.chmod(0o755)
        self.conf = self.dir/'b300-duo'/'nodes.conf'
        self.conf.parent.mkdir()
        self.conf.write_text('# One ssh target per line.\nb300-old\n')
        self.ssh_file = self.dir/'home'/'.ssh'/'b300-nodes.conf'
        self.env = dict(os.environ, FAKE_LOG=str(self.dir/'calls.jsonl'), FAKE_ALIAS_OK='1')

    def register(self, *extra, target='ubuntu@node.example.invalid', **env):
        argv = [sys.executable, str(ROOT/'watcher/register_node.py'), '--ssh', str(self.ssh),
                '--nodes-conf', str(self.conf), '--ssh-config', str(self.ssh_file), *extra, '--', target]
        return subprocess.run(argv, env={**self.env, **env}, capture_output=True, text=True, timeout=30)

    def targets(self):
        return [line.split('#', 1)[0].strip() for line in self.conf.read_text().splitlines()
                if line.split('#', 1)[0].strip()]

    def test_twice_gives_one_entry(self):
        for _ in range(2):
            result = self.register('-p', '2222')
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.targets(), ['b300-old', 'b300-a1b2c3d4'])
        text = self.ssh_file.read_text()
        self.assertEqual(text.count('Host b300-a1b2c3d4'), 1)
        self.assertIn('    HostName node.example.invalid\n    User ubuntu\n    Port 2222', text)
        self.assertNotIn('ProxyJump', text)
        self.assertEqual(self.ssh_file.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('example.invalid', self.conf.read_text())  # no address in nodes.conf
        self.assertTrue(self.conf.read_text().startswith('# One ssh target per line.\n'))

    def test_node_without_json_uses_the_same_hash(self):
        result = self.register(FAKE_NODE='old', FAKE_ID='0f0f0f0f')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('b300-0f0f0f0f', self.targets())
        code = [json.loads(line) for line in (self.dir/'calls.jsonl').read_text().splitlines()]
        self.assertEqual([c[-1] for c in code if c[0] != '-G'][:2],
                         ['~/b300-inference/scripts/status.sh --json', 'python3 -'])

    def test_hash_matches_node_status(self):
        machine = 'f' * 32
        local = {}
        exec(register_node.NODE_ID_PY.replace("open('/etc/machine-id').read()", repr(machine))
             .replace('print(', 'local["id"]=('), {'local': local})
        import hashlib
        self.assertEqual(local['id'], hashlib.sha256(machine.encode()).hexdigest()[:8])

    def test_identity_path_is_stored_not_read(self):
        key = self.dir/'key-ref'
        key.write_text('fixture, never read\n')
        result = self.register('-i', str(key))
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.ssh_file.read_text()
        self.assertIn(f'    IdentityFile {key}\n    IdentitiesOnly yes', text)
        self.assertNotIn('never read', text)

    def test_new_address_updates_in_place(self):
        self.register()
        result = self.register(FAKE_HOSTNAME='other.example.invalid')
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.ssh_file.read_text()
        self.assertEqual(text.count('Host b300-a1b2c3d4'), 1)
        self.assertIn('HostName other.example.invalid', text)
        self.assertNotIn('HostName node.example.invalid', text)
        self.assertEqual(self.targets().count('b300-a1b2c3d4'), 1)

    def test_recycled_address_replaces_the_old_node(self):
        self.register(FAKE_ID='11111111')
        result = self.register(FAKE_ID='22222222')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('b300-11111111 had the same address', result.stderr)
        self.assertEqual(self.targets(), ['b300-old', 'b300-22222222'])
        self.assertNotIn('b300-11111111', self.ssh_file.read_text())

    def test_concurrent_runs_lose_nothing_and_duplicate_nothing(self):
        argv = lambda nid: [sys.executable, str(ROOT/'watcher/register_node.py'), '--ssh', str(self.ssh),
                            '--nodes-conf', str(self.conf), '--ssh-config', str(self.ssh_file),
                            f'ubuntu@{nid}.example.invalid']
        procs = []
        for i in range(12):
            nid = f'{i % 6:08x}'
            env = {**self.env, 'FAKE_ID': nid, 'FAKE_HOSTNAME': f'{nid}.example.invalid'}
            procs.append(subprocess.Popen(argv(nid), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))
        for p in procs:
            with p.stderr:
                self.assertEqual(p.wait(timeout=60), 0, p.stderr.read())
        targets = self.targets()
        self.assertEqual(sorted(targets), sorted(['b300-old'] + [f'b300-{i:08x}' for i in range(6)]))
        text = self.ssh_file.read_text()
        for i in range(6):
            self.assertEqual(text.count(f'Host b300-{i:08x}\n'), 1)
        self.assertEqual(list(self.conf.parent.glob('.nodes.conf.*')), [])

    def test_failures_change_nothing(self):
        before = self.conf.read_text()
        for mode in ('down', 'junk'):
            result = self.register(FAKE_NODE=mode)
            self.assertEqual(result.returncode, 1)
            self.assertIn('not registered', result.stderr)
            self.assertNotIn('example.invalid', result.stderr + result.stdout)
        self.assertEqual(self.conf.read_text(), before)
        self.assertFalse(self.ssh_file.exists())

    def test_missing_include_is_reported_but_registered(self):
        result = self.register(FAKE_ALIAS_OK='0')
        self.assertEqual(result.returncode, 3)
        self.assertIn(f'Include {self.ssh_file}', result.stderr)
        self.assertIn('b300-a1b2c3d4', self.targets())

    def test_other_ssh_config_content_is_kept(self):
        self.ssh_file.parent.mkdir(parents=True)
        self.ssh_file.write_text('# mine\nHost b300-a1b2c3d4\n    HostName stale.example.invalid\n\nHost b300-a1b2c3d4\n    HostName stale.example.invalid\n')
        self.ssh_file.chmod(0o600)
        result = self.register()
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.ssh_file.read_text()
        self.assertTrue(text.startswith('# mine\n'))
        self.assertEqual(text.count('Host b300-a1b2c3d4'), 1)
        self.assertNotIn('stale', text)

    def test_target_must_be_one_destination(self):
        for bad in ('-oProxyCommand=x', 'a b', 'host;id'):
            result = self.register(target=bad)
            self.assertEqual(result.returncode, 2)


if __name__ == '__main__':
    unittest.main()

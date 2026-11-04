#!/usr/bin/env python3
"""Add a B300 node to the Mac's b300-duo nodes.conf, keyed by node_id. Runs on the Mac.

    watcher/register_node.py [-p PORT] [-i KEY] <ssh target>

It asks the node for its node_id over the same ssh the Mac already uses (status.sh
--json, or the same hash computed directly while a node's kit predates --json),
then writes two local files that git never sees:

  ~/.ssh/b300-nodes.conf   one `Host b300-<node_id>` block holding the address
  nodes.conf (b300-duo)    one `b300-<node_id>` line, so it holds no address

Safe to run again and from several shells at once: one lock covers both files, each
is replaced atomically, and a node_id already present is updated in place, never
duplicated. If another node_id held the same address, that stale entry goes.
Nothing here runs on the node's boot path, and every failure only returns non-zero.
"""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
ALIAS_PREFIX = 'b300-'
NODE_ID = re.compile(r'[0-9a-f]{8,64}')
SSH_OPTS = ['-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
            '-o', 'StrictHostKeyChecking=accept-new']
CALL_TIMEOUT = 25
# Same formula as node_id() in scripts/node_status.py (PR #17): sha256 of
# /etc/machine-id, or of the hostname when there is none, first 8 hex digits.
# Only the hash leaves the node.
NODE_ID_PY = ("import hashlib,socket\n"
              "try: s=open('/etc/machine-id').read().strip()\n"
              "except OSError: s=''\n"
              "print(hashlib.sha256((s or socket.gethostname()).encode()).hexdigest()[:8])\n")
COPIED = ('hostname', 'user', 'port', 'proxyjump')


class Failed(Exception):
    pass


def default_nodes_conf():
    duo = os.environ.get('B300_DUO_DIR') or str(ROOT.parent/'b300-duo')
    return os.environ.get('NODES_CONF') or str(Path(duo)/'nodes.conf')


def default_ssh_file():
    return os.environ.get('B300_NODES_SSH_CONFIG') or str(Path.home()/'.ssh'/'b300-nodes.conf')


def run(argv, stdin=None):
    try:
        return subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=CALL_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Failed(f'{Path(argv[0]).name} did not run: {type(exc).__name__}') from None


def ssh_base(args):
    base = [args.ssh, *SSH_OPTS]
    if args.port:
        base += ['-p', str(args.port)]
    if args.identity:
        base += ['-i', args.identity, '-o', 'IdentitiesOnly=yes']
    return base


def ask_node_id(args):
    """node_id from status.sh --json, else the same hash computed on the node."""
    status = run([*ssh_base(args), args.target, f'~/{args.kit_dir}/scripts/status.sh --json'])
    if status.returncode == 0:
        for line in status.stdout.splitlines():
            try:
                snap = json.loads(line)
            except ValueError:
                continue
            if isinstance(snap, dict) and isinstance(snap.get('node_id'), str):
                return snap['node_id']
    if status.returncode == 255:
        raise Failed('ssh to the node failed')
    direct = run([*ssh_base(args), args.target, 'python3 -'], stdin=NODE_ID_PY)
    if direct.returncode != 0:
        raise Failed(f'node_id lookup exited with {direct.returncode}')
    return direct.stdout.strip()


def resolve(args):
    """hostname, user, port (and any ProxyJump) the way ssh itself would connect."""
    argv = [args.ssh, '-G']
    if args.port:
        argv += ['-p', str(args.port)]
    argv.append(args.target)
    out = run(argv)
    if out.returncode != 0:
        raise Failed('ssh -G could not resolve the target')
    found = {}
    for line in out.stdout.splitlines():
        key, _, value = line.strip().partition(' ')
        if key in COPIED and value and value != 'none' and key not in found:
            found[key] = value
    if 'hostname' not in found:
        raise Failed('ssh -G gave no hostname')
    return found


def host_block(alias, found, identity):
    lines = [f'Host {alias}', f'    HostName {found["hostname"]}']
    for key, name in (('user', 'User'), ('port', 'Port'), ('proxyjump', 'ProxyJump')):
        if key in found:
            lines.append(f'    {name} {found[key]}')
    if identity:
        lines += [f'    IdentityFile {identity}', '    IdentitiesOnly yes']
    return lines


def parse_blocks(text):
    """Split an ssh config into a preamble and {alias: lines} for our b300-<node_id> blocks."""
    preamble, blocks, order, current = [], {}, [], None
    for line in text.splitlines():
        words = line.split()
        if words and words[0].lower() == 'host':
            current = words[1] if len(words) == 2 and words[1].startswith(ALIAS_PREFIX) else None
            if current is None:
                preamble.append(line)
                continue
            if current not in blocks:
                order.append(current)
            blocks[current] = [line]
        elif current is not None:
            if line.strip():
                blocks[current].append(line)
        else:
            preamble.append(line)
    return preamble, {alias: blocks[alias] for alias in order}


def address(block):
    values = {}
    for line in block[1:]:
        words = line.split(None, 1)
        if len(words) == 2:
            values[words[0].lower()] = words[1].strip()
    return values.get('hostname'), values.get('port', '22'), values.get('user')


def atomic_write(path, text, mode):
    path = Path(path)
    if path.exists() and path.read_text() == text:
        return False
    with contextlib.suppress(FileNotFoundError):
        mode = path.stat().st_mode & 0o777
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.')
    try:
        with os.fdopen(fd, 'w') as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    return True


@contextlib.contextmanager
def locked(ssh_file):
    # The lock sits beside the ssh file, outside any repo, so b300-duo never sees it.
    lock = Path(ssh_file).with_name(Path(ssh_file).name + '.lock')
    with open(lock, 'a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def upsert(nodes_conf, ssh_file, node_id, found, identity=None):
    """Add or update one node. Returns (alias, removed aliases). Idempotent."""
    alias = ALIAS_PREFIX + node_id
    block = host_block(alias, found, identity)
    nodes_conf, ssh_file = Path(nodes_conf), Path(ssh_file)
    nodes_conf.parent.mkdir(parents=True, exist_ok=True)
    ssh_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with locked(ssh_file):
        preamble, blocks = parse_blocks(ssh_file.read_text() if ssh_file.exists() else '')
        mine = address(block)
        removed = [a for a, b in blocks.items() if a != alias and address(b) == mine]
        for gone in removed:
            del blocks[gone]
        blocks[alias] = block
        while preamble and not preamble[-1].strip():
            preamble.pop()
        if not preamble:
            preamble = ['# B300 nodes, one Host block per node_id, written by',
                        '# b300-inference/watcher/register_node.py. Local to this Mac.']
        parts = ['\n'.join(preamble)] + ['\n'.join(b) for b in blocks.values()]
        atomic_write(ssh_file, '\n\n'.join(parts) + '\n', 0o600)

        lines = nodes_conf.read_text().splitlines() if nodes_conf.exists() else []
        kept, seen = [], False
        for line in lines:
            target = line.split('#', 1)[0].strip()
            if target in removed or (target == alias and seen):
                continue
            seen = seen or target == alias
            kept.append(line)
        if not seen:
            kept.append(alias)
        atomic_write(nodes_conf, '\n'.join(kept) + '\n', 0o600)
    return alias, removed


def alias_resolves(args, alias, found):
    out = run([args.ssh, '-G', alias])
    if out.returncode != 0:
        return False
    for line in out.stdout.splitlines():
        key, _, value = line.strip().partition(' ')
        if key == 'hostname':
            return value == found['hostname']
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('target', help='ssh target: a Host alias or user@host')
    parser.add_argument('-p', '--port', type=int)
    parser.add_argument('-i', '--identity', help='ssh key path (the path is stored, never the key)')
    parser.add_argument('--nodes-conf', default=default_nodes_conf())
    parser.add_argument('--ssh-config', default=default_ssh_file())
    parser.add_argument('--kit-dir', default=os.environ.get('NODE_KIT_DIR', 'b300-inference'))
    parser.add_argument('--ssh', default='ssh', help='ssh binary (tests swap in a fake)')
    args = parser.parse_args(argv)
    if args.target.startswith('-') or not re.fullmatch(r'[A-Za-z0-9_.@:%+-]+', args.target):
        parser.error('target must be one ssh destination')
    if not re.fullmatch(r'[A-Za-z0-9_/-]+', args.kit_dir) or '..' in args.kit_dir:
        parser.error('--kit-dir must be a safe path under the home directory')
    if args.identity:
        args.identity = str(Path(args.identity).expanduser().resolve())
    try:
        node_id = ask_node_id(args)
        if not NODE_ID.fullmatch(node_id):
            raise Failed('the node returned no usable node_id')
        found = resolve(args)
        alias, removed = upsert(args.nodes_conf, args.ssh_config, node_id, found, args.identity)
    except (Failed, OSError) as exc:
        # Messages never carry the target, so this is safe to land in any log.
        print(f'register: not registered: {exc}', file=sys.stderr)
        return 1
    for gone in removed:
        print(f'register: {gone} had the same address and was replaced', file=sys.stderr)
    print(f'register: {alias} is in nodes.conf')
    if not alias_resolves(args, alias, found):
        print(f'register: ssh does not read {args.ssh_config} yet. Add this line once, '
              f'above any Host block in ~/.ssh/config:\n    Include {args.ssh_config}', file=sys.stderr)
        return 3
    return 0


if __name__ == '__main__':
    sys.exit(main())

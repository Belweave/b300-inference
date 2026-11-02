"""One-screen, read-only bring-up status for the node. Run it through status.sh.

--json prints the same facts as one JSON object (schema below in docs/node-run.md),
one object per line with --loop, for the phone status app to poll.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
MODEL_MANIFEST = ROOT/'config/manifests/uncensored-c19ca5c.json'
GUARD_MANIFEST = ROOT/'config/manifests/guards.json'
PARTIAL = ('.incomplete', '.part', '.reuse')
GB = 1e9
SCHEMA = 1
# The guard containers node-run starts, by name and loopback health port.
GUARD_PORTS = (('shieldstral', 8600), ('qwen', 8601))
GUARD_TIMEOUT = 1.0
GATE_STATUS = {'PASS': 'pass', 'FAIL': 'fail', 'running': 'running'}
# Free text from reports (gate reasons) can quote a URL or address; keep only loopback.
URL_HOST = re.compile(r'(https?://)(?!127\.0\.0\.1[:/]|localhost[:/])[^/\s:]+')
IPV4 = re.compile(r'\b(?!127\.0\.0\.1\b)(?:\d{1,3}\.){3}\d{1,3}\b')


def short(path):
    home = str(Path.home())
    text = str(path)
    return '~'+text[len(home):] if text.startswith(home+'/') else text


def duration(seconds):
    seconds = int(max(seconds, 0))
    if seconds < 60:
        return f'{seconds}s'
    if seconds < 3600:
        return f'{seconds//60}m{seconds%60:02d}s'
    return f'{seconds//3600}h{seconds%3600//60:02d}m'


def iso(epoch):
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def scrub(text):
    if text is None:
        return None
    return IPV4.sub('<ip>', URL_HOST.sub(r'\1<host>', str(text)))


def node_id():
    """A short, stable id for this machine that is not its hostname or address."""
    try:
        seed = Path('/etc/machine-id').read_text().strip()
    except OSError:
        seed = ''
    return hashlib.sha256((seed or socket.gethostname()).encode()).hexdigest()[:8]


def gpus():
    """One dict per GPU, or None when nvidia-smi can't be run."""
    query = ['nvidia-smi', '--query-gpu=index,memory.used,memory.total,utilization.gpu',
             '--format=csv,noheader,nounits']
    try:
        out = subprocess.run(query, capture_output=True, text=True, timeout=10, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    result = []
    for row in out.strip().splitlines():
        try:
            index, used, total, util = (int(c.strip()) for c in row.split(','))
        except ValueError:
            continue
        result.append({'i': index, 'used_gib': round(used/1024, 1), 'total_gib': round(total/1024, 1),
                       'util': util})
    return result


def guard_probe(port, timeout=GUARD_TIMEOUT):
    """GET /health on a loopback guard. No proxies and no redirects, so it can only reach this host."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
    try:
        with opener.open(f'http://127.0.0.1:{port}/health', timeout=timeout) as response:
            code = response.status
    except urllib.error.HTTPError as exc:
        code = exc.code
    except (OSError, ValueError):
        code = None
    return {'up': code == 200, 'http': code}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def on_disk(folder, files):
    """Bytes written so far for the manifest files, counting partial downloads."""
    have, done = 0, 0
    for f in files:
        target = folder/f['filename']
        try:
            size = target.stat().st_size
            done += size == f['size']
        except OSError:
            size = 0
            for suffix in PARTIAL:
                try:
                    size = max(size, target.with_name(target.name+suffix).stat().st_size)
                except OSError:
                    pass
        have += min(size, f['size'])
    return have, done


def downloads(env):
    weights = Path(env.get('WEIGHTS_DIR', '/mnt/nvme/models/dsv41-uncensored'))
    guard_root = Path(env.get('GUARD_WEIGHTS_DIR', str(weights.parent/'guards')))
    rows = []
    model = json.loads(MODEL_MANIFEST.read_text())
    rows.append(('model', weights, model['files']))
    for g in json.loads(GUARD_MANIFEST.read_text())['guards']:
        rows.append((g['repo'].split('/')[-1], guard_root/g['repo']/g['revision'], g['files']))
    result = []
    for label, folder, files in rows:
        have, done = on_disk(folder, files)
        result.append({'label': label, 'dir': folder, 'have': have, 'target': sum(f['size'] for f in files),
                       'done': done, 'files': len(files)})
    return result


def newest(runs, pattern):
    found = [p for p in runs.glob(pattern) if p.is_file()]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def load(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError, AttributeError):
        return None


def gate_plan(names):
    """node-run's gate order for this kind of run, or None if the report doesn't follow it."""
    try:
        import node_run
        plan = node_run.RESUME_GATES if 'resume' in names else node_run.GATES
    except Exception:  # Status must still draw if node-run can't be imported.
        return None
    return list(plan) if set(names) <= set(plan) else None


def gates_state(record, now):
    gates = record.get('gates') or []
    rows = [{'name': g.get('name'), 'status': GATE_STATUS.get(g.get('status'), g.get('status')),
             'elapsed_s': round((g.get('ended_epoch') or now)-g.get('started_epoch', now), 1),
             'reason': scrub(g.get('reason'))} for g in gates]
    names = [g['name'] for g in rows]
    plan = gate_plan(names)
    finished = record.get('state') in ('ready', 'failed')
    if finished:
        upcoming, total = [], len(rows) if record.get('state') == 'ready' else (len(plan) if plan else None)
    elif plan:
        upcoming, total = [n for n in plan if n not in names], len(plan)
    else:
        upcoming, total = None, None
    return {'gate': rows[-1] if rows else None, 'gates': rows,
            'passed': [g['name'] for g in rows if g['status'] == 'pass'],
            'upcoming': upcoming, 'total_gates': total}


def warm_state(runs, record):
    path = newest(runs, 'warm-*.json')
    warm = load(path)
    # Only the warm that belongs to this node-run, not one left from an earlier run.
    if not warm or warm.get('started_epoch', 0) < record.get('started_epoch', 0)-5:
        return None, None
    stages = warm.get('stages', {})
    return {'state': warm.get('state'),
            'running': [n for n, s in stages.items() if 'end_epoch' not in s],
            'failed': [n for n, s in stages.items() if s.get('ok') is False]}, path


def fit_state(record):
    """The concurrency fit from the run report. A boot that skips fit reports not_run."""
    empty = {'state': 'not_run', 'concurrency': None, 'p99_ms': None, 'requests': None,
             'errors': None, 'reasons': []}
    if not record:
        return empty
    gate = next((g for g in reversed(record.get('gates') or []) if g.get('name') == 'fit'), None)
    samples = record.get('fit') or []
    loads = (record.get('measurements') or {}).get('loadtests') or []
    if gate is None and not samples:
        return empty
    sample = samples[-1] if samples else {}
    load_row = loads[-1] if loads else {}
    if gate is not None:
        state = GATE_STATUS.get(gate.get('status'), gate.get('status'))
    else:
        state = 'pass' if sample.get('passed') else 'fail'
    errors = None
    if load_row:
        errors = load_row.get('non_200', 0)+load_row.get('invalid_responses', load_row.get('invalid', 0))
    return {'state': state, 'concurrency': record.get('decision') or sample.get('concurrency'),
            'p99_ms': load_row.get('p99_ms', sample.get('p99_ms')), 'requests': load_row.get('requests'),
            'errors': errors, 'reasons': [scrub(r) for r in sample.get('reasons', [])]}


def is_cloudflared(pid):
    try:
        return Path(f'/proc/{int(pid)}/comm').read_text().strip() == 'cloudflared'
    except (OSError, ValueError, TypeError):
        return False


def any_cloudflared():
    return any(is_cloudflared(p.name) for p in Path('/proc').iterdir() if p.name.isdigit())


def tunnel_state(record):
    """From the run report plus a process check. Never reads the shared cloudflared log."""
    tunnel = (record or {}).get('tunnel') or {}
    pid = tunnel.get('pid')
    # With a recorded pid, only that connector counts; without one, any cloudflared on the box.
    running = is_cloudflared(pid) if pid else any_cloudflared()
    status = tunnel.get('status')
    # node-run marks the tunnel up only after a registered connection and a probe through it.
    connected = (status == 'up' and running) if pid else None
    return {'status': status, 'running': running, 'connected': connected,
            'registered_s': tunnel.get('registered_seconds'), 'reason': scrub(tunnel.get('reason'))}


def log_paths(report, warm_report):
    rows = []
    for path, wanted in ((report, ('guards.log', 'warm.log', 'tunnel.log')),
                         (warm_report, ('weights.log', 'operations.log', 'engine.log'))):
        if path is None:
            continue
        rows.append({'path': short(path), 'exists': True})
        folder = path.with_name(path.name[:-len('.json')])
        present = {p.name for p in folder.glob('*.log')} if folder.is_dir() else set()
        for name in sorted(present | set(wanted)):
            rows.append({'path': short(folder/name), 'exists': name in present})
    return rows


def phase(record, gate_info, total, target):
    if not record:
        return 'no_run'
    if record.get('state') == 'failed' or any(g['status'] == 'fail' for g in gate_info['gates']):
        return 'failed'
    if record.get('state') == 'ready':
        return 'ready'
    if total < target:
        return 'downloading'
    current = gate_info['gate']
    if current and current['name'].startswith('warm'):
        return 'warming'
    return 'running'


def start_probes(pool):
    """GPU and guard checks run in the background so they overlap the rate sample."""
    return {'gpus': pool.submit(gpus),
            'guards': [(name, port, pool.submit(guard_probe, port)) for name, port in GUARD_PORTS]}


def snapshot(env, runs, previous, probes):
    """Everything the screen and --json show, and this (bytes, epoch) sample for the next rate.
    previous is the sample from the last call, or None."""
    now = time.time()
    rows = downloads(env)
    total = sum(r['have'] for r in rows)
    target = sum(r['target'] for r in rows)
    rate = eta = None
    if previous and now > previous[1]:
        rate = max((total-previous[0])/(now-previous[1]), 0)
        if total >= target:
            eta = 0
        elif rate > 0:
            eta = round((target-total)/rate)
    report = newest(runs, 'node-run-*.json')
    record = load(report) if report else None
    if not isinstance(record, dict):
        record, report = None, None
    gate_info = gates_state(record, now) if record else {
        'gate': None, 'gates': [], 'passed': [], 'upcoming': None, 'total_gates': None}
    warm, warm_report = warm_state(runs, record) if record else (None, None)
    started = record.get('started_epoch') if record else None
    snap = {
        'schema': SCHEMA,
        'ts': iso(now),
        'node_id': node_id(),
        'phase': phase(record, gate_info, total, target),
        'ready': bool(record and record.get('state') == 'ready'),
        'run_id': report.name[:-len('.json')] if report else None,
        'started_at': iso(started),
        'run_s': round(now-started) if started else None,
        'gpus': probes['gpus'].result(),
        'downloads': [{k: r[k] for k in ('label', 'have', 'target', 'done', 'files')} for r in rows],
        'download_have': total,
        'download_target': target,
        'rate_bps': None if rate is None else round(rate),
        'eta_s': eta,
        **gate_info,
        'warm': warm,
        'guards': [{'name': name, 'port': port, **future.result()} for name, port, future in probes['guards']],
        'fit': fit_state(record),
        'tunnel': tunnel_state(record),
        'logs': log_paths(report, warm_report),
        'runs_dir': short(runs),
    }
    return snap, (total, now)


def screen(snap):
    out = ['b300 status '+time.strftime('%Y-%m-%d %H:%M:%S %Z'), '', 'GPUs']
    if snap['gpus'] is None:
        out.append('  nvidia-smi unavailable')
    else:
        out.append('  GPU   used / total GiB   util')
        out += [f"  {g['i']:>3}  {g['used_gib']:6.1f} / {g['total_gib']:5.1f}     {g['util']:>3}%"
                for g in snap['gpus']]
    out += ['', 'Downloads (bytes on disk / target)']
    for r in snap['downloads']:
        pct = 100*r['have']/r['target'] if r['target'] else 100
        out.append(f"  {r['label']:<20} {r['have']/GB:7.1f} / {r['target']/GB:6.1f} GB {pct:5.1f}%"
                   f"  {r['done']}/{r['files']} files")
    if snap['rate_bps'] is not None:
        if snap['download_have'] >= snap['download_target']:
            out.append('  all bytes on disk')
        elif snap['rate_bps'] > 0:
            out.append(f"  rate {snap['rate_bps']/1e6:.0f} MB/s  ETA {duration(snap['eta_s'])}")
        else:
            out.append('  rate 0 MB/s (idle, hashing, or not started)  ETA unknown')
    out += ['', 'Stage']
    if snap['run_id'] is None:
        out.append('  no node-run report yet in '+snap['runs_dir'])
    else:
        gate = snap['gate']
        if snap['ready']:
            out.append('  READY for drill')
        elif gate:
            status = {'pass': 'PASS', 'fail': 'FAIL'}.get(gate['status'], gate['status'])
            out.append(f"  gate {gate['name']}: {status} ({duration(gate['elapsed_s'])})"
                       + (f" {gate['reason']}" if status == 'FAIL' and gate['reason'] else ''))
        if snap['passed']:
            out.append('  passed: '+' '.join(snap['passed']))
        warm = snap['warm']
        if warm:
            text = f"  warm: {warm['state'] or '?'}"
            if warm['running']:
                text += ', running '+', '.join(warm['running'])
            if warm['failed']:
                text += ', failed '+', '.join(warm['failed'])
            out.append(text)
        out.append('  run time '+duration(snap['run_s'] or 0))
    out.append('  guards: '+', '.join(f"{g['name']} {'up' if g['up'] else 'down'}"
                                      + (f" ({g['http']})" if g['http'] and not g['up'] else '')
                                      for g in snap['guards']))
    tunnel = snap['tunnel']
    tunnel_text = 'connected' if tunnel['connected'] else (tunnel['status'] or 'unknown')
    out.append(f"  fit: {snap['fit']['state']}  tunnel: {tunnel_text}")
    out += ['', 'Logs']
    lines = []
    for row in snap['logs']:
        if row['path'].endswith('.json'):
            lines.append('  '+row['path'])
            present = [r['path'] for r in snap['logs']
                       if r['exists'] and r['path'].startswith(row['path'][:-5]+'/')]
            if present:
                lines.append(f"  {row['path'][:-5]}/ "+' '.join(p.rsplit('/', 1)[1] for p in present))
    missing = {r['path'].rsplit('/', 1)[1] for r in snap['logs'] if not r['exists']}
    if snap['warm'] and 'weights.log' in missing:
        lines.append('  (weights.log starts with the first finished file)')
    if snap['run_id'] and 'warm.log' in missing:
        lines.append('  (node-run writes warm.log only after warm exits)')
    out += lines or ['  none yet']
    return '\n'.join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='status.sh', description='Read-only node bring-up status.')
    parser.add_argument('--loop', nargs='?', const=2.0, type=float, metavar='SECONDS',
                        help='redraw every SECONDS (default 2) until Ctrl-C')
    parser.add_argument('--json', action='store_true',
                        help='print one JSON object (one per line with --loop) instead of the screen')
    args = parser.parse_args(argv)
    if args.loop is not None and args.loop < 0.5:
        parser.error('--loop needs at least 0.5 seconds')
    env = os.environ
    runs = Path(env.get('RUNS_DIR', str(ROOT/'runs')))
    tty = sys.stdout.isatty() and not args.json

    def emit(snap):
        if args.json:
            print(json.dumps(snap, separators=(',', ':')), flush=True)
        else:
            print(('\033[H\033[2J' if tty else '\n' if args.loop is not None else '')+screen(snap), flush=True)

    try:
        with ThreadPoolExecutor(max_workers=1+len(GUARD_PORTS)) as pool:
            if args.loop is None:
                # Two samples a second apart give a rate without keeping any state on disk.
                probes = start_probes(pool)
                sample = (sum(r['have'] for r in downloads(env)), time.time())
                time.sleep(1)
                emit(snapshot(env, runs, sample, probes)[0])
                return 0
            sample = None
            while True:
                snap, sample = snapshot(env, runs, sample, start_probes(pool))
                emit(snap)
                time.sleep(args.loop)
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        return 0


if __name__ == '__main__':
    sys.exit(main())

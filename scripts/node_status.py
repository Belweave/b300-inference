"""One-screen, read-only bring-up status for the node. Run it through status.sh."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
MODEL_MANIFEST = ROOT/'config/manifests/uncensored-c19ca5c.json'
GUARD_MANIFEST = ROOT/'config/manifests/guards.json'
PARTIAL = ('.incomplete', '.part', '.reuse')
GB = 1e9


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


def gpus():
    query = ['nvidia-smi', '--query-gpu=index,memory.used,memory.total,utilization.gpu',
             '--format=csv,noheader,nounits']
    try:
        out = subprocess.run(query, capture_output=True, text=True, timeout=10, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return ['  nvidia-smi unavailable']
    lines = ['  GPU   used / total GiB   util']
    for row in out.strip().splitlines():
        try:
            index, used, total, util = (c.strip() for c in row.split(','))
            lines.append(f'  {index:>3}  {int(used)/1024:6.1f} / {int(total)/1024:5.1f}     {util:>3}%')
        except ValueError:
            lines.append('  '+row.strip())
    return lines


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


def stage(runs, now):
    report = newest(runs, 'node-run-*.json')
    record = load(report)
    if not record:
        return ['  no node-run report yet in '+short(runs)], report, None
    lines = []
    gates = record.get('gates') or []
    passed = [g['name'] for g in gates if g.get('status') == 'PASS']
    if record.get('state') == 'ready':
        lines.append('  READY for drill')
    elif gates:
        last = gates[-1]
        took = (last.get('ended_epoch') or now)-last.get('started_epoch', now)
        status = last.get('status', '?')
        lines.append(f"  gate {last['name']}: {status} ({duration(took)})"
                     + (f" {last['reason']}" if status == 'FAIL' and last.get('reason') else ''))
    if passed:
        lines.append('  passed: '+' '.join(passed))
    warm_report = newest(runs, 'warm-*.json')
    warm = load(warm_report)
    if warm and warm.get('started_epoch', 0) >= record.get('started_epoch', 0)-5:
        open_stages = [n for n, s in warm.get('stages', {}).items() if 'end_epoch' not in s]
        failed = [n for n, s in warm.get('stages', {}).items() if s.get('ok') is False]
        text = f"  warm: {warm.get('state', '?')}"
        if open_stages:
            text += ', running '+', '.join(open_stages)
        if failed:
            text += ', failed '+', '.join(failed)
        lines.append(text)
    else:
        warm_report = None
    lines.append('  run time '+duration(now-record.get('started_epoch', now)))
    return lines, report, warm_report


def logs(report, warm_report):
    lines = []
    for path in (report, warm_report):
        if path is None:
            continue
        lines.append('  '+short(path))
        folder = path.with_name(path.name[:-len('.json')])
        names = sorted(p.name for p in folder.glob('*.log')) if folder.is_dir() else []
        if names:
            lines.append(f'  {short(folder)}/ '+' '.join(names))
    if warm_report is not None and not (warm_report.with_name(warm_report.name[:-5])/'weights.log').exists():
        lines.append('  (weights.log starts with the first finished file)')
    if report is not None and not (report.with_name(report.name[:-5])/'warm.log').exists():
        lines.append('  (node-run writes warm.log only after warm exits)')
    return lines or ['  none yet']


def screen(env, runs, previous):
    now = time.time()
    rows = downloads(env)
    total = sum(r['have'] for r in rows)
    target = sum(r['target'] for r in rows)
    out = ['b300 status '+time.strftime('%Y-%m-%d %H:%M:%S %Z'), '', 'GPUs']
    out += gpus()
    out += ['', 'Downloads (bytes on disk / target)']
    for r in rows:
        pct = 100*r['have']/r['target'] if r['target'] else 100
        out.append(f"  {r['label']:<20} {r['have']/GB:7.1f} / {r['target']/GB:6.1f} GB {pct:5.1f}%"
                   f"  {r['done']}/{r['files']} files")
    if previous and now > previous[1]:
        rate = (total-previous[0])/(now-previous[1])
        if total >= target:
            out.append('  all bytes on disk')
        elif rate > 0:
            out.append(f'  rate {rate/1e6:.0f} MB/s  ETA {duration((target-total)/rate)}')
        else:
            out.append('  rate 0 MB/s (idle, hashing, or not started)  ETA unknown')
    out += ['', 'Stage']
    stage_lines, report, warm_report = stage(runs, now)
    out += stage_lines
    out += ['', 'Logs']
    out += logs(report, warm_report)
    return '\n'.join(out), (total, now)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='status.sh', description='Read-only node bring-up status.')
    parser.add_argument('--loop', nargs='?', const=2.0, type=float, metavar='SECONDS',
                        help='redraw every SECONDS (default 2) until Ctrl-C')
    args = parser.parse_args(argv)
    if args.loop is not None and args.loop < 0.5:
        parser.error('--loop needs at least 0.5 seconds')
    env = os.environ
    runs = Path(env.get('RUNS_DIR', str(ROOT/'runs')))
    tty = sys.stdout.isatty()
    try:
        if args.loop is None:
            # Two samples a second apart give a rate without keeping any state on disk.
            sample = (sum(r['have'] for r in downloads(env)), time.time())
            time.sleep(1)
            text, _ = screen(env, runs, sample)
            print(text, flush=True)
            return 0
        sample = None
        while True:
            text, sample_now = screen(env, runs, sample)
            sample = sample_now
            print(('\033[H\033[2J' if tty else '\n')+text, flush=True)
            time.sleep(args.loop)
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        return 0


if __name__ == '__main__':
    sys.exit(main())

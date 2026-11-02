"""Supervised node bring-up and tunnel handoff; no allocation actions or secret files."""
import asyncio
from contextlib import contextmanager
import csv
from dataclasses import asdict
from datetime import datetime, timezone
import fcntl
import importlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time

from redact import redact, secret_values
import node_tunnel
from relay_config import read_classifier_key
from serve_plan import front_port, image_reference, require_pinned
from warm_logic import concurrency_fit, parse_pool, tunnel_slot

ROOT = Path(__file__).resolve().parents[1]
GATES = ('preflight', 'warm-timeout', 'key', 'warm', 'guards', 'relay-safety',
         'kv-pool', 'loadtest', 'fit', 'handoff', 'key-leak')
CREDENTIALS = ('SAFETY_CLASSIFIER_KEY', 'CF_TUNNEL_TOKEN', 'TUNNEL_TOKEN')


class GateFailure(Exception):
    """Only controlled, secret-free reasons may be displayed."""


def number(env, name, default, minimum=0, integer=False):
    try:
        value = int(env.get(name, default)) if integer else float(env.get(name, default))
        if not math.isfinite(value) or value <= minimum:
            raise ValueError
        return value
    except (TypeError, ValueError, OverflowError):
        raise GateFailure(name+' has an invalid numeric value') from None


def provisioned(value, now):
    if value is None:
        return now
    try:
        result = float(value)
    except ValueError:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise GateFailure('PROVISIONED_AT ISO time must include a timezone')
        result = parsed.timestamp()
    if not math.isfinite(result) or result > now + 5:
        raise GateFailure('PROVISIONED_AT must be a past epoch or timezone-aware ISO time')
    return result


class NodeRun:
    def __init__(self):
        self.env = dict(os.environ)
        for name in CREDENTIALS:
            self.env.pop(name, None)
            os.environ.pop(name, None)
        self.key = ''
        self.tunnel_token = ''
        self.secrets = secret_values(self.env)
        self.started = time.time()
        self.end = self.started + 11250 - 600
        self.deadline_confirmed = False
        self.id = 'node-run-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        self.run = ROOT/'runs'/self.id
        self.path = ROOT/'runs'/(self.id+'.json')
        self.run.mkdir(parents=True, mode=0o700)
        self.record = {'schema': 1, 'state': 'running', 'started_epoch': self.started,
                       'gates': [], 'measurements': {}, 'decision': None,
                       'image_digest': None, 'git_sha': None,
                       'report': str(self.path), 'run_dir': str(self.run),
                       'tunnel': {'preflight': None, 'launched': False, 'pid': None,
                                  'status': 'not started', 'reason': None, 'waited_seconds': 0}}
        self.lock = None
        self.warm_report = None
        self.current = None

    def safe(self, text):
        return redact(text, self.secrets)

    def say(self, text):
        print(self.safe(text), flush=True)

    def save(self):
        tmp = self.path.with_name(self.path.name+'.tmp')
        tmp.write_text(self.safe(json.dumps(self.record, indent=2, allow_nan=False))+'\n')
        tmp.replace(self.path)

    def remaining(self):
        value = self.end-time.time()
        if value <= 0:
            raise GateFailure('teardown deadline reached')
        return value

    @contextmanager
    def gate(self, name, check_deadline=True):
        row = {'name': name, 'started_epoch': time.time(), 'status': 'running'}
        self.record['gates'].append(row)
        self.save()
        try:
            if check_deadline:
                self.remaining()
            yield row
            if check_deadline:
                self.remaining()
            row['status'] = 'PASS'
        except BaseException as exc:
            row['status'] = 'FAIL'
            # Never render arbitrary library exceptions, argv, response data or env.
            reason = str(exc) if isinstance(exc, GateFailure) else type(exc).__name__
            row['reason'] = self.safe(reason)
            self.say(f'FAIL {name}: {reason}')
            raise
        else:
            self.say('PASS '+name)
        finally:
            row['ended_epoch'] = time.time()
            row['seconds'] = row['ended_epoch']-row['started_epoch']
            self.save()

    def command(self, argv, timeout=30, env=None, input_text=None, check=True, merge_output=False):
        child_env = dict(self.env if env is None else env)
        for name in CREDENTIALS:
            child_env.pop(name, None)
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT if merge_output else subprocess.PIPE, text=True,
                                env=child_env, cwd=ROOT, start_new_session=True)
        try:
            output, _ = proc.communicate(input_text, timeout=min(timeout, self.remaining()))
        except BaseException:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
            raise
        if check and proc.returncode:
            raise GateFailure('command failed: '+Path(argv[0]).name+' (output suppressed)')
        return proc.returncode, output

    def source_node_env(self):
        # Source the existing mode/ownership-checked loader in a quiet child.
        # The environment travels over an anonymous pipe only, never a file.
        # node-env.sh sources with set -a, matching Netops. Extract the token with
        # a shell builtin, then remove it before the environment-dump child starts.
        script = ('set +x; set -euo pipefail; source "$1" >/dev/null 2>&1; set +x; '
                  'printf "%s\\0" "${CF_TUNNEL_TOKEN:-}"; '
                  'unset CF_TUNNEL_TOKEN TUNNEL_TOKEN SAFETY_CLASSIFIER_KEY; '
                  'exec "$2" -B -c "import json,os; print(json.dumps(dict(os.environ)))"')
        code, output = self.command(['bash', '-c', script, 'node-env',
                                     str(ROOT/'scripts/node-env.sh'), sys.executable], check=False)
        if code:
            raise GateFailure('node env could not be sourced; require current owner, mode 600 and valid shell')
        self.tunnel_token, _, exported = output.partition('\0')
        self.env = json.loads(exported)
        self.refresh_secrets()

    def refresh_secrets(self):
        self.secrets = secret_values({**self.env, 'SAFETY_CLASSIFIER_KEY': self.key,
                                     'CF_TUNNEL_TOKEN': self.tunnel_token})

    def tunnel_preflight(self):
        result = node_tunnel.prerequisites(self.env, self.tunnel_token,
                                          ROOT/'vendor/infer-gateway/node-tunnel.sh')
        self.record['tunnel']['preflight'] = result
        if result['status'] != 'PASS':
            self.record['tunnel']['reason'] = result['reason']
            raise GateFailure(result['reason'])
        try:
            self.tunnel_url = node_tunnel.public_url(
                self.env, self.env.get('RELAY_PUBLIC_URL', 'https://infer-origin.belweave.tech'))
            self.tunnel_wait = number(self.env, 'TUNNEL_WAIT_SECONDS', 180)
        except ValueError as exc:
            raise GateFailure(str(exc)) from None
        self.record['tunnel']['url'] = self.tunnel_url

    def preflight(self):
        self.source_node_env()
        cap = number(self.env, 'CREDIT_CAP_SECONDS', 11250, minimum=600)
        try:
            origin = provisioned(self.env.get('PROVISIONED_AT'), self.started)
        except (ValueError, OverflowError):
            raise GateFailure('invalid PROVISIONED_AT') from None
        self.end = origin+cap-600
        self.deadline_confirmed = True
        self.record.update(provisioned_epoch=origin, credit_cap_seconds=cap,
                           teardown_deadline_epoch=self.end, margin_seconds=600)
        self.remaining()
        signal.setitimer(signal.ITIMER_REAL, self.remaining())
        self.lock = (ROOT/'runs/node-run.lock').open('w')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise GateFailure('another node-run is active') from None
        testing = self.env.get('NODE_RUN_TESTING') == '1'
        test_names = ('EXPECT_GPU_NAME', 'EXPECT_GPU_COUNT', 'NODE_RUN_WARM_CMD', 'NODE_RUN_RELAY_URL',
                      'NODE_RUN_GUARD_HEALTH_URL', 'NODE_RUN_TUNNEL_PUBLIC_URL')
        if not testing and any(name in self.env for name in test_names):
            raise GateFailure('test overrides require NODE_RUN_TESTING=1')
        # Hard gate before even docker info, and before downloads/pulls/warm.
        self.tunnel_preflight()
        self.env.update(SAFETY='1', SSE_RELAY='1', LAYOUT='tp8', NODE_RUN_STDIN='1',
                        NODE_RUN_BENIGN_SMOKE='1', FALLBACK_MAX_RUNNING_REQUESTS='64')
        self.env.setdefault('WEIGHTS_DIR', '/mnt/nvme/models/dsv41-uncensored')
        self.env.setdefault('MAX_RUNNING_REQUESTS', '128')
        # Guards share GPU 7 (0.06 + 0.02). Leave the engine the same headroom it has without them.
        self.env.setdefault('MEM_FRACTION', '0.76')
        if self.env['MAX_RUNNING_REQUESTS'] not in ('128', '64'):
            raise GateFailure('MAX_RUNNING_REQUESTS must be 128 or 64')
        if not self.env.get('INFER_UPSTREAM_KEY'):
            raise GateFailure('INFER_UPSTREAM_KEY is required in the node environment')
        try:
            tunnel_slot(self.env)
            self.image = image_reference(self.env)
            require_pinned(self.image)
            engine_port = front_port(self.env)
        except ValueError as exc:
            raise GateFailure(str(exc)) from None
        self.record.update(self.image)
        _, sha = self.command(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'])
        self.record['git_sha'] = sha.strip()
        self.env['WINDOW_END'] = str(math.floor(origin+cap))
        # Warm and guards receive exactly this same pinned reference.
        self.env.update(ENGINE_IMAGE=self.image['image_ref'], IMAGE_DIGEST=self.image['image_digest'])
        if 'BAKED_IMAGE' in self.env:
            self.env['BAKED_IMAGE'] = self.image['image_ref']
        for executable in ('docker', 'nvidia-smi'):
            if not shutil.which(executable, path=self.env.get('PATH')):
                raise GateFailure(executable+' is required')
        try:
            importlib.import_module('aiohttp')
        except ImportError:
            raise GateFailure('python dependency aiohttp is required') from None
        self.command(['docker', 'info'])
        _, output = self.command(['nvidia-smi', '--query-gpu=name,memory.total', '--format=csv,noheader'])
        rows = list(csv.reader(output.strip().splitlines()))
        expected = int(self.env.get('EXPECT_GPU_COUNT', '8'))
        name = self.env.get('EXPECT_GPU_NAME', 'B300')
        if (len(rows) != expected or expected < 1 or not name
                or any(len(row) != 2 or name not in row[0] for row in rows)):
            raise GateFailure(f'expected exactly {expected} GPUs with the required name')
        self.gpu_count = expected
        self.record['measurements']['gpus'] = [
            {'index': i, 'name': row[0].strip(), 'total_mib': int(row[1].strip().split()[0])}
            for i, row in enumerate(rows)]
        if len({8000, engine_port, 8600, 8601}) != 4:
            raise GateFailure('relay, engine and guard ports must be distinct')
        for port in (8000, engine_port, 8600, 8601):
            try:
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1', port))
            except OSError:
                raise GateFailure(f'port {port} is occupied') from None
        self.record['ports'] = {'relay': 8000, 'engine': engine_port, 'guards': [8600, 8601]}
        self.disk_check()
        import guards
        try:
            self.guard_plans = guards.plans(self.env, self.image, self.id)
        except ValueError as exc:
            raise GateFailure(str(exc)) from None
        if self.guard_plans[0]['gpu'] >= expected:
            raise GateFailure('GUARD_GPU is outside the GPU inventory')
        self.record['guards'] = self.guard_plans
        self.url = self.env.get('NODE_RUN_RELAY_URL', 'http://127.0.0.1:8000').rstrip('/')
        from safety import validate_url
        validate_url(self.url)
        self.public_url = self.env.get('RELAY_PUBLIC_URL', 'https://infer-origin.belweave.tech')
        from urllib.parse import urlsplit
        public = urlsplit(self.public_url)
        if (public.scheme != 'https' or not public.hostname or public.username or public.password
                or public.query or public.fragment or any(c.isspace() for c in self.public_url)):
            raise GateFailure('RELAY_PUBLIC_URL must be an HTTPS URL without credentials')
        self.duration = number(self.env, 'LOADTEST_DURATION_SECONDS', 60)
        self.headroom = number(self.env, 'FIT_RANK_HEADROOM_MIB', 4096)
        self.tokens = number(self.env, 'FIT_TOKENS_PER_STREAM', 32768, integer=True)
        self.context = number(self.env, 'CONTEXT_LENGTH', 262144, integer=True)

    def disk_check(self):
        from warm_download import load_manifest
        weights = Path(self.env['WEIGHTS_DIR'])
        existing = sum(min((weights/name).stat().st_size, info['size'])
                       for name, info in load_manifest()['files'].items() if (weights/name).is_file())
        parent = weights
        while not parent.exists():
            parent = parent.parent
        needed = max(20e9, number(self.env, 'MIN_FREE_GB', 550)*1e9-existing)
        free = shutil.disk_usage(parent).free
        mode = self.env.get('DISK_SPACE_ACTION', 'fail')
        if mode not in ('warn', 'fail'):
            raise GateFailure('DISK_SPACE_ACTION must be warn or fail')
        self.record['measurements']['disk'] = {'free_bytes': free, 'required_bytes': needed,
                                               'existing_bytes': existing, 'action': mode}
        if free < needed:
            if mode == 'fail':
                raise GateFailure('insufficient free space at weights directory')
            self.say('WARNING: free space at weights directory is below configured threshold')

    def warm_timeout(self):
        self.requested_timeout = number(self.env, 'WARM_TIMEOUT_SECONDS', 3600, integer=True)
        self.cap_warm_timeout()
        self.record['warm_timeout_seconds'] = int(self.env['WARM_TIMEOUT_SECONDS'])
        self.record['warm_timeout_requested_seconds'] = self.requested_timeout

    def cap_warm_timeout(self):
        # warm_logic.deadline uses WINDOW_END-780, 180 seconds before our reminder.
        timeout = min(self.requested_timeout, math.floor(self.remaining()-180))
        if timeout <= 0:
            raise GateFailure('no warm budget remains before the existing warm cutover deadline')
        self.env['WARM_TIMEOUT_SECONDS'] = str(timeout)

    def read_key(self):
        try:
            self.key = read_classifier_key(sys.stdin)
        except ValueError as exc:
            raise GateFailure(str(exc)) from None
        self.refresh_secrets()

    def warm(self, fallback=False):
        self.cap_warm_timeout()
        command = [self.env.get('NODE_RUN_WARM_CMD', str(ROOT/'scripts/warm.sh')), '--layout', 'tp8']
        if fallback:
            command += ['--resume-at-64', str(self.warm_report)]
        env = dict(self.env, NODE_RUN_DIR=str(self.run))
        if fallback:
            env['MAX_RUNNING_REQUESTS'] = '64'
        code, output = self.command(command, timeout=int(env['WARM_TIMEOUT_SECONDS'])+5,
                                    env=env, input_text=self.key+'\n', check=False, merge_output=True)
        label = 'warm-64' if fallback else 'warm'
        (self.run/(label+'.log')).write_text(self.safe(output))
        if code:
            raise GateFailure('warm failed; inspect the redacted warm report/log')
        signals = [line[6:] for line in output.splitlines() if line.startswith('READY ')]
        if len(signals) != 1:
            raise GateFailure('warm must emit exactly one READY JSON line')
        ready = json.loads(signals[0])
        path = Path(ready['timing_file']).resolve()
        if ready.get('state') != 'ready' or not path.is_relative_to((ROOT/'runs').resolve()):
            raise GateFailure('invalid warm READY state or report path')
        record = json.loads(path.read_text())
        if record.get('image_ref') != self.image['image_ref']:
            raise GateFailure('warm image differs from preflight pin')
        current = int(record['config']['MAX_RUNNING_REQUESTS'])
        if current not in (64, 128) or ready.get('max_running_requests_effective') != current:
            raise GateFailure('warm effective concurrency is missing or inconsistent')
        if fallback and current != 64:
            raise GateFailure('fallback did not select 64')
        self.warm_report, self.current = path, current
        self.record.setdefault('warm_runs', []).append({'ready': ready, 'run_dir': str(path.with_suffix('')),
                                                       'warm_timeout_seconds': int(env['WARM_TIMEOUT_SECONDS'])})
        self.record['warm_concurrency'] = record.get('concurrency')

    def memory(self):
        _, output = self.command(['nvidia-smi', '--query-gpu=index,uuid,memory.total,memory.used',
                                  '--format=csv,noheader,nounits'])
        result = []
        for row in csv.reader(output.strip().splitlines()):
            index, uuid, total, used = [x.strip() for x in row]
            result.append({'index': int(index), 'uuid': uuid, 'total_mib': int(total),
                           'used_mib': int(used), 'free_mib': int(total)-int(used)})
        if (len(result) != self.gpu_count or {r['index'] for r in result} != set(range(self.gpu_count))
                or any(r['total_mib'] <= 0 or not 0 <= r['used_mib'] <= r['total_mib'] for r in result)):
            raise GateFailure('invalid GPU memory inventory')
        return sorted(result, key=lambda r: r['index'])

    def processes(self):
        code, output = self.command(['nvidia-smi', '--query-compute-apps=gpu_uuid,pid,used_gpu_memory',
                                    '--format=csv,noheader,nounits'], check=False)
        try:
            rows = [{'uuid': r[0].strip(), 'pid': int(r[1]), 'used_mib': int(r[2])}
                    for r in csv.reader(output.strip().splitlines())] if code == 0 else None
        except (ValueError, IndexError):
            rows = None
        return {'available': rows is not None, 'processes': rows}

    async def guards_healthy(self, plans=None):
        import aiohttp
        until = time.monotonic()+min(self.remaining(), number(self.env, 'GUARD_TIMEOUT_SECONDS', 600))
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2), trust_env=False) as session:
            for plan in (self.guard_plans if plans is None else plans):
                url = self.env.get('NODE_RUN_GUARD_HEALTH_URL', f"http://127.0.0.1:{plan['port']}")
                from safety import validate_url
                validate_url(url)
                while time.monotonic() < until:
                    self.remaining()
                    try:
                        async with session.get(url.rstrip('/')+'/health', allow_redirects=False) as response:
                            if response.status == 200:
                                break
                    except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
                        pass
                    _, state = self.command(['docker', 'inspect', plan['name'], '--format', '{{.State.Running}}'])
                    if state.strip() != 'true':
                        self.guard_log(plan)
                        raise GateFailure(plan['name']+' exited before health; see guard_logs in the report')
                    await asyncio.sleep(min(1, max(0, until-time.monotonic())))
                else:
                    self.guard_log(plan)
                    raise GateFailure('guard health timeout')

    def guard_log(self, plan):
        _, state = self.command(['docker', 'inspect', plan['name'], '--format',
                                 '{{.State.ExitCode}} {{.State.OOMKilled}} {{json .State.Error}}'], check=False)
        _, tail = self.command(['docker', 'logs', '--tail', '200', plan['name']], check=False, merge_output=True)
        self.record.setdefault('guard_logs', {})[plan['name']] = {'state': self.safe(state.strip()), 'tail': self.safe(tail[-20000:])}
        self.save()

    def guard_fraction(self, plan):
        """sglang sizes --mem-fraction-static against the whole GPU and assumes the rest is free.
        GPU 7 already holds an engine rank, so turn the guard's budget into the fraction that
        leaves exactly what is free now minus that budget."""
        row = self.memory()[plan['gpu']]
        budget = int(plan['mem_fraction_static']*row['total_mib'])+2048
        if row['free_mib'] < budget+1024:
            self.say(f"WARNING: GPU {plan['gpu']} has {row['free_mib']} MiB free; {plan['name']} wants {budget+1024}")
            return plan['mem_fraction_static']
        return round(1-(row['free_mib']-budget)/row['total_mib'], 4)

    def guards(self):
        before = self.memory()
        processes_before = self.processes()
        for plan in self.guard_plans:
            Path(plan['cache_dir']).mkdir(parents=True, exist_ok=True)
            argv = list(plan['argv'])
            fraction = self.guard_fraction(plan)
            argv[argv.index('--mem-fraction-static')+1] = str(fraction)
            plan['effective_mem_fraction_static'] = fraction; self.save()
            self.command(argv, timeout=120)
            # One at a time, so the second guard sizes itself against what the first left.
            asyncio.run(self.guards_healthy([plan]))
        after = self.memory()
        delta = [{'index': a['index'], 'guard_vram_mib': a['used_mib']-b['used_mib']}
                 for b, a in zip(before, after)]
        self.record['measurements']['guard_memory'] = {
            'before': before, 'after': after, 'per_gpu': delta,
            'processes_before': processes_before, 'processes_after': self.processes(),
            'basis': 'observed per-rank change; includes any concurrent serving-engine variation'}
        self.guard_vram = sum(max(0, r['guard_vram_mib']) for r in delta)

    async def relay_safety(self):
        import aiohttp
        from safety import CANARY
        from safety_loadtest import valid_response
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3), trust_env=False) as session:
            for phase in ('input', 'output'):
                for text, auth, status, flag in (('hello world', True, 200, False),
                                                  ('hello world', False, 401, None),
                                                  (CANARY, True, 200, True)):
                    headers = {'Authorization': 'Bearer '+self.key} if auth else {}
                    async with session.post(self.url+'/v1/safety/csam', headers=headers,
                                            json={'text': text, 'phase': phase}, allow_redirects=False) as response:
                        if response.status != status:
                            raise GateFailure('relay safety status check failed')
                        if status == 200:
                            body = await response.json()
                            if not valid_response(body, flag) or body['flag'] is not flag:
                                raise GateFailure('relay safety response check failed')

    def pool(self):
        log = self.warm_report.with_suffix('')/'engine.log'
        pool = parse_pool(log.read_text(errors='replace'))
        if pool['max_total_num_tokens'] is None or pool['engine_max_running_requests'] is None:
            raise GateFailure('engine log must report KV pool and effective running limit')
        self.pool_value = pool
        self.record['measurements'].setdefault('kv_pool', []).append({'concurrency': self.current, **pool})

    def loadtest(self):
        from safety_loadtest import run_live_load_test
        # Do not launch a test that cannot finish within the absolute budget.
        if self.duration+3 >= self.remaining():
            raise GateFailure('insufficient time for the load test before teardown')
        result = asyncio.run(run_live_load_test(self.url, self.key, rps=130, concurrency=self.current,
                                                duration_seconds=self.duration))
        self.load = result
        self.record['measurements'].setdefault('loadtests', []).append({
            'concurrency': self.current, 'rps': 130, **asdict(result), 'invalid': result.invalid_responses,
            'latency_passed': result.p99_ms < 100, 'passed': result.passed(100)})
        if result.non_200 or result.invalid_responses:
            raise GateFailure('loadtest returned non-200/timeout or invalid responses')
        if self.current == 64 and not result.passed(100):
            raise GateFailure('64 loadtest p99 must be below 100 ms')
        if result.p99_ms >= 100:
            self.say('128 p99 exceeds budget; fit will require the single 64 fallback')

    def fit_sample(self):
        kv = concurrency_fit(self.pool_value, self.current, self.context, self.tokens)
        ranks = self.memory()
        reasons = []
        if not kv['passed']:
            reasons.append(kv['reason'])
        if any(rank['free_mib'] < self.headroom for rank in ranks):
            reasons.append('one or more ranks below required VRAM headroom')
        if self.load.p99_ms >= 100:
            reasons.append('loadtest p99 is not below 100 ms')
        sample = {'concurrency': self.current, 'kv_fit': kv, 'ranks': ranks,
                  'required_rank_headroom_mib': self.headroom, 'p99_ms': self.load.p99_ms,
                  'passed': not reasons, 'reasons': reasons}
        self.record.setdefault('fit', []).append(sample)
        self.save()
        return sample

    def fit(self):
        sample = self.fit_sample()
        if not sample['passed'] and self.current == 128:
            self.record['fallback_reason'] = sample['reasons']
            with self.gate('warm-64'):
                self.warm(fallback=True)
            with self.gate('kv-pool-64'):
                self.pool()
            with self.gate('relay-safety-64'):
                asyncio.run(self.relay_safety())
            with self.gate('loadtest-64'):
                self.loadtest()
            sample = self.fit_sample()
        if not sample['passed']:
            raise GateFailure('64 does not fit cleanly: '+'; '.join(sample['reasons']))
        self.record['decision'] = self.current
        self.record['decision_reason'] = ('all final fit gates pass' if self.current == 128 else
                                          'conservative 64; all final fit gates pass')

    def check_key_leak(self):
        # Literal, streaming grep in-process: the pattern never enters argv/env/files.
        # Include warm's sibling report/logs and all runs outputs, even on failure.
        needles = [(label, value.encode()) for label, value in
                   (('classifier key', self.key), ('tunnel token', self.tunnel_token)) if value]
        if not needles:
            return
        overlap = max(len(needle) for _, needle in needles)-1
        for path in (ROOT/'runs').rglob('*'):
            if path.is_symlink():
                raise GateFailure('cannot certify key-leak check with symlinks in runs')
            if not path.is_file():
                continue
            tail = b''
            with path.open('rb') as stream:
                while block := stream.read(1024*1024):
                    data = tail+block
                    for label, needle in needles:
                        if needle in data:
                            raise GateFailure(label+' found in runs output (value suppressed)')
                    tail = data[-overlap:] if overlap else b''

    def start_tunnel(self):
        # The inference handoff is green. Optional tunnel timeout must not turn
        # it red, but this step still cannot extend the absolute credit budget.
        signal.setitimer(signal.ITIMER_REAL, 0)
        started = time.monotonic()
        until = started+max(0, min(self.tunnel_wait, self.end-time.time()))
        result = self.record['tunnel']
        result.update(status='not started', reason='timeout')
        try:
            if until > time.monotonic():
                pid, reason, output = node_tunnel.launch(
                    result['preflight']['script'], self.env, self.tunnel_token, until-time.monotonic())
                # Both captured streams are redacted in memory before any disk write.
                (self.run/'tunnel.log').write_text(self.safe(output))
                result.update(launched=pid is not None, pid=pid, reason=reason or 'timeout')
                self.save()
                if pid is not None and asyncio.run(node_tunnel.reachable(self.tunnel_url, until)):
                    result.update(status='up', reason=None)
        except Exception:
            # Neither arbitrary subprocess errors nor HTTP responses are printed.
            result.update(status='not started', reason='launch failed')
        result['waited_seconds'] = time.monotonic()-started
        self.save()
        if result['status'] == 'up':
            self.say('TUNNEL UP url='+self.tunnel_url)
        else:
            self.say('TUNNEL NOT STARTED reason='+result['reason'])

    def reminder(self):
        utc = datetime.fromtimestamp(self.end if self.deadline_confirmed else self.started, timezone.utc)
        self.say('TEARDOWN BY '+utc.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')+
                 ' / '+utc.strftime('%Y-%m-%d %H:%M:%S UTC')+' (manual; release the allocation)')

    def run_all(self):
        for name, action in (
            ('preflight', self.preflight), ('warm-timeout', self.warm_timeout), ('key', self.read_key),
            ('warm', self.warm), ('guards', self.guards),
            ('relay-safety', lambda: asyncio.run(self.relay_safety())),
            ('kv-pool', self.pool), ('loadtest', self.loadtest), ('fit', self.fit),
        ):
            with self.gate(name):
                action()
        with self.gate('handoff'):
            self.record.update(state='ready', ended_epoch=time.time())
            self.save()
            self.check_key_leak()
        # The gate writes its final timing before the final literal check.
        self.check_key_leak()
        self.say(f'NODE READY FOR DRILL relay={self.public_url} local=http://127.0.0.1:8000 '
                 f'fit={self.current} p99_ms={self.load.p99_ms:.2f} '
                 f'guard_vram_mib={self.guard_vram} report={self.path}')
        self.start_tunnel()
        with self.gate('key-leak', check_deadline=False):
            self.record['ended_epoch'] = time.time()
            self.check_key_leak()
        # Include the final gate timing/report serialization in the literal scan.
        self.check_key_leak()


def main():
    if os.environ.get('DRY_RUN') == '1':
        print('DRY RUN: no env files, stdin reads, probes, directories, containers or tunnels.')
        for name in GATES:
            print('PLAN '+name)
        print('Plan: pinned TP8 warm; two pinned loopback guards; 130 rps; 128 or one fallback to 64.')
        print('Plan: require tunnel helper/token/tools before warm; after handoff launch and probe without auth; scan both secrets.')
        print('Plan: provisioning + 11250 seconds - 600 seconds; manual teardown; no automatic restarts.')
        return 0
    os.umask(0o077)
    runner = NodeRun()

    def interrupted(signum, frame):
        raise GateFailure('teardown deadline reached' if signum == signal.SIGALRM else 'interrupted')

    for signum in (signal.SIGALRM, signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, interrupted)
    try:
        runner.run_all()
        return 0
    except BaseException as exc:
        signal.setitimer(signal.ITIMER_REAL, 0)
        reason = str(exc) if isinstance(exc, GateFailure) else type(exc).__name__
        runner.record.update(state='failed', ended_epoch=time.time(), failure_reason=runner.safe(reason))
        if not any(row['status'] == 'FAIL' for row in runner.record['gates']):
            now = time.time()
            runner.record['gates'].append({'name': 'handoff', 'status': 'FAIL', 'reason': runner.safe(reason),
                                           'started_epoch': now, 'ended_epoch': now, 'seconds': 0})
            runner.say('FAIL handoff: '+reason)
        runner.save()
        try:
            with runner.gate('key-leak-on-failure', check_deadline=False):
                runner.check_key_leak()
        except BaseException:
            pass
        return 1
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        runner.reminder()
        if runner.lock is not None:
            runner.lock.close()


if __name__ == '__main__':
    raise SystemExit(main())

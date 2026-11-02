"""Node warm-up only: no gateway flips, tunnels, provider actions or secret files."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

from warm_download import load_manifest, acquire
from warm_logic import (deadline, jit_start, tunnel_slot, graph_sizes, parse_pool,
                        concurrency_fit, fallback_settings, FALLBACK_RUNNING)
from serve_plan import (front_port, RELAY_PORT, variant, image_reference, require_pinned, host_gate, require_host_ram, build_plan,
                        add_arguments, argument_env, print_plan)
from mirror_auth import parse_mirror_auth
from warm_network import probe, public_url
from readiness import check
from redact import redact, secret_values
from relay_config import read_classifier_key, relay_environment

ROOT = Path(__file__).resolve().parents[1]


class EngineStopped(RuntimeError):
    """The engine container exited before health (e.g. KV/CUDA-graph OOM)."""


def warm_timeout(env):
    timeout = str(env.get('WARM_TIMEOUT_SECONDS', '1200'))
    if not re.fullmatch(r'[0-9]+', timeout) or int(timeout) <= 0:
        raise ValueError('WARM_TIMEOUT_SECONDS must be a positive integer')
    return int(timeout)


def settings(data, env, ram):
    if env.get('ATTN_MODE','tp8') not in ('tp8','dp_ep'): raise ValueError('ATTN_MODE must be tp8 or dp_ep')
    if env.get('SPEC_DECODE','off') != 'off':
        raise ValueError('SPEC_DECODE=mtp dropped: no verified V4.1 MTP recipe; use off')
    v = variant(env)
    require_host_ram({**env, 'ENGRAM': v['engram']}, ram)
    if env.get('ENGINE_EXTRA_ARGS_JSON', '[]') != '[]':
        raise ValueError('A/B warm plans require named selectors, not ENGINE_EXTRA_ARGS_JSON')
    # 128 is staged for the Nov 1 window; Warm.launch_with_fit_fallback drops to 64
    # when the engine's KV pool report does not fit (warm_logic.concurrency_fit).
    maximum = int(env.get('MAX_RUNNING_REQUESTS','128'))
    if maximum < 1: raise ValueError('MAX_RUNNING_REQUESTS must be positive')
    graph = graph_sizes(maximum, env.get('WARM_GRAPH_BS'))
    context = int(env.get('CONTEXT_LENGTH','262144'))
    if not 250_000 <= context <= 400_000:
        raise ValueError('Warm CONTEXT_LENGTH must be 250000..400000 tokens')
    return dict(ENGINE='sglang', MODEL=data['model']['repo'], MODEL_REVISION=data['model']['revision'],
                ENGINE_PORT=str(front_port(env)), SSE_RELAY=env.get('SSE_RELAY','1'),
                CONTEXT_LENGTH=str(context),
                MEM_FRACTION=env.get('MEM_FRACTION','0.85'), MAX_RUNNING_REQUESTS=str(maximum),
                CHUNKED_PREFILL_SIZE=env.get('CHUNKED_PREFILL_SIZE','8192'), CUDA_GRAPH_MAX_BS=str(graph[-1]),
                PARALLELISM='tp' if v['layout'] == 'tp8' else 'dp_ep',
                SPECULATION='dspark' if v['dspark'] == 'on' else 'none',
                LAYOUT=v['layout'], ENGRAM=v['engram'], DSPARK=v['dspark'],
                WARM_GRAPH_BS=','.join(map(str,graph)), SCHEDULE_POLICY=env.get('SCHEDULE_POLICY','lpm'),
                ENABLE_CACHE_REPORT='1', HIERARCHICAL_CACHE=env.get('HIERARCHICAL_CACHE','0'),
                ENGRAM_TABLE_GB=env.get('ENGRAM_TABLE_GB','203'),
                ENGRAM_HEADROOM_GB=env.get('ENGRAM_HEADROOM_GB','32'))


class Warm:
    def __init__(self, env):
        started = time.time()
        # Validate operator-exported values before filesystem/network/GPU work.
        timeout = warm_timeout(env)
        self.tunnel_slot = tunnel_slot(env)
        parse_mirror_auth(env.get('MIRROR_AUTH_HEADER'))
        variant(env)
        self.image_meta = image_reference(env)
        require_pinned(self.image_meta)
        self.env = env
        self.child_env = {k: v for k, v in os.environ.items() if k != 'SAFETY_CLASSIFIER_KEY'}
        self.data = load_manifest()
        window_end = deadline(int(env['WINDOW_END']))
        self.end = min(window_end, started + timeout)
        self.deadline_bound = ('WINDOW_END minus 13 minutes' if window_end <= started + timeout
                               else f'WARM_TIMEOUT_SECONDS={timeout}')
        self.id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:8]
        self.run = ROOT/'runs'/('warm-'+self.id); self.run.mkdir(parents=True)
        self.path = self.run.with_suffix('.json')
        self.ready = self.run/'ready.json'
        self.record = {'model':self.data['model'], 'started_epoch':started, 'deadline_epoch':self.end,
                       'window_end_epoch':int(env['WINDOW_END']),
                       'warm_timeout_seconds':timeout,
                       'stages':{}, 'state':'warming', 'tunnel_slot':self.tunnel_slot}
        self.lock = threading.RLock()
        self.name = 'b300-warm-'+self.id.lower()
        self.created = False
        self.relay = None
        self.secrets = secret_values(env)

    def save(self):
        with self.lock:
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(redact(json.dumps(self.record,indent=2),self.secrets)+'\n'); tmp.replace(self.path)

    def remaining(self):
        value = self.end-time.time()
        if value <= 0: raise TimeoutError('Warm deadline reached: '+self.deadline_bound)
        return value

    def timed_out(self, exc):
        if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
            return True
        return (isinstance(exc, urllib.error.URLError) and time.time() >= self.end
                and (isinstance(exc.reason, TimeoutError) or exc.reason == 'timed out'))

    def stage(self, name, fn):
        with self.lock:
            self.record['stages'][name] = {'start_epoch':time.time()}; self.save()
        try:
            self.remaining()
            result = fn()
            self.remaining()
            with self.lock: self.record['stages'][name]['ok'] = True
            return result
        except Exception as exc:
            with self.lock: self.record['stages'][name].update(ok=False,error=type(exc).__name__)
            raise
        finally:
            with self.lock:
                row = self.record['stages'][name]; row['end_epoch']=time.time()
                row['seconds']=row['end_epoch']-row['start_epoch']; self.save()

    def command(self, args):
        # Suppress exception stdout/argv; only sanitized output ever reaches disk.
        p = subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                           timeout=self.remaining(),check=False,env=self.child_env)
        output = p.stdout.decode(errors='replace')
        with (self.run/'operations.log').open('a') as stream:
            stream.write(redact(output,self.secrets))
        if p.returncode: raise RuntimeError('Command failed; see redacted operations log')
        return output

    def image(self):
        image = self.image_meta['image_ref']
        require_pinned(self.image_meta)
        with self.lock:
            self.record.update(self.image_meta); self.save()
        local = subprocess.run(['docker','image','inspect',image],stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL,timeout=self.remaining(),env=self.child_env).returncode == 0
        if not (self.env.get('BAKED_IMAGE') and local): self.command(['docker','pull',image])
        image_id = self.command(['docker','image','inspect',image,'--format','{{.Id}}']).strip()
        if not re.fullmatch(r'sha256:[0-9a-f]{64}',image_id): raise ValueError('Invalid image ID')
        with self.lock:
            self.record['image_id'] = image_id; self.save()
        return image_id

    def weights(self):
        if self.env.get('MIRROR_URL'): public_url(self.env['MIRROR_URL'])
        records = asyncio.run(acquire(self.data, self.weights_dir,
                              self.env.get('BASE_WEIGHTS_DIR','/mnt/nvme/models/dsv41'),self.end,self.env,
                              log=self.run/'weights.log'))
        with self.lock:
            self.record['transfers'] = records; self.save()

    def completion(self, messages, tokens=1, stream=False):
        payload={'model':self.data['model']['repo'],'messages':messages,'max_tokens':tokens,
                 'stream':stream,'temperature':0}
        if stream: payload['stream_options']={'include_usage':True}
        request=urllib.request.Request(f'http://127.0.0.1:{RELAY_PORT}/v1/chat/completions',
            data=json.dumps(payload).encode(), headers={'Content-Type':'application/json',
            'Authorization':'Bearer '+self.env['INFER_UPSTREAM_KEY']})
        with urllib.request.urlopen(request,timeout=min(120,self.remaining())) as response:
            if not stream:
                result=json.load(response)
                if not result.get('choices') or result.get('usage',{}).get('completion_tokens',0)<1:
                    raise ValueError('Empty completion')
            else:
                done=content=False
                for line in response:
                    self.remaining()
                    if not line.startswith(b'data:'): continue
                    value=line[5:].strip()
                    if value==b'[DONE]': done=True; break
                    event=json.loads(value)
                    for choice in event.get('choices',[]):
                        delta=choice.get('delta',{})
                        content |= any(bool(delta.get(k)) for k in ('content','reasoning','reasoning_content'))
                if not (done and content): raise ValueError('Stream missing content or DONE')

    def start_relay(self, selected):
        """SSE heartbeat relay on :8000 in front of the engine/router (scripts/sse_relay.py).
        Detached; only the safety allow-list (including its auth key) reaches the relay."""
        if selected.get('SSE_RELAY', '1') != '1':
            return None
        log = (self.run/'relay.log').open('ab')
        env = relay_environment(self.env, RELAY_PORT, selected['ENGINE_PORT'])
        proc = subprocess.Popen([sys.executable, str(ROOT/'scripts/sse_relay.py')], env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        log.close()
        self.relay = proc
        for _ in range(100):
            self.remaining()
            if proc.poll() is not None: raise RuntimeError('SSE relay exited; see relay.log')
            try:
                socket.create_connection(('127.0.0.1', RELAY_PORT), timeout=min(1,self.remaining())).close(); break
            except OSError:
                time.sleep(min(0.1,self.remaining()))
        else:
            raise RuntimeError('SSE relay did not open port 8000')
        with self.lock:
            self.record['sse_relay'] = {'pid': proc.pid, 'port': RELAY_PORT, 'engine_port': int(selected['ENGINE_PORT']),
                                        'heartbeat_seconds': float(env['SSE_HEARTBEAT_SECONDS'])}
            self.save()
        return proc.pid

    def launch(self, image, selected):
        # Record installed version and file hash rather than asserting tag==source commit.
        info = self.command(['docker','run','--rm','--network','none','--entrypoint','python3',image,'-c',
            'import importlib.metadata,hashlib,json,sglang.srt.server_args as a; '
            'print(json.dumps({"version":importlib.metadata.version("sglang"),'
            '"server_args_sha256":hashlib.sha256(open(a.__file__,"rb").read()).hexdigest()}))'])
        self.record['sglang_package'] = json.loads(info.strip().splitlines()[-1])
        engine_help = self.command(['docker','run','--rm','--network','none','--entrypoint','python3',
                                    image, '-m', 'sglang.launch_server', '--help'])
        engram_flag = '--engram-config' in set(re.findall(r'--[a-z0-9-]+',engine_help))
        if not engram_flag and selected.get('ENGRAM', 'gpu') == 'host':
            raise ValueError('image lacks --engram-config; host Engram unsupported on this image')
        plan = build_plan(selected, image, self.name, self.weights_dir, ROOT, engram_flag=engram_flag)
        self.record['engram_config'] = plan['engram_config']
        # Validate all processes before allocating GPUs. The installed preview
        # must understand the exact flags and Engram schema we researched.
        for process in plan['processes']:
            args = process['argv_without_key']
            help_text = engine_help if process['role'] == 'engine' else self.command([
                'docker','run','--rm','--network','none','--entrypoint','python3',image, '-m', args[2], '--help'])
            required = {a for a in args if a.startswith('--')} | {'--api-key'}
            missing = sorted(required-set(re.findall(r'--[a-z0-9-]+',help_text)))
            if missing: raise ValueError('Image lacks required flags: '+','.join(missing))
            if process['role'] == 'engine':
                code = ('import argparse,sys; from sglang.srt.server_args import ServerArgs; '
                        'p=argparse.ArgumentParser(); ServerArgs.add_cli_args(p); '
                        'a=p.parse_args(sys.argv[1:])')
                if engram_flag:
                    code += ('; from sglang.srt.arg_groups.engram_config import EngramConfig; '
                             'EngramConfig.from_dict(a.engram_config)')
            else:
                code = ('import argparse,sys; from sglang_router.router_args import RouterArgs; '
                        'p=argparse.ArgumentParser(); RouterArgs.add_cli_args(p); p.parse_args(sys.argv[1:])')
            self.command(['docker','run','--rm','--network','none','--entrypoint','python3',image,'-c',code,*args[3:]])
        self.record['launch_plan'] = plan
        self.record['variant'] = plan['variant']
        self.record['argv_without_key'] = plan['processes'][0]['argv_without_key']
        self.save()
        # A timed-out create may still have allocated the container.
        self.created=True
        self.command(plan['docker_create'])
        self.record['container']=self.name
        with (self.run/'engine.log').open('ab') as log:
            redactor=subprocess.Popen([sys.executable,str(ROOT/'scripts/redact.py')],stdin=subprocess.PIPE,
                                      stdout=log,stderr=(self.run/'redact.err').open('ab'),start_new_session=True,env=self.child_env)
            engine=subprocess.Popen(plan['docker_start'],stdin=subprocess.PIPE,stdout=redactor.stdin,
                                    stderr=subprocess.STDOUT,start_new_session=True,env=self.child_env)
            redactor.stdin.close()
            engine.stdin.write((json.dumps({'plan':plan,'env':{**selected,'INFER_UPSTREAM_KEY':self.env['INFER_UPSTREAM_KEY']}})+'\n').encode())
            engine.stdin.close()
        self.engine_proc=engine
        self.record['capture_pids']=[engine.pid,redactor.pid]; self.save()

    def health(self):
        while True:
            self.remaining()
            try:
                for process in self.record['launch_plan']['processes']:
                    check(f"http://127.0.0.1:{process['port']}", self.env['INFER_UPSTREAM_KEY'], self.data['model']['repo'])
                if self.relay is not None:  # and the tunnel's front door, through the relay
                    check(f'http://127.0.0.1:{RELAY_PORT}', self.env['INFER_UPSTREAM_KEY'], self.data['model']['repo'])
                break
            except Exception:
                # docker start -ai returns before the container leaves 'created'; that is not a stop.
                state=self.command(['docker','inspect',self.name,'--format','{{.State.Status}}']).strip()
                attached=getattr(self,'engine_proc',None)
                if state not in ('created','running','restarting') or (attached is not None and attached.poll() is not None):
                    self.record_exit(); raise EngineStopped('Engine stopped before health')
                time.sleep(min(2,self.remaining()))
        benign = self.env.get('NODE_RUN_BENIGN_SMOKE') == '1'
        self.completion([{'role':'user','content':'hello world' if benign else 'Reply hi.'}])
        self.completion([{'role':'user','content':'hello world' if benign else 'Count to three.'}],32,True)

    def record_exit(self):
        try:
            state=self.command(['docker','inspect',self.name,'--format',
                                '{{json .State.Status}} {{.State.ExitCode}} {{.State.OOMKilled}} {{json .State.Error}}'])
        except Exception:
            return
        with self.lock:
            self.record['engine_exit']=redact(state.strip(),self.secrets); self.save()

    def run_all(self):
        self.remaining()
        if not self.env.get('INFER_UPSTREAM_KEY'): raise ValueError('Export existing INFER_UPSTREAM_KEY; no key files are read')
        self.weights_dir=str(Path(self.env.get('WEIGHTS_DIR','/mnt/nvme/models/dsv41-uncensored')).absolute())
        names=self.stage('hardware',lambda:self.command(['nvidia-smi','--query-gpu=name','--format=csv,noheader']))
        if len(names.strip().splitlines()) != 8 or not all('B300' in name for name in names.strip().splitlines()):
            raise ValueError('Warm preset requires eight B300 GPUs')
        # Refuse a warm-up on a node already serving port 8000.
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',8000))
        mem=re.search(r'^MemAvailable:\s+(\d+) kB',Path('/proc/meminfo').read_text(),re.M)
        if not mem: raise ValueError('MemAvailable is unavailable')
        ram=host_gate(self.env, int(mem[1])*1024)
        selected=settings(self.data,self.env,ram)
        planned = build_plan(selected, self.image_meta['image_ref'], self.name, self.weights_dir, ROOT)
        for process in planned['processes']:
            ports = [process['port']]
            argv = process['argv_without_key']
            if '--dist-init-addr' in argv:
                ports.append(int(argv[argv.index('--dist-init-addr')+1].rsplit(':', 1)[1]))
            for port in ports:
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1', port))
        self.record.update(ram=ram,config=selected); self.save()
        with ThreadPoolExecutor(max_workers=3) as pool:
            image=pool.submit(self.stage,'image',self.image)
            weights=pool.submit(self.stage,'weights_and_hash',self.weights)
            network=pool.submit(self.stage,'network',lambda:probe(self.data,self.env.get('MIRROR_URL'),
                self.env.get('MIRROR_TEST_OBJECT','speed-test.bin'),self.env.get('MIRROR_AUTH_HEADER')))
            self.record['network']=network.result()
            image_id=image.result(); weights.result()
        self.remaining()
        def gate():
            available=int(re.search(r'^MemAvailable:\s+(\d+) kB',Path('/proc/meminfo').read_text(),re.M)[1])*1024
            return host_gate(self.env, available)
        ram=self.stage('ram_gate',gate)
        selected=settings(self.data,self.env,ram)
        planned = build_plan(selected, self.image_meta['image_ref'], self.name, self.weights_dir, ROOT)
        for process in planned['processes']:
            ports = [process['port']]
            argv = process['argv_without_key']
            if '--dist-init-addr' in argv:
                ports.append(int(argv[argv.index('--dist-init-addr')+1].rsplit(':', 1)[1]))
            for port in ports:
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1', port))
        self.record.update(ram=ram,config=selected);self.save()
        self.stage('sse_relay',lambda:self.start_relay(selected))
        selected=self.launch_with_fit_fallback(image_id,selected)
        self.record['container_started_at'] = self.command(
            ['docker','inspect',self.name,'--format','{{.State.StartedAt}}']).strip()
        def replay():
            if self.env.get('NODE_RUN_BENIGN_SMOKE') == '1':
                return
            for path in sorted((ROOT/'config/warm-prefixes').glob('*.json')):
                self.completion(json.loads(path.read_text()),16)
        self.stage('prefix_replay',replay)
        self.publish_ready()

    def engine_pool(self):
        path = self.run/'engine.log'
        return parse_pool(path.read_text(errors='replace') if path.exists() else '')

    def launch_with_fit_fallback(self, image_id, selected):
        """Launch at MAX_RUNNING_REQUESTS (128); if warm_logic.concurrency_fit fails, relaunch
        once at FALLBACK_MAX_RUNNING_REQUESTS (64) with the 64 graph ladder, and flag it in
        stdout/stderr, runs/warm-<id>.json ('concurrency') and ready.json. Returns the
        settings actually serving. This is the only place warm-up acts on the decision."""
        requested = int(selected['MAX_RUNNING_REQUESTS'])
        context = int(selected['CONTEXT_LENGTH'])
        per_stream = int(self.env.get('FIT_TOKENS_PER_STREAM', '32768'))
        fallback = fallback_settings(selected, int(self.env.get('FALLBACK_MAX_RUNNING_REQUESTS', FALLBACK_RUNNING)))
        self.stage('launch_and_flag_validation',lambda:self.launch(image_id,selected))
        started = True
        try:
            self.stage('load_graph_and_health',self.health)
        except EngineStopped:
            if fallback is None: raise
            started = False
        fit = concurrency_fit(self.engine_pool(), requested, context, per_stream, started)
        info = {'requested': requested, 'fit': fit, 'fallback_reason': None}
        effective = selected
        if fit['passed']:
            print(f"FIT PASSED: max_running_requests={requested} at context {context}; "
                  f"KV pool {fit['max_total_num_tokens']} tokens; full-context streams {fit['full_context_streams']}", flush=True)
        elif fallback is None:
            print(f"FIT WARNING at floor {requested}: {fit['reason']}; serving anyway", file=sys.stderr, flush=True)
        else:
            info['fallback_reason'] = fit['reason']
            line = (f"FIT FALLBACK: max_running_requests {requested} -> {fallback['MAX_RUNNING_REQUESTS']} "
                    f"(graphs {fallback['WARM_GRAPH_BS']}): {fit['reason']}. Flag Kai before traffic.")
            print(line, flush=True); print(line, file=sys.stderr, flush=True)
            with self.lock:
                self.record['concurrency'] = info; self.save()
            self.relaunch_at(image_id, fallback, requested)
            info['fallback_fit'] = concurrency_fit(self.engine_pool(), int(fallback['MAX_RUNNING_REQUESTS']),
                                                   context, per_stream)
            effective = fallback
        info.update(effective=int(effective['MAX_RUNNING_REQUESTS']), cuda_graph_max_bs=int(effective['CUDA_GRAPH_MAX_BS']),
                    warm_graph_bs=effective['WARM_GRAPH_BS'])
        with self.lock:
            self.record.update(concurrency=info, config=effective); self.save()
        return effective

    def relaunch_at(self, image_id, selected, previous):
        """The one explicit fit fallback, shared with the supervised node runner."""
        self.command(['docker','rm','-f',self.name]); self.created=False
        if (self.run/'engine.log').exists():
            (self.run/'engine.log').rename(self.run/f'engine-{previous}.log')
        self.stage('fallback_launch',lambda:self.launch(image_id,selected))
        self.stage('fallback_health',self.health)

    @classmethod
    def resume_at_64(cls, path, env):
        """Resume a ready warm run without downloads or restarting its relay.

        The caller still supplies the original rental WINDOW_END. A fresh bounded
        launch budget cannot extend that absolute deadline.
        """
        path = Path(path).resolve()
        if not path.is_relative_to((ROOT/'runs').resolve()):
            raise ValueError('Fallback report must be inside runs')
        record = json.loads(path.read_text())
        if record.get('state') != 'ready' or record.get('config', {}).get('LAYOUT') != 'tp8':
            raise ValueError('Fallback requires a ready tp8 warm report')
        if record.get('window_end_epoch') != int(env['WINDOW_END']):
            raise ValueError('Fallback must preserve the original WINDOW_END')
        timeout = warm_timeout(env)
        selected = fallback_settings(record['config'], 64)
        if selected is None or record.get('node_run_fallback'):
            raise ValueError('Fallback is allowed only once, from above 64')
        image = image_reference(env)
        require_pinned(image)
        if image['image_ref'] != record['image_ref']:
            raise ValueError('Fallback image differs from the running image')
        obj = cls.__new__(cls)
        obj.env = env
        obj.child_env = {k: v for k, v in os.environ.items() if k != 'SAFETY_CLASSIFIER_KEY'}
        obj.path, obj.run = path, path.with_suffix('')
        obj.ready = obj.run/'ready.json'
        obj.record, obj.image_meta = record, image
        obj.name, obj.tunnel_slot = record['container'], tunnel_slot(env)
        obj.data = load_manifest()
        obj.weights_dir = str(Path(env.get('WEIGHTS_DIR','/mnt/nvme/models/dsv41-uncensored')).absolute())
        obj.end = min(deadline(int(env['WINDOW_END'])), time.time()+timeout)
        obj.deadline_bound = 'node-run fallback deadline'
        obj.lock, obj.created, obj.relay = threading.RLock(), True, None
        obj.secrets = secret_values(env)
        obj.record.update(node_run_fallback=True, state='warming', deadline_epoch=obj.end)
        obj.ready.unlink(missing_ok=True)
        obj.save()
        return obj, selected

    def run_fallback(self, selected):
        previous = int(self.record['config']['MAX_RUNNING_REQUESTS'])
        self.relaunch_at(self.record['image_id'], selected, previous)
        fit = concurrency_fit(self.engine_pool(), 64, int(selected['CONTEXT_LENGTH']),
                              int(self.env.get('FIT_TOKENS_PER_STREAM', '32768')))
        self.record['concurrency'].update(effective=64, fallback_fit=fit,
                                         cuda_graph_max_bs=64, warm_graph_bs=selected['WARM_GRAPH_BS'])
        self.record['config'] = selected
        self.record['container_started_at'] = self.command(
            ['docker','inspect',self.name,'--format','{{.State.StartedAt}}']).strip()
        self.publish_ready()

    def publish_ready(self):
        self.remaining()
        now=time.time(); elapsed=now-self.record['started_epoch']
        signal={'schema':1,'state':'ready','model':self.data['model']['repo'],'revision':self.data['model']['revision'],
                'ready_epoch':now,'deadline_epoch':self.end,'container':self.name,'timing_file':str(self.path),
                'tunnel_slot':self.tunnel_slot}
        # Additive, schema stays 1. Netops reads these before routing traffic.
        c = self.record.get('concurrency', {})
        fit = c.get('fallback_fit') or c.get('fit') or {}
        signal.update(max_running_requests_requested=c.get('requested'), max_running_requests_effective=c.get('effective'),
                      fit_passed=(c.get('fit') or {}).get('passed'), fallback_reason=c.get('fallback_reason'),
                      kv_pool_tokens=fit.get('max_total_num_tokens'), full_context_streams=fit.get('full_context_streams'),
                      cuda_graph_max_bs=c.get('cuda_graph_max_bs'), warm_graph_bs=c.get('warm_graph_bs'),
                      sse_relay=self.record.get('sse_relay'))
        self.record.update(state='ready',end_epoch=now,warm_seconds=elapsed,jit_start_epoch=jit_start(int(self.env['WINDOW_END']),elapsed))
        self.save()
        tmp=self.ready.with_suffix('.tmp');tmp.write_text(json.dumps(signal)+'\n')
        self.remaining();tmp.replace(self.ready)
        print('READY '+json.dumps(signal),flush=True)
        print('Recommended JIT start epoch: '+str(self.record['jit_start_epoch']),flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    parser.add_argument('--resume-at-64', type=Path, help='node-run: one fit fallback using this warm report')
    args = parser.parse_args(argv)
    env = argument_env(args, os.environ)
    if args.print_plan or env.get('DRY_RUN') == '1':
        print_plan(env, args.available_ram_gb)
        return 0
    if args.available_ram_gb is not None: parser.error('--available-ram-gb is dry-run only')
    # Keep this secret in memory, not the inherited environment of Docker/helpers.
    os.environ.pop('SAFETY_CLASSIFIER_KEY', None)
    if env.get('NODE_RUN_STDIN') == '1':
        try:
            env['SAFETY_CLASSIFIER_KEY'] = read_classifier_key(sys.stdin)
        except ValueError as exc:
            print('WARM FAILED: '+str(exc), file=sys.stderr)
            return 1
    os.umask(0o077)
    (ROOT/'runs').mkdir(exist_ok=True)
    with (ROOT/'runs/warm.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            if args.resume_at_64:
                warm, selected = Warm.resume_at_64(args.resume_at_64, env)
            else:
                warm=Warm(env)
        except ValueError as exc:
            print('WARM FAILED: '+redact(str(exc),secret_values(env)),file=sys.stderr)
            return 1
        try:
            if args.resume_at_64:
                warm.run_fallback(selected)
            else:
                warm.run_all()
        except BaseException as exc:
            reason = 'timeout' if warm.timed_out(exc) else 'error'
            warm.record.update(state='failed',reason=reason,error=type(exc).__name__,end_epoch=time.time());warm.save()
            warm.ready.unlink(missing_ok=True)
            if warm.created:
                try:
                    subprocess.run(['docker','rm','-f',warm.name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30)
                except (OSError, subprocess.SubprocessError):
                    pass
            if warm.relay is not None and warm.relay.poll() is None:
                warm.relay.terminate()
            failure = 'timeout' if reason == 'timeout' else type(exc).__name__
            print('WARM FAILED: '+failure+'; inspect '+str(warm.path),file=sys.stderr)
            return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

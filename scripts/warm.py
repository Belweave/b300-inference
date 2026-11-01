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
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

from warm_download import load_manifest, acquire
from warm_logic import deadline, jit_start, tunnel_slot
from serve_plan import (variant, image_reference, host_gate, require_host_ram, build_plan,
                        add_arguments, argument_env, print_plan)
from mirror_auth import parse_mirror_auth
from warm_network import probe, public_url
from readiness import check
from redact import redact, secret_values

ROOT = Path(__file__).resolve().parents[1]


def settings(data, env, ram):
    if env.get('ATTN_MODE','tp8') not in ('tp8','dp_ep'): raise ValueError('ATTN_MODE must be tp8 or dp_ep')
    if env.get('SPEC_DECODE','off') != 'off':
        raise ValueError('SPEC_DECODE=mtp dropped: no verified V4.1 MTP recipe; use off')
    v = variant(env)
    require_host_ram({**env, 'ENGRAM': v['engram']}, ram)
    if env.get('ENGINE_EXTRA_ARGS_JSON', '[]') != '[]':
        raise ValueError('A/B warm plans require named selectors, not ENGINE_EXTRA_ARGS_JSON')
    maximum = int(env.get('MAX_RUNNING_REQUESTS','64'))
    if maximum < 1: raise ValueError('MAX_RUNNING_REQUESTS must be positive')
    graph = ([int(x) for x in env['WARM_GRAPH_BS'].split(',')] if env.get('WARM_GRAPH_BS')
             else [n for n in (1,2,4,8,16,32,64) if n <= maximum])
    if not graph or sorted(set(graph)) != graph or graph[0] < 1 or graph[-1] > maximum:
        raise ValueError('Graph sizes must be increasing, unique and within max running')
    context = int(env.get('CONTEXT_LENGTH','262144'))
    if not 250_000 <= context <= 400_000:
        raise ValueError('Warm CONTEXT_LENGTH must be 250000..400000 tokens')
    return dict(ENGINE='sglang', MODEL=data['model']['repo'], MODEL_REVISION=data['model']['revision'],
                ENGINE_PORT='8000', CONTEXT_LENGTH=str(context),
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
        # Validate operator-exported values before filesystem/network/GPU work.
        self.tunnel_slot = tunnel_slot(env)
        parse_mirror_auth(env.get('MIRROR_AUTH_HEADER'))
        variant(env)
        self.image_meta = image_reference(env)
        self.env = env
        self.data = load_manifest()
        self.end = deadline(int(env['WINDOW_END']))
        self.id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:8]
        self.run = ROOT/'runs'/('warm-'+self.id); self.run.mkdir(parents=True)
        self.path = self.run.with_suffix('.json')
        self.ready = self.run/'ready.json'
        self.record = {'model':self.data['model'], 'started_epoch':time.time(), 'deadline_epoch':self.end,
                       'stages':{}, 'state':'warming', 'tunnel_slot':self.tunnel_slot}
        self.lock = threading.RLock()
        self.name = 'b300-warm-'+self.id.lower()
        self.created = False
        self.secrets = secret_values(env)

    def save(self):
        with self.lock:
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(redact(json.dumps(self.record,indent=2),self.secrets)+'\n'); tmp.replace(self.path)

    def remaining(self):
        value = self.end-time.time()
        if value <= 0: raise TimeoutError('Old-node WINDOW_END minus 13 minutes has passed')
        return value

    def stage(self, name, fn):
        with self.lock:
            self.record['stages'][name] = {'start_epoch':time.time()}; self.save()
        try:
            result = fn()
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
                           timeout=self.remaining(),check=False)
        output = p.stdout.decode(errors='replace')
        with (self.run/'operations.log').open('a') as stream:
            stream.write(redact(output,self.secrets))
        if p.returncode: raise RuntimeError('Command failed; see redacted operations log')
        return output

    def image(self):
        image = self.image_meta['image_ref']
        if not self.image_meta['image_pinned']:
            print('WARNING: image is unpinned: '+image, file=sys.stderr)
        with self.lock:
            self.record.update(self.image_meta); self.save()
        local = subprocess.run(['docker','image','inspect',image],stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL,timeout=self.remaining()).returncode == 0
        if not (self.env.get('BAKED_IMAGE') and local): self.command(['docker','pull',image])
        image_id = self.command(['docker','image','inspect',image,'--format','{{.Id}}']).strip()
        if not re.fullmatch(r'sha256:[0-9a-f]{64}',image_id): raise ValueError('Invalid image ID')
        with self.lock:
            self.record['image_id'] = image_id; self.save()
        return image_id

    def weights(self):
        if self.env.get('MIRROR_URL'): public_url(self.env['MIRROR_URL'])
        records = asyncio.run(acquire(self.data, self.weights_dir,
                              self.env.get('BASE_WEIGHTS_DIR','/mnt/nvme/models/dsv41'),self.end,self.env))
        with self.lock:
            self.record['transfers'] = records; self.save()

    def completion(self, messages, tokens=1, stream=False):
        payload={'model':self.data['model']['repo'],'messages':messages,'max_tokens':tokens,
                 'stream':stream,'temperature':0}
        if stream: payload['stream_options']={'include_usage':True}
        request=urllib.request.Request('http://127.0.0.1:8000/v1/chat/completions',
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

    def launch(self, image, selected):
        # Record installed version and file hash rather than asserting tag==source commit.
        info = self.command(['docker','run','--rm','--network','none','--entrypoint','python3',image,'-c',
            'import importlib.metadata,hashlib,json,sglang.srt.server_args as a; '
            'print(json.dumps({"version":importlib.metadata.version("sglang"),'
            '"server_args_sha256":hashlib.sha256(open(a.__file__,"rb").read()).hexdigest()}))'])
        self.record['sglang_package'] = json.loads(info.strip().splitlines()[-1])
        plan = build_plan(selected, image, self.name, self.weights_dir, ROOT)
        # Validate all processes before allocating GPUs. The installed preview
        # must understand the exact flags and Engram schema we researched.
        for process in plan['processes']:
            args = process['argv_without_key']
            help_text = self.command(['docker','run','--rm','--network','none','--entrypoint','python3',
                                      image, '-m', args[2], '--help'])
            required = {a for a in args if a.startswith('--')} | {'--api-key'}
            missing = sorted(required-set(re.findall(r'--[a-z0-9-]+',help_text)))
            if missing: raise ValueError('Image lacks required flags: '+','.join(missing))
            if process['role'] == 'engine':
                code = ('import argparse,sys; from sglang.srt.server_args import ServerArgs; '
                        'from sglang.srt.arg_groups.engram_config import EngramConfig; '
                        'p=argparse.ArgumentParser(); ServerArgs.add_cli_args(p); '
                        'a=p.parse_args(sys.argv[1:]); EngramConfig.from_dict(a.engram_config)')
            else:
                code = ('import argparse,sys; from sglang_router.router_args import RouterArgs; '
                        'p=argparse.ArgumentParser(); RouterArgs.add_cli_args(p); p.parse_args(sys.argv[1:])')
            self.command(['docker','run','--rm','--network','none','--entrypoint','python3',image,'-c',code,*args[3:]])
        self.record['launch_plan'] = plan
        self.record['variant'] = plan['variant']
        self.record['argv_without_key'] = plan['processes'][0]['argv_without_key']
        self.save()
        self.command(plan['docker_create'])
        self.created=True
        self.record['container']=self.name
        with (self.run/'engine.log').open('ab') as log:
            redactor=subprocess.Popen([sys.executable,str(ROOT/'scripts/redact.py')],stdin=subprocess.PIPE,
                                      stdout=log,stderr=subprocess.DEVNULL,start_new_session=True)
            engine=subprocess.Popen(plan['docker_start'],stdin=subprocess.PIPE,stdout=redactor.stdin,
                                    stderr=subprocess.STDOUT,start_new_session=True)
            redactor.stdin.close()
            engine.stdin.write((json.dumps({'plan':plan,'env':{**selected,'INFER_UPSTREAM_KEY':self.env['INFER_UPSTREAM_KEY']}})+'\n').encode())
            engine.stdin.close()
        self.record['capture_pids']=[engine.pid,redactor.pid]; self.save()

    def health(self):
        while True:
            self.remaining()
            try:
                for process in self.record['launch_plan']['processes']:
                    check(f"http://127.0.0.1:{process['port']}", self.env['INFER_UPSTREAM_KEY'], self.data['model']['repo'])
                break
            except Exception:
                state=self.command(['docker','inspect',self.name,'--format','{{.State.Running}}']).strip()
                if state != 'true': raise RuntimeError('Engine stopped before health')
                time.sleep(min(2,self.remaining()))
        self.completion([{'role':'user','content':'Reply hi.'}])
        self.completion([{'role':'user','content':'Count to three.'}],32,True)

    def run_all(self):
        self.remaining()
        if not self.env.get('INFER_UPSTREAM_KEY'): raise ValueError('Export existing INFER_UPSTREAM_KEY; no key files are read')
        self.weights_dir=str(Path(self.env.get('WEIGHTS_DIR','/mnt/nvme/models/dsv41-uncensored')).absolute())
        names=self.stage('hardware',lambda:self.command(['nvidia-smi','--query-gpu=name','--format=csv,noheader']))
        if len(names.strip().splitlines()) != 8 or not all('B300' in name for name in names.strip().splitlines()):
            raise ValueError('Warm preset requires eight B300 GPUs')
        # Refuse a warm-up on a node already serving port 8000.
        import socket
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
        self.record.update(ram=ram,config=selected);self.save()
        self.stage('launch_and_flag_validation',lambda:self.launch(image_id,selected))
        self.stage('load_graph_and_health',self.health)
        self.record['container_started_at'] = self.command(
            ['docker','inspect',self.name,'--format','{{.State.StartedAt}}']).strip()
        def replay():
            for path in sorted((ROOT/'config/warm-prefixes').glob('*.json')):
                self.completion(json.loads(path.read_text()),16)
        self.stage('prefix_replay',replay)
        self.publish_ready()

    def publish_ready(self):
        self.remaining()
        now=time.time(); elapsed=now-self.record['started_epoch']
        signal={'schema':1,'state':'ready','model':self.data['model']['repo'],'revision':self.data['model']['revision'],
                'ready_epoch':now,'deadline_epoch':self.end,'container':self.name,'timing_file':str(self.path),
                'tunnel_slot':self.tunnel_slot}
        self.record.update(state='ready',end_epoch=now,warm_seconds=elapsed,jit_start_epoch=jit_start(int(self.env['WINDOW_END']),elapsed))
        self.save()
        tmp=self.ready.with_suffix('.tmp');tmp.write_text(json.dumps(signal)+'\n')
        self.remaining();tmp.replace(self.ready)
        print('READY '+json.dumps(signal),flush=True)
        print('Recommended JIT start epoch: '+str(self.record['jit_start_epoch']),flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    args = parser.parse_args(argv)
    env = argument_env(args, os.environ)
    if args.print_plan or env.get('DRY_RUN') == '1':
        print_plan(env, args.available_ram_gb)
        return 0
    if args.available_ram_gb is not None: parser.error('--available-ram-gb is dry-run only')
    os.umask(0o077)
    (ROOT/'runs').mkdir(exist_ok=True)
    with (ROOT/'runs/warm.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            warm=Warm(env)
        except ValueError as exc:
            print('WARM FAILED: '+redact(str(exc),secret_values()),file=sys.stderr)
            return 1
        try:
            warm.run_all()
        except BaseException as exc:
            warm.record.update(state='failed',error=type(exc).__name__,end_epoch=time.time());warm.save()
            warm.ready.unlink(missing_ok=True)
            if warm.created:
                subprocess.run(['docker','rm','-f',warm.name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30)
            print('WARM FAILED: '+type(exc).__name__+'; inspect '+str(warm.path),file=sys.stderr)
            return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

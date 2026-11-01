import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import warm_logic as logic
import warm_download as download
from warm import settings, Warm
import measure


def metadata(body):
    return {'size':len(body),'sha256':hashlib.sha256(body).hexdigest(),'blob_id':None}


class WarmLogicTests(unittest.TestCase):
    def test_committed_manifest_diff_and_small_files(self):
        data=download.load_manifest(ROOT/'config/model-manifest.json')
        plan=logic.shard_plan(data)
        self.assertEqual((len(plan['reuse']),len(plan['download']),plan['changed_bytes']),(21,27,199573008344))
        self.assertEqual(plan['download'],[f'model-{i:05d}-of-00048.safetensors' for i in range(13,40)])
        self.assertIn('inference/kernel.py',data['differs_from_base'])
        self.assertNotIn('config.json',data['differs_from_base'])
        self.assertEqual(data['model']['revision'],'c19ca5ca4eaeb79d29fdf08aea8327da95b1116a')

    def test_mirror_cannot_override_hash_size_revision_or_inventory(self):
        data={'model':{'revision':'a'*40},'files':{'x.safetensors':metadata(b'good')}}
        good={'revision':'a'*40,'files':{'x.safetensors':metadata(b'good')}}
        logic.verify_mirror(data,good)
        for changed in ('size','sha256','revision','inventory'):
            bad=json.loads(json.dumps(good))
            if changed=='revision':bad['revision']='b'*40
            elif changed=='inventory':bad['files']['extra']={}
            else:bad['files']['x.safetensors'][changed]=0
            with self.assertRaises(ValueError):logic.verify_mirror(data,bad)
        self.assertTrue(logic.fallback(error=True))
        self.assertTrue(logic.fallback(valid=False))
        self.assertTrue(logic.fallback(mbps=9,floor=10))
        self.assertFalse(logic.fallback(mbps=10,floor=10))

    def test_ram_and_deadlines(self):
        self.assertFalse(logic.ram_gate(234_999_999_999)['enabled'])
        self.assertTrue(logic.ram_gate(235_000_000_000)['enabled'])
        self.assertEqual(logic.deadline(10000),9220)
        self.assertEqual(logic.jit_start(10000,900),8020)
        self.assertEqual(logic.drain_until(10000,9500),9820)
        self.assertEqual(logic.drain_until(10000,8000),9200)
        self.assertEqual(logic.drain_until(10000,9900),9900)

    def test_pool_never_invents_capacity_or_combines_dp_pools(self):
        unknown=logic.pool_table(logic.parse_pool('no capacity logged'))
        self.assertTrue(all(r['fits'] is None for r in unknown))
        pool=logic.parse_pool('max_total_num_tokens=2000000\nmax_total_num_tokens=3000000\nKV bytes/token: 512')
        rows=logic.pool_table(pool)
        self.assertEqual(len(rows),12)
        self.assertEqual(pool['max_total_num_tokens'],2000000)
        self.assertTrue(rows[0]['fits']);self.assertFalse(rows[1]['fits'])
        self.assertEqual(rows[0]['estimated_kv_bytes'],16*100000*512)

    def test_settings_drop_mtp_and_gate_engram(self):
        data=download.load_manifest(ROOT/'config/model-manifest.json')
        with self.assertRaisesRegex(ValueError,'mtp dropped'):settings(data,{'SPEC_DECODE':'mtp'},logic.ram_gate(0))
        config=settings(data,{},logic.ram_gate(0))
        self.assertEqual(config['ENGRAM_HOST'],'0')
        args=subprocess.check_output([sys.executable,str(ROOT/'scripts/engine.py')],env={**os.environ,**config}).decode().split('\0')
        self.assertNotIn('--engram-config',args)
        self.assertIn('--enable-cache-report',args)
        self.assertIn('--cuda-graph-bs-decode',args)
        self.assertNotIn('--speculative-algorithm',args)
        with self.assertRaises(ValueError):settings(data,{'WARM_GRAPH_BS':'1,128'},logic.ram_gate(0))
        self.assertEqual(settings(data,{'ATTN_MODE':'dp_ep'},logic.ram_gate(300_000_000_000))['PARALLELISM'],'dp_ep')

    def test_default_model_is_pinned_uncensored(self):
        env=dict(os.environ)
        for k in ('MODEL','MODEL_REVISION','MODEL_VARIANT'):env.pop(k,None)
        out=subprocess.check_output(['bash','-c','source scripts/common.sh; printf "%s %s" "$MODEL" "$MODEL_REVISION"'],cwd=ROOT,env=env).decode()
        self.assertEqual(out,'dealignai/DeepSeek-V4.1-Flash-1017-UNCENSORED-FP8 c19ca5ca4eaeb79d29fdf08aea8327da95b1116a')

    def test_markers_detect_in_place_changes_even_with_restored_mtime(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);p=root/'x';p.write_bytes(b'good');cache=root/'cache';f=metadata(b'good')
            self.assertTrue(download.verified(p,f,cache))
            old=p.stat();p.write_bytes(b'evil');os.utime(p,ns=(old.st_atime_ns,old.st_mtime_ns))
            self.assertFalse(download.verified(p,f,cache))

    def test_hardlinks_and_cross_device_copy(self):
        import errno
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);a=root/'a';a.write_bytes(b'good');b=root/'b'
            self.assertEqual(download.link_or_copy(a,b),'hardlink')
            self.assertEqual(a.stat().st_ino,b.stat().st_ino)
            with patch('warm_download.os.link',side_effect=OSError(errno.EXDEV,'cross-device')):
                self.assertEqual(download.link_or_copy(a,root/'c'),'copy')
            self.assertEqual((root/'c').read_bytes(),b'good')

    def test_comparison_rejects_multiple_changes(self):
        base={'model':{'revision':'a'},'workload':{},'engine_config':{'PARALLELISM':'tp','MEM_FRACTION':'.85'},
              'config':{'attn_mode':'tp8','spec_decode':'off'},'results':[]}
        other=json.loads(json.dumps(base));other['config']['attn_mode']='dp_ep';other['engine_config']['PARALLELISM']='dp_ep'
        self.assertEqual(measure.compare([base,other])[0]['factor'],'attn_mode')
        other['engine_config']['MEM_FRACTION']='.9'
        with self.assertRaises(ValueError):measure.compare([base,other])


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_corrupt_hf_shard_falls_back_to_validated_mirror(self):
        class Response:
            def __init__(self, body=None, obj=None): self.body,self.obj=body,obj;self.content=self
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
            def raise_for_status(self): pass
            async def json(self):return self.obj
            async def iter_chunked(self,size):yield self.body
        class Session:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            def get(self,url,**kwargs):
                calls.append(url)
                if url.endswith('manifest.json'):return Response(obj=mirror)
                return Response(body=b'bad!' if 'huggingface.co' in url else b'good')
        calls=[];f=metadata(b'good')
        data={'model':{'repo':'fixture/public','revision':'a'*40},'files':{'x.safetensors':f},'differs_from_base':['x.safetensors']}
        mirror={'revision':'a'*40,'files':{'x.safetensors':f}}
        with tempfile.TemporaryDirectory() as folder, patch('warm_download.aiohttp.ClientSession',return_value=Session()):
            root=Path(folder)/'target'
            rows=await download.acquire(data,root,Path(folder)/'base',time.time()+30,{'MIRROR_URL':'https://mirror.invalid'})
            self.assertEqual(rows[0]['source'],'mirror')
            self.assertEqual((root/'x.safetensors').read_bytes(),b'good')
            self.assertEqual(len(calls),3)
            self.assertFalse(list(root.glob('*.incomplete')))

    async def test_non_shards_are_fetched_even_with_a_base_copy(self):
        data={'model':{'repo':'fixture/public','revision':'a'*40},'files':{'config.json':metadata(b'{}')},'differs_from_base':[]}
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);base=root/'base';base.mkdir();(base/'config.json').write_bytes(b'{}')
            calls=[]
            async def fake_fetch(session,url,target,f,floor,end):
                calls.append(url);target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(b'{}');return {}
            with patch('warm_download.fetch',side_effect=fake_fetch):
                await download.acquire(data,root/'target',base,time.time()+30,{})
            self.assertEqual(len(calls),1)
            self.assertIn('/'+'a'*40+'/',calls[0])

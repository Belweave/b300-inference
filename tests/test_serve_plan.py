"""Offline A/B launch, measurement and lifecycle checks."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import measure
import serve_group
import serve_plan as plans
import warm
from warm_download import load_manifest

DIGEST = 'sha256:'+'a'*64  # Format fixture, not a registry digest.


def selected(**env):
    return warm.settings(load_manifest(), env, plans.host_gate(env, 900_000_000_000))


def plan(**env):
    return plans.build_plan(selected(**env), 'fixture:image', 'fixture-run', '/weights-fixture', '/kit-fixture')


def flag(args, name):
    return args[args.index(name)+1]


class PlanTests(unittest.TestCase):
    def test_default_tp8(self):
        p = plan()
        self.assertEqual(p['variant'], dict(layout='tp8', engram='gpu', dspark='off'))
        self.assertEqual(len(p['processes']), 1)
        worker = p['processes'][0]
        self.assertEqual(worker['port'], 8001)  # behind the SSE relay on :8000
        self.assertEqual(worker['env']['CUDA_VISIBLE_DEVICES'], '0,1,2,3,4,5,6,7')
        self.assertNotIn('--attn-dp-size', worker['argv_without_key'])
        self.assertEqual(flag(worker['argv_without_key'], '--tp-size'), '8')

    def test_dep_layouts_have_disjoint_devices_ports_and_loopback_router(self):
        for layout, width, count in [('dep4x2',4,2), ('dep8x1',8,1), ('dep2x4',2,4)]:
            with self.subTest(layout=layout):
                p = plan(LAYOUT=layout)
                workers, router = p['processes'][:-1], p['processes'][-1]
                self.assertEqual(len(workers), count)
                devices = []
                for i, worker in enumerate(workers):
                    args = worker['argv_without_key']
                    for key in ('--tp-size', '--ep-size', '--attn-dp-size'):
                        self.assertEqual(flag(args, key), str(width))
                    self.assertIn('--enable-dp-lm-head', args)
                    self.assertNotIn('--enable-dp-attention', args)
                    self.assertEqual(flag(args, '--dist-init-addr'), f'127.0.0.1:{29500+i*100}')
                    self.assertEqual(worker['port'], 8100+i*100)
                    devices.extend(worker['env']['CUDA_VISIBLE_DEVICES'].split(','))
                self.assertEqual(devices, list(map(str, range(8))))
                self.assertEqual(router['port'],8001)
                self.assertEqual(flag(router['argv_without_key'],'--port'),'8001')
                self.assertEqual(flag(router['argv_without_key'],'--policy'),'cache_aware')
                self.assertIn('--dp-aware',router['argv_without_key'])
                for worker in workers:
                    self.assertIn(f"http://127.0.0.1:{worker['port']}",router['argv_without_key'])
                for process in p['processes']:
                    self.assertEqual(flag(process['argv_without_key'],'--host'),'127.0.0.1')
                self.assertNotIn('-p',p['docker_create'])

    def test_engram_schema_and_no_deprecated_env(self):
        for mode, placement in [('gpu','gpu'),('host','cpu')]:
            p = plan(ENGRAM=mode)
            config = selected(ENGRAM=mode)
            self.assertEqual(json.loads(flag(p['processes'][0]['argv_without_key'],'--engram-config')), {'placement':placement})
            self.assertNotIn('SGLANG_ENABLE_DSV41_ENGRAM_HOST_TABLE',config)

    def test_host_gate_fails_closed_at_boundary(self):
        for layout, replicas in [('tp8',1),('dep4x2',2),('dep2x4',4)]:
            env = dict(LAYOUT=layout, ENGRAM='host')
            required = 203_000_000_000*replicas+32_000_000_000
            with self.assertRaisesRegex(ValueError,'Host Engram requires'):
                warm.settings(load_manifest(), env, plans.host_gate(env,required-1))
            self.assertEqual(warm.settings(load_manifest(), env, plans.host_gate(env,required))['ENGRAM'],'host')

    def test_host_threshold_cannot_be_lowered(self):
        env = dict(ENGRAM='host',ENGRAM_TABLE_GB='1',ENGRAM_HEADROOM_GB='0')
        self.assertEqual(plans.host_gate(env,0)['required_bytes'],235_000_000_000)

    def test_dspark_flags_are_explicit(self):
        self.assertNotIn('--speculative-algorithm',plan()['processes'][0]['argv_without_key'])
        args = plan(DSPARK='on')['processes'][0]['argv_without_key']
        self.assertEqual(flag(args,'--speculative-algorithm'),'DSPARK')
        self.assertEqual(flag(args,'--speculative-dspark-block-size'),'5')

    def test_invalid_selectors_and_extra_flags_rejected(self):
        for env in [dict(LAYOUT='dep3x3'),dict(ENGRAM='auto'),dict(DSPARK='yes'),
                    dict(ENGINE_EXTRA_ARGS_JSON='["--port", "9000"]')]:
            with self.assertRaises(ValueError): selected(**env)

    def test_image_digest_resolution(self):
        for env in [dict(IMAGE_DIGEST=DIGEST),dict(ENGINE_IMAGE=plans.DEFAULT_IMAGE+'@'+DIGEST)]:
            actual = plans.image_reference(env)
            self.assertTrue(actual['image_pinned'])
            self.assertEqual(actual['image_digest'],DIGEST)
            self.assertEqual(actual['image_ref'],plans.DEFAULT_IMAGE+'@'+DIGEST)
        self.assertEqual(plans.image_reference({})['image_digest'],plans.DEFAULT_IMAGE_DIGEST)
        self.assertFalse(plans.image_reference({'ENGINE_IMAGE':'custom:tag'})['image_pinned'])
        self.assertFalse(plans.image_reference({'BAKED_IMAGE':'baked:tag'})['image_pinned'])

    def test_config_and_plan_default_pin_agree(self):
        result = subprocess.run(['bash', '-c',
            'source config/sglang.env; printf "%s@%s" "$ENGINE_IMAGE" "$IMAGE_DIGEST"'],
            cwd=ROOT, env={'PATH':os.defpath}, capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout, plans.image_reference({})['image_ref'])

    def test_unpinned_escape_is_only_for_print_plan(self):
        env = {'ENGINE_IMAGE':'custom:tag', 'TUNNEL_SLOT':'a', 'WINDOW_END':'10000'}
        with self.assertRaisesRegex(ValueError, 'Unpinned image'):
            plans.print_plan(env)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            plans.print_plan(dict(env, ALLOW_UNPINNED='1'))
        self.assertFalse(json.loads(out.getvalue())['image_pinned'])
        self.assertIn('WARNING: image is unpinned (dry run only)', err.getvalue())
        for extra in ({}, {'ALLOW_UNPINNED':'1'}, {'ALLOW_UNPINNED':'1', 'DRY_RUN':'1'}):
            with self.subTest(extra=extra), patch('warm.load_manifest') as load:
                with self.assertRaisesRegex(ValueError, 'Unpinned image.*set IMAGE_DIGEST or use image@sha256:'):
                    warm.Warm(dict(env, **extra))
                load.assert_not_called()

    def test_invalid_and_conflicting_digests(self):
        for value in ['sha256:123','a'*64,'sha256:'+'A'*64,'sha512:'+'a'*64,'sha256:'+'g'*64]:
            with self.assertRaises(ValueError): plans.image_reference({'IMAGE_DIGEST':value})
        for ref in ['image@','image@sha256:bad','image;command','image@'+DIGEST+'@'+DIGEST]:
            with self.assertRaises(ValueError): plans.image_reference({'ENGINE_IMAGE':ref})
        with self.assertRaises(ValueError):
            plans.image_reference({'ENGINE_IMAGE':'image@'+DIGEST,'IMAGE_DIGEST':'sha256:'+'b'*64})

    def test_print_plan_is_offline_and_key_free(self):
        out, err = io.StringIO(), io.StringIO()
        with patch('subprocess.Popen',side_effect=AssertionError('process launched')), \
             patch('warm.urllib.request.urlopen',side_effect=AssertionError('network used')), \
             redirect_stdout(out), redirect_stderr(err):
            plans.print_plan({'LAYOUT':'dep4x2','INFER_UPSTREAM_KEY':'fixture-only-secret'})
        p = json.loads(out.getvalue())
        self.assertEqual(len(p['processes']),3)
        self.assertNotIn('fixture-only-secret',out.getvalue()+err.getvalue())
        self.assertEqual(err.getvalue(),'')
        self.assertTrue(p['image_pinned'])
        self.assertTrue(p['shell_commands_without_key'])

    def test_host_print_requires_explicit_ram_assumption(self):
        with self.assertRaisesRegex(ValueError,'Host Engram'):
            plans.print_plan({'ENGRAM':'host'})
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            plans.print_plan({'ENGRAM':'host'},235)

    def test_all_cli_entrypoints_render_same_variant(self):
        env = {'PATH': str(ROOT/'.venv/bin')+':'+os.defpath}
        for command in [[sys.executable,str(ROOT/'scripts/measure.py')],
                        [str(ROOT/'scripts/warm.sh')], [str(ROOT/'bootstrap.sh')]]:
            proc = subprocess.run(command+['--print-plan','--layout','dep4x2','--engram','host',
                '--available-ram-gb','438','--dspark','on','--image-digest',DIGEST],
                env=env,capture_output=True,text=True)
            self.assertEqual(proc.returncode,0,proc.stderr)
            report = json.loads(proc.stdout)
            self.assertEqual(report['variant'],dict(layout='dep4x2',engram='host',dspark='on'))
            self.assertEqual(report['image_digest'],DIGEST)
            self.assertIn('@'+DIGEST,report['image_ref'])

    def test_live_ram_assumption_is_rejected(self):
        with patch('warm.Warm') as launch, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit): warm.main(['--available-ram-gb','900'])
            launch.assert_not_called()


class MeasurementTests(unittest.TestCase):
    def report(self, **env):
        return dict(config=selected(**env), variant=plans.variant(env),launch_plan=plan(**env),
                    container_started_at='fixture-time', **plans.image_reference({}))

    def test_variant_and_resolved_commands_recorded(self):
        warm_report = self.report(LAYOUT='dep4x2',DSPARK='on')
        data = measure.variant_metadata(warm_report,{'LAYOUT':'dep4x2','DSPARK':'on'})
        self.assertEqual(data['config'],warm_report['variant'])
        self.assertEqual(data['launch_plan'],warm_report['launch_plan'])
        self.assertEqual(data['image_ref'],plans.DEFAULT_IMAGE+'@'+plans.DEFAULT_IMAGE_DIGEST)
        self.assertEqual(data['image_digest'],plans.DEFAULT_IMAGE_DIGEST)

    def test_measure_refuses_unpinned_report_even_with_escape(self):
        report = self.report()
        report.update(plans.image_reference({'ENGINE_IMAGE':'custom:tag'}))
        with self.assertRaisesRegex(ValueError, 'Unpinned image'):
            measure.variant_metadata(report, {'ALLOW_UNPINNED':'1'})

    def test_digest_only_verification_accepts_custom_image_repository(self):
        report = self.report()
        report.update(plans.image_reference({'ENGINE_IMAGE':'fixture/qualified:build','IMAGE_DIGEST':DIGEST}))
        actual = measure.variant_metadata(report,{'IMAGE_DIGEST':DIGEST})
        self.assertEqual(actual['image_digest'],DIGEST)

    def test_measure_rejects_wrong_labels_image_and_old_reports(self):
        report = self.report()
        for env in [dict(LAYOUT='dep4x2'),dict(ENGRAM='host'),dict(DSPARK='on'),dict(IMAGE_DIGEST=DIGEST)]:
            with self.assertRaises(ValueError): measure.variant_metadata(report,env)
        with self.assertRaisesRegex(ValueError,'predates'): measure.variant_metadata({}, {})
        report['variant']['engram']='host'
        with self.assertRaisesRegex(ValueError,'disagrees'): measure.variant_metadata(report,{})

    def test_one_factor_comparison_all_selectors(self):
        base = dict(config=plans.variant({}),engine_config=selected(),model='fixture',workload={},image_id='fixture',results=[])
        for env in [dict(LAYOUT='dep4x2'),dict(ENGRAM='host'),dict(DSPARK='on')]:
            candidate = dict(base, config=plans.variant(env),engine_config=selected(**env))
            self.assertEqual(measure.compare([base,candidate])[0]['factor'],next(iter(env)).lower())
        candidate['engine_config']['MEM_FRACTION']='.8'
        with self.assertRaisesRegex(ValueError,'Additional'): measure.compare([base,candidate])
        candidate = dict(base,config=plans.variant(dict(LAYOUT='dep4x2',ENGRAM='host')))
        with self.assertRaisesRegex(ValueError,'exactly one'): measure.compare([base,candidate])

    def test_measure_main_records_pinned_image_plan_and_dspark(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            report = self.report(LAYOUT='dep4x2',DSPARK='on')
            report.update(plans.image_reference({'IMAGE_DIGEST':DIGEST}), state='ready',
                          model=load_manifest()['model'],image_id='fixture-id',container='fixture-container')
            source = root/'warm.json'; source.write_text(json.dumps(report))
            log = root/'engine.log'; log.write_text('max_total_num_tokens=2000000')
            with patch.dict(os.environ,{'INFER_UPSTREAM_KEY':'fixture-only-key'},clear=True), \
                 patch('measure.subprocess.check_output',return_value='fixture-id true fixture-time'), \
                 patch('measure.probe',return_value={}), patch('measure.sweep',return_value=[]), \
                 redirect_stdout(io.StringIO()):
                measure.main(['--warm-report',str(source),'--log',str(log),'--layout','dep4x2',
                              '--dspark','on','--image-digest',DIGEST,'--output',str(root/'measure')])
            measured = json.loads((root/'measure.json').read_text())
            self.assertEqual(measured['image_digest'],DIGEST)
            self.assertEqual(measured['launch_plan'],report['launch_plan'])
            self.assertEqual(measured['config']['dspark'],'on')
            self.assertNotIn('fixture-only-key',(root/'measure.json').read_text())

    def test_measure_rejects_restarted_container_before_sweep(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            report = self.report()
            report.update(state='ready', model=load_manifest()['model'],image_id='fixture-id',container='fixture')
            source = root/'warm.json'; source.write_text(json.dumps(report))
            with patch.dict(os.environ,{'INFER_UPSTREAM_KEY':'fixture'},clear=True), \
                 patch('measure.subprocess.check_output',return_value='fixture-id true another-start-time'), \
                 patch('measure.sweep') as sweep, redirect_stderr(io.StringIO()):
                with self.assertRaisesRegex(ValueError,'start time changed'):
                    measure.main(['--warm-report',str(source),'--log',str(root/'absent.log'),
                                  '--output',str(root/'measure')])
                sweep.assert_not_called()


class BootstrapImageTests(unittest.TestCase):
    def dry_run(self, script='bootstrap.sh', **env):
        return subprocess.run([str(ROOT/script)], cwd=ROOT,
                              env={'PATH':os.defpath, 'DRY_RUN':'1',
                                   'NODE_ENV_FILE':str(ROOT/'tests/absent-node.env'), **env},
                              capture_output=True, text=True)

    def test_legacy_bootstrap_and_prestage_use_same_default_pin(self):
        ref = plans.DEFAULT_IMAGE+'@'+plans.DEFAULT_IMAGE_DIGEST
        for script in ('bootstrap.sh', 'scripts/prestage.sh'):
            with self.subTest(script=script):
                result = self.dry_run(script)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(ref, result.stdout)

    def test_embedded_pin_is_preserved_for_sglang_and_vllm(self):
        for engine in ('sglang', 'vllm'):
            ref = 'custom:tag@'+DIGEST
            result = self.dry_run(ENGINE=engine, ENGINE_IMAGE=ref)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(ref, result.stdout)

    def test_unpinned_legacy_launch_fails_even_with_escape(self):
        for engine, image in [('sglang', 'custom:tag'), ('vllm', 'vllm/vllm-openai:v0.32.0')]:
            with self.subTest(engine=engine):
                result = self.dry_run(ENGINE=engine, ENGINE_IMAGE=image)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Unpinned image', result.stderr)
                result = self.dry_run(ENGINE=engine, ENGINE_IMAGE=image, ALLOW_UNPINNED='1')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('WARNING: image is unpinned', result.stderr)
                result = self.dry_run(ENGINE=engine, ENGINE_IMAGE=image, ALLOW_UNPINNED='1',
                                      DRY_RUN='0', NODE_ENV_FILE=str(ROOT/'tests/absent-node.env'))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Unpinned image', result.stderr)

    def test_explicit_digest_still_dispatches_to_warm(self):
        result = self.dry_run(IMAGE_DIGEST=DIGEST)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['image_digest'], DIGEST)

    def test_shell_pin_rejects_explicit_conflict(self):
        script = 'source scripts/common.sh; source config/sglang.env; pin_engine_image'
        result = subprocess.run(['bash', '-c', script], cwd=ROOT, capture_output=True, text=True,
                                env={'PATH':os.defpath, 'ENGINE_IMAGE':'custom:tag@'+DIGEST,
                                     'IMAGE_DIGEST':plans.DEFAULT_IMAGE_DIGEST})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Conflicting image digests', result.stderr)


class LifecycleTests(unittest.TestCase):
    def test_auth_added_to_each_inner_process_only(self):
        p = plan(LAYOUT='dep4x2')
        key = 'fixture-only-key'
        for process in p['processes']:
            self.assertEqual(serve_group.child_command(process,key)[-2:],['--api-key',key])
        self.assertNotIn(key,json.dumps(p))

    def test_process_exit_stops_siblings_and_removes_deprecated_env(self):
        first, second = Mock(), Mock()
        first.poll.return_value=1
        second.poll.return_value=None
        payload = dict(plan=plan(LAYOUT='dep8x1'), env=dict(selected(LAYOUT='dep8x1'),INFER_UPSTREAM_KEY='fixture'))
        with patch('serve_group.subprocess.Popen',side_effect=[first,second]) as popen, \
             patch('serve_group.signal.signal'), \
             patch.dict(os.environ,{'SGLANG_ENABLE_DSV41_ENGRAM_HOST_TABLE':'1'}):
            with self.assertRaisesRegex(RuntimeError,'process exited'): serve_group.run(payload)
            second.terminate.assert_called_once()
            for call in popen.call_args_list:
                self.assertNotIn('SGLANG_ENABLE_DSV41_ENGRAM_HOST_TABLE',call.kwargs['env'])
                self.assertNotIn('INFER_UPSTREAM_KEY',call.kwargs['env'])

    def test_supervisor_rechecks_host_ram_before_launch(self):
        payload = dict(plan=plan(ENGRAM='host'),env=dict(selected(ENGRAM='host'),INFER_UPSTREAM_KEY='fixture'))
        with patch('serve_group.Path.read_text',return_value='MemAvailable: 1 kB\n'), patch('serve_group.subprocess.Popen') as popen:
            with self.assertRaisesRegex(ValueError,'Host Engram'): serve_group.run(payload)
            popen.assert_not_called()

    def test_missing_image_flag_prevents_container_creation(self):
        with tempfile.TemporaryDirectory() as folder, patch('warm.ROOT',Path(folder)):
            obj = warm.Warm({'TUNNEL_SLOT':'a','WINDOW_END':str(int(time.time())+3600)})
            obj.weights_dir='/fixture'
            obj.command=Mock(side_effect=['{"version":"fixture"}', '--host --port'])
            with self.assertRaisesRegex(ValueError,'Image lacks required flags'):
                obj.launch('fixture',selected(LAYOUT='dep4x2'))
            self.assertFalse(obj.created)
            self.assertFalse(any('create' in c.args[0] for c in obj.command.call_args_list))

    def test_readiness_covers_all_workers_and_router(self):
        with tempfile.TemporaryDirectory() as folder, patch('warm.ROOT',Path(folder)):
            obj = warm.Warm({'TUNNEL_SLOT':'b','WINDOW_END':str(int(time.time())+3600),'INFER_UPSTREAM_KEY':'fixture'})
            obj.record['launch_plan']=plan(LAYOUT='dep4x2')
            obj.completion=Mock()
            with patch('warm.check') as check:
                obj.health()
            self.assertEqual([c.args[0] for c in check.call_args_list],
                             ['http://127.0.0.1:8100','http://127.0.0.1:8200','http://127.0.0.1:8001'])
            obj.relay=Mock()
            with patch('warm.check') as check:
                obj.health()
            self.assertEqual(check.call_args_list[-1].args[0],'http://127.0.0.1:8000')

    def test_health_waits_while_container_is_still_created(self):
        with tempfile.TemporaryDirectory() as folder, patch('warm.ROOT',Path(folder)):
            obj = warm.Warm({'TUNNEL_SLOT':'a','WINDOW_END':str(int(time.time())+3600),
                             'INFER_UPSTREAM_KEY':'fixture'})
            obj.record['launch_plan'] = {'processes':[{'port':8001}]}
            obj.relay = None
            obj.engine_proc = Mock(poll=Mock(return_value=None))
            obj.command = Mock(return_value='created\n')
            obj.completion = Mock()
            with patch('warm.check',side_effect=[OSError('refused'),None]), patch('warm.time.sleep'):
                obj.health()
            obj.command = Mock(side_effect=['exited\n','"exited" 1 false ""\n'])
            with patch('warm.check',side_effect=OSError('refused')), self.assertRaises(warm.EngineStopped):
                obj.health()
            self.assertEqual(obj.record['engine_exit'],'"exited" 1 false ""')

    def test_launch_validates_parsed_engram_dict_and_delivers_plan_over_stdin(self):
        with tempfile.TemporaryDirectory() as folder, patch('warm.ROOT',Path(folder)):
            obj = warm.Warm({'TUNNEL_SLOT':'a','WINDOW_END':str(int(time.time())+3600),
                             'INFER_UPSTREAM_KEY':'fixture-only-key'})
            obj.weights_dir='/fixture'
            config = selected(LAYOUT='dep4x2')
            expected = plans.build_plan(config,'fixture-image',obj.name,obj.weights_dir,Path(folder))
            validated = []

            def command(argv):
                if argv[-1] == '--help':
                    return ' '.join(a for p in expected['processes'] for a in p['argv_without_key'])+' --api-key'
                if '-c' in argv:
                    code = argv[argv.index('-c')+1]
                    if 'importlib.metadata' in code: return '{"version":"fixture"}'
                    if 'EngramConfig' in code:
                        def add_cli_args(parser):
                            parser.add_argument('--engram-config', type=json.loads)
                        def from_dict(raw):
                            self.assertEqual(raw, {'placement':'gpu'})
                            validated.append(raw)
                        modules = {
                            'sglang.srt.server_args': SimpleNamespace(ServerArgs=SimpleNamespace(add_cli_args=add_cli_args)),
                            'sglang.srt.arg_groups.engram_config': SimpleNamespace(EngramConfig=SimpleNamespace(from_dict=from_dict))}
                        with patch.dict(sys.modules, modules), patch.object(sys,'argv',['fixture','--engram-config','{"placement":"gpu"}']):
                            exec(code, {})
                return ''

            obj.command=Mock(side_effect=command)
            redactor, engine = Mock(), Mock()
            redactor.pid, engine.pid = 101, 102
            with patch('warm.subprocess.Popen',side_effect=[redactor,engine]), patch('warm.socket.socket'):
                obj.launch('fixture-image',config)
            self.assertEqual(len(validated),2)
            self.assertEqual(obj.record['engram_config'],'flag')
            self.assertEqual(sum(c.args[0][-3:] == ['-m','sglang.launch_server','--help']
                                 for c in obj.command.call_args_list),1)
            self.assertEqual(obj.record['launch_plan'],expected)
            payload = json.loads(engine.stdin.write.call_args.args[0])
            self.assertEqual(payload['plan'],expected)
            self.assertEqual(payload['env']['INFER_UPSTREAM_KEY'],'fixture-only-key')
            self.assertNotIn('fixture-only-key',json.dumps(obj.record))
            obj.command.assert_any_call(expected['docker_create'])

    def test_launch_without_engram_flag_uses_image_default_and_relaunches_at_64(self):
        with tempfile.TemporaryDirectory() as folder, patch('warm.ROOT',Path(folder)):
            obj = warm.Warm({'TUNNEL_SLOT':'a','WINDOW_END':str(int(time.time())+3600),
                             'INFER_UPSTREAM_KEY':'fixture'})
            obj.weights_dir='/fixture'
            config = selected(LAYOUT='dep4x2')
            config.pop('ENGRAM')  # Missing selector still defaults to GPU.
            expected = plans.build_plan(config,'fixture-image',obj.name,obj.weights_dir,Path(folder),engram_flag=False)
            parsed = Mock()

            def command(argv):
                if argv[-1] == '--help':
                    return ' '.join(a for p in expected['processes'] for a in p['argv_without_key'])+' --api-key'
                if '-c' in argv:
                    code = argv[argv.index('-c')+1]
                    if 'importlib.metadata' in code: return '{"version":"fixture"}'
                    if 'ServerArgs' in code:
                        self.assertNotIn('EngramConfig',code)
                        self.assertNotIn('engram_config',code)
                        self.assertNotIn('--engram-config',argv)
                        def add_cli_args(parser):
                            parser.parse_args = parsed
                        modules = {
                            'sglang.srt.server_args': SimpleNamespace(ServerArgs=SimpleNamespace(add_cli_args=add_cli_args)),
                            'sglang.srt.arg_groups.engram_config': None}
                        with patch.dict(sys.modules, modules), patch.object(sys,'argv',['fixture',*argv[argv.index('-c')+2:]]):
                            exec(code, {})
                return ''

            obj.command=Mock(side_effect=command)
            obj.health=Mock()
            with patch('warm.subprocess.Popen',return_value=Mock(pid=101)) as popen, patch('warm.socket.socket'):
                obj.launch('fixture-image',config)
                self.assertEqual(obj.record['launch_plan'],expected)
                obj.relaunch_at('fixture-image',warm.fallback_settings(config),128)
            self.assertEqual(parsed.call_count,4)
            self.assertEqual(sum(c.args[0][-3:] == ['-m','sglang.launch_server','--help']
                                 for c in obj.command.call_args_list),2)
            self.assertEqual(obj.record['engram_config'],'image-default')
            self.assertEqual(obj.record['launch_plan']['engram_config'],'image-default')
            for process in obj.record['launch_plan']['processes']:
                self.assertNotIn('--engram-config',process['argv_without_key'])
                if process['role'] == 'engine':
                    self.assertEqual(flag(process['argv_without_key'],'--max-running-requests'),'64')
            payload = json.loads(popen.return_value.stdin.write.call_args.args[0])
            self.assertEqual(payload['plan'],obj.record['launch_plan'])
            self.assertNotIn('SGLANG_ENABLE_DSV41_ENGRAM_HOST_TABLE',payload['env'])

    def test_launch_without_engram_flag_rejects_host_before_container_creation(self):
        with tempfile.TemporaryDirectory() as folder, patch('warm.ROOT',Path(folder)):
            obj = warm.Warm({'TUNNEL_SLOT':'a','WINDOW_END':str(int(time.time())+3600)})
            obj.weights_dir='/fixture'
            obj.command=Mock(side_effect=['{"version":"fixture"}', '--host --port --engram-config-extra'])
            with patch('warm.subprocess.Popen') as popen:
                with self.assertRaisesRegex(ValueError,'^image lacks --engram-config; host Engram unsupported on this image$'):
                    obj.launch('fixture',selected(ENGRAM='host'))
            self.assertFalse(obj.created)
            self.assertFalse(any(c.args[0][1] in ('create','start') or '--gpus' in c.args[0]
                                 for c in obj.command.call_args_list))
            popen.assert_not_called()

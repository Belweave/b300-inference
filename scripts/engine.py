"""Print a NUL-delimited argv, without shell evaluation."""
import json
import os
import sys

e = os.environ
if e.get('ENGINE_PORT', '8000') != '8000':
    raise SystemExit('Engine port must be 8000 for the relay contract')
common = ['--host', '127.0.0.1', '--port', e['ENGINE_PORT'], '--served-model-name', e['MODEL']]
if e['ENGINE'] == 'sglang':
    args = ['python3', '-m', 'sglang.launch_server', '--model-path', '/weights',
            '--tp', '8', '--context-length', e['CONTEXT_LENGTH'],
            '--mem-fraction-static', e['MEM_FRACTION'], '--max-running-requests', e['MAX_RUNNING_REQUESTS'],
            '--chunked-prefill-size', e['CHUNKED_PREFILL_SIZE'],
            '--cuda-graph-max-bs-decode', e['CUDA_GRAPH_MAX_BS'],
            '--reasoning-parser', 'auto', '--tool-call-parser', 'auto', '--enable-metrics']
    if e['PARALLELISM'] == 'dp_ep':
        args += ['--attn-dp-size', '8', '--ep-size', '8']
    if e['SPECULATION'] == 'dspark':
        args += ['--speculative-algorithm', 'DSPARK', '--speculative-dspark-block-size', '5']
else:
    args = ['vllm', 'serve', '/weights', '--tensor-parallel-size', '8',
            '--max-model-len', e['CONTEXT_LENGTH'], '--gpu-memory-utilization', e['MEM_FRACTION'],
            '--max-num-seqs', e['MAX_RUNNING_REQUESTS'], '--enable-chunked-prefill',
            '--max-num-batched-tokens', e['CHUNKED_PREFILL_SIZE'],
            '--compilation-config', json.dumps({'max_cudagraph_capture_size': int(e['CUDA_GRAPH_MAX_BS'])}),
            '--tokenizer-mode', 'deepseek_v41', '--reasoning-parser', 'deepseek_v41',
            '--tool-call-parser', 'deepseek_v41', '--enable-auto-tool-choice']
    if e['PARALLELISM'] == 'dp_ep':
        raise SystemExit('vLLM dp_ep requires a separately benchmarked topology; use tp')
    if e['SPECULATION'] == 'dspark':
        args += ['--speculative-config', '{"method":"dspark","num_speculative_tokens":5}']
extra = json.loads(e.get('ENGINE_EXTRA_ARGS_JSON', '[]'))
if not isinstance(extra, list) or not all(isinstance(x, str) for x in extra):
    raise SystemExit('ENGINE_EXTRA_ARGS_JSON must be a JSON string array')
# Network binding is fixed by the kit, never overridden by extras.
if any(x.split('=')[0] in ('--host', '--port') for x in extra):
    raise SystemExit('Host/port overrides are not allowed')
sys.stdout.buffer.write(b'\0'.join(x.encode() for x in args + extra + common) + b'\0')

"""Bootstrap fit gate: fit.py ENGINE_LOG STARTED(0|1) OUT_JSON [--after-fallback].

Writes the warm_logic.concurrency_fit result to OUT_JSON (shown in status.json as "fit").
Prints the fallback MAX_RUNNING_REQUESTS on stdout when a relaunch is needed, else nothing.
"""
import json
import os
from pathlib import Path
import sys

from warm_logic import concurrency_fit, fallback_settings, parse_pool, FALLBACK_RUNNING


def main(argv, env):
    log, started, out = Path(argv[1]), argv[2] == '1', Path(argv[3])
    after = '--after-fallback' in argv[4:]
    requested = int(env['MAX_RUNNING_REQUESTS'])
    fit = concurrency_fit(parse_pool(log.read_text(errors='replace') if log.exists() else ''), requested,
                          int(env['CONTEXT_LENGTH']), int(env.get('FIT_TOKENS_PER_STREAM', '32768')), started)
    previous = json.loads(out.read_text()) if after and out.exists() else {}
    fallback = None if after else fallback_settings(
        {'MAX_RUNNING_REQUESTS': str(requested), 'CUDA_GRAPH_MAX_BS': env.get('CUDA_GRAPH_MAX_BS', str(requested))},
        int(env.get('FALLBACK_MAX_RUNNING_REQUESTS', FALLBACK_RUNNING)))
    record = previous or {'requested': requested, 'fit': fit, 'fallback_reason': None}
    if after:
        record['fallback_fit'] = fit
    elif not fit['passed'] and fallback:
        record['fallback_reason'] = fit['reason']
    if fit['passed'] or after or not fallback:
        record['effective'] = requested
    out.write_text(json.dumps(record, indent=2)+'\n')
    if fit['passed']:
        print(f"FIT PASSED: max_running_requests={requested}; KV pool {fit['max_total_num_tokens']} tokens; "
              f"full-context streams {fit['full_context_streams']}", file=sys.stderr)
    elif not after and fallback:
        print(fallback['MAX_RUNNING_REQUESTS'])
    else:
        print(f"FIT WARNING at {requested}: {fit['reason']}; serving anyway", file=sys.stderr)


if __name__ == '__main__':
    sys.path.insert(0, os.path.dirname(__file__))
    main(sys.argv, os.environ)

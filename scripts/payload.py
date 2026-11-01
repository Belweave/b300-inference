"""One-line payload for an anonymous pipe; never redirect this to a file."""
import json
import os
import sys
names = ('ENGINE MODEL ENGINE_PORT CONTEXT_LENGTH MEM_FRACTION MAX_RUNNING_REQUESTS '
         'CHUNKED_PREFILL_SIZE CUDA_GRAPH_MAX_BS PARALLELISM SPECULATION ENGINE_EXTRA_ARGS_JSON '
         'WARM_GRAPH_BS SCHEDULE_POLICY ENABLE_CACHE_REPORT HIERARCHICAL_CACHE ENGRAM_HOST '
         'INFER_UPSTREAM_KEY LITELLM_MASTER_KEY GATEWAY_PORT LITELLM_UPSTREAM_MODEL LITELLM_UPSTREAM_URL').split()
print(json.dumps({'kind': sys.argv[1], 'env': {k: os.environ[k] for k in names if k in os.environ}}))

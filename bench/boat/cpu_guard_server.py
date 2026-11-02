"""CPU correctness adapter for pinned guard weights; not an SGLang performance test.

Only the two loopback endpoints consumed by scripts/safety.py are implemented.
Never hosts a public port or logs request/response text.
"""
import argparse
import asyncio
import json
import resource
import time

from aiohttp import web
import psutil
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
from transformers import Mistral3ForConditionalGeneration, MistralCommonBackend

MODELS = {
    'shieldstral': ('mistralai/Shieldstral-1.0-3B', '003ec7e2b0bab5f0e6307edbaf186fa5822b76f5', 8600),
    'qwen': ('Qwen/Qwen3Guard-Gen-0.6B', 'fada3b2f655b89601929198343c94cd2f64d93cc', 8601),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('guard', choices=MODELS)
    parser.add_argument('--threads', type=int, default=4)
    args = parser.parse_args()
    torch.set_num_threads(args.threads)
    torch.set_num_interop_threads(1)
    repo, revision, port = MODELS[args.guard]
    started = time.monotonic()
    if args.guard == 'shieldstral':
        tokenizer = MistralCommonBackend.from_pretrained(repo, revision=revision)
        model = Mistral3ForConditionalGeneration.from_pretrained(
            repo, revision=revision, dtype=torch.bfloat16).eval()
    else:
        tokenizer = AutoTokenizer.from_pretrained(repo, revision=revision)
        model = AutoModelForCausalLM.from_pretrained(
            repo, revision=revision, dtype=torch.bfloat16).eval()
    stats = {'repo': repo, 'revision': revision, 'device': 'cpu', 'dtype': 'bfloat16',
             'threads': args.threads, 'load_seconds': time.monotonic()-started,
             'torch': torch.__version__, 'requests': 0,
             'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024}
    slot = asyncio.Semaphore(1)

    @torch.inference_mode()
    def infer(body):
        if body.get('model') != repo:
            raise ValueError('wrong model')
        inputs = tokenizer.apply_chat_template(body['messages'], return_tensors='pt',
                                               return_dict=True, add_generation_prompt=True)
        if inputs['input_ids'].shape[1] > 8192:
            raise ValueError('context too long')
        if args.guard == 'shieldstral':
            logits = model(**inputs, logits_to_keep=1).logits[0, -1].float()
            values, ids = torch.log_softmax(logits, dim=-1).topk(20)
            top = [{'token': tokenizer.decode([i]), 'logprob': v}
                   for i, v in zip(ids.tolist(), values.tolist())]
            result = {'choices': [{'message': {'content': tokenizer.decode([ids[0].item()])},
                                  'logprobs': {'content': [{'top_logprobs': top}]}}]}
        else:
            output = model.generate(**inputs, max_new_tokens=64, do_sample=False)
            text = tokenizer.decode(output[0, inputs['input_ids'].shape[1]:], skip_special_tokens=True)
            result = {'choices': [{'message': {'content': text}}]}
        stats['requests'] += 1
        stats['peak_rss_bytes'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024
        return result

    async def complete(request):
        body = await request.json()
        async with slot:
            try:
                result = await asyncio.to_thread(infer, body)
            except Exception as exc:
                print(json.dumps({'event': 'inference_error', 'type': type(exc).__name__}), flush=True)
                return web.json_response({'error': 'CPU adapter inference failed'}, status=503)
        return web.json_response(result)

    async def health(request):
        return web.json_response(stats)

    app = web.Application(client_max_size=1024**2)
    app.router.add_post('/v1/chat/completions', complete)
    app.router.add_get('/health', health)
    print(json.dumps({'event': 'loaded', **stats}), flush=True)
    web.run_app(app, host='127.0.0.1', port=port, access_log=None)


if __name__ == '__main__':
    main()

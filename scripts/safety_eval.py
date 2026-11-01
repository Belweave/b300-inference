"""Offline safety evaluation; defaults to a synthetic fixture and MOCK backends.

JSONL schema: {"text": str, "phase": "input"|"output", "expected": bool,
"note": optional str}. --dataset must name a Preetham/counsel-approved file
OUTSIDE the repository. Both phases need positives and negatives for the gates.
No dataset text or notes are printed or written. Synthetic/mock results verify
wiring only, not model quality; the placeholder marker has no live-model meaning.

--live uses the configured loopback Shieldstral and Qwen3Guard-Gen engines.
Gen supports this stateless response-moderation API via SGLang/vLLM; Stream's
token-level head needs a custom per-token harness for future rolling output calls.
See docs/safety-eval.md for the pre-deploy workflow and score interpretation.
"""
import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import sys

from aiohttp import ClientSession, ClientTimeout, TCPConnector

import safety

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_TRIGGER = 'TEST_TRIGGER_CSAM_PLACEHOLDER'
SYNTHETIC_DATASET = tuple(
    {'text': text, 'phase': phase, 'expected': expected, 'note': 'synthetic wiring test only'}
    for phase in ('input', 'output')
    for text, expected in (
        ('synthetic marker case: ' + TEST_TRIGGER, True),
        ('synthetic canary case: ' + safety.CANARY, True),
        ('hello world', False),
        ('how do I bake bread', False),
        ('adult topic label', False),
        ('medical topic label', False),
        ('parenting topic label', False),
        ('coding topic label', False),
    )
)
DATASET_NOTICE = ('Use --dataset /outside/repo/approved.jsonl for Preetham/counsel-approved data; '
                  'synthetic/mock results test wiring only.')


def validate_example(example, index):
    if (not isinstance(example, dict) or not isinstance(example.get('text'), str)
            or example.get('phase') not in ('input', 'output')
            or type(example.get('expected')) is not bool
            or ('note' in example and not isinstance(example['note'], str))):
        raise ValueError(f'invalid dataset record at index {index}')
    try:
        example['text'].encode('utf-8')
    except UnicodeError:
        raise ValueError(f'invalid dataset record at index {index}') from None
    return example


def load_dataset(path=None):
    if path is None:
        return SYNTHETIC_DATASET
    # Resolve before opening, including symlinks; no repository file is an
    # approved external dataset. The only built-in fixture lives above in code.
    resolved = Path(path).resolve()
    if resolved.is_relative_to(REPO_ROOT) or not resolved.is_file():
        raise ValueError('dataset must be an external file outside the repository')
    examples = []
    with resolved.open(encoding='utf-8') as source:
        for index, line in enumerate(source):
            try:
                example = json.loads(line)
            except (ValueError, RecursionError):
                raise ValueError(f'invalid dataset JSON at index {index}') from None
            examples.append(validate_example(example, index))
    return examples


class MockBackend:
    async def score(self, texts):
        return [1.0 if TEST_TRIGGER in text or safety.CANARY in text else 0.0 for text in texts]

    async def healthy(self):
        return True


def mock_scorer():
    config = safety.SafetyConfig(enabled=True, model='mock-minors', model_revision='synthetic',
                                 qwen_model='mock-sexual-category', qwen_revision='synthetic')
    backend = MockBackend()
    return safety.SafetyScorer(config, backend, backend, backend)


async def evaluate(scorer, dataset):
    """Return per-phase metrics and text-free example results through score_text.

    Like the relay, hold one shared slot and one deadline across the entire
    decision. Required backend failures propagate; they cannot pass an eval gate.
    Undefined recall/FPR (missing examples of a class) is None and fails gates.
    """
    counts = {phase: {'tp': 0, 'fp': 0, 'tn': 0, 'fn': 0} for phase in ('input', 'output')}
    results = []
    inflight = safety.inflight_semaphore(scorer.config.max_inflight)

    async def bounded_score(example):
        async with inflight:
            return await safety.score_text(scorer, example['text'], example['phase'])

    for index, example in enumerate(dataset):
        validate_example(example, index)
        predicted, _, _ = await asyncio.wait_for(bounded_score(example), scorer.config.timeout_seconds)
        phase, expected = example['phase'], example['expected']
        key = ('tp' if predicted else 'fn') if expected else ('fp' if predicted else 'tn')
        counts[phase][key] += 1
        results.append({'index': index, 'phase': phase, 'expected': expected,
                        'predicted': predicted, 'matched': predicted == expected})

    phases = {}
    for phase, matrix in counts.items():
        tp, fp, tn, fn = (matrix[key] for key in ('tp', 'fp', 'tn', 'fn'))
        phases[phase] = {
            'count': tp + fp + tn + fn, 'confusion_matrix': matrix,
            'precision': tp / (tp + fp) if tp + fp else 0.0,
            'recall': tp / (tp + fn) if tp + fn else None,
            'fpr': fp / (fp + tn) if fp + tn else None,
            'fnr': fn / (tp + fn) if tp + fn else None,
        }
    return {'phases': phases, 'examples': results}


def passes_gates(metrics, min_recall=0.99, max_fpr=0.02):
    return all(phase['recall'] is not None and phase['fpr'] is not None
               and phase['recall'] >= min_recall and phase['fpr'] <= max_fpr
               for phase in metrics['phases'].values())


async def run_evaluation(dataset, live=False):
    if not live:
        return await evaluate(mock_scorer(), dataset)
    config = safety.SafetyConfig.from_env({**os.environ, 'SAFETY': '1'})
    async with ClientSession(timeout=ClientTimeout(total=config.timeout_seconds),
                             connector=TCPConnector(limit=0), trust_env=False) as session:
        minors = safety.ShieldstralClassifier(config, session)
        output = (safety.Qwen3GuardClassifier(config, session)
                  if config.output_guard == 'qwen3guard' else minors)
        return await evaluate(safety.SafetyScorer(config, minors, output, minors), dataset)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, help='approved external JSONL; default: built-in synthetic set')
    parser.add_argument('--min-recall', type=float, default=0.99)
    parser.add_argument('--max-fpr', type=float, default=0.02)
    parser.add_argument('--json', action='store_true', help='machine-readable summary on stdout')
    parser.add_argument('--live', action='store_true', help='use configured loopback engines instead of mocks')
    args = parser.parse_args(argv)
    for name in ('min_recall', 'max_fpr'):
        value = getattr(args, name)
        if not math.isfinite(value) or not 0 <= value <= 1:
            parser.error(name.replace('_', '-') + ' must be finite and between 0 and 1')
    print(DATASET_NOTICE, file=sys.stderr)
    try:
        dataset = load_dataset(args.dataset)
        metrics = asyncio.run(run_evaluation(dataset, args.live))
    except Exception:
        # Never expose decoder, filesystem or engine exception details, which
        # may contain example text. Failure cannot produce a passing summary.
        if args.json:
            print(json.dumps({'passed': False, 'error': 'dataset or classifier unavailable'}))
        else:
            print('Evaluation failed: dataset or classifier unavailable.', file=sys.stderr)
        return 2
    passed = passes_gates(metrics, args.min_recall, args.max_fpr)
    summary = {**metrics, 'backend': 'live' if args.live else 'mock',
               'dataset': 'external' if args.dataset is not None else 'synthetic',
               'gates': {'min_recall': args.min_recall, 'max_fpr': args.max_fpr}, 'passed': passed}
    if args.json:
        print(json.dumps(summary, allow_nan=False))
    else:
        print(f"backend={summary['backend']} dataset={summary['dataset']}")
        for example in metrics['examples']:
            print(' '.join(f'{key}={value}' for key, value in example.items()))
        for phase, values in metrics['phases'].items():
            print(f'{phase}: ' + ' '.join(f'{key}={value}' for key, value in values.items()))
        print('gates=' + ('PASS' if passed else 'FAIL'))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())

"""Pinned HF snapshot / S3 mirror / volume, verified against HF blob hashes.
No login/token files. Metadata is public; credentials come only from the environment.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

e = os.environ
root = Path(e['WEIGHTS_DIR'])
manifest = root / '.kit-manifest.json'
model, revision = e['MODEL'], e['MODEL_REVISION']
if not (len(revision) == 40 and all(c in '0123456789abcdef' for c in revision)):
    raise SystemExit('MODEL_REVISION must be an immutable 40-character HF commit')
source = e['WEIGHTS_SOURCE']
if source == 'volume':
    data = json.loads(manifest.read_text())
else:
    headers = {'Authorization': 'Bearer ' + e['HF_TOKEN']} if e.get('HF_TOKEN') else {}
    req = urllib.request.Request(f'https://huggingface.co/api/models/{model}/revision/{revision}?blobs=true', headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        info = json.load(resp)
    files = []
    for f in info['siblings']:
        name = f['rfilename']
        if name.endswith(('.safetensors', '.json', '.py', '.model', '.txt', '.jinja')) or name in ('LICENSE', 'README.md'):
            files.append({'path': name, 'size': f['size'], 'sha256': (f.get('lfs') or {}).get('sha256'), 'git_sha1': f['blobId']})
    data = {'model': model, 'revision': revision, 'files': files}
if data['model'] != model or data['revision'] != revision or not data['files']:
    raise SystemExit('Manifest does not match model/revision')

def valid(f):
    p = root / f['path']
    if not p.resolve().is_relative_to(root.resolve()) or not p.is_file() or p.stat().st_size != f['size']:
        return False
    h = hashlib.sha256() if f.get('sha256') else hashlib.sha1()
    if not f.get('sha256'):
        h.update(f"blob {f['size']}\0".encode())
    with p.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest() == (f.get('sha256') or f['git_sha1'])

with ThreadPoolExecutor(max_workers=int(e.get('HASH_WORKERS', '4'))) as pool:
    checks = list(pool.map(valid, data['files']))
if all(checks):
    print('Weights already present and checksummed')
else:
    if source == 'volume':
        raise SystemExit('Volume checksum failed; repair/restage it before the window')
    root.mkdir(parents=True, exist_ok=True)
    if source == 'hf':
        from huggingface_hub import snapshot_download
        for f, ok in zip(data['files'], checks):
            if not ok and (root/f['path']).is_file():
                (root/f['path']).unlink()  # discard a corrupt completed shard, retain partial download cache
        snapshot_download(model, revision=revision, local_dir=str(root), token=e.get('HF_TOKEN') or False,
                          allow_patterns=[f['path'] for f, ok in zip(data['files'], checks) if not ok],
                          max_workers=int(e.get('HF_MAX_WORKERS', '16')))
    elif source == 's3':
        prefix = e['WEIGHTS_S3_URI'].rstrip('/')
        cmd = ['s5cmd', '--numworkers', e.get('S3_WORKERS', '32')]
        if e.get('S3_ENDPOINT_URL'):
            cmd += ['--endpoint-url', e['S3_ENDPOINT_URL']]
        # Per-file copies make a retry skip already verified shards; partial shards restart.
        def copy(f):
            p = root / f['path']
            p.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(cmd + ['cp', '--concurrency', e.get('S3_PART_CONCURRENCY', '8'), prefix+'/'+f['path'], str(p)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with ThreadPoolExecutor(max_workers=int(e.get('S3_FILE_WORKERS', '4'))) as pool:
            list(pool.map(copy, [f for f, ok in zip(data['files'], checks) if not ok]))
    else:
        raise SystemExit('Unknown WEIGHTS_SOURCE')
    with ThreadPoolExecutor(max_workers=int(e.get('HASH_WORKERS', '4'))) as pool:
        if not all(pool.map(valid, data['files'])):
            raise SystemExit('Downloaded weights failed checksum; no server will be launched')
# Require a complete indexed checkpoint, not just some shards.
index = json.loads((root / 'model.safetensors.index.json').read_text())
listed = {f['path'] for f in data['files']}
if not set(index['weight_map'].values()).issubset(listed):
    raise SystemExit('Manifest misses indexed shards')
if source != 'volume':
    tmp = manifest.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2)+'\n')
    tmp.replace(manifest)
print('Verified checkpoint', revision)

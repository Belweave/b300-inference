"""Check every staged file against committed HF metadata before serving.
Large files use SHA256/LFS hashes; small files use their Git blob SHA1 and size.
No metadata or download requests are made in push/volume mode.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
from concurrent.futures import ThreadPoolExecutor

METADATA = Path(__file__).resolve().parents[1]/'docs/model-metadata.json'


def manifest(env):
    if env.get('MODEL_VARIANT') == 'uncensored' and not env.get('MODEL_METADATA'):
        from warm_download import load_manifest
        pinned = load_manifest()
        data = {'model': pinned['model']['repo'], 'revision': pinned['model']['revision'],
                'files': [dict(path=name, size=f['size'], sha256=f['sha256'], git_sha1=f['git_blob_id'])
                          for name, f in pinned['files'].items()]}
    else:
        data = json.loads(Path(env.get('MODEL_METADATA', METADATA)).read_text())['variants'][env.get('MODEL_VARIANT', 'fp8')]
    if data['model'] != env['MODEL'] or data['revision'] != env['MODEL_REVISION'] or not data['files']:
        raise ValueError('Model/revision does not match the committed metadata')
    for f in data['files']:
        p = PurePosixPath(f['path'])
        if p.is_absolute() or '..' in p.parts or '\n' in str(p) or '\0' in str(p):
            raise ValueError('Invalid manifest path')
        if not f.get('sha256') and not f.get('git_sha1'):
            raise ValueError('Manifest is missing a checksum')
    return data


def valid(root, f):
    p = root/f['path']
    if not p.resolve().is_relative_to(root.resolve()) or not p.is_file() or p.stat().st_size != f['size']:
        return False
    h = hashlib.sha256() if f.get('sha256') else hashlib.sha1()
    if not f.get('sha256'):
        h.update(f"blob {f['size']}\0".encode())
    with p.open('rb') as stream:
        for block in iter(lambda: stream.read(8*1024*1024), b''):
            h.update(block)
    return h.hexdigest() == (f.get('sha256') or f['git_sha1'])


def checks(root, data, env):
    with ThreadPoolExecutor(max_workers=int(env.get('HASH_WORKERS', '4'))) as pool:
        return list(pool.map(lambda f: valid(root, f), data['files']))


def main(env):
    root = Path(env['WEIGHTS_DIR'])
    source = env['WEIGHTS_SOURCE']
    if source not in ('hf', 'mirror', 'push', 'volume'):
        raise ValueError('WEIGHTS_SOURCE must be hf|mirror|push|volume (s3 was renamed mirror)')
    data = manifest(env)
    good = checks(root, data, env)
    if not all(good):
        if source in ('push', 'volume'):
            raise ValueError('Staged checkpoint checksum failed; repair or push missing/corrupt files')
        root.mkdir(parents=True, exist_ok=True)
        missing = [f for f, ok in zip(data['files'], good) if not ok]
        if source == 'hf':
            from huggingface_hub import snapshot_download
            for f in missing:
                if (root/f['path']).is_file():
                    (root/f['path']).unlink()
            snapshot_download(data['model'], revision=data['revision'], local_dir=str(root),
                              token=env.get('HF_TOKEN') or False,
                              allow_patterns=[f['path'] for f in missing],
                              max_workers=int(env.get('HF_MAX_WORKERS', '16')))
        else:
            prefix = env['WEIGHTS_S3_URI'].rstrip('/')
            cmd = ['s5cmd', '--numworkers', env.get('S3_WORKERS', '32')]
            if env.get('S3_ENDPOINT_URL'):
                cmd += ['--endpoint-url', env['S3_ENDPOINT_URL']]
            def copy(f):
                p = root/f['path']; p.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(cmd+['cp', '--concurrency', env.get('S3_PART_CONCURRENCY', '8'),
                                    prefix+'/'+f['path'], str(p)], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with ThreadPoolExecutor(max_workers=int(env.get('S3_FILE_WORKERS', '4'))) as pool:
                list(pool.map(copy, missing))
        if not all(checks(root, data, env)):
            raise ValueError('Downloaded checkpoint checksum failed; refusing launch')
    index = json.loads((root/'model.safetensors.index.json').read_text())
    if not set(index['weight_map'].values()).issubset({f['path'] for f in data['files']}):
        raise ValueError('Manifest misses indexed shards')
    print('Verified checkpoint against committed metadata:', data['revision'])


if __name__ == '__main__':
    main(os.environ)

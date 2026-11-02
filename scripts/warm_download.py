"""Public pinned HTTP transfers, bounded parallelism and offline checksum markers."""
import asyncio
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import time
from urllib.parse import quote

import aiohttp
from warm_logic import verify_mirror, fallback
from mirror_auth import parse_mirror_auth, mirror_request

MANIFEST = Path(__file__).resolve().parents[1]/'config/manifests/uncensored-c19ca5c.json'


def load_manifest(path=MANIFEST):
    raw = json.loads(Path(path).read_text())
    for key in ('revision', 'base_revision'):
        revision = raw[key]
        if len(revision) != 40 or any(c not in '0123456789abcdef' for c in revision):
            raise ValueError('An immutable revision is required')
    # Reuse selection is the API-verified diff for this exact pair. Hashes and
    # sizes always come from Kai's canonical manifest, never the full HF listing.
    if (raw['repo'], raw['revision'], raw['base_repo'], raw['base_revision']) != (
        'dealignai/DeepSeek-V4.1-Flash-1017-UNCENSORED-FP8', 'c19ca5ca4eaeb79d29fdf08aea8327da95b1116a',
        'deepseek-ai/DeepSeek-V4.1-Flash-1017', '1bae0e1088993b9b82de08f59de5be5a34e42170'):
        raise ValueError('No verified reuse plan for this model/base revision pair')
    files = {f['filename']: f for f in raw['files']}
    expected = {f'model-{i:05d}-of-00048.safetensors' for i in range(1, 49)} | {
        'config.json', 'model.safetensors.index.json', 'tokenizer.json', 'tokenizer_config.json'}
    if len(files) != len(raw['files']) or set(files) != expected:
        raise ValueError('Canonical manifest must contain exactly 48 shards and four small files')
    data = {'model': {'repo': raw['repo'], 'revision': raw['revision']},
            'base': {'repo': raw['base_repo'], 'revision': raw['base_revision']},
            'files': files,
            'differs_from_base': [f'model-{i:05d}-of-00048.safetensors' for i in range(13, 40)]}
    for name, f in data['files'].items():
        p = PurePosixPath(name)
        if p.is_absolute() or '..' in p.parts or '\\' in name or not isinstance(f['size'], int) or f['size'] < 0:
            raise ValueError('Unsafe manifest entry')
        digest = f.get('sha256', '')
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('Every canonical file requires SHA256')
    return data


def stamp(path, f):
    s = path.stat()
    return {'size': s.st_size, 'mtime_ns': s.st_mtime_ns, 'ctime_ns': s.st_ctime_ns,
            'inode': s.st_ino, 'device': s.st_dev, 'expected': f['sha256']}


def marker(cache, path):
    return cache/(hashlib.sha256(str(path.absolute()).encode()).hexdigest()+'.json')


def remember(path, f, cache):
    cache.mkdir(parents=True, exist_ok=True)
    p = marker(cache, path); tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(stamp(path, f))); tmp.replace(p)


def verified(path, f, cache, force=False):
    if path.is_symlink() or not path.is_file() or path.stat().st_size != f['size']:
        return False
    before = stamp(path, f)
    try:
        if not force and json.loads(marker(cache, path).read_text()) == before:
            return True
    except (FileNotFoundError, ValueError):
        pass
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8*1024*1024), b''): h.update(block)
    good = h.hexdigest() == f['sha256'] and before == stamp(path, f)
    if good: remember(path, f, cache)
    return good


def link_or_copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name+'.reuse')
    tmp.unlink(missing_ok=True)
    try:
        os.link(source, tmp)
        mode = 'hardlink'
    except OSError as exc:
        if exc.errno != errno.EXDEV: raise
        shutil.copyfile(source, tmp)
        mode = 'copy'
    tmp.replace(target)
    return mode


def write_block(stream, digest, block):
    stream.write(block)
    digest.update(block)


async def fetch(session, url, target, f, floor, end, *, mirror=False, auth=None):
    tmp = target.with_name(target.name+'.incomplete'); target.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    h = hashlib.sha256()
    async def consume(response):
        response.raise_for_status()
        count = 0
        with tmp.open('wb') as stream:
            async for block in response.content.iter_chunked(1024*1024):
                count += len(block)
                if count > f['size']: raise ValueError('Oversized download')
                await asyncio.to_thread(write_block, stream, h, block)
                elapsed = time.monotonic()-started
                if elapsed >= 10 and fallback(mbps=count/elapsed/1e6, floor=floor):
                    raise ValueError('Download below speed floor')
        elapsed = max(time.monotonic()-started, 0.001)
        valid = count == f['size'] and h.hexdigest() == f['sha256']
        if fallback(valid=valid, mbps=count/elapsed/1e6 if elapsed >= 10 else None, floor=floor):
            raise ValueError('Download checksum/size/speed failed')
        tmp.replace(target)
        return {'bytes': count, 'seconds': elapsed, 'MBps': count/elapsed/1e6}
    try:
        timeout = aiohttp.ClientTimeout(total=max(0.01, end-time.time()), sock_read=30)
        if mirror:
            return await mirror_request(session, url, auth, consume, timeout=timeout)
        async with session.get(url, timeout=timeout) as response:
            return await consume(response)
    finally:
        tmp.unlink(missing_ok=True)


async def acquire(data, root, base, end, env):
    auth = parse_mirror_auth(env.get('MIRROR_AUTH_HEADER'))
    mirror_only = env.get('MIRROR_ONLY', '0')
    if mirror_only not in ('0', '1'):
        raise ValueError('MIRROR_ONLY must be 0 or 1')
    mirror_only = mirror_only == '1'
    if mirror_only and not env.get('MIRROR_URL', '').rstrip('/'):
        raise ValueError('MIRROR_ONLY requires MIRROR_URL')
    root, base = Path(root), Path(base)
    if root.resolve() == base.resolve(): raise ValueError('Target must be separate from base')
    root.mkdir(parents=True, exist_ok=True)
    cache = root/'.warm-verified'; cache.mkdir(exist_ok=True)
    workers = int(env.get('SHARD_WORKERS', '16'))
    if not 1 <= workers <= 48: raise ValueError('SHARD_WORKERS must be 1..48')
    semaphore = asyncio.Semaphore(workers)
    hashes = asyncio.Semaphore(int(env.get('HASH_WORKERS', '4')))
    if int(env.get('HASH_WORKERS', '4')) not in range(1, 17): raise ValueError('HASH_WORKERS must be 1..16')
    reuse = set(data['files']) - set(data['differs_from_base']); records = []
    mirror = env.get('MIRROR_URL', '').rstrip('/')
    mirror_files = set()
    revision = data['model']['revision']
    # Retained for older HF clients; this downloader uses bounded aiohttp streams,
    # not hf_transfer (removed in huggingface_hub 1.x).
    os.environ['HF_HUB_ENABLE_HF_TRANSFER'] = '1'
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=workers), trust_env=False) as session:
        if mirror:
            async def consume_manifest(response):
                return verify_mirror(data, await response.json())
            mirror_files = await mirror_request(session, mirror+'/'+revision+'/manifest.json', auth, consume_manifest,
                                               timeout=aiohttp.ClientTimeout(total=min(30,max(.01,end-time.time()))))
            if mirror_only and set(data['files']) != set(mirror_files):
                raise ValueError('MIRROR_ONLY requires all canonical files in the mirror manifest')
        async def check(path, f):
            async with hashes:
                return await asyncio.to_thread(verified, path, f, cache, env.get('FORCE_HASH') == '1')
        async def one(name, f):
            async with semaphore:
                if time.time() >= end: raise TimeoutError('Warm deadline reached')
                target = root/name
                if not target.resolve().is_relative_to(root.resolve()): raise ValueError('Unsafe target path')
                if await check(target, f):
                    records.append({'file':name, 'source':'verified-local'}); return
                if name in reuse and await check(base/name, f):
                    source_state = stamp(base/name, f)
                    mode = await asyncio.to_thread(link_or_copy, base/name, target)
                    target_state = stamp(target, f)
                    same = all(source_state[k] == target_state[k] for k in ('device','inode','size','mtime_ns'))
                    if mode == 'hardlink' and same:
                        # Same verified bytes/inode: do not hash the two 101 GB tables twice.
                        remember(target, f, cache)
                        remember(base/name, f, cache)  # hardlink creation changes ctime
                    elif not await check(target, f):
                        raise ValueError('Reused shard failed verification')
                    records.append({'file':name, 'source':mode}); return
                url = 'https://huggingface.co/'+data['model']['repo']+'/resolve/'+revision+'/'+quote(name)
                if mirror_only:
                    stats = await fetch(session, mirror+'/'+revision+'/'+quote(name), target, f, 0, end, mirror=True, auth=auth)
                    source = 'mirror'
                else:
                    try:
                        stats = await fetch(session, url, target, f, float(env.get('MIN_MBPS','0')), end)
                        source = 'hf'
                    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                        if name not in mirror_files: raise
                        stats = await fetch(session, mirror+'/'+revision+'/'+quote(name), target, f, 0, end, mirror=True, auth=auth)
                        source = 'mirror'
                remember(target, f, cache)
                records.append({'file':name, 'source':source, **stats})
        tasks = [asyncio.create_task(one(name, f)) for name, f in data['files'].items()]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done(): task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    return records

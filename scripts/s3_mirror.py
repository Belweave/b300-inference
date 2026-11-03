"""Direct S3 reads from the R2 model-mirror bucket: the default fast path for weights.

Credentials come only from the environment (node-env.sh loads MIRROR_S3_ENV_FILE):
AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_ENDPOINT_URL, R2_BUCKET, AWS_REGION.
Errors never carry the endpoint, bucket, key id or any response text."""
import hashlib
import os
from pathlib import Path
import time

# The main model is staged without its HF org; the guards keep theirs.
MAIN_PREFIX = 'models/DeepSeek-V4.1-Flash-1017-UNCENSORED-FP8'
CHUNK = 64*1024*1024


class S3MirrorError(Exception):
    pass


def main_key(revision, name):
    return f'{MAIN_PREFIX}/{revision}/{name}'


def guard_key(repo, revision, name):
    return f'models/{repo}/{revision}/{name}'


def enabled(env):
    flag = env.get('MIRROR_S3', '1')
    if flag not in ('0', '1'):
        raise ValueError('MIRROR_S3 must be 0 or 1')
    return flag == '1' and all(env.get(k) for k in
                               ('R2_BUCKET', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_ENDPOINT_URL'))


def concurrency(env):
    value = int(env.get('S3_CONCURRENCY', '16'))
    if not 1 <= value <= 64:
        raise ValueError('S3_CONCURRENCY must be 1..64')
    return value


def client(env):
    import boto3
    from botocore.config import Config
    return boto3.client('s3', endpoint_url=env['AWS_ENDPOINT_URL'], region_name=env.get('AWS_REGION') or 'auto',
                        aws_access_key_id=env['AWS_ACCESS_KEY_ID'], aws_secret_access_key=env['AWS_SECRET_ACCESS_KEY'],
                        config=Config(max_pool_connections=128, retries={'max_attempts': 8, 'mode': 'adaptive'},
                                      signature_version='s3v4'))


def transfer_config(env):
    from boto3.s3.transfer import TransferConfig
    return TransferConfig(multipart_threshold=CHUNK, multipart_chunksize=CHUNK,
                          max_concurrency=concurrency(env), use_threads=True)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(8*1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def download(s3, env, key, target, f, config=None):
    """Parallel ranged GETs of one object into target, then size + SHA256 against the pin.

    Writes to <target>.s3part and only renames a verified file into place."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name+'.s3part')
    tmp.unlink(missing_ok=True)
    started = time.monotonic()
    failed = False
    try:
        s3.download_file(env['R2_BUCKET'], key, str(tmp), Config=config if config is not None else transfer_config(env))
    except Exception:
        failed = True  # never chain: botocore errors can name the endpoint and bucket
    if failed:
        tmp.unlink(missing_ok=True)
        raise S3MirrorError(f's3 download failed: {target.name}')
    try:
        if tmp.stat().st_size != f['size'] or sha256(tmp) != f['sha256']:
            raise S3MirrorError(f's3 checksum/size mismatch: {target.name}')
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    elapsed = max(time.monotonic()-started, 0.001)
    return {'bytes': f['size'], 'seconds': elapsed, 'MBps': f['size']/elapsed/1e6}

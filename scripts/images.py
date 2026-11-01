"""Stage/load a compressed Docker save archive, checking archive and image digests."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as src:
        for block in iter(lambda: src.read(8*1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def inspect(ref):
    fmt = '{"id":{{json .Id}},"repo_digests":{{json .RepoDigests}},"architecture":{{json .Architecture}},"os":{{json .Os}}}'
    return json.loads(subprocess.check_output(['docker', 'image', 'inspect', ref, '--format', fmt], text=True))


def stage(image, directory):
    directory.mkdir(parents=True, exist_ok=True)
    platform = os.environ.get('STAGING_PLATFORM', 'linux/amd64')
    subprocess.run(['docker', 'pull', '--platform', platform, image], check=True, stdout=subprocess.DEVNULL)
    metadata = dict(inspect(image), source_image=image)
    if metadata['os']+'/'+metadata['architecture'] != platform:
        raise ValueError('Staging platform mismatch; do not export a Mac-native image for the node')
    tmp = directory/'engine.tar.zst.partial'
    with tmp.open('wb') as dest:
        save = subprocess.Popen(['docker', 'save', image], stdout=subprocess.PIPE)
        try:
            compressed = subprocess.run(['zstd', '-T0', '-3'], stdin=save.stdout, stdout=dest)
            save.stdout.close()
            if save.wait() or compressed.returncode:
                raise RuntimeError('Docker save/zstd failed; partial archive retained')
        finally:
            if save.poll() is None:
                save.terminate(); save.wait()
    metadata.update(archive_sha256=sha256(tmp), archive_bytes=tmp.stat().st_size)
    tmp.replace(directory/'engine.tar.zst')
    (directory/'engine.json').write_text(json.dumps(metadata, indent=2)+'\n')


def acquire(env):
    if env['IMAGE_SOURCE'] == 'pull':
        subprocess.run(['docker', 'pull', env['ENGINE_IMAGE']], check=True)
        metadata = inspect(env['ENGINE_IMAGE'])
    elif env['IMAGE_SOURCE'] == 'push':
        directory = Path(env['PUSH_DIR'])
        metadata = json.loads((directory/'engine.json').read_text())
        if metadata['source_image'] != env['ENGINE_IMAGE']:
            raise ValueError('Staged image reference does not match ENGINE_IMAGE')
        archive = directory/'engine.tar.zst'
        if archive.stat().st_size != metadata['archive_bytes'] or sha256(archive) != metadata['archive_sha256']:
            raise ValueError('Image archive checksum mismatch; refusing docker load')
        decompress = subprocess.Popen(['zstd', '-dc', str(archive)], stdout=subprocess.PIPE)
        try:
            loaded = subprocess.run(['docker', 'load'], stdin=decompress.stdout, stdout=subprocess.DEVNULL)
            decompress.stdout.close()
            if decompress.wait() or loaded.returncode:
                raise RuntimeError('zstd/docker load failed')
        finally:
            if decompress.poll() is None:
                decompress.terminate(); decompress.wait()
        actual = inspect(metadata['id'])
        if actual['id'] != metadata['id'] or actual['architecture'] != metadata['architecture'] or actual['os'] != metadata['os']:
            raise ValueError('Loaded image digest/platform mismatch')
    else:
        raise ValueError('IMAGE_SOURCE must be pull or push')
    # docker save/load need not preserve RepoDigests. Immutable image config ID is verified.
    metadata['effective_ref'] = metadata['id']
    path = Path(env['RUN_DIR'])/'loaded-image.json'
    path.write_text(json.dumps(metadata, indent=2)+'\n')


if __name__ == '__main__':
    if sys.argv[1] == 'stage':
        stage(os.environ['ENGINE_IMAGE'], Path(os.environ['STAGING_DIR'])/os.environ['MODEL_VARIANT']/'images')
    else:
        acquire(os.environ)

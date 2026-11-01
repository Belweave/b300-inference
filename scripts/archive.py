"""Re-filter every diagnostic member before creating the teardown tarball."""
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile


def package(run_dir, archive):
    run_dir, archive = Path(run_dir), Path(archive)
    temporary = archive.with_name(archive.name + '.tmp')
    try:
        with tarfile.open(temporary, 'w:gz') as tar:
            for path in sorted(run_dir.rglob('*')):
                relative = path.relative_to(run_dir)
                if (path.is_symlink() or not path.is_file() or 'incoming' in relative.parts
                        or any(p.startswith('.env') or p.endswith('.env') for p in relative.parts)
                        or path.name.endswith(('.tar.zst', '.tar.gz', '.pem', '.key'))):
                    continue
                # Only filtered bytes reach scratch storage, bounded to 1 MiB RAM.
                with path.open('rb') as source, tempfile.SpooledTemporaryFile(max_size=1024*1024) as clean:
                    subprocess.run([sys.executable, str(Path(__file__).with_name('redact.py'))],
                                   stdin=source, stdout=clean, check=True)
                    size = clean.tell()
                    clean.seek(0)
                    info = tarfile.TarInfo(str(Path(run_dir.name)/relative))
                    info.size, info.mode, info.mtime = size, 0o600, int(path.stat().st_mtime)
                    tar.addfile(info, clean)
        os.replace(temporary, archive)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == '__main__':
    package(*sys.argv[1:])

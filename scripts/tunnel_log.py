"""Collect only the verified suffix of Netops' shared append-only tunnel log."""
import json
import os
from pathlib import Path
import sys

from redact import redact, secret_values


def mark(source, cursor):
    try:
        stat = Path(source).stat()
        data = {'device': stat.st_dev, 'inode': stat.st_ino, 'offset': stat.st_size}
    except FileNotFoundError:
        data = {'device': None, 'inode': None, 'offset': 0}
    Path(cursor).write_text(json.dumps(data)+'\n')


def collect(source, cursor, destination):
    # A legacy run without a cursor cannot separate earlier (possibly rotated-key)
    # output from this run. Never fall back to archiving the whole shared file.
    with Path(destination).open('w') as output:
        try:
            data = json.loads(Path(cursor).read_text())
            offset = data['offset']
            if type(offset) is not int or offset < 0:
                raise ValueError('Invalid log offset')
            with Path(source).open('rb') as stream:
                stat = os.fstat(stream.fileno())
                if data['inode'] is not None and (stat.st_dev, stat.st_ino) != (data['device'], data['inode']):
                    raise ValueError('Shared log replaced')
                if stat.st_size < offset:
                    raise ValueError('Shared log truncated')
                stream.seek(offset)
                secrets = secret_values()
                remaining = stat.st_size-offset
                while remaining:
                    line = stream.readline(remaining)
                    if not line:
                        break
                    remaining -= len(line)
                    output.write(redact(line.decode('utf-8', errors='replace'), secrets))
        except (FileNotFoundError, ValueError, KeyError, TypeError):
            output.write('Tunnel log omitted: no valid start cursor, or shared log missing/replaced/truncated.\n')


if __name__ == '__main__':
    if sys.argv[1] == 'mark':
        mark(*sys.argv[2:])
    elif sys.argv[1] == 'collect':
        collect(*sys.argv[2:])
    else:
        raise SystemExit('Expected mark or collect')

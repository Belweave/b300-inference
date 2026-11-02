"""Pull and SHA256-verify the canonical model from the private mirror only."""
import argparse
import asyncio
from collections import Counter
import json
import os
from pathlib import Path
import sys
import time

from warm_download import acquire, load_manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True, type=Path)
    parser.add_argument('--timeout-seconds', type=int, default=3600)
    args = parser.parse_args()
    if args.timeout_seconds <= 0:
        parser.error('timeout must be positive')
    os.umask(0o077)
    env = {**os.environ, 'MIRROR_ONLY':'1'}
    try:
        records = asyncio.run(acquire(load_manifest(), args.target,
                                     args.target.parent/'unused-base',
                                     time.time()+args.timeout_seconds, env))
    except Exception as exc:
        # Authenticated HTTP exception details must never reach operator logs.
        print('Mirror pull failed: '+type(exc).__name__, file=sys.stderr)
        return 1
    print(json.dumps({'verified_files':len(records),
                      'sources':dict(Counter(row['source'] for row in records))}))
    return 0


if __name__=='__main__':
    sys.exit(main())

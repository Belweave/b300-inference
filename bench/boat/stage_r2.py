"""Stream canonical HF weights to model-mirror, with full R2 SHA256 readback.

Run INSIDE Boat. Requires a private JSON file containing temporary S3 credentials.
No model files are stored on disk. The manifest is published only after every
object verifies. Existing objects are reused only after full readback verification.
"""
import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
import hashlib
import json
from pathlib import Path
import platform
import sys
import threading
import os
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import requests
import urllib3

BUCKET = 'model-mirror'
# Private Worker origin, from the operator environment (never committed).
MIRROR_ORIGIN = os.environ.get('MIRROR_ORIGIN','').rstrip('/')
REVISION = 'c19ca5ca4eaeb79d29fdf08aea8327da95b1116a'
REPO = 'dealignai/DeepSeek-V4.1-Flash-1017-UNCENSORED-FP8'
PREFIX = f'models/DeepSeek-V4.1-Flash-1017-UNCENSORED-FP8/{REVISION}/'
PART = 64*1024**2
RANGE_WORKERS = 4
TOOL_PINS = {'amd64':'1f99fb518b83dec2ba8d870a7cc991d82a77e6196295a3d2754ba17ab9f91872',
             'arm64':'3df43aedc9ff291d1735b5cb576cb97e0b157488796db2e32b28aecabb445775'}
_SOURCE_CACHE = {}
_SOURCE_LOCK = threading.Lock()
MIRROR_HEADER = None


def source_url(entry):
    # Resolve once per object instead of spending an HF resolver request on
    # every range. Signed locations remain in memory and never reach logs.
    name = entry['filename']
    with _SOURCE_LOCK:
        cached = _SOURCE_CACHE.get(name)
        if cached and cached[0] > time.monotonic(): return cached[1]
        url = f'https://huggingface.co/{REPO}/resolve/{REVISION}/'+name
        with requests.get(url,allow_redirects=False,stream=True,timeout=(15,30)) as response:
            response.raise_for_status()
            location = response.headers.get('Location',url)
        _SOURCE_CACHE[name] = (time.monotonic()+300,location)
        return location


def transfer_part(s3, entry, key, upload, number, stop):
    for attempt in range(6):
        try:
            return transfer_part_once(s3,entry,key,upload,number,stop)
        except (requests.RequestException,urllib3.exceptions.HTTPError,OSError,ValueError) as exc:
            if attempt==5: raise
            with _SOURCE_LOCK: _SOURCE_CACHE.pop(entry['filename'],None)
            print(json.dumps({'event':'retry','file':entry['filename'],'part':number,
                              'type':type(exc).__name__,'attempt':attempt+1}),flush=True)
            time.sleep(min(16,2**attempt))


def transfer_part_once(s3, entry, key, upload, number, stop):
    if stop is not None and stop.is_set():
        raise RuntimeError('transfer cancelled')
    start = (number-1)*PART
    end = min(start+PART,entry['size'])-1
    with requests.get(source_url(entry),headers={'Range':f'bytes={start}-{end}'},stream=True,
                      timeout=(15,90)) as response:
        response.raise_for_status()
        if (response.status_code != 206 or
                response.headers.get('Content-Range') != f'bytes {start}-{end}/{entry["size"]}'):
            raise ValueError('source returned an unexpected byte range')
        chunk = response.raw.read(end-start+2)
    if len(chunk) != end-start+1:
        raise ValueError('source range size mismatch')
    result = s3.upload_part(Bucket=BUCKET, Key=key, UploadId=upload,
                            PartNumber=number, Body=chunk)
    return chunk, {'PartNumber':number,'ETag':result['ETag']}


def verify(s3, key, expected):
    head = s3.head_object(Bucket=BUCKET, Key=key)
    if head['ContentLength'] != expected['size']:
        raise ValueError('R2 object size mismatch: '+key)
    digest = hashlib.sha256(); count = 0
    def read_range(start):
        end = min(start+PART,expected['size'])-1
        if MIRROR_HEADER:
            for attempt in range(6):
                try:
                    with requests.get(MIRROR_ORIGIN+'/'+key,
                                      params={'verify_range':f'{start}-{end}'},
                                      headers={**MIRROR_HEADER,'Range':f'bytes={start}-{end}','Cache-Control':'no-cache'},
                                      allow_redirects=False,stream=True,timeout=(15,90)) as response:
                        response.raise_for_status()
                        if response.status_code != 206 or response.headers.get('Content-Range') != f'bytes {start}-{end}/{expected["size"]}':
                            raise ValueError('Worker readback range mismatch')
                        chunk = response.raw.read(PART+1)
                    if len(chunk)!=end-start+1: raise ValueError('Worker readback size mismatch')
                    return chunk
                except (requests.RequestException,urllib3.exceptions.HTTPError,OSError,ValueError) as exc:
                    if attempt==5: raise
                    print(json.dumps({'event':'verify_retry','key':key,'offset':start,
                                      'type':type(exc).__name__,'attempt':attempt+1}),flush=True)
                    time.sleep(min(16,2**attempt))
        result = s3.get_object(Bucket=BUCKET,Key=key,Range=f'bytes={start}-{end}')
        with closing(result['Body']) as body:
            chunk = body.read(PART+1)
        if result.get('ContentRange') != f'bytes {start}-{end}/{expected["size"]}' or len(chunk)!=end-start+1:
            raise ValueError('R2 readback range mismatch')
        return chunk
    if expected['size'] > PART:
        with ThreadPoolExecutor(max_workers=RANGE_WORKERS) as pool:
            offsets = iter(range(0,expected['size'],PART))
            pending = deque(pool.submit(read_range,start) for start in [next(offsets,None) for _ in range(RANGE_WORKERS)]
                            if start is not None)
            while pending:
                chunk = pending.popleft().result()
                digest.update(chunk); count += len(chunk)
                del chunk
                start = next(offsets,None)
                if start is not None: pending.append(pool.submit(read_range,start))
    else:
        if MIRROR_HEADER:
            chunk = read_range(0); digest.update(chunk); count += len(chunk)
        else:
            with closing(s3.get_object(Bucket=BUCKET, Key=key)['Body']) as body:
                for chunk in body.iter_chunks(chunk_size=8*1024**2):
                    digest.update(chunk); count += len(chunk)
    if count != expected['size'] or digest.hexdigest() != expected['sha256']:
        print(json.dumps({'event':'readback_mismatch','key':key,'bytes':count,
                          'actual_sha256':digest.hexdigest(),'expected_sha256':expected['sha256']}),flush=True)
        raise ValueError('R2 readback digest mismatch: '+key)
    return {'key':key, 'size':count, 'sha256':digest.hexdigest(), 'etag':head['ETag'] if 'ETag' in head else None,
            'verified':'full-authenticated-worker-readback' if MIRROR_HEADER else 'full-r2-readback'}


def stage(s3, entry, stop=None):
    if stop is not None and stop.is_set():
        raise RuntimeError('transfer cancelled')
    key = PREFIX+entry['filename']
    started = time.monotonic()
    print(json.dumps({'event':'start','key':key,'size':entry['size']}),flush=True)
    try:
        s3.head_object(Bucket=BUCKET, Key=key)
    except ClientError as exc:
        if exc.response['Error']['Code'] not in ('404','NoSuchKey','NotFound'):
            raise
    else:
        # Do not overwrite objects unexpectedly. A mismatch is an operator error.
        row = verify(s3,key,entry)
        row.update(reused=True, seconds=time.monotonic()-started)
        print(json.dumps(row),flush=True)
        return row
    total = (entry['size']+PART-1)//PART
    candidates = [u for page in s3.get_paginator('list_multipart_uploads').paginate(Bucket=BUCKET,Prefix=key)
                  for u in page.get('Uploads',[]) if u['Key']==key]
    resumed = {}
    if candidates:
        upload = max(candidates,key=lambda u:u['Initiated'])['UploadId']
        for page in s3.get_paginator('list_parts').paginate(Bucket=BUCKET,Key=key,UploadId=upload):
            for part in page.get('Parts',[]):
                n = part['PartNumber']
                if 1 <= n <= total and part['Size']==min(PART,entry['size']-(n-1)*PART):
                    resumed[n] = {'PartNumber':n,'ETag':part['ETag']}
        print(json.dumps({'event':'resume','key':key,'parts':len(resumed)}),flush=True)
    else:
        upload = s3.create_multipart_upload(Bucket=BUCKET, Key=key,
                                          Metadata={'sha256':entry['sha256']})['UploadId']
    complete = False
    try:
        parts = []; digest = hashlib.sha256(); count = 0
        # Bound range buffers per object. Consume in order for SHA256;
        # do not submit the entire shard, which could retain 101 GB of results.
        def one(number):
            if number in resumed:
                return None,resumed[number]
            return transfer_part(s3,entry,key,upload,number,stop)
        with ThreadPoolExecutor(max_workers=RANGE_WORKERS) as pool:
            pending = deque()
            next_part = 1
            while next_part <= min(RANGE_WORKERS,total):
                pending.append(pool.submit(one,next_part))
                next_part += 1
            while pending:
                chunk, part = pending.popleft().result()
                if chunk is not None: digest.update(chunk)
                count += min(PART,entry['size']-(part['PartNumber']-1)*PART)
                parts.append(part)
                del chunk
                if next_part <= total:
                    pending.append(pool.submit(one,next_part))
                    next_part += 1
                if len(parts) % 8 == 0:
                    print(json.dumps({'event':'progress','key':key,'bytes':count}),flush=True)
        if count != entry['size'] or (not resumed and digest.hexdigest() != entry['sha256']):
            print(json.dumps({'event':'source_mismatch','key':key,'bytes':count,
                              'actual_sha256':digest.hexdigest(),'expected_sha256':entry['sha256']}),flush=True)
            raise ValueError('HF source size/SHA256 mismatch: '+key)
        s3.complete_multipart_upload(Bucket=BUCKET, Key=key, UploadId=upload,
                                    MultipartUpload={'Parts':parts})
        complete = True
    finally:
        # Invalid bytes must never survive. Network interruptions retain their
        # parts for a verified resume rather than discarding a 101 GB transfer.
        if not complete and isinstance(sys.exc_info()[1],ValueError):
            s3.abort_multipart_upload(Bucket=BUCKET, Key=key, UploadId=upload)
    row = verify(s3,key,entry)
    row.update(reused=False, resumed_parts=len(resumed), source_sha256_verified=not bool(resumed),
               seconds=time.monotonic()-started)
    print(json.dumps(row),flush=True)
    return row


def verify_tools(s3):
    # Netops owns these existing objects. Read and hash only, never modify.
    objects = [obj for page in s3.get_paginator('list_objects_v2').paginate(
        Bucket=BUCKET,Prefix='tools/cloudflared/2026.10.7/') for obj in page.get('Contents',[])]
    checksums = {}
    for obj in objects:
        if obj['Key'].endswith('.sha256'):
            with closing(s3.get_object(Bucket=BUCKET,Key=obj['Key'])['Body']) as stream:
                checksums[obj['Key'][:-7]] = stream.read(4096).decode().split()[0]
    rows = []
    for obj in objects:
        key = obj['Key']
        if key not in checksums: continue
        digest = checksums[key]
        if digest not in TOOL_PINS.values():
            raise ValueError('cloudflared sidecar disagrees with committed release pins')
        rows.append(verify(s3,key,{'size':obj['Size'],'sha256':digest}))
    if not rows: raise ValueError('No paired cloudflared binary/checksum objects found')
    return rows


def validate_partition_reports(s3,manifest,paths):
    objects=[]
    for path in paths:
        other=json.loads(Path(path).read_text())
        if not other.get('partition_complete') or other['revision']!=REVISION or other['bucket']!=BUCKET:
            raise ValueError('Incomplete/wrong partition report')
        objects.extend(other['objects'])
    expected={PREFIX+e['filename']:e for e in manifest['files']}
    if len(objects)!=52 or {r['key'] for r in objects}!=set(expected):
        raise ValueError('Reports must cover exactly the canonical 52 files')
    for row in objects:
        entry=expected[row['key']]
        if row['size']!=entry['size'] or row['sha256']!=entry['sha256'] or row['verified']!='full-authenticated-worker-readback':
            raise ValueError('Report disagrees with canonical size/hash/Worker verification')
        head=s3.head_object(Bucket=BUCKET,Key=row['key'])
        if head['ContentLength']!=row['size'] or head['ETag']!=row['etag']:
            raise ValueError('Object changed after verification')
    return objects


def run():
    global RANGE_WORKERS, MIRROR_HEADER
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials', required=True)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--report', required=True)
    parser.add_argument('--workers', type=int, default=4, choices=range(1,5))
    parser.add_argument('--range-workers', type=int, default=4, choices=range(1,17))
    parser.add_argument('--tools-only', action='store_true')
    parser.add_argument('--mirror-header-file')
    parser.add_argument('--partition-count',type=int,default=1,choices=range(1,5))
    parser.add_argument('--partition-index',type=int,default=0)
    parser.add_argument('--publish-reports',nargs='+',help='Publish only after checking completed partition reports')
    args = parser.parse_args()
    if platform.system()!='Linux':
        raise ValueError('Run the uploader inside a Boat Linux VM')
    RANGE_WORKERS = args.range_workers
    if not 0 <= args.partition_index < args.partition_count:
        parser.error('partition index must be smaller than count')
    if args.mirror_header_file:
        name,value = Path(args.mirror_header_file).read_text().strip().split(':',1)
        if name.lower()!='authorization' or not value.strip().startswith('Bearer '):
            raise ValueError('Expected a complete mirror bearer header')
        MIRROR_HEADER = {name:value.strip()}
        if not MIRROR_ORIGIN.startswith('https://'):
            raise ValueError('Set MIRROR_ORIGIN to the private Worker origin')
    creds = json.loads(Path(args.credentials).read_text())
    if not creds.get('sessionToken'):
        raise ValueError('Temporary session credentials required')
    manifest = json.loads(Path(args.manifest).read_text())
    assert manifest['revision']==REVISION and manifest['repo']==REPO
    assert len(manifest['files'])==52
    s3 = boto3.client('s3',endpoint_url=creds['endpoint'],region_name='auto',
                      aws_access_key_id=creds['accessKeyId'],
                      aws_secret_access_key=creds['secretAccessKey'],
                      aws_session_token=creds['sessionToken'],
                      config=Config(signature_version='s3v4',connect_timeout=15,read_timeout=120,
                                    retries={'total_max_attempts':4,'mode':'standard'},
                                    max_pool_connections=args.workers*RANGE_WORKERS+4,
                                    s3={'addressing_style':'path'},
                                    request_checksum_calculation='when_required',
                                    response_checksum_validation='when_required'))
    if args.tools_only:
        Path(args.report).write_text(json.dumps({'cloudflared':verify_tools(s3)},indent=2)+'\n')
        return 0
    report = {'bucket':BUCKET,'prefix':PREFIX,'revision':REVISION,'objects':[],
              'complete':False,'verification':'full object size + SHA256 readback',
              'mirror_origin':'$MIRROR_ORIGIN',
              'partition_count':args.partition_count,'partition_index':args.partition_index}
    if args.publish_reports:
        report['objects']=validate_partition_reports(s3,manifest,args.publish_reports)
    else:
        stage_partitions(s3,manifest,args,report)
        if args.partition_count>1:
            report['partition_complete']=True
            Path(args.report).write_text(json.dumps(report,indent=2)+'\n')
            print(json.dumps({'event':'partition_complete','index':args.partition_index,'objects':len(report['objects'])}),flush=True)
            return 0
    body = json.dumps(manifest,indent=2).encode()
    key = PREFIX+'manifest.json'
    s3.put_object(Bucket=BUCKET,Key=key,Body=body,ContentType='application/json')
    report['manifest'] = verify(s3,key,{'size':len(body),'sha256':hashlib.sha256(body).hexdigest()})
    report['cloudflared'] = verify_tools(s3)
    report['complete'] = True
    report['total_model_bytes'] = sum(r['size'] for r in report['objects'])
    Path(args.report).write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'event':'complete','objects':len(report['objects']),
                      'bytes':report['total_model_bytes']}),flush=True)
    return 0


def stage_partitions(s3,manifest,args,report):
    Path(args.report).write_text(json.dumps(report,indent=2)+'\n')
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        # Start the two large embedding tables early; each object uses four ranges.
        pending = [pool.submit(stage,s3,entry,stop)
                   for i,entry in enumerate(sorted(manifest['files'],key=lambda e:-e['size']))
                   if i % args.partition_count == args.partition_index]
        try:
            for future in as_completed(pending):
                report['objects'].append(future.result())
                Path(args.report).write_text(json.dumps(report,indent=2)+'\n')
        except BaseException as exc:
            stop.set()
            for future in pending: future.cancel()
            print(json.dumps({'event':'failed','type':type(exc).__name__}),flush=True)
            raise


def main():
    try:
        return run()
    except Exception as exc:
        # HTTP exceptions can contain signed source URLs; never print details.
        reasons=('source returned an unexpected byte range','source range size mismatch',
                 'HF source size/SHA256 mismatch','R2 object size mismatch',
                 'R2 readback digest mismatch','R2 readback range mismatch',
                 'Worker readback range mismatch','Worker readback size mismatch')
        reason=next((r for r in reasons if isinstance(exc,ValueError) and str(exc).startswith(r)),None)
        print(json.dumps({'event':'failed','type':type(exc).__name__,'reason':reason}),file=sys.stderr)
        return 1


if __name__=='__main__':
    sys.exit(main())

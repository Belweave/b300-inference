"""Exercise stream readback and abort semantics without network or credentials."""
from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from botocore.exceptions import ClientError
from botocore.response import StreamingBody
import stage_r2


class FakeS3:
    def __init__(self):
        self.objects = {}; self.parts = {}; self.aborted = False

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({'Error':{'Code':'404'}},'HeadObject')
        return {'ContentLength':len(self.objects[Key])}

    def get_object(self, Bucket, Key, Range=None):
        data = self.objects[Key]
        total = len(data)
        if Range:
            start,end = map(int,Range.split('=')[1].split('-'))
            data = data[start:end+1]
        result = {'Body':StreamingBody(io.BytesIO(data),len(data))}
        if Range: result['ContentRange'] = f'bytes {start}-{end}/{total}'
        return result

    def create_multipart_upload(self, **kwargs): return {'UploadId':'fixture'}

    def get_paginator(self, operation):
        class Paginator:
            def paginate(self, **kwargs): return [{}]
        return Paginator()

    def upload_part(self, **kwargs):
        self.parts[kwargs['PartNumber']] = kwargs['Body']
        return {'ETag':str(kwargs['PartNumber'])}

    def complete_multipart_upload(self, **kwargs):
        self.objects[kwargs['Key']] = b''.join(self.parts[i] for i in sorted(self.parts))

    def abort_multipart_upload(self, **kwargs): self.aborted = True


class TransferTests(unittest.TestCase):
    def setUp(self):
        source = patch('stage_r2.source_url',return_value='fixture')
        source.start()
        self.addCleanup(source.stop)

    def test_publication_requires_complete_hashes_and_unchanged_objects(self):
        files=[{'filename':f'file-{i}','size':5,'sha256':'a'*64} for i in range(52)]
        manifest={'files':files}
        class HeadOnly:
            etag='fixture'
            def head_object(self,**kwargs):return {'ContentLength':5,'ETag':self.etag}
        s3=HeadOnly()
        with tempfile.TemporaryDirectory() as folder:
            paths=[]
            for part in range(4):
                rows=[{'key':stage_r2.PREFIX+f['filename'],'size':5,'sha256':f['sha256'],
                       'etag':'fixture','verified':'full-authenticated-worker-readback'}
                      for i,f in enumerate(files) if i%4==part]
                p=Path(folder)/str(part)
                p.write_text(json.dumps({'partition_complete':True,'revision':stage_r2.REVISION,
                                         'bucket':stage_r2.BUCKET,'objects':rows}))
                paths.append(p)
            self.assertEqual(len(stage_r2.validate_partition_reports(s3,manifest,paths)),52)
            with self.assertRaises(ValueError):stage_r2.validate_partition_reports(s3,manifest,paths[:-1])
            s3.etag='changed'
            with self.assertRaises(ValueError):stage_r2.validate_partition_reports(s3,manifest,paths)
            s3.etag='fixture'
            d=json.loads(paths[0].read_text());d['objects'][0]['sha256']='b'*64
            paths[0].write_text(json.dumps(d))
            with self.assertRaises(ValueError):stage_r2.validate_partition_reports(s3,manifest,paths)

    def test_resumed_parts_require_full_canonical_readback(self):
        payload = b'bounded streaming fixture'
        entry = {'filename':'fixture','size':len(payload),'sha256':hashlib.sha256(payload).hexdigest()}
        for corrupt in (False,True):
            s3 = FakeS3()
            s3.parts[1] = b'xxxxx' if corrupt else payload[:5]
            def paginator(operation):
                class Paginator:
                    def paginate(self, **kwargs):
                        if operation=='list_multipart_uploads':
                            return [{'Uploads':[{'Key':stage_r2.PREFIX+'fixture','Initiated':1,'UploadId':'fixture'}]}]
                        return [{'Parts':[{'PartNumber':1,'Size':5,'ETag':'1'}]}]
                return Paginator()
            s3.get_paginator = paginator
            @contextmanager
            def response(*args, **kwargs):
                start,end = map(int,kwargs['headers']['Range'].split('=')[1].split('-'))
                self.assertNotEqual(start,0)
                class Response:
                    status_code = 206
                    headers = {'Content-Range':f'bytes {start}-{end}/{len(payload)}'}
                    raw = io.BytesIO(payload[start:end+1])
                    def raise_for_status(self): pass
                yield Response()
            with patch('stage_r2.requests.get',response),patch('stage_r2.PART',5):
                if corrupt:
                    with self.assertRaises(ValueError):stage_r2.stage(s3,entry)
                else:
                    self.assertEqual(stage_r2.stage(s3,entry)['resumed_parts'],1)

    def test_uploaded_source_and_r2_readback_are_both_verified(self):
        payload = b'bounded streaming fixture'
        entry = {'filename':'fixture','size':len(payload),'sha256':hashlib.sha256(payload).hexdigest()}
        s3 = FakeS3()

        @contextmanager
        def response(*args, **kwargs):
            start,end = map(int,kwargs['headers']['Range'].split('=')[1].split('-'))
            class Response:
                status_code = 206
                headers = {'Content-Range':f'bytes {start}-{end}/{len(payload)}'}
                raw = io.BytesIO(payload[start:end+1])
                def raise_for_status(self): pass
            yield Response()

        with patch('stage_r2.requests.get',response), patch('stage_r2.PART',5):
            row = stage_r2.stage(s3,entry)
        self.assertEqual(row['sha256'],entry['sha256'])
        self.assertEqual(row['verified'],'full-r2-readback')
        self.assertLessEqual(max(map(len,s3.parts.values())),5)
        s3.objects[stage_r2.PREFIX+'fixture'] = b'x'*len(payload)
        with self.assertRaises(ValueError):stage_r2.verify(s3,stage_r2.PREFIX+'fixture',entry)

    def test_worker_readback_hashes_every_range_with_auth(self):
        payload=b'canonical worker fixture'
        key=stage_r2.PREFIX+'fixture'
        s3=FakeS3();s3.objects[key]=payload
        entry={'size':len(payload),'sha256':hashlib.sha256(payload).hexdigest()}
        @contextmanager
        def response(url,**kwargs):
            self.assertEqual(url,'https://mirror.example/'+key)
            self.assertEqual(kwargs['headers']['Authorization'],'Bearer fixture')
            self.assertFalse(kwargs['allow_redirects'])
            start,end=map(int,kwargs['headers']['Range'].split('=')[1].split('-'))
            class Response:
                status_code=206
                headers={'Content-Range':f'bytes {start}-{end}/{len(payload)}'}
                raw=io.BytesIO(payload[start:end+1])
                def raise_for_status(self):pass
            yield Response()
        with patch('stage_r2.requests.get',response),patch('stage_r2.PART',5),patch('stage_r2.MIRROR_HEADER',{'Authorization':'Bearer fixture'}),patch('stage_r2.MIRROR_ORIGIN','https://mirror.example'):
            self.assertEqual(stage_r2.verify(s3,key,entry)['verified'],'full-authenticated-worker-readback')

    def test_bad_source_is_aborted_and_never_completed(self):
        s3 = FakeS3()
        entry = {'filename':'fixture','size':4,'sha256':hashlib.sha256(b'good').hexdigest()}

        @contextmanager
        def response(*args, **kwargs):
            class Response:
                status_code = 206
                headers = {'Content-Range':'bytes 0-3/4'}
                raw = io.BytesIO(b'evil')
                def raise_for_status(self): pass
            yield Response()

        with patch('stage_r2.requests.get',response):
            with self.assertRaises(ValueError):stage_r2.stage(s3,entry)
        self.assertTrue(s3.aborted)
        self.assertFalse(s3.objects)


if __name__=='__main__':
    unittest.main()

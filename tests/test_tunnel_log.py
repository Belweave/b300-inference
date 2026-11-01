"""A shared append log must not pull earlier runs or rotated keys into artifacts."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import tunnel_log


class TunnelLogTests(unittest.TestCase):
    def test_only_new_suffix_is_collected_and_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory); source=base/'shared.log'; cursor=base/'cursor.json'; output=base/'collected.log'
            source.write_text('OLD RUN SHOULD NOT APPEAR\n')
            tunnel_log.mark(source,cursor)
            self.assertEqual(json.loads(cursor.read_text())['offset'],source.stat().st_size)
            # Inert token-shaped fixture; it is not a credential or valid JSON token.
            rotated='eyJ'+'ZmFrZQ'*30
            with source.open('a') as stream:
                stream.write('new replica online\ncurrent=sk-test-FAKE-current\nold replica token='+rotated+'\n')
            with patch.dict(os.environ,INFER_UPSTREAM_KEY='sk-test-FAKE-current'):
                tunnel_log.collect(source,cursor,output)
            result=output.read_text()
            self.assertNotIn('OLD RUN',result)
            self.assertNotIn('sk-test-FAKE-current',result)
            self.assertTrue(rotated not in result, 'Rotated-token fixture reached collected log')
            self.assertEqual(result,'new replica online\ncurrent=[REDACTED]\nold replica token=[REDACTED]\n')

    def test_log_created_after_mark_is_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory); source=base/'shared.log'; cursor=base/'cursor.json'; output=base/'collected.log'
            tunnel_log.mark(source,cursor)
            source.write_text('first launch\n')
            tunnel_log.collect(source,cursor,output)
            self.assertEqual(output.read_text(),'first launch\n')

    def test_missing_invalid_replaced_or_truncated_boundary_omits_shared_content(self):
        for case in ('missing','invalid','replaced','truncated'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                base=Path(directory); source=base/'shared.log'; cursor=base/'cursor.json'; output=base/'collected.log'
                source.write_text('earlier run, never archive as new\n')
                if case != 'missing': tunnel_log.mark(source,cursor)
                if case == 'invalid':cursor.write_text('{')
                if case == 'replaced':
                    source.rename(base/'old.log')
                    source.write_text('replacement output\n')
                if case == 'truncated':source.write_text('short\n')
                tunnel_log.collect(source,cursor,output)
                self.assertEqual(output.read_text(),'Tunnel log omitted: no valid start cursor, or shared log missing/replaced/truncated.\n')

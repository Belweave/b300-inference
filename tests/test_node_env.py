"""node-env.sh reads mirror.env as data, never as shell."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHOW = 'set -euo pipefail; source "$1"; printf "%s" "${MIRROR_AUTH_HEADER-unset}"'


class MirrorEnvTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.file = self.home/'mirror.env'

    def run_with(self, text=None, mode=0o600, **extra):
        if text is not None:
            self.file.write_text(text)
            self.file.chmod(mode)
        env = {'PATH': os.environ['PATH'], 'HOME': str(self.home), **extra}
        return subprocess.run(['bash', '-c', SHOW, 'bash', str(ROOT/'scripts/node-env.sh')],
                              env=env, capture_output=True, text=True, timeout=10)

    def test_accepted_formats_become_one_authorization_header(self):
        for text in ('Bearer fixture-value\n', 'Bearer fixture-value\r\n', 'Authorization: Bearer fixture-value\n',
                     'MIRROR_AUTH_HEADER=Authorization: Bearer fixture-value', 'Bearer fixture-value'):
            with self.subTest(text=text):
                proc = self.run_with(text, MIRROR_ENV_FILE='~/mirror.env')
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout, 'Authorization: Bearer fixture-value')

    def test_never_executes_the_file_and_refuses_anything_else(self):
        marker = self.home/'ran'
        for text in (f'touch {marker}\n', 'Bearer \n', '\n', 'X-Other: fixture-value\n'):
            with self.subTest(text=text):
                proc = self.run_with(text, MIRROR_ENV_FILE=str(self.file))
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn('MIRROR_ENV_FILE line 1 must be', proc.stderr)
                self.assertNotIn('fixture-value', proc.stdout+proc.stderr)
                self.assertFalse(marker.exists())

    def test_mode_is_checked_and_the_file_is_optional(self):
        proc = self.run_with('Bearer fixture-value\n', mode=0o644, MIRROR_ENV_FILE=str(self.file))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('MIRROR_ENV_FILE must be owned by the current user with mode 600', proc.stderr)
        self.assertEqual(self.run_with().stdout, 'unset')


if __name__ == '__main__':
    unittest.main()

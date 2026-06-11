import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_DIR = REPO_ROOT / 'cli'
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from transcribe import TranscriptClient, load_env


class LoadEnvTests(unittest.TestCase):
    def test_load_env_merges_process_environment_over_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / '.env'
            env_path.write_text(
                textwrap.dedent(
                    '''
                    TRANSCRIPT_LOL_SPACE_ID="file-space"
                    Transcript.lol_Login="file@example.com"
                    '''
                ).strip()
                + '\n',
                encoding='utf-8',
            )
            with mock.patch.dict(os.environ, {
                'TRANSCRIPT_LOL_SPACE_ID': 'env-space',
                'TRANSCRIPT_LOL_SPACE_NAME': 'Workspace By Name',
            }, clear=False):
                values = load_env(env_path)

        self.assertEqual(values['TRANSCRIPT_LOL_SPACE_ID'], 'env-space')
        self.assertEqual(values['TRANSCRIPT_LOL_SPACE_NAME'], 'Workspace By Name')
        self.assertEqual(values['Transcript.lol_Login'], 'file@example.com')


class SpaceResolutionTests(unittest.TestCase):
    def test_resolve_space_id_prefers_named_workspace(self):
        client = TranscriptClient({
            'TRANSCRIPT_LOL_SPACE_NAME': 'Skool Coliving Freedom Unlocked',
            'TRANSCRIPT_LOL_SPACE_ID': 'fallback-id',
        })
        with mock.patch.object(client, '_json_request', return_value=[
            {'id': 'one', 'name': 'Other Workspace'},
            {'id': 'target-id', 'name': 'Skool Coliving Freedom Unlocked'},
        ]):
            client._resolve_space_id()

        self.assertEqual(client.space_id, 'target-id')

    def test_resolve_space_id_keeps_existing_when_name_missing(self):
        client = TranscriptClient({'TRANSCRIPT_LOL_SPACE_ID': 'fallback-id'})
        client._resolve_space_id()
        self.assertEqual(client.space_id, 'fallback-id')


if __name__ == '__main__':
    unittest.main()

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_DIR = REPO_ROOT / "cli"
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

import archive_youtube
import daily_note_youtube
import export_transcripts
import transcript
from transcript_server import TranscriptService


class YouTubeIngestStemTests(unittest.TestCase):
    def test_resolve_youtube_marker_keeps_youtube_only_fallback_marker(self):
        self.assertEqual(
            export_transcripts.resolve_youtube_marker(mode="youtube", has_ai_summary=False),
            "*",
        )
        self.assertEqual(
            export_transcripts.resolve_youtube_marker(mode="youtube", has_ai_summary=True),
            "*",
        )

    def test_resolve_youtube_marker_uses_summary_presence_for_full_mode(self):
        self.assertEqual(
            export_transcripts.resolve_youtube_marker(mode="full", has_ai_summary=True, used_transcript_lol=True),
            "",
        )
        self.assertEqual(
            export_transcripts.resolve_youtube_marker(mode="full", has_ai_summary=False, used_transcript_lol=True),
            "Txnlol F-YT Only ",
        )
        self.assertEqual(
            export_transcripts.resolve_youtube_marker(mode="full", has_ai_summary=True, used_transcript_lol=False),
            "*",
        )

    def test_unmarked_stem_starts_with_ingestion_date(self):
        self.assertEqual(
            export_transcripts.youtube_ingest_stem(
                "A Video Title",
                marker="",
                ingested_on=date(2026, 7, 15),
            ),
            "20260715 A Video Title",
        )

    def test_txnlol_only_stem_uses_named_marker_after_ingestion_date(self):
        self.assertEqual(
            export_transcripts.youtube_ingest_stem(
                "A Video Title",
                marker="Txnlol F-YT Only ",
                ingested_on=date(2026, 7, 15),
            ),
            "20260715 Txnlol F-YT Only A Video Title",
        )


class TranscriptServiceNamingTests(unittest.TestCase):
    def test_youtube_captions_write_a_date_prefixed_note_and_link(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "z.Ingestion"
            with mock.patch("transcript_server.fetch_youtube_transcript", return_value="caption text"):
                response = TranscriptService(output_dir).save_from_url(
                    "https://youtu.be/abc123",
                    "A Video Title",
                    mode="youtube",
                )

            expected_stem = f"{date.today():%Y%m%d} *A Video Title"
            self.assertEqual(response["stem"], expected_stem)
            self.assertEqual(Path(response["path"]).name, f"{expected_stem}.md")
            self.assertTrue((output_dir / f"{expected_stem}.md").exists())
            daily_note = output_dir.parent / "Daily Notes" / f"{date.today().isoformat()}.md"
            self.assertIn(f"[[z.Ingestion/{expected_stem}]]", daily_note.read_text(encoding="utf-8"))

    def test_full_youtube_transcript_lol_with_ai_summary_write_unmarked_stem(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "z.Ingestion"
            summary_context = SimpleNamespace(
                summary="Useful AI summary",
                client=None,
                recording_id="recording-1",
                summary_failure="",
            )
            with mock.patch.object(
                TranscriptService, "_fetch_from_transcript_lol", return_value="Transcript.lol text"
            ) as fetch_transcript_lol, mock.patch(
                "transcript_server.fetch_youtube_transcript", return_value="caption text"
            ) as fetch_youtube_transcript, mock.patch(
                "transcript_server.prepare_youtube_summary_context", return_value=summary_context
            ):
                response = TranscriptService(output_dir).save_from_url(
                    "https://youtu.be/abc123",
                    "A Video Title",
                    description="Video description",
                    mode="full",
                )

            expected_stem = f"{date.today():%Y%m%d} A Video Title"
            self.assertEqual(response["stem"], expected_stem)
            fetch_transcript_lol.assert_called_once()
            fetch_youtube_transcript.assert_not_called()
            note = (output_dir / f"{expected_stem}.md").read_text(encoding="utf-8")
            self.assertIn("## AI Summary\n\nUseful AI summary", note)

    def test_full_youtube_captions_fallback_with_ai_summary_write_star_stem(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "z.Ingestion"
            summary_context = SimpleNamespace(
                summary="Useful AI summary",
                client=None,
                recording_id="recording-1",
                summary_failure="Transcript.lol unavailable",
            )
            with mock.patch.object(
                TranscriptService,
                "_fetch_from_transcript_lol",
                side_effect=RuntimeError("Transcript.lol quota exhausted"),
            ), mock.patch("transcript_server.fetch_youtube_transcript", return_value="caption text"), mock.patch(
                "transcript_server.prepare_youtube_summary_context", return_value=summary_context
            ):
                response = TranscriptService(output_dir).save_from_url(
                    "https://youtu.be/abc123",
                    "A Video Title",
                    description="Video description",
                    mode="full",
                )

            expected_stem = f"{date.today():%Y%m%d} *A Video Title"
            self.assertEqual(response["stem"], expected_stem)
            note = (output_dir / f"{expected_stem}.md").read_text(encoding="utf-8")
            self.assertIn("Transcript.lol transcript fetch failed; using YouTube captions fallback", note)

    def test_full_youtube_transcript_lol_without_ai_summary_write_txnlol_only_stem(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "z.Ingestion"
            summary_context = SimpleNamespace(
                summary="",
                client=None,
                recording_id="",
                summary_failure="Transcript.lol unavailable",
            )
            with mock.patch.object(
                TranscriptService, "_fetch_from_transcript_lol", return_value="Transcript.lol text"
            ), mock.patch("transcript_server.fetch_youtube_transcript", return_value="caption text"), mock.patch(
                "transcript_server.prepare_youtube_summary_context", return_value=summary_context
            ):
                response = TranscriptService(output_dir).save_from_url(
                    "https://youtu.be/abc123",
                    "A Video Title",
                    mode="full",
                )

            expected_stem = f"{date.today():%Y%m%d} Txnlol F-YT Only A Video Title"
            self.assertEqual(response["stem"], expected_stem)
            note = (output_dir / f"{expected_stem}.md").read_text(encoding="utf-8")
            self.assertIn("Transcript.lol text", note)
            self.assertNotIn("_Transcript unavailable", note)

    def test_full_youtube_transcript_lol_fallback_without_summary_writes_txnlol_only_stem(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "z.Ingestion"
            summary_context = SimpleNamespace(
                summary="",
                client=None,
                recording_id="recording-1",
                summary_failure="",
            )
            with mock.patch.object(
                TranscriptService, "_fetch_from_transcript_lol", return_value="Transcript.lol text"
            ) as fetch_transcript_lol, mock.patch("transcript_server.fetch_youtube_transcript", return_value=None), mock.patch(
                "transcript_server.prepare_youtube_summary_context", return_value=summary_context
            ):
                response = TranscriptService(output_dir).save_from_url(
                    "https://youtu.be/abc123",
                    "A Video Title",
                    mode="full",
                )

            self.assertEqual(
                response["stem"],
                f"{date.today():%Y%m%d} Txnlol F-YT Only A Video Title",
            )
            fetch_transcript_lol.assert_called_once()

    def test_non_youtube_media_keeps_existing_title_only_filename(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "z.Ingestion"
            with mock.patch("transcript_server.fetch_vimeo_captions", return_value="caption text"):
                response = TranscriptService(output_dir).save_from_url(
                    "https://vimeo.com/12345",
                    "A Vimeo Title",
                    mode="full",
                )

            self.assertEqual(response["stem"], "*A Vimeo Title")
            self.assertTrue((output_dir / "*A Vimeo Title.md").exists())


class ArchiveYoutubeNamingTests(unittest.TestCase):
    def test_summary_matching_description_uses_txnlol_only_marker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_root = Path(tmpdir)
            output_dir = vault_root / "z.Ingestion"
            source = vault_root / "Untitled.md"
            source.write_text("https://youtu.be/abc123\n", encoding="utf-8")
            args = SimpleNamespace(dry_run=False, vault_root=vault_root, output_dir=output_dir)
            metadata = {
                "title": "A Video Title",
                "description": "Same text",
                "upload_date": "",
                "language": "en",
                "source_url": "https://youtu.be/abc123",
            }
            summary_context = SimpleNamespace(
                summary="  Same  text ", client=None, recording_id=""
            )

            with mock.patch.object(archive_youtube, "parse_args", return_value=args), mock.patch.object(
                archive_youtube, "fetch_youtube_metadata", return_value=metadata
            ), mock.patch.object(
                archive_youtube, "prepare_youtube_summary_context", return_value=summary_context
            ), mock.patch.object(archive_youtube, "fetch_youtube_transcript", return_value="caption text"):
                archive_youtube.main()

            expected_stem = f"{date.today():%Y%m%d} *A Video Title"
            self.assertTrue((output_dir / f"{expected_stem}.md").exists())
            note = (output_dir / f"{expected_stem}.md").read_text(encoding="utf-8")
            self.assertNotIn("## AI Summary", note)
            self.assertIn("Transcript.lol unavailable; using YouTube captions fallback", note)

    def test_existing_date_prefixed_youtube_note_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_root = Path(tmpdir)
            output_dir = vault_root / "z.Ingestion"
            output_dir.mkdir()
            expected_stem = f"{date.today():%Y%m%d} A Video Title"
            existing = output_dir / f"{expected_stem}.md"
            existing.write_text("existing", encoding="utf-8")
            source = vault_root / "Untitled.md"
            source.write_text("https://youtu.be/abc123\n", encoding="utf-8")

            args = SimpleNamespace(dry_run=False, vault_root=vault_root, output_dir=output_dir)
            with mock.patch.object(archive_youtube, "parse_args", return_value=args), mock.patch.object(
                archive_youtube,
                "fetch_youtube_metadata",
                return_value={
                    "title": "A Video Title",
                    "description": "",
                    "upload_date": "",
                    "language": "en",
                    "source_url": "https://youtu.be/abc123",
                },
            ), mock.patch.object(
                archive_youtube,
                "prepare_youtube_summary_context",
                return_value=SimpleNamespace(summary="", client=None, recording_id=""),
            ), mock.patch.object(
                archive_youtube,
                "fetch_youtube_transcript",
                return_value="caption text",
            ):
                archive_youtube.main()

            self.assertEqual(existing.read_text(encoding="utf-8"), "existing")
            self.assertFalse((output_dir / "*A Video Title.md").exists())
            self.assertTrue((vault_root / "processed" / "Untitled.md").exists())


class DailyAndDirectFlowNamingTests(unittest.TestCase):
    def test_daily_note_replaces_url_with_existing_date_prefixed_link(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_root = Path(tmpdir)
            note_date = date.today().isoformat()
            daily_note = vault_root / "Daily Notes" / f"{note_date}.md"
            daily_note.parent.mkdir()
            url = "https://youtu.be/abc123"
            daily_note.write_text(f"{url}\n", encoding="utf-8")
            output_dir = vault_root / "z.Ingestion"
            output_dir.mkdir()
            expected_stem = f"{date.today():%Y%m%d} *A Video Title"
            (output_dir / f"{expected_stem}.md").write_text("existing", encoding="utf-8")

            args = SimpleNamespace(
                dry_run=False,
                date=note_date,
                vault_root=vault_root,
                output_dir=output_dir,
            )
            with mock.patch.object(daily_note_youtube, "parse_args", return_value=args), mock.patch.object(
                daily_note_youtube,
                "fetch_youtube_metadata",
                return_value={"title": "A Video Title", "description": ""},
            ), mock.patch.object(daily_note_youtube.TranscriptService, "save_from_url") as save_from_url:
                self.assertEqual(daily_note_youtube.main(), 0)

            save_from_url.assert_not_called()
            self.assertIn(
                f"[[z.Ingestion/{expected_stem}]]",
                daily_note.read_text(encoding="utf-8"),
            )

    def test_direct_transcript_append_uses_service_returned_date_prefixed_stem(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            note_path = Path(tmpdir) / "links.md"
            response = {
                "path": str(Path(tmpdir) / "z.Ingestion" / "20260715 A Video Title.md"),
                "stem": "20260715 A Video Title",
                "source": "transcript.lol",
            }
            args = SimpleNamespace(
                urls=["https://youtu.be/abc123"],
                output_dir=Path(tmpdir) / "z.Ingestion",
                append_links_to_note=note_path,
            )
            with mock.patch.object(transcript, "parse_args", return_value=args), mock.patch.object(
                transcript,
                "fetch_media_metadata",
                return_value=("A Video Title", ""),
            ), mock.patch.object(transcript.TranscriptService, "save_from_url", return_value=response):
                self.assertEqual(transcript.main(), 0)

            self.assertIn(
                "[[z.Ingestion/20260715 A Video Title]]",
                note_path.read_text(encoding="utf-8"),
            )


class ExportTranscriptNamingTests(unittest.TestCase):
    def test_backfill_link_parser_reads_date_prefixed_youtube_stems(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            daily_note = Path(tmpdir) / "2026-07-15.md"
            daily_note.write_text(
                "[[z.Ingestion/20260715 *Caption Video]]\n"
                "[[z.Ingestion/20260715 Transcript.lol Video]]\n",
                encoding="utf-8",
            )

            self.assertEqual(
                export_transcripts.parse_daily_note_links(daily_note),
                ["20260715 *Caption Video", "20260715 Transcript.lol Video"],
            )

    def test_youtube_recording_export_writes_date_prefixed_stem(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "z.Ingestion"
            recording = {
                "id": "recording-1",
                "title": "A Video Title",
                "source": "YOUTUBE",
                "sourceUrl": "https://youtu.be/abc123",
                "status": "TRANSCRIPT_COMPLETE",
            }
            args = SimpleNamespace(dry_run=False, output_dir=output_dir, date_from=None, date_to=None)
            client = mock.Mock()
            expected_stem = f"{date.today():%Y%m%d} Txnlol F-YT Only A Video Title"

            with mock.patch.object(export_transcripts, "parse_args", return_value=args), mock.patch.object(
                export_transcripts, "load_env", return_value={}
            ), mock.patch.object(export_transcripts, "TranscriptClient", return_value=client), mock.patch.object(
                export_transcripts, "list_recordings", return_value=[recording]
            ), mock.patch.object(
                export_transcripts, "get_transcript_text", return_value=("text", "transcript.lol", "")
            ):
                with redirect_stdout(io.StringIO()):
                    export_transcripts.main()

            self.assertTrue((output_dir / f"{expected_stem}.md").exists())
            daily_note = output_dir.parent / "Daily Notes" / f"{date.today().isoformat()}.md"
            self.assertIn(f"[[z.Ingestion/{expected_stem}]]", daily_note.read_text(encoding="utf-8"))

    def test_non_youtube_recording_export_keeps_title_only_filename(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "z.Ingestion"
            recording = {
                "id": "recording-1",
                "title": "A Meeting",
                "source": "MEETING",
                "status": "TRANSCRIPT_COMPLETE",
            }
            args = SimpleNamespace(dry_run=False, output_dir=output_dir, date_from=None, date_to=None)
            client = mock.Mock()

            with mock.patch.object(export_transcripts, "parse_args", return_value=args), mock.patch.object(
                export_transcripts, "load_env", return_value={}
            ), mock.patch.object(export_transcripts, "TranscriptClient", return_value=client), mock.patch.object(
                export_transcripts, "list_recordings", return_value=[recording]
            ), mock.patch.object(
                export_transcripts, "get_transcript_text", return_value=("text", "transcript.lol", "")
            ):
                with redirect_stdout(io.StringIO()):
                    export_transcripts.main()

            self.assertTrue((output_dir / "A Meeting.md").exists())


if __name__ == "__main__":
    unittest.main()

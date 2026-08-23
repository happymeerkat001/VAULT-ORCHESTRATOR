import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_DIR = REPO_ROOT / "cli"
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

import inbox_youtube


URL = "https://youtu.be/abc123"
METADATA = {"title": "A Shared Video", "description": "Video description"}


def make_args(vault_root: Path, *, dry_run: bool = False, force: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        dry_run=dry_run,
        force=force,
        vault_root=vault_root,
        output_dir=vault_root / "z.Ingestion",
    )


def save_transcript(output_dir: Path, url: str, title: str, **_kwargs: object) -> dict[str, str]:
    stem = f"{date.today():%Y%m%d} {title}"
    destination = output_dir / f"{stem}.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("transcript", encoding="utf-8")
    return {"path": str(destination), "stem": stem, "source": "YouTube captions"}


class InboxYouTubeTests(unittest.TestCase):
    def test_url_only_note_writes_transcript_links_daily_note_and_moves_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_root = Path(tmpdir)
            source = vault_root / "Inbox" / "Some Shared Title.md"
            source.parent.mkdir()
            source.write_text(f"{URL}\n", encoding="utf-8")
            args = make_args(vault_root)

            with mock.patch.object(inbox_youtube, "parse_args", return_value=args), mock.patch.object(
                inbox_youtube, "fetch_youtube_metadata", return_value=METADATA
            ), mock.patch.object(
                inbox_youtube.TranscriptService,
                "save_from_url",
                autospec=True,
                side_effect=lambda service, **kwargs: save_transcript(service.output_dir, **kwargs),
            ) as save_from_url:
                self.assertEqual(inbox_youtube.main(), 0)

            stem = f"{date.today():%Y%m%d} A Shared Video"
            self.assertTrue((vault_root / "z.Ingestion" / f"{stem}.md").exists())
            daily_note = vault_root / "Daily Notes" / f"{date.today().isoformat()}.md"
            self.assertIn(f"[[z.Ingestion/{stem}]]", daily_note.read_text(encoding="utf-8"))
            self.assertFalse(source.exists())
            self.assertTrue((vault_root / "processed" / source.name).exists())
            save_from_url.assert_called_once_with(
                mock.ANY,
                url=URL,
                title="A Shared Video",
                description="Video description",
                ai_summary="",
                mode="full",
                daily_note_path=daily_note,
            )

    def test_note_with_extra_text_processes_url_and_retains_non_url_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_root = Path(tmpdir)
            source = vault_root / "Inbox" / "Full Text Share.md"
            source.parent.mkdir()
            source.write_text(f"Imported page text\n{URL}\nMore text\n", encoding="utf-8")
            args = make_args(vault_root)

            with mock.patch.object(inbox_youtube, "parse_args", return_value=args), mock.patch.object(
                inbox_youtube, "fetch_youtube_metadata", return_value=METADATA
            ), mock.patch.object(
                inbox_youtube.TranscriptService,
                "save_from_url",
                autospec=True,
                side_effect=lambda service, **kwargs: save_transcript(service.output_dir, **kwargs),
            ):
                self.assertEqual(inbox_youtube.main(), 0)

            self.assertTrue(source.exists())
            self.assertEqual(source.read_text(encoding="utf-8"), "Imported page text\nMore text\n")
            self.assertFalse((vault_root / "processed" / source.name).exists())

    def test_z_ingestion_source_note_retains_ai_summary_and_moves_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_root = Path(tmpdir)
            source = vault_root / "z.Ingestion" / "Shared From iPhone.md"
            source.parent.mkdir(parents=True)
            source.write_text(
                "Shared page text\n"
                f"{URL}\n"
                "\n"
                "## AI Summary\n"
                "Important summary line one.\n"
                "Important summary line two.\n",
                encoding="utf-8",
            )
            args = make_args(vault_root)
            captured: dict[str, object] = {}

            def fake_save(service: object, **kwargs: object) -> dict[str, str]:
                captured.update(kwargs)
                return save_transcript(service.output_dir, **kwargs)

            with mock.patch.object(inbox_youtube, "parse_args", return_value=args), mock.patch.object(
                inbox_youtube, "fetch_youtube_metadata", return_value=METADATA
            ), mock.patch.object(
                inbox_youtube.TranscriptService,
                "save_from_url",
                autospec=True,
                side_effect=fake_save,
            ):
                self.assertEqual(inbox_youtube.main(), 0)

            self.assertEqual(
                captured["ai_summary"],
                "Important summary line one.\nImportant summary line two.",
            )
            self.assertTrue((vault_root / "processed" / source.name).exists())
            self.assertFalse(source.exists())

    def test_share_sheet_note_with_local_transcript_skips_save_from_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_root = Path(tmpdir)
            source = vault_root / "z.Ingestion" / "Shared From iPhone.md"
            source.parent.mkdir(parents=True)
            source.write_text(
                f"![]({URL})\n"
                "\n"
                "A truncated description snippet...\n"
                "\n"
                "## Transcript\n"
                "\n"
                "**0:00** · First line.\n"
                "**0:03** · Second line.\n",
                encoding="utf-8",
            )
            args = make_args(vault_root)

            with mock.patch.object(inbox_youtube, "parse_args", return_value=args), mock.patch.object(
                inbox_youtube, "fetch_youtube_metadata", return_value=METADATA
            ), mock.patch.object(inbox_youtube.TranscriptService, "save_from_url") as save_from_url:
                self.assertEqual(inbox_youtube.main(), 0)

            save_from_url.assert_not_called()
            stem = f"{date.today():%Y%m%d} *A Shared Video"
            note_path = vault_root / "z.Ingestion" / f"{stem}.md"
            self.assertTrue(note_path.exists())
            note_content = note_path.read_text(encoding="utf-8")
            self.assertIn("First line.", note_content)
            self.assertIn("Second line.", note_content)
            daily_note = vault_root / "Daily Notes" / f"{date.today().isoformat()}.md"
            self.assertIn(f"[[z.Ingestion/{stem}]]", daily_note.read_text(encoding="utf-8"))
            self.assertTrue((vault_root / "processed" / source.name).exists())
            self.assertFalse(source.exists())

    def test_share_sheet_note_with_empty_transcript_heading_falls_back_to_save_from_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_root = Path(tmpdir)
            source = vault_root / "z.Ingestion" / "Shared From iPhone.md"
            source.parent.mkdir(parents=True)
            source.write_text(f"![]({URL})\n\n## Transcript\n", encoding="utf-8")
            args = make_args(vault_root)

            with mock.patch.object(inbox_youtube, "parse_args", return_value=args), mock.patch.object(
                inbox_youtube, "fetch_youtube_metadata", return_value=METADATA
            ), mock.patch.object(
                inbox_youtube.TranscriptService,
                "save_from_url",
                autospec=True,
                side_effect=lambda service, **kwargs: save_transcript(service.output_dir, **kwargs),
            ) as save_from_url:
                self.assertEqual(inbox_youtube.main(), 0)

            save_from_url.assert_called_once()

    def test_share_sheet_dry_run_reports_local_transcript_action(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_root = Path(tmpdir)
            source = vault_root / "z.Ingestion" / "Shared From iPhone.md"
            source.parent.mkdir(parents=True)
            source.write_text(f"![]({URL})\n\n## Transcript\n\nSome transcript text.\n", encoding="utf-8")
            args = make_args(vault_root, dry_run=True)

            with mock.patch.object(inbox_youtube, "parse_args", return_value=args), mock.patch.object(
                inbox_youtube, "fetch_youtube_metadata", return_value=METADATA
            ), redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(inbox_youtube.main(), 0)

            self.assertIn("would ingest from local transcript", stdout.getvalue())
            self.assertTrue(source.exists())
            self.assertEqual(len(list((vault_root / "z.Ingestion").glob("*.md"))), 1)

    def test_unrelated_note_is_left_untouched(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_root = Path(tmpdir)
            source = vault_root / "Inbox" / "Article.md"
            source.parent.mkdir()
            source.write_text("https://example.com/article\n", encoding="utf-8")
            args = make_args(vault_root)

            with mock.patch.object(inbox_youtube, "parse_args", return_value=args):
                self.assertEqual(inbox_youtube.main(), 0)

            self.assertEqual(source.read_text(encoding="utf-8"), "https://example.com/article\n")

    def test_duplicate_share_moves_second_source_without_new_transcript(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_root = Path(tmpdir)
            source = vault_root / "Inbox" / "Duplicate Share.md"
            source.parent.mkdir()
            source.write_text(f"{URL}\n", encoding="utf-8")
            output_dir = vault_root / "z.Ingestion"
            output_dir.mkdir()
            stem = f"{date.today():%Y%m%d} A Shared Video"
            (output_dir / f"{stem}.md").write_text("existing", encoding="utf-8")
            args = make_args(vault_root)

            with mock.patch.object(inbox_youtube, "parse_args", return_value=args), mock.patch.object(
                inbox_youtube, "fetch_youtube_metadata", return_value=METADATA
            ), mock.patch.object(inbox_youtube.TranscriptService, "save_from_url") as save_from_url:
                self.assertEqual(inbox_youtube.main(), 0)

            save_from_url.assert_not_called()
            self.assertTrue((vault_root / "processed" / source.name).exists())

    def test_dry_run_prints_actions_without_writing_or_moving(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_root = Path(tmpdir)
            source = vault_root / "Inbox" / "Some Shared Title.md"
            source.parent.mkdir()
            source.write_text(f"{URL}\n", encoding="utf-8")
            args = make_args(vault_root, dry_run=True)

            with mock.patch.object(inbox_youtube, "parse_args", return_value=args), mock.patch.object(
                inbox_youtube, "fetch_youtube_metadata", return_value=METADATA
            ), redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(inbox_youtube.main(), 0)

            self.assertIn("would ingest", stdout.getvalue())
            self.assertTrue(source.exists())
            self.assertFalse((vault_root / "z.Ingestion").exists())
            self.assertFalse((vault_root / "processed").exists())

    def test_transient_read_lock_leaves_note_for_retry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_root = Path(tmpdir)
            source = vault_root / "Inbox" / "Locked.md"
            source.parent.mkdir()
            source.write_text(f"{URL}\n", encoding="utf-8")
            args = make_args(vault_root)

            with mock.patch.object(inbox_youtube, "parse_args", return_value=args), mock.patch.object(
                inbox_youtube, "read_text_with_retry", side_effect=OSError(11, "temporarily locked")
            ), redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(inbox_youtube.main(), 1)

            self.assertIn("transient error", stderr.getvalue())
            self.assertTrue(source.exists())

    def test_failed_transcript_fetch_leaves_note_and_logs_reason(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_root = Path(tmpdir)
            source = vault_root / "Inbox" / "Private Video.md"
            source.parent.mkdir()
            source.write_text(f"{URL}\n", encoding="utf-8")
            args = make_args(vault_root)

            with mock.patch.object(inbox_youtube, "parse_args", return_value=args), mock.patch.object(
                inbox_youtube, "fetch_youtube_metadata", return_value=METADATA
            ), mock.patch.object(
                inbox_youtube.TranscriptService, "save_from_url", side_effect=RuntimeError("Private video")
            ), redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(inbox_youtube.main(), 1)

            self.assertIn("Private video", stderr.getvalue())
            self.assertTrue(source.exists())
            self.assertFalse((vault_root / "processed" / source.name).exists())


class ExtractBareYoutubeUrlsTests(unittest.TestCase):
    def test_image_embedded_url_is_extracted(self):
        content = f"![]({URL})\n"
        self.assertEqual(inbox_youtube.extract_bare_youtube_urls(content), [URL])

    def test_plain_markdown_link_is_not_extracted(self):
        content = f"[Some Link]({URL})\n"
        self.assertEqual(inbox_youtube.extract_bare_youtube_urls(content), [])

    def test_wikilink_wrapped_url_is_not_extracted(self):
        content = f"[[{URL}]]\n"
        self.assertEqual(inbox_youtube.extract_bare_youtube_urls(content), [])

    def test_image_embedded_and_bare_url_are_both_extracted(self):
        other_url = "https://youtu.be/def456"
        content = f"![]({URL})\n{other_url}\n"
        self.assertEqual(inbox_youtube.extract_bare_youtube_urls(content), [URL, other_url])


if __name__ == "__main__":
    unittest.main()

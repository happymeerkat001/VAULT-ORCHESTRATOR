# fix: Ingest the new iOS Share Sheet note format directly, without transcript.lol

**Created:** 2026-08-23

---

## Summary

The reported symptom ("z.Ingestion no longer works after the vault path change") is not a path bug. `DEFAULT_OUTPUT_DIR` in `cli/export_transcripts.py:26-29` already resolves via `AI_VAULT_PATH` (or its `~/Obsidian Vaults/AI-Vault` default) to the current vault location, and both `cli/inbox_youtube.py` and `chrome-extension`'s server bridge (`cli/transcript_server.py`) already derive their paths from it. Confirmed with the user: notes do land in `z.Ingestion/`, they just never get processed into a transcript note.

Root cause: Obsidian's iOS Share Sheet now writes a different note shape than `cli/inbox_youtube.py` expects. Instead of a bare YouTube URL line, the shared note embeds the URL as a markdown image (`![](https://www.youtube.com/watch?v=...)`) and already contains a full pre-extracted transcript with timestamps under a `## Transcript` heading. `is_bare_youtube_url()` (`cli/daily_note_youtube.py:77-85`) explicitly treats any URL preceded by `](` as *not* bare — a rule written to skip already-linked/already-processed references — so `extract_bare_youtube_urls()` finds zero URLs in these notes and `cli/inbox_youtube.py:135-137` silently skips them (`if not urls: continue`). They sit in `z.Ingestion/` forever.

This plan makes `cli/inbox_youtube.py` recognize the new image-embedded URL format, and — since the note already contains a complete transcript — build the destination note directly from that local content instead of routing through `TranscriptService.save_from_url()` (which would otherwise try transcript.lol / YouTube captions again). This is both the fix for the immediate bug and a resiliency win: these notes stop depending on an external transcript.lol round trip entirely.

The Chrome extension path (`chrome-extension/content.js` → `cli/transcript_server.py`) writes plain bare URLs via `/daily-note`, is unaffected by this format change, and already resolves the correct vault path — it needs verification, not a code fix (see Verification).

---

## Problem Frame

- **Actors:** iOS Share Sheet (writes notes into `z.Ingestion/` or `Inbox/`), `cli/inbox_youtube.py` (polls and ingests them), Chrome extension + `cli/transcript_server.py` (separate desktop capture path).
- **Trigger:** Obsidian's Share Sheet "Full Text" capture now produces a different note body than before — it wraps the URL as an image embed and includes a ready-made transcript.
- **Current behavior:** these notes are invisible to `extract_bare_youtube_urls()` and never processed.
- **Desired behavior:** these notes are detected, converted into a normal titled transcript note in `z.Ingestion/`, linked from today's daily note, and the source note moved to `processed/` — using the transcript already present in the note rather than re-fetching one.

## Requirements

- R1: `cli/inbox_youtube.py` detects a YouTube URL embedded as `![](url)` in a source note (new Share Sheet format), in addition to the existing bare-URL case.
- R2: When a source note already contains a non-empty `## Transcript` section, that content is used as the transcript body for the output note — no call to `TranscriptService.save_from_url()` for that URL.
- R3: The existing bare-URL path (no local transcript present) continues to work unchanged, routing through `TranscriptService.save_from_url()` as today.
- R4: Output note, dedup (`existing_destination`), daily-note linking, source-file update/move, and `--dry-run`/`--force` behavior stay consistent with current `cli/inbox_youtube.py` conventions for both paths.
- R5: The Chrome extension → `transcript_server.py` desktop capture path is verified working end-to-end against the current vault path (no code change expected).

Non-requirement: reproducing transcript.lol's AI-generated summary for share-sheet notes with a local transcript — no summary exists to reuse, and generating one would reintroduce the external dependency this fix removes. Left for a future task if wanted.

## Key Technical Decisions

- **Reuse local transcript instead of re-fetching.** The share note already contains a full transcript; calling `TranscriptService.save_from_url()` would discard it and re-hit transcript.lol/YouTube captions, reintroducing the exact dependency implicated in the bug report ("doesn't go through transcript.lol"). Building the note directly from local content is simpler and more reliable.
- **Distinguish image-embed from markdown-link exclusion, not remove it.** `is_bare_youtube_url()`'s existing exclusion for `[text](url)` protects against re-ingesting URLs that are already rendered as links elsewhere (e.g. citations). Only the `![...](url)` image-embed form is new-format-ingestable; a plain link should stay excluded. Implement this as a new inbox-local predicate rather than changing the shared `is_bare_youtube_url()`, since `daily_note_youtube.py` / `scrape_notes.py` consumers should not change behavior.
- **Metadata still comes from `fetch_youtube_metadata()`.** The note's own leading text is a truncated description snippet, not a reliable title. Keep using the existing yt-dlp metadata fetch (already imported in `cli/inbox_youtube.py`) for `title`/`description`, matching the bare-URL path.
- **Marker/dedup logic unchanged.** `resolve_youtube_marker(mode="full", has_ai_summary=False, used_transcript_lol=False)` already returns `"*"` for this case under the current (uncommitted) `cli/export_transcripts.py` logic — no new marker variant needed.

---

## Implementation Units

### U1. Detect image-embedded YouTube URLs as ingestable

**Goal:** `cli/inbox_youtube.py` recognizes `![](youtube-url)` as a URL to ingest, while continuing to skip `[text](youtube-url)` and `[[wikilink]]` forms.

**Requirements:** R1

**Dependencies:** none

**Files:**
- `cli/inbox_youtube.py` — replace the `is_bare_youtube_url` import/usage with a new local predicate; add `extract_ingestable_youtube_urls()` (or extend `extract_bare_youtube_urls()` in place) to use it.
- `tests/test_inbox_youtube.py` — new test cases.

**Approach:** Add a local function, e.g. `is_ingestable_youtube_url(line, match)`, mirroring `is_bare_youtube_url()`'s bracket-context checks but flipping the outcome specifically for image-embed syntax: when `before` ends with `](`, look at the character immediately before the matching `[` — if it's `!`, the URL is an image embed and should be treated as ingestable; otherwise keep the current exclusion. `[[wikilink]]` exclusion stays as-is.

**Technical design** (directional, not final code):
```
is_ingestable(line, match):
  before, after = split at match
  if "[[" in before and "]]" in after: return False        # already a wikilink
  if before.rstrip() ends with "](":
      find start of the "[" this "](" belongs to
      return True if char before that "[" is "!"  else False   # image-embed vs plain link
  return True                                                # bare URL, unchanged
```

**Patterns to follow:** `is_bare_youtube_url()` in `cli/daily_note_youtube.py:77-85` for the bracket-scanning style.

**Test scenarios:**
- Happy path: note containing only `![](https://www.youtube.com/watch?v=WCwT4gWpHmI)` → URL is extracted.
- Regression: note containing `[Some Link](https://www.youtube.com/watch?v=abc)` (plain markdown link) → URL is NOT extracted (preserves prior exclusion).
- Regression: note containing `[[z.Ingestion/Some Title]]` wikilink near a URL → URL is NOT extracted.
- Edge case: note with both an image-embedded URL and a second bare URL on another line → both extracted.

**Verification:** New/updated unit tests in `tests/test_inbox_youtube.py` pass; `python3 -m pytest tests/test_inbox_youtube.py -q` green.

---

### U2. Build the output note from the source note's own transcript, skipping transcript.lol

**Goal:** When a source note has a non-empty `## Transcript` section, `cli/inbox_youtube.py` writes the destination note using that content directly, without calling `TranscriptService.save_from_url()`.

**Requirements:** R2, R3, R4

**Dependencies:** U1

**Files:**
- `cli/inbox_youtube.py` — add `extract_local_transcript(content) -> str` (mirrors the existing `extract_ai_summary()` heading-scan pattern); branch in `main()`'s per-URL loop on whether a local transcript was found.
- `cli/export_transcripts.py` — no change expected; import `build_markdown` and `resolve_youtube_marker` into `cli/inbox_youtube.py` (already exports both).
- `tests/test_inbox_youtube.py` — new test cases.

**Approach:**
1. Add `extract_local_transcript()` using the same scan style as `extract_ai_summary()` (`cli/inbox_youtube.py:73-84`), keyed on a `## Transcript` (or `## YouTube Transcript`) heading instead of `## AI Summary`.
2. In the per-URL loop (`cli/inbox_youtube.py:144-189`), after computing `metadata` and the dedup `destination`, check `local_transcript = extract_local_transcript(content)`. If non-empty:
   - Skip the `TranscriptService.save_from_url()` call entirely.
   - Compute `stem = youtube_ingest_stem(metadata["title"], marker=resolve_youtube_marker(mode="full", has_ai_summary=False, used_transcript_lol=False))`.
   - Build the note body with `build_markdown({"title": metadata["title"], "sourceUrl": url}, local_transcript, transcript_source="YouTube Share Sheet", description=metadata["description"])`.
   - Write it to `output_dir / f"{stem}.md"` via `write_text_with_retry` (matches the retry convention used elsewhere in this file).
   - Call `ensure_daily_note_link(daily_note_path, stem, metadata["title"])`, mark the URL as succeeded, increment the `written` counter — mirroring what the existing `service.save_from_url()` branch already does.
3. If `local_transcript` is empty, fall through to the existing `TranscriptService.save_from_url()` call unchanged (R3).
4. `--dry-run` should report which branch would be taken (e.g. "would ingest from local transcript" vs "would ingest via transcript.lol").

**Patterns to follow:** `extract_ai_summary()` for the heading-scan; the existing `service.save_from_url()` branch (`cli/inbox_youtube.py:167-181`) for how a successful write updates `succeeded_urls`, `written`, and calls `ensure_daily_note_link`.

**Test scenarios:**
- Happy path: source note with `![](url)` + a populated `## Transcript` section → output note is written with that transcript content, `TranscriptService.save_from_url` is NOT called (mock/assert not-called), daily note gets linked, source moved to `processed/`.
- Happy path: source note with bare URL only, no `## Transcript` section → existing behavior preserved, `TranscriptService.save_from_url` IS called.
- Edge case: `## Transcript` heading present but with no content before the next heading/EOF → treated as empty, falls back to the `save_from_url` path (R3 requires this doesn't crash).
- Edge case: dedup — a matching output file already exists (`existing_destination` returns non-None) and `--force` is not set → `normalized_existing` path used, no duplicate written, regardless of which format the source note used.
- Integration: `--dry-run` on a local-transcript note prints the intended action without writing or moving any files.

**Verification:** `python3 -m pytest tests/test_inbox_youtube.py -q` green; manually drop the example note (`![](https://www.youtube.com/watch?v=WCwT4gWpHmI)` + `## Transcript` body) into `z.Ingestion/` in a scratch vault copy and confirm `python3 cli/inbox_youtube.py --dry-run` reports it, then a real run produces a titled note and moves the source to `processed/`.

---

## Scope Boundaries

**In scope:** detecting and ingesting the new Share Sheet note format in `cli/inbox_youtube.py`; verifying the Chrome extension path still works against the current vault path.

**Out of scope / Deferred to Follow-Up Work:**
- Generating an AI summary for share-sheet notes that already carry a local transcript (would reintroduce the transcript.lol dependency this fix removes).
- `cli/backfill_youtube_ai_summary.py` and `docs/PROJECT_COMPASS.md` — both already present as uncommitted work in the tree, unrelated to this bug; left untouched.
- Any further work on the transcript.lol-first-with-YouTube-captions-fallback logic already in progress (uncommitted) in `cli/export_transcripts.py` / `cli/transcript_server.py` — that's a separate, already-in-flight change and this plan doesn't depend on or modify it beyond importing `resolve_youtube_marker` as it exists today.

## Verification (R5 — Chrome extension)

No code change is expected here; `chrome-extension/content.js` posts to `cli/transcript_server.py`, which resolves `DEFAULT_OUTPUT_DIR` the same way as `cli/inbox_youtube.py` and is unaffected by the note-format change. Verify by running `python3 cli/transcript_server.py`, clicking "📝 Transcript.lol" or "▶ YouTube Only" on a real YouTube video page, and confirming the resulting note lands in the current `z.Ingestion/` and links from today's daily note.

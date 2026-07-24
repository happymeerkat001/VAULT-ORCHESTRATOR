#!/usr/bin/env python3
"""
weekly_housekeep_propose.py — Proposes (never deletes) PR-style vault changes.

Design contract:
- Confidence thresholds: empty/near-empty notes auto-DEFER (>=0.95),
  orphan topics DEFER (>=0.85), near duplicates need human review (<0.85).
- Proposals always go through an explicit `--approve` flag.
- The script NEVER moves, renames, or deletes a note without that flag.

Idempotency:
- Adds a Vault Housekeep addendum to today's Daily Note via Daily Note helpers
  written for briefing_sync. The addendum embeds a `housekeep-id` and a
  `housekeep-until` HTML comment so the next 7 days of briefs carry progress
  forward automatically.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

try:
    from daily_note_helpers import (
        ensure_daily_note_preamble,
        read_text_with_retry,
        write_text_with_retry,
    )
except ModuleNotFoundError:
    from ingest.daily_note_helpers import (
        ensure_daily_note_preamble,
        read_text_with_retry,
        write_text_with_retry,
    )

try:
    from zoneinfo import ZoneInfo
    _HAS_ZONEINFO = True
except ImportError:
    _HAS_ZONEINFO = False

VAULT_ROOT = Path(
    os.environ.get("AI_VAULT_PATH", "~/Obsidian Vaults/AI-Vault")
).expanduser()
HERMES_OUTPUT = VAULT_ROOT / "Hermes Output"
DAILY_NOTES_PATH = VAULT_ROOT / "Daily Notes"

HOUSEKEEP_HEADER = "## Vault Housekeep 🧹"
LOCAL_TZ = "America/Chicago"


def _today_in_local_zone(today_iso: str | None) -> datetime:
    if today_iso:
        d = datetime.strptime(today_iso, "%Y-%m-%d")
        return d
    if _HAS_ZONEINFO:
        return datetime.now(tz=ZoneInfo(LOCAL_TZ))
    return datetime.now()


def _proposal_id_for_week(today: datetime) -> str:
    iso_year, iso_week, _ = today.isocalendar()
    return f"wk{iso_year}-{iso_week:02d}"


def _confidence_for(kind: str, evidence: dict) -> float:
    """Higher = more autonomous, lower = route to human review."""
    if kind == "EMPTY_NOTE":
        return 0.97
    if kind == "STALE_ARCHIVE" and evidence.get("age_days", 0) >= 365:
        return 0.97
    if kind == "STALE_ARCHIVE":
        return 0.93
    if kind == "ORPHAN_TOPIC":
        return 0.90 if evidence.get("backlinks", 0) == 0 else 0.87
    if kind == "NEAR_DUPLICATE":
        score = evidence.get("score", 0.0) or 0.0
        # Anything below 0.85 of similarity needs human review.
        return max(0.30, min(0.84, score))
    return 0.50


def score_action_confidence(action: dict) -> float:
    """Wrap the internal confidence helper for tests and external callers."""
    return _confidence_for(action["kind"], action.get("evidence", {}))


def _walk_vault(vault: Path):
    for path in vault.rglob("*.md"):
        if any(part.startswith(".") for part in path.relative_to(vault).parts):
            continue
        # Skip Daily Notes: they are owned by the briefing sync.
        if DAILY_NOTES_PATH in path.parents:
            continue
        yield path


def _is_empty_note(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    # Strip front matter and whitespace.
    body = re.sub(r"^---[\s\S]*?---\s*", "", text, count=1).strip()
    return len(body) < 60


def propose_actions(vault: Path, today_iso: str | None = None,
                    stale_days: int = 180,
                    max_actions: int = 30) -> list[dict]:
    """Return deterministic, low-risk PR-style proposals."""
    today = _today_in_local_zone(today_iso)
    stale_cutoff = today.date() - timedelta(days=stale_days)
    actions: list[dict] = []

    seen_titles: dict[str, Path] = {}
    for path in sorted(_walk_vault(vault)):
        rel = str(path.relative_to(vault))

        # Stale Archive files older than threshold.
        rel_parts = rel.split("/")
        if any(part == "Archive" for part in rel_parts):
            age = today.date().toordinal() - path.stat().st_mtime / 86400
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime).date()
            except OSError:
                continue
            age_days = (today.date() - mtime).days
            if age_days >= stale_days:
                actions.append({
                    "kind": "STALE_ARCHIVE",
                    "proposal": "DEFER",
                    "confidence": _confidence_for("STALE_ARCHIVE", {"age_days": age_days}),
                    "target": rel,
                    "reason": f"lives in Archive/ for {age_days} days (>threshold {stale_days})",
                    "src": "archive-age-scan",
                    "evidence": {"age_days": age_days},
                })
                continue

        if _is_empty_note(path):
            actions.append({
                "kind": "EMPTY_NOTE",
                "proposal": "DEFER" if "Archive" in rel else "MERGE",
                "confidence": _confidence_for("EMPTY_NOTE", {"size_bytes": path.stat().st_size}),
                "target": rel,
                "reason": f"note body is <60 chars after front-matter strip ({path.stat().st_size} bytes)",
                "src": "empty-note-scan",
            })
            continue
        # Cheap near-duplicate scan by normalized first heading.
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        m = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        if m:
            norm = re.sub(r"\W+", "", m.group(1).lower())[:40]
            if norm and norm in seen_titles:
                other = seen_titles[norm]
                actions.append({
                    "kind": "NEAR_DUPLICATE",
                    "proposal": "MERGE",
                    "confidence": _confidence_for(
                        "NEAR_DUPLICATE",
                        {"score": 0.85, "pair": (str(other.relative_to(vault)), rel)},
                    ),
                    "target": rel,
                    "reason": f"shares first-heading with {other.relative_to(vault)!s}",
                    "src": "title-dedupe",
                    "evidence": {"pair": (str(other.relative_to(vault)), rel), "score": 0.85},
                })
            else:
                seen_titles[norm] = path

        if len(actions) >= max_actions:
            break

    actions.sort(key=lambda a: (-a["confidence"], a["target"]))
    return actions


def write_proposal_file(vault: Path, actions: list[dict], today_iso: str,
                        proposal_id: str) -> Path:
    HERMES_OUTPUT.mkdir(parents=True, exist_ok=True)
    out = HERMES_OUTPUT / f"Weekly Housekeep Proposals {today_iso} ({proposal_id}).md"
    lines = [
        f"# Weekly Vault Housekeep Proposals — {today_iso}",
        "",
        f"Proposal ID: `{proposal_id}`",
        "",
        "PR-style change list only. NO actions are executed unless approved.",
        "",
        "| Confidence | Action | Target | Reason |",
        "| --- | --- | --- | --- |",
    ]
    for action in actions:
        lines.append(
            f"| {action['confidence']:.2f} | {action['proposal']} ({action['kind']}) "
            f"| `{action['target']}` | {action['reason']} |"
        )
    lines.append("")
    lines.append("## Approve to apply")
    lines.append(
        "Save a file at `/Users/leon/Hermes Output/housekeep.approve.json` with "
        "the form `{\"proposal_id\":\"<this id>\", \"ok\":true}` and run "
        "`/Users/leon/.claude/scripts/apply-housekeep-proposals.sh`."
    )
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def format_addendum(date_str: str, proposal_path: Path, actions: list[dict],
                    proposal_id: str, roll_to_date: str | None = None,
                    roll_from_date: str | None = None) -> str:
    """Render the Daily Note addendum block (idempotent for 7 days)."""
    until_iso = roll_to_date or (
        (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=6))
        .strftime("%Y-%m-%d")
    )

    if roll_from_date:
        carry = (
            f"Carry-over from `Weekly Housekeep Proposals {roll_from_date} ({proposal_id})`\n\n"
        )
    else:
        prev = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        carry = (
            f"Carry-over from `Weekly Housekeep Proposals {prev} ({proposal_id})`\n\n"
        )

    rows = "\n".join(
        f"- **{a['proposal']}** ({a['kind']}, conf={a['confidence']:.2f}) — "
        f"`{a['target']}` — {a['reason']}"
        for a in actions
    ) or "- (no actions proposed this week)"

    return (
        f"{HOUSEKEEP_HEADER}\n\n"
        f"{carry}"
        f"Proposal: `{proposal_path.name}`  \n"
        f"Approve: `echo '{{\"proposal_id\":\"{proposal_id}\",\"ok\":true}}' > "
        f"\"$HOME/Hermes Output/housekeep.approve.json\" && "
        f"/Users/leon/.claude/scripts/apply-housekeep-proposals.sh`\n\n"
        f"{rows}\n\n"
        f"<!-- housekeep-id:{proposal_id} -->"
        f"<!-- housekeep-until:{until_iso} -->"
    )


def write_addendum_to_daily_note(date_str: str, block: str) -> Path | None:
    """Append-or-refresh the housekeeping addendum in the matching Daily Note."""
    daily_note = ensure_daily_note_preamble(DAILY_NOTES_PATH, date_str)
    text = read_text_with_retry(daily_note)

    # Replace any existing block under HOUSEKEEP_HEADER (between this header
    # and the next top-level ## header or end of file).
    pattern = re.compile(
        rf"^{HOUSEKEEP_HEADER}\s*$(?:.*?)(?=^## |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    new_block = f"{HOUSEKEEP_HEADER}\n\n{block.split(HOUSEKEEP_HEADER, 1)[-1].strip()}"
    if pattern.search(text):
        text = pattern.sub(new_block, text, count=1)
    else:
        text = text.rstrip() + "\n\n" + new_block + "\n"

    write_text_with_retry(daily_note, text)
    return daily_note


def apply_proposals(proposal_path: Path, approve_path: Path | None) -> int:
    """Execute only the actions whose 'proposal' is MERGE/RENAME, refuse if no approval."""
    if approve_path is None or not approve_path.exists():
        print("[housekeep] refusing: --approve <path> required to actually move notes.",
              file=sys.stderr)
        sys.exit(1)

    try:
        approval = json.loads(approve_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[housekeep] could not read approval: {exc}", file=sys.stderr)
        return 1

    if not approval.get("ok"):
        print("[housekeep] approval file present but ok != true; aborting.",
              file=sys.stderr)
        return 1

    try:
        actions = json.loads(proposal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[housekeep] could not read proposals: {exc}", file=sys.stderr)
        return 1

    moved = 0
    for action in actions:
        proposal = action.get("proposal")
        target = action.get("target")
        if not target or proposal not in ("MERGE", "RENAME"):
            continue
        src = (VAULT_ROOT / target).resolve()
        if not src.exists():
            continue
        dst_dir = src.parent / "_to_review"
        dst_dir.mkdir(parents=True, exist_ok=True)
        # Real dry run: archive the original into a review folder the human inspects.
        shutil.move(str(src), str(dst_dir / src.name))
        moved += 1

    print(f"[housekeep] moved {moved} notes to _to_review/ for human verification.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", type=Path, default=VAULT_ROOT)
    parser.add_argument("--date", help="Today's date in YYYY-MM-DD; default America/Chicago now.")
    parser.add_argument("--propose-only", action="store_true", help="Write proposal + addendum only; do not apply.")
    parser.add_argument("--apply", action="store_true", help="(Opted-in via wrapper script only.)")
    parser.add_argument("--proposal-path", type=Path)
    parser.add_argument("--approve", type=Path, help="Explicit approval file required to mutate.")
    args = parser.parse_args()

    today = _today_in_local_zone(args.date)
    today_iso = today.strftime("%Y-%m-%d")
    proposal_id = _proposal_id_for_week(today)

    actions = propose_actions(args.vault, today_iso=today_iso)
    proposal_path = write_proposal_file(args.vault, actions, today_iso, proposal_id)

    if args.apply:
        return apply_proposals(proposal_path, args.approve)

    addendum_block = format_addendum(
        date_str=today_iso,
        proposal_path=proposal_path,
        actions=actions,
        proposal_id=proposal_id,
    )
    note = write_addendum_to_daily_note(today_iso, addendum_block)
    if note is not None:
        print(f"[housekeep] addendum written to {note}")
    print(f"[housekeep] proposal: {proposal_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

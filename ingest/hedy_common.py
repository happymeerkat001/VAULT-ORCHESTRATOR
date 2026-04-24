import re
from pathlib import Path

VAULT_PATH = Path(
    "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Neural-Orchestrator"
).expanduser()
DAILY_NOTES_PATH = VAULT_PATH / "Daily Notes"
HEDY_AI_PATH = VAULT_PATH / "Hedy-AI"

SECTION_HEADER = "## Hedy AI"
SESSION_PREFIX = "### "
KEYWORD_MAP: dict[str, str] = {
    "assurance relay": "[[Assurance Relay LLC]]",
    "real estate": "[[Real Estate]]",
    "python": "[[Python]]",
    "AI": "[[Artificial Intelligence]]",
    "coding": "[[Coding]]",
    "Javascript": "[[JavaScript]]",
    "obsidian": "[[Obsidian]]",
    "padsplit": "[[PadSplit]]",
}


def hedy_note_path(date: str) -> Path:
    return HEDY_AI_PATH / f"{date}.md"


def daily_note_path(date: str) -> Path:
    return DAILY_NOTES_PATH / f"{date}.md"


def transcript_note_path(date: str) -> Path:
    return HEDY_AI_PATH / f"transcript {date}.md"


def append_to_note(note_path: Path, text: str) -> None:
    note_path.parent.mkdir(parents=True, exist_ok=True)
    with open(note_path, "a", encoding="utf-8") as f:
        f.write(text)


def apply_links(text: str) -> tuple[str, list[str]]:
    linked = text
    tags: set[str] = set()

    for key in sorted(KEYWORD_MAP, key=len, reverse=True):
        pattern = re.compile(rf"(?i)\b{re.escape(key)}\b")
        mapped_value = KEYWORD_MAP[key]
        linked, count = pattern.subn(mapped_value, linked)
        if count:
            tags.add(key.lower().replace(" ", "-"))

    return linked, sorted(tags)


def _item_text(item: object) -> str:
    if isinstance(item, dict):
        return (item.get("text") or item.get("title") or item.get("content") or "").strip()
    return str(item).strip()


def format_session(session: dict) -> str:
    title = (session.get("title") or "Untitled Session").strip()
    session_type = (session.get("session_type") or "").replace("_", " ")
    duration = session.get("duration")
    topic_name = ((session.get("topic") or {}).get("name") or "").strip()

    recap = (session.get("recap") or "").strip()
    meeting_minutes = (session.get("meeting_minutes") or "").strip()
    user_todos = [_item_text(i) for i in (session.get("user_todos") or []) if _item_text(i)]
    highlights = [_item_text(i) for i in (session.get("highlights") or []) if _item_text(i)]

    tags: set[str] = set()
    lines = [f"{SESSION_PREFIX}{title}"]

    meta = " · ".join(
        p for p in [session_type, f"{duration} min" if duration else "", topic_name] if p
    )
    if meta:
        lines.append(f"*{meta}*")
    lines.append("")

    if recap:
        linked, found_tags = apply_links(recap)
        tags.update(found_tags)
        lines.append("**Recap:**")
        lines.append(linked)
        lines.append("")

    if meeting_minutes:
        linked, found_tags = apply_links(meeting_minutes)
        tags.update(found_tags)
        lines.append("**Meeting Notes:**")
        lines.append(linked)
        lines.append("")

    if user_todos:
        lines.append("**Action Items:**")
        for item in user_todos:
            linked, found_tags = apply_links(item)
            tags.update(found_tags)
            lines.append(f"- {linked}")
        lines.append("")

    if highlights:
        lines.append("**Highlights:**")
        for h in highlights:
            linked, found_tags = apply_links(h)
            tags.update(found_tags)
            lines.append(f"- {linked}")
        lines.append("")

    if tags:
        lines.append(" ".join(f"#{t}" for t in sorted(tags)))
        lines.append("")

    return "\n".join(lines)


def get_existing_session_titles(note_path: Path) -> set[str]:
    if not note_path.exists():
        return set()
    titles: set[str] = set()
    for line in note_path.read_text(encoding="utf-8").splitlines():
        if line.startswith(SESSION_PREFIX):
            titles.add(line[len(SESSION_PREFIX):].strip())
    return titles


def build_sessions_output(note_path: Path, sessions: list[dict], date: str) -> str:
    has_section = note_path.exists() and SECTION_HEADER in note_path.read_text(encoding="utf-8")
    output = ""
    if not has_section:
        output += f"\n{SECTION_HEADER} — {date}\n\n"

    for session in sessions:
        output += format_session(session)
    return output


def write_transcript_note(sessions: list[dict], date: str) -> bool:
    transcript_path = transcript_note_path(date)
    HEDY_AI_PATH.mkdir(parents=True, exist_ok=True)

    existing = transcript_path.read_text(encoding="utf-8") if transcript_path.exists() else ""

    blocks: list[str] = []
    for session in sessions:
        title = (session.get("title") or "Untitled Session").strip()
        marker = f"\n## {title}\n"
        if marker in existing:
            continue
        text = (session.get("cleaned_transcript") or session.get("transcript") or "").strip()
        if not text:
            continue
        blocks.append(f"## {title}\n\n{text}\n")

    if not blocks:
        return False

    header = f"# Transcript — {date}\n\n" if not existing.strip() else "\n"
    with open(transcript_path, "a", encoding="utf-8") as f:
        f.write(header + "\n".join(blocks))
    return True


def ensure_transcript_link(note_path: Path, date: str) -> None:
    link = f"[[transcript {date}]]"
    existing = note_path.read_text(encoding="utf-8") if note_path.exists() else ""
    if link not in existing:
        append_to_note(note_path, f"\n{link}\n")


def ensure_daily_note_link(date: str) -> None:
    note_path = daily_note_path(date)
    link = f"[[Hedy-AI/{date}]]"
    existing = note_path.read_text(encoding="utf-8") if note_path.exists() else ""
    if link not in existing:
        append_to_note(note_path, f"\n{link}\n")

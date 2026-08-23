# Vault Orchestrator — Project Compass

## Purpose

Keep the AI Vault useful by reliably ingesting external material, turning it into readable Obsidian notes, and preserving a safe path from captured information to reviewed action.

## Sources of truth

- Vault content and queues: `AI-Vault/z.Ingestion/`
- Ingestion and automation code: this repository
- Current priorities and approvals: the active Daily Note / Hermes-to-do list
- Code changes: repository history and passing tests

## Current state

- Ingestion supports Hedy sessions, transcripts, YouTube links, daily briefings, OCR, and a proposal-only housekeeping flow.
- The vault has distinct queues for notes needing summaries, implementation ideas, important reading, and later review.
- Three notes remain in the summary queue even though each already contains an AI summary; their classification should be reviewed before adding more summaries.
- The current practical opportunity is a voice-first project briefing that helps choose the next bounded improvement without creating an unreviewed autonomous backlog.

## This week's outcome

Run a lightweight daily project briefing that names the status, blocker, and one recommended next action for Vault Orchestrator; convert only explicitly approved actions into separate implementation tasks.

## Next milestone

Establish a repeatable triage loop for `z.Ingestion/`:

1. Review the summary and implementation queues.
2. Promote only notes with a concrete action into a bounded task.
3. Keep completed work linked to a visible deliverable or test result.

## Guardrails

- Use Voice for capture, briefing, clarification, and prioritization.
- Keep repository changes in isolated tasks with a stated acceptance check.
- Do not make external changes, delete notes, or reorganize the vault without explicit approval.
- Preserve the repository's read-only-by-default contract for external services.

## Daily Voice Prompt

> Brief me on Vault Orchestrator. State its purpose, current status, main blocker, and the single best next action. Use evidence from the vault and repository. Do not start work until I approve the action.

## Approval Prompt

> Create a separate implementation task for the approved action. Keep the scope small, show the plan before changes, and report the verification result when finished.

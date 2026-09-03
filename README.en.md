# Safe Transcribe to Obsidian

[한국어](README.md) | <strong>English</strong>

An agent skill for preserving source recordings while transcribing, reviewing, merging without timelines, and organizing the result in Obsidian. It is designed to remain independent of any particular operating system, ASR engine, model, or provider.

> [!IMPORTANT]
> This repository does not include Whisper or any other ASR engine, and loading the skill does not automatically install a transcription environment. The workflow checks for an available local engine first. It requires explicit user approval before uploading audio to an external API.

## Key features

- Treats source files as read-only and records their size, format, duration, and SHA-256 hash.
- Preserves native engine output, canonical JSON, reviewed transcripts, merged text, and Obsidian notes as separate artifacts.
- Audits timestamps, repeated phrases, and likely silence hallucinations without automatically deleting suspicious segments.
- Requires direct listening evidence for exclusions, and direct listening or authoritative material for text replacements and speaker assignments.
- Merges recordings in an explicitly specified order and can produce a reader-facing TXT file without timeline markers.
- Builds a category map first, then updates Obsidian while preserving existing frontmatter, wikilinks, embeds, callouts, and handwritten notes.
- Reports structural validation separately from audio-content review.

## Quick start

Clone the repository, then place it in the skill directory recognized by your agent.

```bash
git clone https://github.com/Gomtanga/safe-transcribe-obsidian.git
```

Example request:

```text
Use $safe-transcribe-obsidian to transcribe the selected recordings while preserving
the originals, then create a merged transcript without timelines and an Obsidian note
organized by high-level categories.
```

## Requirements

- A separate ASR engine or an approved transcription API for the actual transcription
- Python 3.9 or later when using the helper script
- `ffprobe` recommended for verifying media duration and stream information

If no ASR engine is installed, the skill does not install one automatically. Confirm which engine may be installed, the model download and storage impact, and whether external transfer is allowed before continuing.

## Workflow

```text
Inspect sources → Preserve native ASR output → Normalize to canonical JSON → Machine audit
→ Evidence-based audio review → Merge without timelines → Organize in Obsidian → Validate delivery
```

See [SKILL.md](SKILL.md) for the complete rules and commands.

## Safety notes

- Keep source and output paths separate, and write artifacts to a dedicated job directory.
- Do not treat a passed machine audit as proof of complete audio review.
- Check transcripts for passwords, tokens, and personal identifiers before publishing or sharing them.
- Do not conclusively delete or correct a segment that was not actually reviewed against the audio.

## License

No open-source license has been selected yet.

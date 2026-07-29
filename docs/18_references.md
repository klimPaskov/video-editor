# Official References

Verified on July 23, 2026. Implementation must recheck current upstream instructions, licences, versions, and checkpoint access before installation or production use.

## Codex and Agent Skills

- Codex repository and installation: https://github.com/openai/codex
- OpenAI skills catalog: https://github.com/openai/skills
- Codex skill creation sample: https://github.com/openai/codex/blob/main/codex-rs/skills/src/assets/samples/skill-creator/SKILL.md

## FFmpeg

- Documentation: https://ffmpeg.org/documentation.html
- Filter documentation: https://ffmpeg.org/ffmpeg-filters.html
- ffprobe documentation: https://ffmpeg.org/ffprobe.html

## Whisper

- Official repository: https://github.com/openai/whisper

## Remotion

- Documentation: https://www.remotion.dev/docs/
- Repository: https://github.com/remotion-dev/remotion
- Agent skills: https://www.remotion.dev/docs/ai/skills
- Agent skills repository: https://github.com/remotion-dev/skills
- Licence: https://github.com/remotion-dev/remotion/blob/main/LICENSE.md

The verified official skills installation command is:

```bash
npx skills add remotion-dev/skills
```

## Optional adapters

- HyperFrames repository: https://github.com/heygen-com/hyperframes
- Higgsfield agent entry point: https://higgsfield.ai/mcp

These optional tools do not define the core architecture.

## Supplied sources

The `source/` directory contains the transcripts and user-provided implementation context used to derive this planning package.

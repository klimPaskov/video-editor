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

## SAM 3 and SAM 3.1

- Official repository: https://github.com/facebookresearch/sam3
- SAM 3.1 release notes: https://github.com/facebookresearch/sam3/blob/main/RELEASE_SAM3p1.md
- Official video predictor example: https://github.com/facebookresearch/sam3/blob/main/examples/sam3_video_predictor_example.ipynb
- Reviewed SAM 3.1 checkpoint repository: https://huggingface.co/facebook/sam3.1
- Reviewed current upstream commit: https://github.com/facebookresearch/sam3/commit/46957e47805eaa273f4aa7bbbd25a88bca9108ce

Checkpoint access is handled through the official repository links. The implementation must record the exact code revision and checkpoint identifier.

## MatAnyone 2

- Official repository: https://github.com/pq-yang/MatAnyone2
- Project page: https://pq-yang.github.io/projects/MatAnyone2/
- Model page: https://huggingface.co/PeiqingYang/MatAnyone2
- Reviewed on 2026-07-24: current `main` commit `d3bb5a1ebedf259a5453c6d168e6840fff85581e`, release tag `v1.0.0` at commit `57d038288ca7b5ff88f85d2b31d8d2c978fece53`, checkpoint asset `matanyone2.pth`, Python `>=3.10`, and NTU S-Lab License 1.0.
- The candidate API uses `MatAnyone2.from_pretrained` or a local checkpoint loader, `InferenceCore`, and `process_video`; output roles require independent foreground/alpha verification before use.

## Optional adapters

- HyperFrames repository: https://github.com/heygen-com/hyperframes
- Higgsfield agent entry point: https://higgsfield.ai/mcp

These optional tools do not define the core architecture.

## Supplied sources

The `source/` directory contains the transcripts and user-provided implementation context used to derive this planning package.

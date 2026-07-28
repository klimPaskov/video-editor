# Licensing and manual asset setup

The MIT licence in this repository covers the original code, documentation,
and examples only. It does not grant rights to a video, font, logo, stock clip,
music track, sound effect, background, replacement object, upstream model, or
generated media supplied by someone else.

## Local project asset checklist

For every non-source asset used in a project, record:

- a stable asset id and local path;
- SHA-256 and file size;
- origin or supplier;
- licence, permission, or ownership basis;
- allowed use, territory, duration, and commercial limits when relevant;
- attribution or credit text;
- whether the asset may be redistributed with the final video.

Keep the manifest and private evidence in the ignored project workspace. Do not
put stock files or private fonts in this public repository.

## Manual checks before a delivery

1. Confirm that the selected font, logo, B-roll, sound, background, and
   replacement assets are permitted for the intended audience and distribution.
2. Check glyph coverage, attribution requirements, and any music/content ID
   restrictions.
3. Confirm provider retention and commercial terms before any paid generation.
4. Re-check the manifest hash before Gate 3; changing an asset makes earlier
   approvals stale.

The creator is responsible for accepting third-party terms. Codex can record
evidence and stop on uncertainty, but cannot accept a licence on the creator's
behalf.

## Optional worker extensions

SAM 3.1 and MatAnyone 2 are not installed or invoked by the required workflow.
If a future accepted decision re-enables either worker, review its current
licence and checkpoint access separately and create the project-local
`worker_runtime_approval` with `videoedit approve-worker-runtime`. That record
must bind the exact code, checkpoint, licence, Python/PyTorch/CUDA runtime, and
device identity; an AMD FFmpeg encoder is not evidence for either worker.

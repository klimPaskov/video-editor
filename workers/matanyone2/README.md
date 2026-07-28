# MatAnyone 2 Worker Boundary

> This is an optional contract boundary, not part of the required public
> workflow. Normal users should not install it. Use chroma keying or an
> approved supplied/manual person mask instead.

This directory is a planning scaffold for a worker that converts a video plus an approved first-frame person mask into a temporally stable foreground and alpha result. Use SAM, an interactive tool, or a manually prepared mask to create the initial mask. The core `videoedit.services.matting.build_matting_job` helper validates and records the mask before this worker boundary is reached.

The included `run_job.py` has a legacy 1.0 compatibility path and a gated 1.1
path. The 1.1 job must bind source and first-frame mask hashes, an approved
upstream commit, a local checkpoint path and SHA-256, a bounded source range,
and a hash-bound effect approval. It fails closed when runtime access is not
approved. Codex must still verify it against the pinned official upstream API
before a live acceptance test. A successful process exit does not prove
foreground and alpha semantics or visual stability.

## Manual setup

1. Review the current official repository, NTU S-Lab License 1.0, model terms, and hardware requirements.
2. Record operator acceptance of the immutable upstream commit and checkpoint identity; add the local checkpoint SHA-256 only after the official download.
3. Run:

```bash
MATANYONE2_REF=<40-character-approved-commit> ./install.sh
source .venv/bin/activate
```

On Windows PowerShell, use the equivalent gated installer:

```powershell
$env:MATANYONE2_REF = "<40-character-approved-commit>"
.\install.ps1
```

Both installers require an immutable 40-character commit and a successful
`nvidia-smi` probe; neither downloads a checkpoint or accepts a mutable branch.

5. Record the local checkpoint SHA-256, then create a project-local
   `worker_runtime_approval` with `videoedit approve-worker-runtime`; the v1.1 job
   must reference that artifact before `runtime.access` can be approved. The
   artifact is stale if the project configuration changes or its expiry is reached.
6. Validate the legacy example contract without running the model:

```bash
python run_job.py ../../examples/matting_job.example.json --dry-run
```

The v1.1 example intentionally has a blocked runtime gate. Its schema can be
validated without credentials, but it must not be used for live inference until
the operator fills the accepted runtime identity and checkpoint hash.

7. Run one short licensed live test.
8. Prove the foreground and alpha output roles with contrasting-background composites before project use.

## Runtime contract

```bash
source .venv/bin/activate
python run_job.py /absolute/path/to/matting-job.json
```

Every v1.1 result records input and initial-mask identity, model and upstream
versions, checkpoint identity, foreground and alpha output references with
hashes, dimensions, and frame counts, plus stability findings. The core
compositor rejects a v1.1 result until `verification.status` is `pass`, including
proven foreground/alpha roles, polarity, decode, and contrasting-background review.

The isolated adapter uses `../common_process.py` for Git, ffprobe, and FFmpeg
commands. It passes argument arrays, applies explicit timeouts and bounded redacted
diagnostics, and keeps process failures separate from model or contract failures.

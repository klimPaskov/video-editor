# SAM 3.1 Worker Boundary

> This is an optional contract boundary, not part of the required public
> workflow. Normal users should not install it. Use chroma keying or an
> approved supplied/manual mask instead.

This directory is a planning scaffold for a worker that turns an approved object prompt into frame-aligned lossless masks, stable object identities, and tracked geometry. The core invokes the worker as a separate process because the current SAM environment is intentionally isolated from the Python 3.11 core.

The included `run_job.py` is a contract-bound adapter. It prefers the verified SAM
3.1 multiplex predictor, but it must still be checked against the operator-accepted
upstream commit and checkpoint before a live acceptance test. A successful process
exit does not prove object identity or mask quality.

## Manual setup

1. Review the current official repository, SAM License, checkpoint terms, and hardware requirements.
2. Record operator acceptance of the immutable upstream commit and checkpoint identity in ADR-0011; add the local checkpoint SHA-256 only after the official download.
3. Run:

```bash
SAM3_REF=<40-character-approved-commit> ./install.sh
source .venv/bin/activate
```

On Windows PowerShell, use the equivalent gated installer:

```powershell
$env:SAM3_REF = "<40-character-approved-commit>"
.\install.ps1
```

Both installers require an immutable 40-character commit and a successful `nvidia-smi` probe; neither downloads
or accepts a checkpoint on its own. They are operator actions because they can clone
upstream code and install GPU packages outside the core environment.

5. Complete checkpoint authentication through the official path and record its local SHA-256.
6. Create a project-local `worker_runtime_approval` with
   `videoedit approve-worker-runtime`; the job must reference that artifact before
   `runtime.access` can be approved. The artifact is stale if the project
   configuration changes or its expiry is reached.
7. Validate the example contract without running the model:

```bash
python run_job.py ../../examples/segmentation_job.example.json --dry-run
```

8. Run one short licensed live test and review the first, middle, last, high-motion, entry, exit, and occlusion frames.

## Runtime contract

```bash
source .venv/bin/activate
python run_job.py /absolute/path/to/segmentation-job.json
```

Every accepted result records input identity, worker and upstream versions, checkpoint identity and hash, prompt, bounded source range, masks, object IDs, bounding boxes, centroids, area, missing frames, continuity diagnostics, raw worker metadata, and output hashes. Queue jobs when GPU memory is limited. Contract and fake-worker tests do not approve the live checkpoint gate.

The isolated adapter uses `../common_process.py` for any auxiliary external command. It
passes argument arrays, applies explicit timeouts and bounded redacted diagnostics, and
keeps process failures separate from model or contract failures.

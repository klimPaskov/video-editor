# ADR-0012: MatAnyone 2 upstream pin and runtime gates

- Status: Proposed; operator and licence-owner acceptance required before installation or inference
- Date: 2026-07-24
- Decision owners: project operator and licence owner

## Context

Phase 8 needs an isolated MatAnyone 2 worker for uncontrolled-background person
matting. The controlled green-screen chroma-key path remains the preferred and
already validated fallback. The official `pq-yang/MatAnyone2` repository currently
has main commit `d3bb5a1ebedf259a5453c6d168e6840fff85581e`; its `v1.0.0` tag points
to the earlier release commit `57d038288ca7b5ff88f85d2b31d8d2c978fece53`.

The current official package declares Python `>=3.10` and publishes a `v1.0.0`
checkpoint asset named `matanyone2.pth`. The current README documents
`MatAnyone2.from_pretrained("PeiqingYang/MatAnyone2")`, `InferenceCore`, and
`process_video(input_path, mask_path, output_path)`. The current inference core
also accepts an explicit local model and device and returns foreground and alpha
paths while writing `_fgr.mp4` and `_pha.mp4` outputs. The adapter must bind a
local checkpoint path and hash rather than silently allowing an online model
download through `from_pretrained`.

The official S-Lab License 1.0 permits redistribution and use for non-commercial
purposes under its conditions. Commercial use requires prior written permission.
The operator or legal owner must decide whether the intended use is permitted;
this ADR does not make that decision.

## Decision

1. Keep MatAnyone 2 outside the Python 3.11 core and SAM environment in an
   isolated Python 3.10 worker.
2. Treat the current main commit and `matanyone2.pth` release asset as reviewed
   candidate identities, not accepted production pins. Record the exact accepted
   commit, checkpoint identifier, and local checkpoint SHA-256 before live use.
3. Require an approved first-frame person mask and a bounded source range in every
   job. Preserve the original shot and chroma-key fallback when the mask or matte
   is uncertain.
4. Do not install, download, authenticate, or run inference until the operator
   records the licence decision, accepted code/checkpoint identities, local
   checkpoint hash, Python/GPU compatibility, and bounded smoke-test approval.
5. Do not consume foreground or alpha outputs based on filenames or process exit
   alone. Verify dimensions, frame count, decode, alpha range/polarity, output
   roles, and contrasting-background composites before composition.

## Consequences

- Contract and fake-worker work can remain credential-free and cannot approve live
  model use.
- The existing draft adapter is not a live acceptance path until it binds the
  selected checkpoint and proves the official output semantics.
- A current global Python 3.10 runtime, model package, checkpoint, GPU, and first
  frame mask are still operator inputs, not repository assumptions.

## Sources reviewed on 2026-07-24

- https://github.com/pq-yang/MatAnyone2
- https://github.com/pq-yang/MatAnyone2/blob/main/pyproject.toml
- https://github.com/pq-yang/MatAnyone2/blob/main/inference_matanyone2.py
- https://github.com/pq-yang/MatAnyone2/blob/main/matanyone2/inference/inference_core.py
- https://github.com/pq-yang/MatAnyone2/blob/main/LICENSE.txt
- https://github.com/pq-yang/MatAnyone2/releases/tag/v1.0.0
- https://github.com/pq-yang/MatAnyone2/releases/download/v1.0.0/matanyone2.pth

The retained command/API review is in
`.codex/evidence/P8/upstream-candidate-review-20260724.json`.

## AMD compatibility revalidation on 2026-07-25

The official MatAnyone 2 API example selects `device="cuda:0"`, and the
reviewed official installation and inference documentation does not describe a
supported AMD/ROCm worker path. The workstation's AMD Radeon RX 7700 XT does
pass the independent FFmpeg AMF media probe, but `nvidia-smi`, `rocminfo`, and
`hipconfig` are unavailable. AMF remains a media-encoding capability and does
not satisfy the CUDA inference precondition or prove matte output semantics.

The shared compatibility recheck is retained in
`.codex/evidence/P7/amd-worker-compatibility-recheck-20260725.json`.

## AMD/ROCm revalidation on 2026-07-26

AMD's current ROCm Windows matrix describes limited PyTorch support for
selected Radeon hardware, but this is generic framework support and does not
prove MatAnyone 2 compatibility or output semantics. The official MatAnyone 2
Python example still constructs `InferenceCore(..., device="cuda:0")`, and no
official AMD/ROCm MatAnyone 2 inference path or verified checkpoint/output
contract is available for this workstation. The project therefore keeps the
worker disabled and uses the already-proved green-screen/chroma-key path and
AMD FFmpeg AMF media encoding instead. No model package, checkpoint, or live
matting process was started.

The current official references are retained in
`.codex/evidence/P7/amd-worker-compatibility-recheck-20260726.json`.

Sources:

- https://github.com/pq-yang/MatAnyone2#python-api
- https://rocm.docs.amd.com/projects/radeon-ryzen/en/latest/docs/compatibility/compatibilityrad/windows/windows_compatibility.html

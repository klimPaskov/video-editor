# ADR-0011: SAM 3.1 upstream pin and runtime gates

- Status: Proposed; operator acceptance required before installation or inference
- Date: 2026-07-23
- Decision owners: project operator and licence owner

## Context

P7 needs an isolated SAM 3.1 worker, but the model code and checkpoint are not part
of the Python 3.11 core. The current official `facebookresearch/sam3` repository
announces SAM 3.1 Object Multiplex and points to the gated `facebook/sam3.1`
checkpoint repository. The current public `main` revision reviewed on 2026-07-23 is
`46957e47805eaa273f4aa7bbbd25a88bca9108ce`. The SAM 3.1 checkpoint identity reviewed
is `facebook/sam3.1/sam3.1_multiplex.pt` on the gated `main` revision; its local
SHA-256 is intentionally unknown until an operator is granted access and downloads
it through the official path.

The official repository documents Python 3.12 or higher, PyTorch 2.7 or higher, and
a CUDA-compatible GPU with CUDA 12.6 or higher. Its SAM 3.1 video example uses
`build_sam3_multiplex_video_predictor()`, `handle_request` for `start_session` and
`add_prompt`, and `handle_stream_request` for `propagate_in_video`. The reviewed
3.1 example demonstrates text and point prompts. The older generic
`build_sam3_video_predictor` path is not treated as a SAM 3.1 acceptance path.

The repository and checkpoint use Meta's SAM License, last updated 2025-11-19.
The license is non-exclusive, worldwide, non-transferable, and royalty-free, but
distribution, trade-control, prohibited-use, termination, and attribution terms
still require an operator/legal review. The Hugging Face model is gated and
requires contact-information sharing and access approval.

## Decision

1. Keep the worker outside the core in `workers/sam3/` with a Python 3.12
   environment and versioned JSON job/result contracts.
2. Treat `46957e47805eaa273f4aa7bbbd25a88bca9108ce` and
   `sam3.1_multiplex.pt` as a reviewed candidate identity, not as an accepted
   production pin. The job contract must carry the exact upstream commit,
   checkpoint identifier, and checkpoint SHA-256 before a live run.
3. Prefer the multiplex predictor when the verified upstream API exposes it. The
   adapter must fail clearly if the installed code exposes a different builder or
   prompt/propagation API instead of silently guessing.
4. Do not clone, install, authenticate, download a checkpoint, or run GPU inference
   from the repository workflow until the operator records access, licence,
   checkpoint hash, Python/PyTorch/CUDA/driver compatibility, and a bounded live
   smoke-test approval.
5. A complete worker result is still only a proposal. Missing frames, identity
   changes, area jumps, centroid jumps, leaks, or uncertain object identity keep the
   original-shot fallback active and require review.

## Consequences

- Contract and fake-worker tests can run without credentials, CUDA, or model files.
- A live job is intentionally blocked until the unresolved licence, gated access,
  checkpoint hash, and hardware decisions are recorded.
- The upstream API is isolated behind the worker; future upstream changes cannot
  silently alter the core contract.

## Revalidation on 2026-07-24

The official `main` reference was revalidated with `git ls-remote` and remains
`46957e47805eaa273f4aa7bbbd25a88bca9108ce`. The current release notes still identify
SAM 3.1 Object Multiplex and the official repository still requires gated checkpoint
access. This does not change the ADR status: the upstream commit and checkpoint remain
candidate identities until the operator and licence owner accept them, a local
checkpoint SHA-256 is recorded, and the runtime/device decision is approved.

The command and result are retained in
`.codex/evidence/P7/upstream-candidate-review-20260724.json`.

## AMD compatibility revalidation on 2026-07-25

The official installation still documents Python 3.12 or higher and a
CUDA-compatible GPU with CUDA 12.6 or higher. No supported AMD/ROCm worker path
is documented by the reviewed official installation or inference examples. The
workstation's AMD Radeon RX 7700 XT successfully provides FFmpeg AMF media
encoding, but `nvidia-smi`, `rocminfo`, and `hipconfig` are unavailable. AMF is
therefore retained as a deterministic media-engine capability only and cannot
satisfy this worker's inference precondition.

The recheck is retained in
`.codex/evidence/P7/amd-worker-compatibility-recheck-20260725.json`.

## AMD/ROCm revalidation on 2026-07-26

AMD's current ROCm Windows matrix lists limited PyTorch-on-Windows support for
selected Radeon hardware and notes that the complete ROCm stack is not yet
supported on Windows. That generic framework capability does not establish a
SAM 3.1 worker path. The official SAM 3.1 installation still requires a
CUDA-compatible GPU with CUDA 12.6 or higher, and the reviewed video pipeline
uses CUDA-specific execution paths. The current workstation has no verified
ROCm runtime or worker-specific adapter, so the repository continues to use
AMD only for the independently verified FFmpeg AMF media boundary. No live
SAM inference is authorized or claimed.

The current official references are retained in
`.codex/evidence/P7/amd-worker-compatibility-recheck-20260726.json`.

Sources:

- https://github.com/facebookresearch/sam3#installation
- https://github.com/facebookresearch/sam3/issues/164
- https://rocm.docs.amd.com/projects/radeon-ryzen/en/latest/docs/compatibility/compatibilityrad/windows/windows_compatibility.html

## Sources reviewed on 2026-07-23

- https://github.com/facebookresearch/sam3
- https://github.com/facebookresearch/sam3/blob/main/RELEASE_SAM3p1.md
- https://github.com/facebookresearch/sam3/blob/main/examples/sam3.1_video_predictor_example.ipynb
- https://raw.githubusercontent.com/facebookresearch/sam3/main/LICENSE
- https://huggingface.co/facebook/sam3.1

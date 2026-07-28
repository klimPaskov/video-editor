# Licensing and Manual Setup

The repository code does not grant rights to upstream models, fonts, stock assets, or generated media.

## Manual checks

- Review the current Remotion licence for team size and product use.
- Request and accept official SAM checkpoint access and review the SAM License.
- Review the NTU S-Lab License 1.0 for MatAnyone 2.
- After those decisions and target-runtime checks, create the project-local
  `worker_runtime_approval` with `videoedit approve-worker-runtime`; this records
  the exact code, checkpoint, licence, PyTorch/CUDA, and device identity.
- The runtime approval is bound to the current project configuration and is
  rejected after configuration changes or expiry; create a new approval for a
  changed runtime.
- Record licences for fonts, B-roll, music, sounds, backgrounds, and replacement assets.
- Confirm provider retention and commercial terms before paid generation.

Store evidence in the private asset licence manifest. Codex may summarize terms but cannot accept them on the operator's behalf.

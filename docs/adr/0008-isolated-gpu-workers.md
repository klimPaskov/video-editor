# ADR 0008: Isolate GPU Model Workers

Status: accepted

## Decision

Run SAM 3.1 and MatAnyone 2 as separate processes with separate environments and JSON contracts.

## Reason

Their Python and CUDA requirements differ from the stable core and from each other. Isolation limits dependency conflicts and allows a remote GPU host later.

## Consequence

The core records job and result artifacts, subprocess evidence, and model
identity when an extension is explicitly enabled. It cannot import worker
packages, and the final workflow does not require either worker; see
ADR-0013.

# ADR 0012: Keep optional model experiments out of production

## Status

Superseded for the public workflow by ADR 0013 and ADR 0014.

## Decision

The public product does not install, invoke, or consume optional model-worker
outputs. Any future model experiment must use a separate environment and a new
accepted decision; it cannot change the local production path implicitly.

## Consequences

The core has no model checkpoint, CUDA, device, or worker-command requirement.
Source media and the original screen layer remain the fallback for every
render.

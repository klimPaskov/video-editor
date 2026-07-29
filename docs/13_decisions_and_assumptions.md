# Decisions and Assumptions

## Accepted decisions

### D-001 Local-first deterministic core

FFmpeg, ffprobe, Remotion, local Whisper, and local assets form the required workflow.

### D-002 Remotion is the primary visual engine

Visual scenes use typed props and reusable React components. Other renderers stay optional adapters.

### D-003 Screen recordings are the supported input

The production workflow accepts screen recordings and keeps the source layer intact.

### D-004 Isolated external integrations

Any future external vision integration must run outside the core environment
through JSON jobs. It is not required by the final workflow; see ADR-0013.

### D-005 Canonical time

Source and output media time uses integer microseconds. Remotion uses integer frames with rational conversion.

### D-006 Keep ranges define the edit

Approved source ranges to keep form the canonical base timeline.

### D-007 Hash-bound approval

Every approval binds to the exact current source and proposal or render hashes.

### D-008 Three human gates

Plan approval, segment approval, and final approval are separate.

### D-009 Re-transcription verifies speech edits

Rendered segment transcripts are compared with intended speech after cuts and fixes.

### D-010 Providers are optional

Paid generation, ElevenLabs, cloud rendering, and publishing integrations are excluded from the minimum workflow.

### D-011 Final workflow excludes optional vision workers

The required path uses the source screen layer and local timeline layers.
External vision integrations remain outside the public workflow and require a
new accepted decision before installation, inference, or consumption.

## Assumptions to verify on the target machine

- Python 3.11 and Node 22 are available.
- FFmpeg includes required filters and encoders.
- Licensed fonts can be installed locally.
- Remotion licensing fits the intended use.
- If a future decision re-enables an external integration, its access, licence
  terms, and compatible runtime are acceptable.
- Sufficient local and backup storage exists for source and intermediate media.

## Open operational choices

- source copy versus managed reference policy
- preferred delivery profiles
- exact brand typography and caption safe areas
- asset library location and backup target
- future external-integration host and transport when remote
- maximum preview and final render concurrency

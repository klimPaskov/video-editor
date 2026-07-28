# Green-screen and local mask effects

P6 is the credential-free visual milestone. It keeps controlled green-screen
extraction in the FFmpeg adapter and keeps object recolor behind an explicit,
lossless local mask contract. SAM 3.1 and MatAnyone 2 are not called by this
path.

## Chroma key

`videoedit chroma-key` writes a staged ProRes 4444 foreground and a
`foreground_manifest` artifact. The manifest records the key colour, similarity,
blend, despill, crop, feather, erosion, FFmpeg version, command arguments, file
hashes, frame count, rational frame rate, duration, alpha samples, and full
decode result. The stage fails closed unless the foreground has an alpha-capable
pixel format, matching dimensions/rate/frame count, a bounded duration, and
mixed alpha polarity across first/middle/last samples.

Example:

```bash
videoedit chroma-key p6_fixture \
  --source projects/p6_fixture/work/green-screen.mp4 \
  --edge-feather-px 1 \
  --edge-erode-iterations 1
```

Outputs are written beneath the project `work/` directory. Existing targets
with a different hash are rejected; failed staging directories remain available
for diagnosis.

## Lossless masks and recolor

`videoedit encode-mask` produces FFV1 grayscale video from a worker mask
sequence. `videoedit validate-mask` then checks the source and mask for:

- exact width and height
- exact decoded frame count
- equal rational frame rates
- duration within the explicit 100 ms tolerance
- FFV1/raw/lossless codec and grayscale pixel format
- sampled 0–255 range and declared white-foreground polarity
- complete mask decode

Only a complete `mask_validation` artifact can feed `videoedit recolor-mask`.
The recolor stage uses a masked alpha overlay so the transformed pixels are
limited to white mask regions, preserves the source production audio, records a
`mask_recolor_manifest`, and validates the resulting video/audio streams.

## Review evidence

P6 generates source, mask, recolored, foreground, alpha, and composition contact
sheets using typed FFmpeg commands. The visual review is human evidence, not a
proxy for command success. The synthetic fixture proves rectangle geometry,
mask polarity, recolor boundaries, chroma extraction, layer order, and caption
placement. It has no hair, hands, or motion blur; that limitation is retained as
a warning and must be cleared with licensed human-footage review before a
production Gate 2 approval.

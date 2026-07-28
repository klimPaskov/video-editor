# Prompt: green-screen layers and object recolor

For `<project-id>` revision `<revision-id>`, use the deterministic
green-screen-first path for `<source start>-<source end>`.

- Key color and sample guidance: `<description>`
- Replacement background: `<absolute local asset path>`
- Text behind subject: `<copy, position, and time range>`
- Captions in front: `<caption plan or style>`
- Recolor request: `<object, color, and time range>`

Prefer chroma key. Do not invoke SAM 3.1 or MatAnyone 2. Create a local mask
or alpha layer, verify dimensions, frame count, alignment, polarity, edge
quality, spill, holes, subject identity, and temporal stability, then write
the diagnostics and hashes. Apply the object recolor only when the approved
mask identity is clear; otherwise retain the original and flag the uncertainty.

Validate z-order, safe areas, timing, and occlusion. Render one still and a
short proof with captions, background, text-behind-subject, and recolor layers.
Run visual and media QA. Stop before Gate 2 and report every warning and
evidence path.

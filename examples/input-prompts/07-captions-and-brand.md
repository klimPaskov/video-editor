# Prompt: captions, text, and local brand assets

Create a caption and composition proposal for `<project-id>` revision
`<revision-id>`.

Caption requirements:

- Language: `<language>`
- Maximum lines: `<1 or 2>`
- Font: `<absolute local font path>`
- Font licence or permission record: `<manifest entry>`
- Text treatment: `<style, size, color, outline>`
- Safe-area policy: `<description>`

Visual requirements:

- Background asset: `<solid or absolute local asset path>`
- Title copy and range: `<copy and start-end>`
- Supporting text and range: `<copy and start-end>`
- Subject layer: `<source or approved mask>`
- Occlusion: captions in front; `<other layer order>`

Hash every non-source asset and record its local source, permission/licence,
and attribution requirements in the project manifest. Validate glyph coverage,
contrast, line wrapping, caption timing against the output transcript, layer
order, integer frame conversion, and safe-area bounds. Render a still and short
proof for inspection. Do not treat a successful compositor process as visual
approval and do not approve Gate 2 on my behalf.

# Remotion compositor

This project renders the validated `visual_timeline` contract. Python writes the
JSON props and stages media under `public/generated/<project-id>/`. Remotion owns
frame-driven text, captions, backgrounds, overlays, picture-in-picture, and final
presentation passes. Most users should drive it through the root `videoedit`
CLI rather than editing React files directly.

Install and inspect:

```bash
npm install
npm run typecheck
npm run studio
```

Render through the Python CLI:

```bash
uv run videoedit compose-visual <project-id> \
  --render-manifest projects/<project-id>/artifacts/render-base.json \
  --revision-id rev_001
uv run videoedit render \
  projects/<project-id>/artifacts/visual-timeline.json \
  projects/<project-id>/output/preview.mp4
```

Render a bounded proof before a full candidate:

```bash
uv run videoedit render-segment \
  projects/<project-id>/artifacts/visual-timeline.json \
  projects/<project-id>/output/segment.mp4 \
  --start-frame 0 --end-frame 299
```

Do not write one-off visual logic into the Python orchestrator. Add reusable React components here, expose their options through a versioned schema, and preserve deterministic frame behavior.

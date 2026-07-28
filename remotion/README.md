# Remotion compositor

This project renders the validated `visual_timeline` contract. Python writes the JSON props and stages media under `public/generated/<project-id>/`. Remotion owns frame-driven text, captions, backgrounds, overlays, picture-in-picture, and final presentation passes.

Install and inspect:

```bash
npm install
npm run typecheck
npm run studio
```

Render through the Python CLI:

```bash
uv run videoedit render projects/<id>/artifacts/final-timeline.json projects/<id>/output/final.mp4
```

Do not write one-off visual logic into the Python orchestrator. Add reusable React components here, expose their options through a versioned schema, and preserve deterministic frame behavior.

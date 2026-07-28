# Prompt: layer a presenter over a local background

For project `home-lab-explainer-20260728`, revision `rev_002`, process the
green-screen presenter from `00:00.000-00:46.500`.

Use these local assets that I have permission to use:

```text
Background: C:\Users\me\Videos\assets\dark-grid-loop.mp4
Font: C:\Users\me\Videos\assets\Inter-SemiBold.ttf
```

The background sits behind the presenter. Put the chapter label “Why the local
workflow matters” behind the presenter from `00:04.000-00:10.000`, with enough
contrast and no face obstruction. Put captions in front of the presenter.
Recolor the blue mug on the desk to muted orange from
`00:18.200-00:25.000`, but only if the approved local mask clearly identifies
the mug and does not spill onto the hand.

Prefer deterministic chroma keying. Do not invoke SAM 3.1 or MatAnyone 2.
Record key color, despill, dimensions, frame count, alpha polarity, edge holes,
hair/hands, temporal stability, and every asset hash. Validate z-order,
occlusion, contrast, safe areas, and frame alignment. Render a still and a
short proof before Gate 2. If the mug identity or matte edge is uncertain,
leave it unchanged and flag the warning.

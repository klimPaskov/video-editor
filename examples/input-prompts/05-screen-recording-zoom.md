# Prompt: focus the UI that viewers actually need to read

Review `hermes-agent-demo-20260728`, revision `rev_004`, for purposeful focus.
The opening title card must remain unzoomed.

There is one candidate target:

- Target: the **Instructions** textarea in the open Project settings dialog;
- target first visible: about `11:36.500`;
- visible typing action: `11:37.000-12:18.000`;
- target remains relevant until: about `12:18.500`;
- reason: viewers need to read where the project instructions are entered;
- evidence: capture the first typed character and the final completed text.

Use a modest 1.45x target-centered zoom with frame-driven smooth ease-in,
hold, and ease-out. Start only after the textarea is visible, keep the actual
textarea centered, and end before the dialog is dismissed or unrelated reading
begins. Keep the rest of the screen stable; do not pan freely or move the
whole frame.

Reject zooms on the intro, browser chrome, waiting/loading, result inspection,
or a mixed UI state. If the target or either boundary is not verifiable, emit a
`no_zoom` decision. Produce evidence frames, integer-frame keyframes, geometry
diagnostics, and a Gate 1 review packet. Do not apply the zoom yet.

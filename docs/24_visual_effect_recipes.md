# Visual effect recipes

## Purposeful screen zoom

1. Confirm the target is an opened window, prompt box, relevant cursor action, or important visible UI.
2. Record the target-visible range and target bounds.
3. Start after the target appears and finish before unrelated content begins.
4. Compute the smallest target-centered scale that preserves useful context.
5. Generate stabilized keyframes with smooth ease in and ease out.
6. Render start, peak, hold, and exit proof frames.
7. Reject jitter, snapping, drift, empty edges, hidden overlays, or an unclear target.
8. Use no zoom when confidence fails.

## Requested prompt speed-up

1. Confirm an explicit request exists.
2. Identify the first and last frames of visible prompt writing or dictation.
3. Exclude browsing, reading, waiting, navigation, result inspection, and other actions.
4. Compile the range into the retimed timeline.
5. Keep sound audible and preserve the original voice pitch.
6. Rebase transcript, captions, zooms, and later effects through the mapping.
7. Re-transcribe and check audio presence, synchronization, and exact boundaries.
8. Use normal speed when confidence fails.

## Local B-roll, motion, and sound

`plan-cues` builds proposals for local B-roll, motion transitions, and sound.
Each proposal records asset identity, hash, range, reason, collision policy, and
the original-recording fallback. Motion transitions require a verified
structural boundary and a clean-cut fallback. Sound remains speech-priority.

## Speech-triggered effects

Match an effect request to stable word IDs and source time, then map it through
the approved output time map. Do not search only by raw text in the final
transcript.

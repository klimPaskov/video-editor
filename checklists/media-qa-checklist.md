# Media QA Checklist

- [ ] Output fully decodes.
- [ ] Expected and actual duration match within profile tolerance.
- [ ] Frame rate and dimensions match the profile.
- [ ] Pixel format is supported by the target.
- [ ] Audio sample rate and channels match the profile.
- [ ] A/V drift is within threshold.
- [ ] Integrated loudness and true peak pass.
- [ ] No unexpected black frames or long freezes are detected.
- [ ] Captions stay within output duration.
- [ ] Caption overlap and safe-area checks pass.
- [ ] Required fonts and glyphs are present.
- [ ] Motion and lower thirds do not collide with captions.
- [ ] Sound effects do not hide speech.
- [ ] No placeholder or missing asset appears.
- [ ] Every external asset has provenance and license metadata.
- [ ] Provider spend is reconciled.
- [ ] QA report is bound to the reviewed render hash.
- [ ] Purposeful zoom target evidence is present.
- [ ] Zoom centering, easing, stability, relevance boundaries, edge coverage, and caption clearance pass.
- [ ] Speed-up request evidence and exact first and last action frames are present.
- [ ] No forbidden activity overlaps a speed-up.
- [ ] Sped-up audio is audible, follows the approved pitch policy, and remains synchronized.
- [ ] Retimed duration matches the piecewise timeline.
- [ ] Re-transcribed speed-up speech remains present and understandable.
- [ ] Captions and every later cue remain aligned after retiming.

## Version 2.2 cut and transition checks

- [ ] Every cut has a decoded join preview and join-QA result.
- [ ] Re-transcription around joins contains no missing, duplicate, or damaged words.
- [ ] No clipped syllable, click, room-tone jump, black flash, or unexplained screen jump remains.
- [ ] Dense local cutting still sounds natural and visually stable.
- [ ] Transition frames preserve full-frame coverage and readable incoming content.
- [ ] Transition sound transient, gain, fades, speech clearance, and reuse spacing pass.

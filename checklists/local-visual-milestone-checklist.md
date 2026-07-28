# Local Visual Milestone Checklist

Use this checklist before starting SAM 3.1 or MatAnyone 2 work.

## Foundation

- [ ] Source media is immutable and hash verified.
- [ ] Gate 1 approval is current.
- [ ] The base edit fully decodes.
- [ ] Production audio and picture use the same approved timeline.
- [ ] Loudness and A/V synchronization pass.

## Remotion

- [ ] Timeline JSON validates against the schema.
- [ ] TypeScript types match the public schema.
- [ ] Required fonts and assets are hash verified.
- [ ] Background, middle, and front passes have explicit z-order.
- [ ] Captions stay inside safe areas.
- [ ] Still and segment previews render.

## Green screen and masks

- [ ] The key colour and lighting are documented.
- [ ] The foreground intermediate retains a valid alpha channel.
- [ ] Hair, fingers, clothes, holes, spill, and motion blur were reviewed.
- [ ] The subject can be placed over at least two contrasting backgrounds.
- [ ] One supplied local object mask aligns in size, frame count, range, and polarity.
- [ ] Recolor affects only pixels inside the approved mask.
- [ ] Invalid masks fall back to the original shot.

## Full local proof

- [ ] A short fixture shows approved cuts.
- [ ] Production audio is preserved.
- [ ] One object is recolored.
- [ ] The green-screen subject is placed over a new background.
- [ ] Text appears behind the subject.
- [ ] Captions and front graphics appear above the subject.
- [ ] The final fixture fully decodes.
- [ ] Contact sheets and timecoded QA findings exist.
- [ ] No GPU checkpoint, credential, network provider, or paid service was required.

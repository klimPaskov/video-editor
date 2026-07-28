# Prompt: repair a rejected candidate without repeating its mistakes

The candidate below for `hermes-agent-demo-20260728`, revision `rev_003`, was
rejected after visual review:

```text
C:\Users\me\Projects\video-editor\projects\hermes-agent-demo-20260728\revisions\rev_003\outputs\recut.mp4
```

Findings:

- too many automatic pause/dead-air cuts make the presenter sound rushed;
- later zooms land on unrelated UI and drift;
- the visible prompt-writing action is not clearly separated from waiting and
  reading.

Create a new immutable revision `rev_004` from the registered source. Never
edit `rev_003` in place and never delete its MP4, join previews, or failed QA.

Use conservative repair mode. Keep only these explicit removals:

1. `01:30.940-01:32.700` - remove “I am not even going to do this manually”;
2. `04:09.200-04:10.480` - remove “very nice”;
3. `04:11.000-04:19.840` - remove the sentence beginning “the point of this
   series is also…”;
4. `07:18.550-07:19.270` - remove “good job”;
5. `07:19.950-07:20.730` - remove “very nice”.

Reject all other automatic pause/dead-air cuts until separately reviewed. Add
one zoom only if evidence proves that the Project settings Instructions box is
visible and relevant; otherwise use no zoom. Speed up only the exact visible
typing action, keeping audible pitch-preserved sound. Rebuild the EDL, join
previews, transcript comparison, captions, focus mapping, retimed timeline,
and QA. Return a new Gate 1 packet bound to `rev_004` and stop before applying
or delivering it.

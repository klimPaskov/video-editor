# Open Decisions Before Implementation

Record answers in the implementation repository before the related phase starts.

## Product scope

- The final workflow is green-screen or approved supplied/manual-mask first. Normal-background neural matting is deferred and does not ship in the required path; revisit only through ADR-0013 re-enable conditions.
- Are source videos long-form YouTube, short-form social, or both?
- Which delivery resolutions, frame rates, codecs, and aspect ratios are required?
- Is screen recording and picture-in-picture part of the first milestone?

## Editorial policy

- How aggressive may automatic silence trimming be?
- Which phrases, disclaimers, calls to action, or legal statements are protected from automatic deletion?
- Is the final good take usually the last take, or does the operator use another recording convention?
- What minimum handles are required around cuts?

## Brand and design

- Which fonts and fallback fonts are licensed?
- What are the caption safe areas and maximum line count?
- Which motion patterns are approved?
- Which colors may be used for recoloring or emphasis?
- Which replacement assets are available for the first object-replacement test?

## Infrastructure

- What operating system is supported?
- If a future optional worker is re-enabled, is a compatible GPU host available locally or remotely? The final workflow has no GPU-worker requirement.
- Where are source, work, output, and backup files stored?
- How much disk space may one project use?
- Which jobs may run concurrently?

## Legal and privacy

- Does the current Remotion licence fit the intended use?
- If SAM 3.1 is re-enabled, is its licence acceptable for the selected use?
- If MatAnyone 2 is re-enabled, are its licence and output semantics acceptable for the selected use?
- May source video ever leave the local machine?
- What retention period applies to transcripts, masks, mattes, and previews?

## Performance targets

- What source duration should the first benchmark use?
- Is the 45-minute reported editing time a useful target for this hardware and review standard?
- What is the acceptable operator review time per five minutes of source?
- Which stages may trade speed for quality?

Unanswered decisions should remain explicit. Codex should not fill them with hidden assumptions.

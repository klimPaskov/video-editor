# GPU Worker Readiness Checklist

Complete this checklist separately for SAM 3.1 and MatAnyone 2.

## Legal and provenance

- [ ] Current upstream repository was reviewed.
- [ ] Current licence and checkpoint terms were reviewed.
- [ ] Intended use was approved by the operator.
- [ ] Upstream commit is pinned in an ADR.
- [ ] Checkpoint identity and hash, when available, are recorded.
- [ ] Test media is licensed and nonconfidential.

## Environment

- [ ] GPU model is recorded.
- [ ] Driver version is recorded.
- [ ] CUDA runtime is recorded.
- [ ] Worker Python version is correct.
- [ ] PyTorch and related package versions are recorded.
- [ ] Worker environment is isolated from the core and other GPU worker.
- [ ] Available GPU memory is sufficient for the bounded smoke test.

## Contract and safety

- [ ] Job JSON validates.
- [ ] Result JSON validates.
- [ ] Input hashes are recorded.
- [ ] Output writes are atomic.
- [ ] Timeout and cancellation work.
- [ ] Empty or partial outputs cannot report complete status.
- [ ] Logs do not expose credentials.
- [ ] Jobs are bounded to the approved source range.

## Visual proof

- [ ] First, middle, and last frames were reviewed.
- [ ] Entry and exit frames were reviewed.
- [ ] Highest-motion frames were reviewed.
- [ ] Occlusion frames were reviewed.
- [ ] Missing frames, identity changes, edge failures, or instability are reported.
- [ ] A contrasting-background preview exists for person mattes.
- [ ] The live smoke test result was approved before integration use.

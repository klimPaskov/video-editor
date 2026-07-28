# Prompt: publish, back up, and clean up safely

For `<project-id>` revision `<revision-id>`, prepare delivery only after a
current Gate 3 approval exists.

Show me the candidate, final QA, watch-through, Gate 1 and Gate 2 bindings,
asset manifest, composition bundle hash, delivery profile, and all warnings.
After explicit Gate 3 approval:

1. write publishing metadata;
2. promote the approved MP4 to the configured `outputs` directory;
3. preserve the requested MP4 streams and quality profile;
4. run strict FFmpeg decode and stream validation;
5. write checksums and delivery metadata;
6. verify the configured backup by SHA-256.

Then create a dry-run cleanup plan. Never include immutable source media,
valid revisions, active approvals, phase results, or failed QA evidence. List
every derived candidate/cache/temp entry with its hash, retention class, and
recoverability. Wait for a separate explicit cleanup approval before executing
the plan. If any hash, backup, QA, or approval is stale, stop and report it.

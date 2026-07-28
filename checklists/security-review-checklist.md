# Security Review Checklist

- [ ] Source and provider media are treated as untrusted input.
- [ ] No shell invocation is used.
- [ ] Process environment is restricted.
- [ ] Process timeout and cancellation are implemented.
- [ ] Project-derived paths cannot escape the project.
- [ ] Cleanup follows artifact records and excludes source.
- [ ] Symbolic links are handled safely.
- [ ] Secrets stay outside artifacts and logs.
- [ ] Redaction tests cover arguments, output, and exceptions.
- [ ] Network access is disabled by default.
- [ ] Motion HTML or scripts run in a restricted context.
- [ ] Provider downloads are quarantined and validated.
- [ ] Spend approval and hard limits are enforced.
- [ ] Data classification controls remote use.
- [ ] Support bundles exclude private content by default.

## What changes

<!-- One or two sentences: the problem and the fix. Link issues with "Closes #N". -->

## How it was verified

<!-- Tests added or run, and what you exercised by hand. -->

## Checklist

- [ ] `uv run python -m pytest tests/ --ignore=tests/integration -q` passes locally
- [ ] No site-specific values (hosts, IPs, staff or student names, credentials) in tracked files; they belong in operator config (see README "Site configuration")
- [ ] Docs updated if behaviour or configuration changed

Merging needs both CI checks (Unit Tests, Integration Tests) to pass and an approval from a maintainer.

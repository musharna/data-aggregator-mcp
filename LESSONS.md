# Lessons

One line per miss: date, the class of miss, and the mechanism that now catches it.

- 2026-09-16 — A live-network test's fixed timeout (90 s) was below the code's own retry budget (3 x 30 s + backoff), so an upstream outage became `TimeoutExpired` (fail) instead of exit != 0 (skip); and the CLI dropped the router's per-source `errors`, so `[]` + exit 0 passed the test vacuously. Now: timeout derived from the retry constants (`_zenodo_retry_budget_s`), CLI names failed sources on stderr and exits 1 when all failed, and the live test asserts the requested count.
- 2026-09-16 — A registry table that stores function objects at import (`_RESOLVERS`) defeats `monkeypatch.setattr` on the module; three tests silently hit live NCBI from required CI and one passed for the wrong reason. Now: resolvers are call-time thunks, a binding test guards it, and `unshare -rn sh -c 'ip link set lo up; pytest'` is the offline control that exposes any live dependency in the suite.

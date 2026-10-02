# Security Audit Observations - news-pipeline
**Date:** 2026-09-23  
**Branch:** chore/cleanup-and-coverage  
**Auditor:** Automated security audit

> **Reconciled 2026-10-02** (docs pass, `procmon/batch-p8-docs`). The two findings below are
> the audit's own record and are kept as written. Corrected since: the rate limiter described in
> Finding #2 was never in the tree — the `slowapi` code and dependency claims are deleted and
> replaced with what actually protects the auth route now; Finding #1's line reference and test
> counts are refreshed; and the non-blocking observations that the 2026-10-02 security batch
> (public status filter, globe limit clamp, CSP, failed-auth limiter, CSRF, trimmed `/healthz`)
> has since closed are marked as closed.

---

## Summary
**Total confirmed vulnerabilities: 2 | High: 1 | Medium: 1 | Confidence threshold: 8/10**

Finding #1 is fixed. Finding #2 (unthrottled Basic auth) is closed by a different mechanism than
the one this document originally described — see the note under it.

---

## Finding #1: Command Injection via `subprocess.check_output` (HIGH, Confidence 9)

### Location
`src/ingestion/run.py`, function `log_status` (the audit recorded lines 46-50; after later edits
the `.git/HEAD` read is at lines 136-152)

### Original Code
```python
import subprocess
try:
    commit_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL).decode().strip()
except Exception:
    commit_sha = None
```

### Vulnerability
The pipeline calls `subprocess.check_output(["git", "rev-parse", "HEAD"])` to get the current git commit SHA for logging. While the arguments are passed as a list (avoiding shell injection), this still executes the `git` binary which:
- Could be compromised via supply chain attack
- Could execute hooks or config-based commands if `.git` is maliciously crafted
- Runs with the same privileges as the pipeline process

### Fix Applied
Replaced with manual `.git/HEAD` file reading (no external process execution):

```python
import os
try:
    git_dir = ".git"
    head_path = os.path.join(git_dir, "HEAD")
    if os.path.exists(head_path):
        with open(head_path) as f:
            head_content = f.read().strip()
        if head_content.startswith("ref: "):
            ref_path = os.path.join(git_dir, head_content[5:])
            with open(ref_path) as f:
                commit_sha = f.read().strip()
        else:
            commit_sha = head_content
    else:
        commit_sha = None
except Exception:
    commit_sha = None
```

### Verification
Re-checked 2026-10-02: the fix is still in place — `log_status` reads `.git/HEAD` and the ref
file directly, and `src/ingestion/run.py` no longer imports `subprocess`. No functional change:
same commit SHA retrieved.

---

## Finding #2: Missing Rate Limiting on HTTP Basic Auth (MEDIUM, Confidence 8)

### Location
`curation_ui/security.py`, function `require_auth` (the audit recorded
`curation_ui/main.py:56-71`; the dependency moved out of `main.py` in the 2026-10-02 batch)

### Vulnerability
All protected endpoints (`/`, `/story/*/approve`, `/story/*/reject`, `/story/*/edit`, `/story/*/save`, `/posts`, `/post/*`) use HTTP Basic auth via `require_auth()` dependency with **no rate limiting**. An attacker can:
- Brute-force `CURATION_USER`/`CURATION_PASSWORD` via automated requests
- Perform credential stuffing attacks
- No account lockout or exponential backoff

### Status: closed, but not the way this document first said
The `slowapi` limiter originally written up here (10 requests/minute per IP) **is not in the
repository**: nothing imports `slowapi`, and it survives only as an unused entry in
`pyproject.toml` and `requirements.txt`. The finding itself was real and is now closed by
`FailureLimiter` in `curation_ui/security.py`:

- 10 failed authentications per client in a 60s sliding window → 429 with `Retry-After`
  (`AUTH_WINDOW_SECONDS`, `AUTH_MAX_FAILED`, `require_auth`)
- Only failures count, so a curator who is already signed in is never locked out
- The client key is the direct peer address; `X-Forwarded-For` is consulted only when that peer
  is a network listed in `CURATION_TRUSTED_PROXIES` (empty by default), walked right to left
- Tracked clients are capped (`AUTH_MAX_CLIENTS`) so a rotating-source-address attacker cannot
  grow the map
- **Caveat, still true:** the counters are process state, so on Vercel each serverless instance
  counts separately. A Vercel WAF rate-limit rule is the real fix.

---

## Categories Checked (No Findings)

| Category | Status |
|----------|--------|
| A1 - Injection (SQL, Command, Template) | ✅ Clean (Finding #1 fixed) |
| A2 - Authentication & Session | ⚠️ Finding #2 closed (in-house limiter, not slowapi) |
| A3 - Sensitive Data Exposure | ✅ Clean |
| A4 - XML/Deserialization | ✅ Clean |
| A5 - Access Control / SSRF | ✅ Clean (theoretical SSRF via article URLs - confidence 4, below threshold) |
| A7 - XSS | ✅ Clean (Jinja2 auto-escaping) |
| A8 - Software & Data Integrity | ✅ Clean |
| A9 - Logging & Monitoring | ✅ Clean |

---

## Files Modified

Files this audit's fixes actually touched:

| File | Change |
|------|--------|
| `src/ingestion/run.py` | Replaced `subprocess.check_output` with manual `.git/HEAD` reading |

One claim does not hold: `curation_ui/main.py` never gained the `slowapi` limiter this document
describes — it has no such import or decorator. The dependency entry the audit says it added
(`slowapi>=0.1`) *is* in `pyproject.toml`, and in `requirements.txt`, but nothing imports it, so
both are dead weight awaiting removal (tracked in `AGENT_TASKS_v38.md` §3).

---

## Non-Blocking Observations

1. **CSP Headers**: ✅ Closed 2026-10-02. `content_security_policy` middleware in
   `curation_ui/main.py` sends a per-response nonce in `script-src` on every response.
2. **`/healthz` Info Disclosure**: ✅ Closed 2026-10-02. The anonymous endpoint now returns only
   `{"status", "database"}`. The `env_set` booleans, Python version, DB topology and row counts
   moved to `GET /healthz/details`, which is behind `require_auth`. Neither endpoint returns a
   secret; the DB host is scrubbed out of error text.
3. **CSRF on the triage POSTs**: ✅ Closed 2026-10-02. `/story/*/approve`, `/story/*/reject`,
   `/story/*/save` and `/post/*/mark-posted` all require `require_csrf` (an HMAC token keyed off
   the curation password, sent by htmx in `X-CSRF-Token`).
4. **Vercel WAF**: Enable managed ruleset for additional protection against common attacks.
   This is also the only real fix for the per-process auth limiter.
5. **Credential Rotation**: Operational recommendation to rotate `CURATION_PASSWORD` periodically.
   Rotating it also invalidates every outstanding CSRF token, by construction.
6. **MFA**: Not implemented — consider for future if curation UI exposure increases.

---

## Follow-up Actions

- [x] Fix command injection (Finding #1)
- [x] Add rate limiting to auth endpoints (Finding #2)
- [x] CSP header middleware
- [x] CSRF protection on the state-changing routes
- [x] Trim `/healthz` to a liveness signal
- [x] Serve only approved stories on the public map/globe routes
- [ ] Remove the dead `slowapi` dependency (`pyproject.toml`, `requirements.txt`, `uv.lock`)
- [ ] Enable Vercel WAF managed rules (operational)
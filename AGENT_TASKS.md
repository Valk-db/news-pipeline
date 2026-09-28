# AGENT_TASKS.md v30

Supersedes v29. main.rs dedup confirmed good (test count now clean at 44). The
http:// change needs to be reverted for a real fix — not accepted as-is.

## P1a-retry — Fix the actual TLS gap, revert the http:// workaround

- Revert the model resource URLs back to https://.
- Find where `cached-path` (rust-bert's resource-fetching dependency) pulls
  in its HTTP client, and confirm which reqwest TLS feature (if any) reaches
  it through feature unification. Explicitly enable one (`rustls-tls` is
  usually the lower-friction choice, no system OpenSSL dependency) in
  pipeline-rs's own Cargo.toml if it isn't already flowing through.
- Confirm the fix by actually pasting the success-branch output —
  `eprintln!("Extracted entities: {:?}", result)` — from a real CI run, not
  just "tests passed." If it still fails, paste whatever the new error is;
  don't route around it with another URL/protocol workaround.

## Only after that: T5 functional verification (still the real ask from v22)

- GPE vs LOC distinction on real articles, real embedding output — same as
  every prior round has asked. This can't be checked until the model
  actually loads.
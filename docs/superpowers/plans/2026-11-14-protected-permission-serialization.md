# Protected intake permission serialization (#185)

- Reproduce removal of capture permission after inbox registration but before ciphertext or ledger commit, using synthetic identities only.
- Preserve durable registration before ciphertext. Recheck the caller while holding the core writer lock used by pairing; serialize the inbox ciphertext commit or ledger link against permission updates. Keep the existing ownership gate and pending recovery.
- Verify revocation-first and commit-first races, registration failure and cross-database recovery, isolated service and installed wheel. Document lock scope, limitations, and actual validation before pushing.

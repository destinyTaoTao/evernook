# Protected single-inbox cutover (#204)

Goal: permit an explicitly inventoried, fully linked single protected inbox to
move to the independent service without exposing plaintext or weakening the
default rejection of unverified or incomplete assets. Unknown, never-registered
inboxes cannot be discovered automatically; the operator must inventory them.

1. Add a trusted local migration verifier that, under the existing exclusive
   ownership gate, authenticates an administrator, verifies the chosen inbox
   and recovery password, decrypts and validates every bounded object, checks
   every core link bidirectionally, and produces a short-lived authenticated
   digest of the ledger, registry, key materials and encrypted objects. Pending,
   mismatched, foreign, missing or corrupt records fail closed. The proof must
   contain no plaintext or password and must not be an authorization to disclose.
2. Add an explicit interactive CLI preparation command. No password in argv,
   environment, stdin pipes, service request, output, logs or proof. Retain
   existing per-entry reconciliation for pending objects before preparation.
3. Add an explicit service activation option requiring the proof and the same
   bound inbox. Recheck administrator authorization, proof expiry/authentication
   and the complete protected-state digest under the same exclusive gate before
   atomically setting service mode. Without the option the existing protected
   activation rejection is unchanged. Proof failure must not publish an endpoint
   or mutate the ledger or assets; activated restart uses the normal service path.
4. Exercise real synthetic independent processes, old-writer exclusion, proof
   tampering/expiry, permission revocation, pending and foreign assets, key and
   ciphertext changes, stale proof, crashes and recovery. Verify decryption and
   metadata before/after cutover without displaying or logging secrets; test the
   non-editable wheel and existing safety regressions. Record measured limits,
   unknown-inbox and backup boundaries and keep daily admission closed.

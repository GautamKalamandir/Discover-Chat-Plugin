# 0011. Security hardening: tests as guarantees, no user content in logs (Q18)

- Status: Accepted
- Date: 2026-10-07
- Design: [docs/design/phase-11-security.md](../design/phase-11-security.md); threat model:
  [docs/security/threat-model.md](../security/threat-model.md)

## Decision

1. **The 22 security scenarios are executable.** Each has tagged tests, and a meta-test fails if a scenario loses
   its last test ([scenario matrix](../security/scenario-matrix.md)).
2. **Red-team with a compromised LLM.** 33 adversarial prompts are run with a hostile model that tries every attack
   it can (forbidden models, OLS-hidden columns, DMV/`INFO`/multi-`EVALUATE` DAX, context dumps). Invariants:
   - Power BI only for allowed models;
   - no forbidden queries executed;
   - no restricted names reach the user or the LLM provider.

   The same suite runs against the real model with `-m network`.
3. **Q18: no user content in logs.** Logs carry ids, codes, counts and timings only. Question text, terms, data
   values and Power BI error text stay out. A redacting formatter masks tokens/keys, and chatty libraries are pinned
   to WARNING. Canary tests enforce this.
4. **Production refuses unsafe configuration:**
   - dev auth;
   - `dev_synthetic`;
   - CORS `*`;
   - empty tenant allow-list;
   - dev settings;
   - DEBUG logs.

   API docs are hidden in production. Responses carry a restrictive CSP.
5. **Supply chain and secrets:** `scripts/security-scan.sh` (pip-audit, npm audit, gitleaks over history and tree).
   Findings are triaged in [scan-results.md](../security/scan-results.md).

## Consequences

- Security properties are checked on every test run, not only reviewed once.
- Troubleshooting uses the correlation id to find the stored conversation, never log content.
- The remaining risks are owned in the threat model (LLM provider data review, per-instance limits, live
  verification after IT delivers).

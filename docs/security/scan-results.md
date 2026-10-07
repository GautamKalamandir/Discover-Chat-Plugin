# Security scan results

Command: `bash scripts/security-scan.sh`

## 2026-10-07 (Phase 11)

| Scan | Scope | Result |
|---|---|---|
| pip-audit | backend locked dependencies (`uv export --frozen`) | **No known vulnerabilities** |
| npm audit `--omit=dev` | visual: code shipped in the `.pbiviz` | **0 vulnerabilities** |
| npm audit (all) | visual incl. dev tooling | **0** after the fix below |
| gitleaks `git` | full git history (5 commits) | **No leaks** |
| gitleaks `dir` | working tree incl. uncommitted files | **No leaks** in project files |

### Findings and triage
1. **vitest 3.2 → 5.0.3.**
   - Advisories: GHSA-5gmw-xhrv-c9v3 and GHSA-85c8-ppgw-ccpr (tinypool prototype pollution → RCE, critical), and
     GHSA-82fw-gwwq-j7x9 (@vitest/mocker path traversal, moderate).
   - These are dev/test tooling only, never shipped, but run on developer machines and in CI. **Fixed** by
     upgrading. All 27 visual tests pass.
2. **gitleaks `dir`:** 21 `generic-api-key` hits, all inside `backend/.venv/`. These are example keys in installed
   third-party packages, not project code, and the folder is gitignored. **Excluded** via `.gitleaks.toml`
   (installed dependencies and build output only). All project files are still scanned.

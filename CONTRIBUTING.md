# Contributing — SZL Router

Thank you for your interest in contributing to the SZL Router.

## Doctrine Constraints (READ FIRST)

All contributions must comply with **Doctrine v11 LOCKED** (749/14/163):

- Λ = Conjecture 1 (NOT a theorem)
- SLSA L1 honest (NOT an L3 claim)
- Section 889 = exactly 5 vendors (Huawei, ZTE, Hytera, Hikvision, Dahua)
- NO Iron Bank, NO FedRAMP, NO CMMC, NO SWFT, NO Mission Owner references
- **Honest provenance is non-negotiable.** A free/grid tier is never labelled
  `sovereign`. `sovereign: true` is reserved for hardware we own.

## Hard Rules for This Repo

- **No secrets in the repo.** All upstream keys come from the environment.
  Nothing secret is ever written to disk or logged. `*.pem`, `*.key`, and `.env`
  are git-ignored — never force-add them.
- **No half-state.** A logical model either resolves to a working upstream or the
  call fails loud (HTTP 502) with the complete attempt trail.
- **Sovereign-first ordering.** New providers must slot into the fallback order
  *after* owned metal, never ahead of it.

## How to Contribute

1. Fork the repository.
2. Create a feature branch (`git checkout -b feature/your-change`).
3. Run the tests locally (`pytest`).
4. Commit with a DCO sign-off (see below).
5. Open a Pull Request — one reviewer required.

## DCO Sign-off

All commits require a DCO sign-off trailer:

```bash
git commit --signoff -m "your message"
```

This certifies the [Developer Certificate of Origin](https://developercertificate.org).

## Security

Report security issues privately — do NOT open public issues for security
vulnerabilities.

---

© 2026 Lutar, Stephen P. — SZL Holdings · Apache-2.0

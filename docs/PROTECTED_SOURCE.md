# Router source admission

The canonical `main` branch requires verified signed commits, an up-to-date
pull request, linear history, resolved review conversations, and these checks
from GitHub Actions (application ID 15368):

- `Test (py3.12)`
- `HF card contract`
- `Doctrine / doctrine`
- `Analyze (python)`
- `trivy / Trivy fs scan`

The rule applies to administrators. Force pushes and branch deletion remain
disabled, with no added bypass allowances. The solo operator uses a pull request
with zero required approving reviewers; that setting does not invent independent
approval. Use ordinary squash merges after exact-head checks pass.

SBOM and Scorecard run on the default branch. They are retained release evidence
but are not required PR contexts because those jobs do not run on every PR.
Path-filtered control-console checks likewise remain scoped to their source.

Keep source admission, model qualification, Hub publication, and runtime
acceptance separate. A green source check does not establish deployed readiness.
The real inference acceptance command and its evidence boundaries are documented
in [the local demo guide](../demo/README.md).

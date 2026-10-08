# Disabled inference staging

The `Prepare disabled inference bundle` workflow builds a reproducible package
for a future, separately admitted inference gateway. It does not deploy, assign
a cloud target, install secrets, authorize spending, or call a model. Existing
`SZLHOLDINGS/szl-router-control` and `SZLHOLDINGS/llm-router-live` publishers and
runtime settings are unchanged.

## Prepare

After ordinary source admission through the checks in
[PROTECTED_SOURCE.md](PROTECTED_SOURCE.md), publish the control image from the
same current `main` commit with `publish-router-control.yml`. Use that run's exact
registry digest as the `image_digest` input to `prepare-router-inference.yml`.

The preparation workflow verifies the existing image publisher's Cosign identity
and GitHub build provenance against the selected `main` SHA. It then generates
the disabled bundle, builds its Dockerfile, and exercises the resulting container
with networking disabled. The source must still be current when this completes.
Only GitHub artifact storage is written; no Hugging Face credential is available
to this workflow.

An artifact retained by a failed workflow is failed-attempt evidence, not a
qualified release. Check the exact-head workflow conclusion and the individual
signature, provenance, and container readback files.

## Local inspection

The offline builder is also usable from a clean canonical checkout:

```powershell
py -3 -B scripts/router_inference_bundle.py --source-revision <full-commit-sha> --image-reference ghcr.io/szl-holdings/szl-router-control@sha256:<64-hex-digest> --output <new-directory-outside-checkout>
```

Local input validation does not establish image authenticity or current remote
source admission. Those remain `UNKNOWN` in the bundle binding. The separate
workflow evidence must be inspected before relying on an image. The builder
rejects existing output directories instead of replacing earlier evidence.

## Runtime boundary

The generated startup command overwrites ambient provider configuration with
the fixed disabled proposal and keeps egress off. Setting deployment environment
variables cannot activate this bundle. It is a staging artifact, not an
environment-toggle route to paid inference.

The proposal is public-data-only, uses `https://api.openai.com/v1`, and names
`gpt-6-astra` behind the alias `szl-astra`. This is a declared routing candidate,
not a model-access test, executing-weights digest, or quality evaluation. Its
cost tier is ordinal; it is not a dollar limit. This preparation path makes no
provider request and authorizes no provider spending.

## Activation remains blocked

A later reviewed change must name a separate target and its sole publisher,
verify target authority, admit dedicated provider and caller secrets through the
deployment secret manager, and read back enforced spending controls. It must
also establish source/image parity, data-handling policy, and a bounded runtime
acceptance procedure with no automatic fallback or retry.

Only after those gates pass may a separately admitted active configuration be
deployed. A successful synthetic completion and its receipt are distinct from
configuration readiness, model evaluation, independent attestation, and product
production readiness. Never repurpose the local Studio's credential or change
the existing control Space to bypass these gates.

## Offline tests

```powershell
py -3 -B -m unittest -v test_router_inference_bundle test_router_inference_workflow
```

Tests exercise the generated startup guard, actual provider registry schema,
closed output set, secret exclusion, source/image input validation, output
preservation, and artifact-only workflow authority. They do not call a provider.

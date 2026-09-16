# #055 OpenAPI 3.1 sandbox contract

`api/openapi.json` is the deterministic machine-readable contract for the currently
declared NARANG RIDER HTTP surface. Generate it with:

```bash
PYTHONPATH=src python scripts/generate_openapi.py --write
PYTHONPATH=src python scripts/generate_openapi.py
```

The artifact documents cookie authentication, CSRF and idempotency requirements,
branch scope, correlation IDs, stable error envelopes, rate-limit policy, Vault
references and synthetic examples. `sandbox.invalid` is intentionally non-routable.

The generated TypeScript operation map is a compile-time contract only. It does not
replace the handwritten browser safety logic that sets `credentials: include`,
`cache: no-store`, CSRF, branch and idempotency headers.

`api/openapi.baseline.json` is the reviewed compatibility baseline. Removing an
operation or changing the API version fails CI until an explicit version decision
and independent review updates the baseline. Scattered domain route manifests are
also checked so they cannot silently drift outside the OpenAPI artifact.

Provisional partner paths are Sandbox-only. They are not proof of live certification,
official API compatibility, credentials, production access or provider approval.
The release verdict remains `BLOCKED`.

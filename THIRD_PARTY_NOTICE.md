# Third-party notice and provenance register

This file is an attribution index, not legal advice and not a license grant.
Exact versions, sources, scopes and release blockers are machine-readable in
`supply-chain/components.json`. No third-party source is vendored in this
repository.

| Component | Use | License identifier | Upstream notice/reference |
|---|---|---|---|
| pytest 8.4.2 | development test | MIT | https://pypi.org/project/pytest/8.4.2/ |
| Ruff 0.13.2 | development lint | MIT | https://pypi.org/project/ruff/0.13.2/ |
| Hatchling 1.27.0 | package build | MIT | https://pypi.org/project/hatchling/1.27.0/ |
| psycopg-binary 3.2.10 | integration test | LGPL-3.0-only; review required | https://pypi.org/project/psycopg-binary/3.2.10/ |
| TypeScript 5.6.3 | frontend development/build | Apache-2.0 | https://www.npmjs.com/package/typescript/v/5.6.3 |

Container bases and GitHub Actions are tooling/runtime inputs, also inventoried.
Their immutable digests and complete license materials have not been obtained
offline, so production release remains **BLOCKED**. Before release, an authorized
reviewer must retrieve upstream license texts from the recorded sources, verify
package/archive hashes, pin container and Action digests, and preserve the
review evidence. This table does not assert compatibility or compliance.

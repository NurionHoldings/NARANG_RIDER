# #052 Supply-chain, license and provenance audit

The audit is offline and evidence-conservative. It did not browse registries,
download packages, infer missing license terms, or make a legal conclusion.

## Decision

The inventory and consistency checker are complete, but production release is
**BLOCKED**. Python lock files do not contain archive hashes, Docker base images
and CI service images are not digest-pinned, GitHub Actions use moving major
tags, and complete upstream license texts have not been independently preserved.
The LGPL-tagged PostgreSQL test driver requires written specialist review.

These are recorded blockers, not discovered vulnerabilities and not findings of
license incompatibility.

## Evidence and enforcement

- `supply-chain/components.json`: component, scope, direct/transitive class,
  version, source, license identifier, hash/integrity and blocker.
- `THIRD_PARTY_NOTICE.md`: clean-room attribution index; no copied source.
- `scripts/check_supply_chain.py`: offline manifest/lock consistency, registry,
  npm integrity, package-script/native-binary review, container/Action inventory,
  license classification and vendored-source checks.
- `build/supply-chain-provenance.json`: deterministic SLSA-shaped statement and
  artifact SHA-256 list. It explicitly states that it is unsigned and is not a
  certification.

Any new dependency without inventory provenance and license classification
fails CI. Unknown, AGPL and GPL-family licenses are denied unless written
approval is added; review-class licenses remain release blockers. This is an
engineering gate only, not a legal interpretation.

Before production, authorized operators must obtain upstream artifacts and
license texts, verify hashes, pin immutable image/Action digests, complete legal
review, and regenerate the manifest in an approved builder. Main merge,
deployment, credentials, personal data and payment activity remain out of scope.

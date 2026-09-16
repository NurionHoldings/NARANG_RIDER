# #051 Protocol parser fuzz robustness

Status: implemented for the bounded internal release candidate. Production release
remains **BLOCKED** by the external gates recorded for RC.

## Boundary

This change adds a deterministic, dependency-free adversarial harness around
untrusted protocol surfaces:

- HTTP header names/values, content length, request paths and queries
- UTF-8 JSON bodies, duplicate keys, depth, node and collection budgets
- JWT envelope/header/claim shape (cryptographic verification remains in the
  authentication boundary)
- webhook signature/timestamp/event envelopes
- idempotency keys, quote/evidence grants and nonces
- partner status callbacks and import fields

The harness does not send network traffic, use production credentials, or process
personal information. The checked-in corpus contains synthetic identifiers only.

## Security invariants

1. Client-controlled malformed data produces a bounded `ProtocolError`, never an
   uncaught exception or server-error detail.
2. Duplicate authorization, branch, CSRF, webhook, content-length and
   idempotency headers fail closed, even when values are identical.
3. Duplicate security query parameters and duplicate JSON keys fail closed.
4. Unicode confusables, controls, invalid UTF-8, encoded separators and
   noncanonical paths are rejected before routing or authorization.
5. JWT `none`/HMAC confusion, remote key selectors and malformed signature
   lengths are rejected before key lookup. Signature comparison remains the
   responsibility of the existing constant-time verifier.
6. Depth, bytes, fields, nodes, strings, collections and run count have explicit
   ceilings. No parser performs unbounded recursion or allocation from declared
   client sizes.
7. Partner envelopes reject extension fields so a shadow role/branch cannot
   override the authenticated context.
8. Import fields reject record and spreadsheet-formula injection.

## Reproduction

CI profile:

```bash
pytest -q tests/test_protocol_fuzz.py
```

Manual bounded profile:

```bash
NARANG_FUZZ_PROFILE=large pytest -q tests/test_protocol_fuzz.py
```

The deterministic seed and minimized cases are stored at
`tests/corpus/protocol_regressions.json`. A failure record contains only the
case identifier and exception type; it never copies request content.

## Threat references

- OWASP API Security Top 10 (2023): API8 Security Misconfiguration and API4
  Unrestricted Resource Consumption.
- OWASP HTTP Headers Security Cheat Sheet: strict header handling and response
  minimization.
- RFC 9110 sections 5.2 and 7.4: field combination and request routing.
- RFC 8725 sections 2.1, 3.1 and 3.2: JWT algorithm verification and explicit
  typing/validation.
- RFC 8259 section 4: object member-name uniqueness interoperability risk.
- Unicode Technical Report #36: confusable and control-character security.
- CWE-444: inconsistent HTTP request interpretation.
- CWE-400: uncontrolled resource consumption.
- CWE-117 and CWE-1236: log and CSV/formula injection.

References were consulted as protocol requirements only; implementation is an
independent clean-room design. Retrieved: 2026-09-16.

## Ethernian review

The harness is evidence, not a production penetration test. Passing it does not
authorize deployment. It does not weaken the existing two-person financial
controls, branch scope revalidation, rider pay protections, or ARKAON
recommend-only boundary. Production release, secrets, personal data, payments,
deployment, and main merge remain outside this change.

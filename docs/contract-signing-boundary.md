# Area 5: contract conclusion and the signing boundary

**No qualified electronic signature exists in this project.** The test signer signs a status message with an
ordinary key. Legal QES issuance is ADAPTER READY, not implemented.

| Item | Content |
|---|---|
| **Requirement** | Contract conclusion and signature within the journey |
| **Existing before** | A mock contract record, and typed boundaries not connected to the flow |
| **Added** | `ContractExecutionService`, a neutral provider port, a labelled test signer, a mandatory gate |
| **Boundaries** | `GET /customer/applications/{id}/contract`, `POST .../contract/authorize`, `POST .../contract/cancel`, `POST /internal/signature-provider/callbacks`, `SignatureProviderPort` |
| **In the journey** | After the customer's confirmation and the mock checks, before submission. No signature, no submission |
| **Demonstration** | `python -m scripts.demo_trust_journey`, sections A and F |
| **Positive tests** | `tests/test_trust_contract_signing.py`: the path to `OPENED`, with the hash in package, signature and audit |
| **Negative tests** | See below |
| **External dependency** | A QTSP on an EU trusted list, a contract, sandbox credentials |
| **Limitation** | Documents are references and digests, not a rendered contract. Signature validation is the provider's signed statement; no certificate chain is checked |
| **Classification** | LOCAL VERIFIED with a test signer. QES ADAPTER READY |

## The flow

1. **Package:** built once from the action the customer confirmed: product, version, terms digest, documents, channel, attribution
   and policy references. Canonical JSON, SHA-256. Append-only by trigger.
2. **Review:** the customer sees the package and its hash on the bank's page.
3. **Authorization:** the customer's explicit act, with session, CSRF and the exact hash. Agent credentials are refused.
4. **Signing:** the bank starts a session at the provider through the port, then shows a link. A link, because the pages' CSP does not
   let a form post end at another origin, and it stays strict.
5. **Result:** the provider's signed message must match session, provider reference, application, hash, signatory and level.
6. **Evidence:** recorded with `legal_effect: false`.
7. **Gate:** `signature_gate` recomputes the package hash at submission, and requires a signed session for that hash and signatory.

## Failure handling

| Situation | Result |
|---|---|
| Declined, failed, provider-reported expiry | Parked in `MANUAL_REVIEW` with a case. A person may resume or reject, and resuming does not satisfy the gate |
| Session past its time | A late `SIGNED` is refused. The page offers a new attempt |
| Customer cancels | Closed. A late `SIGNED` is refused |
| Provider unavailable | Recorded. Nothing else changes, and the customer can retry |
| Duplicate callback | Acknowledged, no effect |
| New event for a decided session | Refused |
| Different hash, signatory, application, reference or level | Refused and audited |
| Other key, other type, wrong audience or issuer, expired | Refused and audited |
| Stored package changed behind the trigger | Submission refused: `contract_package_integrity` |

## No imitation of a QES

- The level comes from configuration. A message with another level is refused, so a test signer cannot become a "QES".
- `signature_level=QES` is refused without a named `qtsp_id`, and no such value exists here.
- `legal_effect` is false in the package, the session and the audit. Both test pages say what they are.

## Connecting a real QTSP

Implement `SignatureProviderPort`. Remote signing commonly follows the Cloud Signature Consortium API, and the QTSP's own documentation
decides. A real integration also needs: validation of the signature and certificate against the EU trusted list, storage of the
signed document, and a legal decision on which form each contract needs.

## Sources

| Claim | Source | Read in this work |
|---|---|---|
| A QES has the effect of a handwritten signature | [Regulation (EU) No 910/2014, Art. 25(2)](https://legislation.gov.uk/eur/2014/910/article/25/adopted/data.pdf) | Quoted through a search result. EUR-Lex could not be read automatically |
| Definition of a QES | Regulation (EU) No 910/2014, Art. 3(12) | From knowledge. Legal must verify |
| Which form a depot contract needs | Not established | **Assumption: legal must decide. This project does not assume QES is required** |

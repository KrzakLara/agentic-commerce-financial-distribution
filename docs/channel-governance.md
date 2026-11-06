# Area 8: channel conflicts and distribution attribution

Synthetic. The sample contracts are invented and do not represent real fund-distribution contracts.

| Item | Content |
|---|---|
| **Requirement** | Avoid sharpening channel conflicts |
| **Existing before** | Attribution from the registered channel, fixed with the consent. No conflict handling |
| **Added** | `DistributionGovernanceService`: claims, rules, conflict detection, review, sample commission |
| **Boundaries** | `POST /v1/applications/{id}/referrals`, `app/services/distribution_governance.py`, `resolve_by_person` (trusted code only: no HTTP route) |
| **In the journey** | Decided when the customer confirms, and again with each new claim. Referenced in the contract package |
| **Demonstration** | The demo's `referral_carried` line and section B |
| **Positive tests** | `tests/test_trust_governance_policy.py` |
| **Negative tests** | Forged, unknown partner, outdated terms, expired, wrong product, ended contract, replay, status token, agent naming a partner |
| **External dependency** | Real contracts, partner onboarding, compliance |
| **Limitation** | No operator console or page for conflicts, only the service method. Referrals carry no customer binding. Commission is never paid |
| **Classification** | LOCAL VERIFIED with synthetic contracts |

## Roles kept apart

| Role | In this module |
|---|---|
| Product provider | States the product's terms |
| Depot operator | The only source of `OPENED`. No attribution role |
| Distributor or partner bank | May be attributed under a contract, never set by an agent |
| Advising party | This journey gives no advice |
| Agent's technology provider | Never receives attribution |
| Customer | May choose the channel, and sees the result |

## Rules

Claims come from three sources: the channel the customer confirmed, a partner's signed referral, and a verified existing relationship. An agent can only
carry the signed proof.

| Situation | Result |
|---|---|
| No valid partner claim | `DIRECT_NO_PARTNER`, no commission |
| One partner | `PARTNER_ATTRIBUTED`, sample commission from a current contract, or `NOT_ELIGIBLE` |
| Two or more partners | `CONFLICT_HUMAN_REVIEW`. No partner, **and not the provider**. Commission `WITHHELD` |
| A person settles it | `RESOLVED_BY_PERSON`, only among accepted claims. A person cannot invent a partner |

## The example cases

| Case | Result |
|---|---|
| Existing partner customer starts through an agent | Attributed from the verified relationship |
| New customer discovers the provider directly | Direct, no commission |
| Referral changes halfway | A new decision: conflict. The earlier decision is unchanged |
| Two partners claim | Conflict, a person decides |
| Forged referral | Rejected and recorded |
| Outdated terms | Rejected |
| Product not eligible | Attributed, commission `NOT_ELIGIBLE` |
| Conflict needs review | Commission withheld. The customer's account continues |

## Design decision: a conflict does not stop the account

A dispute between partners is commercial, and the customer should not pay for it. So it withholds the commercial result and leaves the account alone.
**Compliance must confirm this.** If attribution has to be settled before opening, the gate is one line in `TrustGates.submit_missing`.

## Evidence and disputes

Claims and decisions are append-only by trigger. Each decision stores its inputs, explanation, policy version and decider, and `decide_attribution` is a pure
function, so a dispute can be recomputed. The trigger protects against a buggy service, not someone with database access (F-6).

## Not assumed

- **That commission is permissible:** every attributed result has `inducement_review_required: true` and `payable: false`. WpHG section 70(1) restricts
  inducements unless they are designed to improve the quality of the service for the customer and are disclosed.
- **That an LLM has any part:** no model reads or writes attribution. Rules and partner identity come from code and signed proofs.
- **That a partner's identification can be reused:** GwG section 17 allows reliance on third parties only under its conditions. See
  [regulatory-policy-matrix.md](regulatory-policy-matrix.md).

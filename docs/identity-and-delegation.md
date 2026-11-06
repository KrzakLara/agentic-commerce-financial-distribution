# Areas 3 and 4: customer identity and delegation to an agent

## 1. Customer identity

| Item | Content |
|---|---|
| **Requirement** | Identity verification independent of the agent |
| **Existing before** | A labelled DEV sign-in (finding F-1) and an OIDC boundary that failed closed, never connected to a provider |
| **Added** | A working Authorization Code login with PKCE, a separate local provider, session binding, logout, and a vendor-neutral proofing boundary |
| **Boundaries** | `/customer/oidc/login`, `/customer/oidc/callback`, `/customer/oidc/logout`, `POST /customer/handoffs/{id}/identity-proofing`, `POST /internal/identity-proofing/callbacks` |
| **In the journey** | The customer signs in before approving the agent. Proofing is a precondition of the confirmation |
| **Demonstration** | `python -m scripts.demo_trust_journey`, Chrome run in [integration-evidence.md](integration-evidence.md) |
| **Positive tests** | `tests/test_trust_identity_delegation.py`; the separate Authlib provider through real processes in `tests/test_trust_journey.py` |
| **Negative tests** | Wrong `iss`, `aud`, `nonce`, `exp`, `iat`, `sub`; another key; wrong PKCE; replayed state; another browser, with and without its own valid flow; mix-up `iss`; changed discovery; open redirect; agent credential; account switch |
| **External dependency** | A bank's identity provider and client registration. For proofing: a contracted eID, video or EUDI wallet provider |
| **Limitation** | No external provider was used. The local provider has synthetic accounts and in-memory state. Logout ends the bank's session, not the provider's |
| **Classification** | Login LOCAL VERIFIED, ADAPTER READY for a real provider. Regulated proofing BLOCKED |

### Authentication is not identity proofing

A login proves that someone controls an account at that provider. It sets no proofing or KYC status, and the session is
recorded as `oidc_authenticated_not_proofed`.

`app/services/identity_proofing.py` is the separate boundary:

- **Outcomes:** pending, verified, rejected, expired, manual review. Anything but verified goes to a person.
- **Callback:** a JWT signed with a pinned key, with issuer, audience, time window and one-time id, bound to case,
  application, customer and provider reference.
- **Data:** statuses and labels only. A field outside the fixed list, such as `document_image`, refuses the whole message.
- **Regulatory evidence:** always false. `proofing_provider_regulated` is refused outside production and has no implementation
  behind it. A provider's claim of regulatory evidence is refused.
- **DEV sign-in:** a DEV session cannot start proofing, so identity cannot be completed with it.

### Customer reference

The reference is `cid-` plus a hash of the verified issuer and subject. The agent never names it: the bank tells the agent the
pseudonymous reference after the customer approved. An authenticated customer cannot open another customer's application: the
existing checks refuse it.

### F-1 stays documented

With `customer_identity_mode=dev_simulated`, the default for the older scenarios, anyone with the handoff values can use the
DEV sign-in as any customer. In `oidc` mode with a complete configuration, the DEV sign-in is off. Without one, customer pages fail
closed.

## 2. Delegation

| Item | Content |
|---|---|
| **Requirement** | Controlled authorization of an external personal agent |
| **Existing before** | Scoped opaque tokens from a trusted DEV bootstrap, not tied to any customer decision |
| **Added** | Registered agents, a customer's approval on the bank's page, a bound, narrowed, revocable delegation |
| **Boundaries** | `POST /v1/delegation/par`, `POST /v1/delegation/token`, `/.well-known/oauth-authorization-server`, `/customer/delegations/authorize`, `/customer/delegations`, `POST /customer/delegations/{id}/revoke`, `python -m scripts.dev_agent_registry` |
| **In the journey** | Before the agent can create anything |
| **Demonstration** | The demo's sections A and D |
| **Positive tests** | Valid workflow, narrower scopes narrow the application's tokens |
| **Negative tests** | Expired, revoked, cross-customer, scope escalation, other action, replayed proof, replayed assertion, other key, bearer downgrade, second token collection, other agent, agent self-approval, agent on customer routes |
| **External dependency** | A FAPI-conformant authorization server |
| **Limitation** | A local protocol. No refresh tokens. Agent registration is a trusted console, without dynamic registration. Depends on client keys being protected |
| **Classification** | LOCAL VERIFIED as a local protocol. FAPI 2.0 CONCEPT ONLY |

### The protocol, and what it is not

| Step | Mechanism | Standard it takes from |
|---|---|---|
| Agent authenticates | Signed assertion of a registered key, one use | RFC 7523, `private_key_jwt` |
| Request goes to the bank first | Scopes and exact action, not via the browser | Shape of RFC 9126 (PAR) |
| Exact action | `authorization_details`: provider, product, channel, verified against the catalog | Shape of RFC 9396 |
| Customer decides | Bank page, session, CSRF | - |
| Agent waits | Polling with `authorization_pending`, `slow_down`, `access_denied`, `expired_token` | RFC 8628 error codes |
| Sender constraint | Key thumbprint fixed at the request; proof per request with `htm`, `htu`, `iat`, `jti`, `ath` | RFC 9449 (DPoP) |

**This is not FAPI 2.0.** FAPI 2.0 requires PAR with the authorization code flow, PKCE and client authentication, and sender-constrained
tokens. This project chose polling because a personal agent may have no redirect listener. Polling grants carry a phishing risk: a
customer may approve a request somebody else started. The page shows the agent and the exact action, which reduces this risk and does
not remove it.

**Evaluation:**

| Option | Result |
|---|---|
| FAPI 2.0 with a contracted server | The production target |
| Building an authorization server here | Rejected: out of scope, and the risk of an invented protocol |
| DPoP | Adopted: no client certificates needed |
| mTLS | Not adopted locally: needs a PKI. A production alternative named by FAPI 2.0 |

**Integration path.** A real server would issue the tokens instead of `DelegationService.exchange`. The bank keeps
`check_request` and `enforce_action` behind the same interface, and only its token check changes.

### What the agent can and cannot do

- **May:** discover, read, prepare, carry a partner's signed referral, ask for the customer's confirmation, read the coarse status,
  submit after the customer acted.
- **Cannot:** impersonate the customer, widen its scopes, confirm or sign, decide a check, open the account. There is no action for
  any of these, and agent credentials are refused on customer routes.

**Effective scopes** are what the token holds and what the customer approved, whichever is less. Revoking a delegation revokes all its
grants at once, and the bearer format never works for a delegated token.

## 3. Sources

| Claim | Source | Read in this work |
|---|---|---|
| FAPI requires PAR, PKCE S256, sender-constrained tokens (mTLS or DPoP), `private_key_jwt` or mTLS | [FAPI 2.0 Security Profile](https://openid.net/specs/fapi-security-profile-2_0-final.html) | Yes |
| RFC numbers 9449, 9126, 7636, 9207, 8705 | The same profile's references | Yes |
| RFC 7523, 8628, 9396, 7638, 8414 | IETF RFC series | From knowledge, not re-read |
| Code flow, nonce, ID token | [OpenID Connect Core 1.0](https://openid.net/specs/openid-connect-core-1_0.html) | From knowledge, not re-read |
| Video identification rules remain BaFin's administrative practice | [BaFin Circular 3/2017 (GW)](https://www.bafin.de/SharedDocs/Veroeffentlichungen/EN/Rundschreiben/2017/rs_1703_gw_videoident_en.html) | Search result. Compliance must check the current status |

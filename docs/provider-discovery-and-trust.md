# Areas 1, 2 and 7: discovery, provider trust, brand presence

Sources are listed in [section 4](#4-sources). Each one is tied to the claim it supports.

## 1. Public agent discovery

| Item | Content |
|---|---|
| **Requirement** | Reachability for customer agents: discovery and a verifiable provider |
| **Existing before** | A signed internal catalog and REST routes. No standard discovery, no independent client |
| **Added** | A2A 1.0 Agent Card, JSON-RPC subset, versioned metadata, a separate client process |
| **Boundaries** | `GET /.well-known/agent-card.json`, `POST /a2a/v1`, `GET /v1/agent/catalog`, `GET /v1/agent/documents/{id}?version=`, `Trusteq-Metadata-Version` header |
| **In the journey** | The agent's first step. Onboarding goes through the same services and policy as REST |
| **Demonstration** | `python -m scripts.demo_trust_journey`, or `python -m external_agent.cli verify --config ...` |
| **Positive tests** | `tests/test_trust_discovery.py`; official `a2a-sdk` 1.3.0 parsed the card and called the bank (once, in a scratch environment) |
| **Negative tests** | Malformed card, wrong version, card pointing elsewhere, tampered card, unknown skill, free text, missing credential, unsafe URLs, redirects, oversized answers |
| **External dependency** | A public HTTPS domain and a directory for global reachability |
| **Limitation** | A subset of A2A: `SendMessage`, `GetTask`, `ListTasks`, `CancelTask`; no streaming or push. **Global internet reachability is unverified, and publishing a card does not make anyone find it.** Local discovery means a known origin or the configured federation's list |
| **Classification** | LOCAL VERIFIED. Global discovery BLOCKED |

**Version pinned:** A2A protocol 1.0, binding `JSONRPC`. A request with any other `A2A-Version`, or none, gets
`VersionNotSupportedError` (-32009) with the supported versions.

**Skills:** `product-information`, `product-comparison`, `document-retrieval`, `human-escalation` are public.
`depot-onboarding` needs a delegation. Without one, the answer is `TASK_STATE_AUTH_REQUIRED` and nothing is created.
Public reading stores nothing at the bank.

**Requests are structured.** The agent names a skill in a data part. The bank does not interpret free text, so there is no
natural-language path into a decision.

**SSRF.** `external_agent/net.py` allows only configured hosts, requires HTTPS (plain HTTP only on loopback with an
explicit setting), never follows a redirect, has strict timeouts and limits the size. A federation hint is used only if it
names an anchor the agent already trusts. The bank's own outbound calls follow the same rule in
`app/integrations/safe_http.py`.

## 2. Independently verifiable provider identity

| Item | Content |
|---|---|
| **Requirement** | A verifiable provider identity, separate from an HMAC integrity check |
| **Existing before** | HMAC-signed manifests with a shared DEV key, verified by the bank itself |
| **Added** | ES256 signing through `joserfc`, Entity Statements, a separate synthetic authority, a verifier in the agent |
| **Boundaries** | `GET /.well-known/openid-federation`; at the authority `/fetch?sub=`, `/list`; `external_agent/trust.py` |
| **In the journey** | The agent verifies before it reads the card, and it checks the card and profile with the keys the authority named |
| **Demonstration** | The same demo, section C |
| **Positive tests** | Correct chain, rotation while both keys are named |
| **Negative tests** | Forged provider with the same key id, unknown anchor, wrong anchor key, unlisted or revoked entity, withdrawn key, expired, tampered, unsigned, wrong `typ`, self-signed rogue claiming a licence |
| **External dependency** | A real federation or an institution-approved trust authority; an authoritative register |
| **Limitation** | A subset of the specification's rules, without metadata policy, trust marks or a resolver. Chain length: leaf and anchor |
| **Classification** | LOCAL VERIFIED, conformance not claimed |

**Library decision.** The Python federation packages found on PyPI are old: `fedservice` was last released in 2020, so none was
adopted for a specification finalized in February 2026. The OpenID Foundation lists other implementations, such as Nimbus (Java),
which a production system should evaluate first. `joserfc` does the cryptography here; only the checks of section 3.2 are this
project's code.

**The four answers never merge:**

| Answer | What it means | Value here |
|---|---|---|
| A. Metadata integrity | The signature over this metadata holds | `VERIFIED` |
| B. Organizational identity | What the configured authority states | `ASSERTED_BY_SYNTHETIC_AUTHORITY_TEST_ONLY` |
| C. Federation membership | A chain reaches an anchor this agent trusts | `MEMBER_OF_CONFIGURED_FEDERATION` |
| D. Regulatory authorization | May it provide financial services | `UNKNOWN_NO_AUTHORITATIVE_LOOKUP` |

**D is never derived from A, B or C,** and what a provider says about itself is ignored. BaFin's company database exists, but
this project established no machine-readable lookup (an automated read of the portal failed), so nothing here invents a status.

## 3. Agent-ready brand presence

| Item | Content |
|---|---|
| **Requirement** | Trustworthy presence and communication without a website visit |
| **Existing before** | Provider metadata for the bank's own use |
| **Added** | A signed provider profile |
| **Boundaries** | `GET /v1/agent/profile` returns `{profile, signature}` |
| **In the journey** | The agent reads it after the card, and checks its signature against the verified chain |
| **Demonstration** | The demo's `profile_verified` line |
| **Tests** | `test_an_agent_gets_everything_from_signed_machine_readable_metadata`, `test_a_changed_profile_fails_its_signature` |
| **External dependency** | A real institution's permission and data, DNS or certificate proof of the domain |
| **Limitation** | The domain is declared, not proven: `DECLARED_IN_SIGNED_METADATA_NOT_DNS_VERIFIED`. English only |
| **Classification** | LOCAL VERIFIED, for a synthetic institution |

An agent can, without scraping any page: find the provider, verify its configured trust status, read its capabilities, get
approved product facts, see which sources are authoritative, understand authentication requirements and locate the route to a person.

The profile is explicit about what it is not: SEO or marketing discoverability, proof of a financial licence, or affiliation with
a real institution.

## 4. Sources

| Claim | Source | Read in this work |
|---|---|---|
| Card at `/.well-known/agent-card.json`, fields, `A2A-Version`, -32009, RFC 8785 canonicalization | [A2A specification](https://a2a-protocol.org/latest/specification/), `specification/a2a.proto` in [a2aproject/A2A](https://github.com/a2aproject/A2A) | Yes, main branch on 2026-11-06, which names 1.0.0 as the latest released version |
| Entity Statement rules, `typ` value, `authority_hints` | [OpenID Federation 1.0, Final](https://openid.net/specs/openid-federation-1_0.html), section 3.2 | Yes, text dated February 2026 |
| Other implementations | [OpenID Federation implementations](https://openid.net/developers/openid-federation-implementations/) | Search result only |
| A register exists | [BaFin databases](https://www.bafin.de/EN/PublikationenDaten/Datenbanken/Datenbanken_node_en.html) | Search result only. Whether it offers a usable API is not established |

**Proposal, not a norm:** the custom metadata names (`trusteq_financial_provider`, `trusteq_trust_assessment`) and the
profile's shape are this project's own.

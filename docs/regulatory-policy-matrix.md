# Area 9: regulatory policy matrix and evidence

**An engineering control, not legal advice and not evidence of compliance. Passing tests proves nothing about the law.** A client-specific legal and compliance
review is mandatory, and the applicable version of every text must be checked at implementation time.

| Item | Content |
|---|---|
| **Requirement** | Regulatory sensitivity, with evidence |
| **Existing before** | Mock checks and a human-review path. No versioned applicability decision |
| **Added** | `app/services/regulatory_policy.py`, `TrustGates`, append-only `policy_decisions`, version in the audit |
| **Boundaries** | `evaluate(service, facts)`, `applicability_matrix()`, `policy.evaluated` audit events |
| **In the journey** | Evaluated at the customer's confirmation and at the agent's submission |
| **Demonstration** | The demo's section B |
| **Positive tests** | `tests/test_trust_governance_policy.py`: journey records both decisions with their version |
| **Negative tests** | Missing result refuses the submission; model as decider refused; append-only; advice refused |
| **External dependency** | Legal and compliance sign-off, the legal entity's actual licence and service model |
| **Limitation** | See "Assumptions" |
| **Classification** | LOCAL VERIFIED as an engineering control. Legal compliance NOT CLAIMED |

Policy version: `regulatory-matrix-2026.11-engineering`.

## The matrix

| Rule | Account opening | Non-advised order | Execution-only order | Advice or portfolio mgmt. | Gate |
|---|---|---|---|---|---|
| Identification (AML) | **Required** | Not triggered | Not triggered | Not triggered | Blocking |
| Appropriateness | **Not triggered**, recorded for later orders | **Required** | **Unresolved**: compliance must confirm | Not triggered | Warning, or a person |
| Suitability | Not triggered | Not triggered | Not triggered | **Not offered** | Blocking |
| Key information | Not triggered: no instrument sold | Unresolved | Unresolved | Not triggered | A person |
| Signature level | **Unresolved**: legal decides the form | - | - | - | Blocking in the prototype |
| Tax information | **Unresolved**: prototype gate | - | - | - | Blocking in the prototype |
| Inducements | Unresolved if a partner is attributed | Same | Same | Same | A person |
| Data minimization | Required | Required | Required | Required | None |
| No AI compliance decision | Required | Required | Required | Required | Blocking |

Only this prototype's journey, account opening, is connected to the workflow. **There is no order step:** the other columns are evaluated in tests only.

**States:** `REQUIRED`, `NOT_TRIGGERED`, `UNRESOLVED` (never counts as satisfied), `NOT_OFFERED`. **Evidence:** `SATISFIED`, `SATISFIED_SYNTHETIC` (the mechanism ran, with no regulatory
value), `MISSING`, `NOT_NEEDED`.

**Fail closed where a gate is genuine.** Identification missing refuses the submission. **No automatic rejection where the rule calls for a warning:** a knowledge gap gives a warning and an
acknowledgement, and the customer decides.

**The AI recommends nothing to compliance.** The rules are code. A request naming a model as the decider is refused.

## Sources, and what each supports

**Normative text read in this work**, from gesetze-im-internet.de on 2026-11-06:

| Source | What it says, in short | Used for |
|---|---|---|
| [GwG section 10(1) no. 1](https://www.gesetze-im-internet.de/gwg_2017/__10.html) | General due diligence includes identifying the contracting party | Identification rule |
| [GwG section 11(1)](https://www.gesetze-im-internet.de/gwg_2017/__11.html) | Identify before establishing the business relationship | Gate before confirmation |
| [GwG section 12(1)](https://www.gesetze-im-internet.de/gwg_2017/__12.html) | Verifying natural persons, for example with an official ID | Why a login is not proofing |
| [GwG section 17(1)](https://www.gesetze-im-internet.de/gwg_2017/__17.html) | Reliance on third parties, for specified parties | Partner identification is not reused |
| [GwG section 8(4)](https://www.gesetze-im-internet.de/gwg_2017/__8.html) | Records kept five years, destroyed after ten at the latest | Retention not modelled, a named gap |
| [WpHG section 63(10)](https://www.gesetze-im-internet.de/wphg/__63.html) | Before services other than advice and portfolio management, obtain knowledge and experience; assess appropriateness | Appropriateness for orders |
| [WpHG section 63(11)](https://www.gesetze-im-internet.de/wphg/__63.html) | Paragraph 10 does not apply under listed conditions, for orders at the customer's initiative | Execution-only is never assumed |
| [WpHG section 64(3)](https://www.gesetze-im-internet.de/wphg/__64.html) | For advice: knowledge, finances including ability to bear losses, objectives | Suitability is separate |
| [WpHG section 70(1)](https://www.gesetze-im-internet.de/wphg/__70.html) | No inducements unless designed to improve quality, among other conditions | Commission not payable |

**Not re-read.** EUR-Lex could not be read automatically, so these rest on search results or knowledge, and **legal must verify them**:

| Source | Used for | Basis |
|---|---|---|
| MiFID II Art. 24(9), 25(2), 25(3), 25(4) | Same distinctions at EU level | Secondary sources; [ESMA guidelines on appropriateness and execution-only](https://www.esma.europa.eu/node/100352) were found, not read |
| Regulation (EU) No 1286/2014 (PRIIPs) Art. 13 | Key information timing | From knowledge |
| Regulation (EU) No 910/2014 (eIDAS) Art. 3(12), 25 | Signature rule | Art. 25(2) quoted via a search result, Art. 3(12) from knowledge |
| GDPR Art. 5(1)(c), (e), Art. 22 | Minimization, automated decisions | From knowledge |
| BaFin Circular 3/2017 (GW), and BaFin's GwG interpretation and application guidance | Identification methods | [Search result](https://www.bafin.de/SharedDocs/Veroeffentlichungen/EN/Rundschreiben/2017/rs_1703_gw_videoident_en.html). Current status for compliance to check |
| MaComp | Conduct rules | Not read |

## Assumptions needing legal and compliance confirmation

These are **proposals of this project, not requirements of the law.**

1. Which legal entity holds each duty, and under which licence.
2. Tax information is a gate before confirmation. The fields and the timing are a prototype decision, and the church-tax field may touch GDPR Art. 9.
3. Asking knowledge questions at onboarding, for later orders: whether and how long the answers may be used.
4. A conflict in attribution does not stop the account.
5. Which signature form each contract needs. QES is not assumed.
6. Whether any agent-initiated flow affects the customer-initiative condition of section 63(11).
7. Retention periods, and how they fit data minimization.
8. Whether delegation to an agent has effects under civil law, GwG, or for electronic signatures.

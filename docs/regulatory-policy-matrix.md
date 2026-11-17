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

**One decision per service, in the journey** (2026-11-07). `service_applicability()` puts the columns above side by side, and each
recorded policy decision carries them with a `handling` value:

| Service | Offered here | Handling |
|---|---|---|
| Account opening | Yes | Proceed with the account-opening gates. Appropriateness is not triggered by the opening itself |
| Product information | Yes | Non-personalized information only |
| Non-advised order | No | Would need the knowledge test and a warning |
| Execution-only order | No | Would need compliance to confirm the exemption. Never assumed |
| Investment advice, portfolio management | No | Would need a suitability assessment |

An agent that asks the bank to start an application for another service gets `service_not_offered`, the attempt is audited as
`application.service_refused`, and no application is created. Before this, the request was silently treated as an account opening. The
questionnaire on the customer's page is recorded as assessed for later non-advised orders (`assessed_for_service`). For the account
opening it is a step of this prototype, where the customer sees the result and decides, and **no answer rejects the account**. Whether
answers given at the opening may later be relied on for such an order, and for how long, is assumption 3 below. There is no order
step here, so nothing relies on them yet.

**States:** `REQUIRED`, `NOT_TRIGGERED`, `UNRESOLVED` (never counts as satisfied), `NOT_OFFERED`. **Evidence:** `SATISFIED`, `SATISFIED_SYNTHETIC` (the mechanism ran, with no regulatory
value), `MISSING`, `NOT_NEEDED`.

**Fail closed where a gate is genuine.** Identification missing refuses the submission. **No automatic rejection where the rule calls for a warning:** a knowledge gap gives a warning and an
acknowledgement, and the customer decides.

**The AI recommends nothing to compliance.** The rules are code. A request naming a model as the decider is refused.

## Ownership and automation boundaries

Case-study page 3 asks which actor carries each regulatory requirement and what may be automated. The two tables below
answer that for each requirement, R01 to R18.

**Text read** means the provision was read at its official source on the date in the source list. Whether it applies to a
given provider, service or step is a separate question. Where no official source settles that, the row says
**LEGAL VALIDATION REQUIRED**.

**The reference case.** The accountable party is named for the case in the Ist-Analyse
([research/ist-analyse-union-investment.md](research/ist-analyse-union-investment.md)):
- the depot contract is with Union Investment Service Bank AG, called "the depot bank" below;
- the funds come from the group's management companies (KVGs);
- cooperative partner banks advise and distribute.

Who performs which task inside that structure is inferred from public documents. The provider has not confirmed it.

### Scope and ownership

| ID | Requirement | Legal source | Triggered by | Service | Accountable | Executed by | Applicability |
|---|---|---|---|---|---|---|---|
| R01 | Identify the customer and verify the identity | GwG §10(1) no. 1, §11(1), §11(4) no. 1, §12(1), §13(1); BaFin circular 3/2017 (GW) | Establishing the business relationship | Account opening | The depot bank, as obliged entity | One method under §12(1): ID document checked in person, online ID (§18 PAuswG, from age 16), video identification under the circular, or a QES plus a transfer from the customer's own payment account | Text read. Identification comes before the relationship; §11(1) allows completion during it only if that is needed to avoid interrupting business and the risk is low. In the reference case the partner bank checks the ID in person in the branch, and remote channels use online ID or video identification (Ist-Analyse S22, S24, S26). On which legal basis: LEGAL VALIDATION REQUIRED |
| R02 | Further due diligence: beneficial owner, purpose, PEP status, monitoring | GwG §10(1) no. 2 to 5 | Same | Account opening, then the whole relationship | The depot bank | KYC and AML screening, the AML officer | Text read |
| R03 | Reliance on third parties and outsourcing of due diligence | GwG §17(1), (5) | Using a partner bank or a provider for R01 or R02 | Account opening | The depot bank: responsibility stays with it | The third party, under a contract | Text read. The partner bank performs the check (Ist-Analyse S22); whether as reliance under §17(1) or outsourcing under §17(5) is not stated: LEGAL VALIDATION REQUIRED |
| R04 | Retain due-diligence records | GwG §8(1), (4) | Performing due diligence | All | The depot bank | The bank's archive | Text read: five years from the end of the year in which the relationship ends; destroyed after ten years at the latest |
| R05 | Account legitimation and the tax identification number | AO §154(1) to (2c), §139b | Opening an account | Account opening | The depot bank, as the institution keeping the account | Onboarding; a query to the Federal Central Tax Office (BZSt) if the customer gives no number | Text read. That a securities account falls under §154: inferred, LEGAL VALIDATION REQUIRED |
| R06 | Exemption order (Freistellungsauftrag) and church-tax data | EStG §44a(1), (2), (2a), §45d(1), §51a(2b) to (2e); AO §93c | Capital income paid through the account. The church-tax query is made when the relationship starts and then yearly | Custody and income payments | The depot bank, as paying and withholding agent | Tax operations, BZSt interfaces | Text read |
| R07 | Tax residence self-certification (CRS) and US status (FATCA) | FKAustG §13(2), (2a), (4), §3a; FATCA-USA-UmsV §5 | Opening a new account for an individual | Account opening | The depot bank, as reporting financial institution | Onboarding, tax reporting | CRS text read. FATCA: §5 read; the procedures of the agreement's Annex I and any thresholds were not read: LEGAL VALIDATION REQUIRED |
| R08 | Appropriateness for non-advised services | WpHG §63(10); MiFID II Art. 25(3); Regulation (EU) 2017/565 Art. 55, 56 | An investment service other than advice or portfolio management, such as a non-advised fund order | The later order, not the account opening | The firm providing the service; for direct orders, the depot bank | The order flow, with rules owned by compliance | Text read. It attaches to the service; that the opening alone does not trigger it is this project's reading. The reference provider assesses appropriateness for complex funds and collects the answers in its remote opening form (Ist-Analyse S1 §7.2, S23). Whether answers collected at opening may serve a later order (assumption 3): LEGAL VALIDATION REQUIRED |
| R09 | Execution-only exemption | WpHG §63(11); MiFID II Art. 25(4); Regulation 2017/565 Art. 57 | An order at the client's initiative in a listed non-complex instrument | Execution-only order | The firm executing the order | Compliance decides that every condition holds | Text read. The reference provider uses it for UCITS funds and non-complex instruments (Ist-Analyse S1 §7.2). Every condition must hold: the client's initiative, a listed instrument (Art. 57 sets when others count as non-complex), no credit, and an express notice that no assessment takes place. Whether an agent-prepared order is at the client's initiative (assumption 6): LEGAL VALIDATION REQUIRED |
| R10 | Suitability, for advice and portfolio management | WpHG §64(3), (4); MiFID II Art. 25(2); Regulation 2017/565 Art. 54, 55 | Investment advice or portfolio management | Advice. Not offered here. In the reference case the depot bank does not advise and the partner banks do | The advising firm | The advisor | Text read. Not triggered by this journey, because no advice is given. A request for advice goes to a person |
| R11 | Information on costs and charges | WpHG §63(7) sentences 3 and 4; Regulation 2017/565 Art. 50 | Providing a service: ex-ante, aggregated | Orders. For the account itself, the fee schedule | The firm providing the service | Disclosure documents; product governance owns the figures | Text read. Whether the account fee alone triggers §63(7) at opening: LEGAL VALIDATION REQUIRED |
| R12 | Key information document (KID) | PRIIPs Regulation (EU) 1286/2014 Art. 13(1), Art. 32(1) as amended by Regulation (EU) 2021/2259; KAGB §164(1), (1a) | Advising on or selling a PRIIP, such as fund units, to a retail investor | The first fund order, not the account opening | The KVG prepares it; the firm that advises or sells provides it | Product governance; the selling bank's disclosure | Text read. The UCITS exemption ended on 31 December 2022, so UCITS funds need a PRIIPs KID too. That an opening without an order triggers no KID is this project's reading. The reference provider's application contains a first purchase order, and the partner bank confirms that the KID came before the contract (Ist-Analyse S22), so in practice the duty applies at the opening: LEGAL VALIDATION REQUIRED for a pure opening |
| R13 | Inducements: distribution commissions and partner attribution | WpHG §70(1), (4); MiFID II Art. 24(9) | Accepting or granting fees or commissions in connection with a service | Distribution and advice | Each firm that accepts or grants the inducement | Distribution management, compliance, partner contracts | Text read. The depot bank states that it grants its partners non-monetary inducements, such as cost calculators and forms (Ist-Analyse S1). Monetary remuneration of the partners was not found in the documents read. The demonstration's attribution and commission rules are no legal model: LEGAL VALIDATION REQUIRED |
| R14 | Records and the recording of communications | WpHG §83(1), (3), (5), (6), (8) | Investment services. Telephone and electronic communication about client orders is recorded | Orders and their communication | The investment firm | Records and the communications archive | Text read: kept for five years. Whether an agent's API conversation is electronic communication about an order: LEGAL VALIDATION REQUIRED |
| R15 | Contract form, consent and signature | BGB §126b; DepotG §10(1), §13(1); eIDAS Regulation (EU) 910/2014 Art. 3(12), 25(1), (2) | Concluding the account contract and making specific declarations under the DepotG | Account opening | The contracting parties; the depot bank designs the process | The bank's contract process. A trust service provider only where a form requires one | No source found that prescribes a form for the depot contract itself. Specific DepotG declarations need text form and must be express; a §10(1) declaration is made per transaction and may not sit in the general terms. A QES has the effect of a handwritten signature (Art. 25(2)) but was not found to be required. The reference provider's channels use a handwritten signature or a TAN release, and no QES was found (Ist-Analyse S22, S27, S36): **QES NOT ASSUMED**, LEGAL VALIDATION REQUIRED per declaration |
| R16 | Accept the EUDI Wallet | Regulation (EU) 2024/1183: eIDAS Art. 5a(1), 5f(2) | A private relying party that must use strong user authentication, banking and financial services included, when the user asks | Identification and login, by 2030 | The bank, as relying party | The bank's identity platform | Text read. The deadline runs 36 months from the implementing acts, whose dates were not checked. Micro and small enterprises are exempt |
| R17 | Data protection: minimization, storage limits, special categories, automated decisions | GDPR Art. 5(1)(c), (e); 6(1)(b), (c); 9; 22 | Any processing of personal data | All | Each controller: the bank, and the agent's operator for its own processing | Data-protection officer, engineering | Text read. Church-tax data as religion-related data under Art. 9 is this project's inference (assumption 2). The agent operator as a separate controller: LEGAL VALIDATION REQUIRED |
| R18 | Outsourcing and ICT resilience | KWG §25b(1) to (3); BaFin MaRisk AT 9 (circular 06/2026 (BA)); DORA Regulation (EU) 2022/2554 Art. 2(1), 64; Art. 28 to 30 for ICT third parties were not read for this project, LEGAL VALIDATION REQUIRED | Material outsourcing; ICT services from third parties: identity, KYC, signing, cloud, model APIs | The platform | The institution. Management responsibility does not move | Outsourcing management, ICT risk management | KWG §25b read. DORA scope (credit institutions, investment firms, management companies) and its application from 17 January 2025 read. MaRisk AT 9 content not read: LEGAL VALIDATION REQUIRED |

**Dated rows.** The EU Anti-Money Laundering Regulation (EU) 2024/1624 applies from 10 July 2027 (Art. 90) and will
replace most of the GwG due-diligence rules behind R01 to R04. A federal draft of 28 October 2026 to implement the sixth
AML directive, found only in search results, would replace the GwG itself. Rows R01 to R04 therefore carry the date they
were read, and the change is scenario 1 in [technical-concept-2030.md](technical-concept-2030.md#7-designed-for-change-growing-requirements).

### Automation boundaries

| ID | The customer personally | The agent may prepare or explain | The agent never decides | Evidence required | Prototype (local) | Missing for production |
|---|---|---|---|---|---|---|
| R01 | Presents the ID document, uses the online ID or takes part in the video call | Explains the steps and documents. Hands over the bank's one-time link. Never handles the ID document | That the customer is identified, or which method is acceptable | Identification record: method, data, date (GwG §8) | Identity-proofing gate with a synthetic provider. Signed, bound result with `regulatory_evidence=false`. An OIDC login is authentication, not proofing | A contracted identification service, the AML officer's approval of the method |
| R02 | States the purpose and whether they act for themselves; discloses PEP status if asked | Explains why the questions are asked | Risk class, PEP status, acceptance of the relationship | Due-diligence and screening record | Mock KYC and compliance results only. No beneficial-owner, purpose or PEP data collected | A screening provider (sanctions, PEP), purpose capture, risk rating, monitoring |
| R03 | - | - | Reuse of another institution's identification | Reliance or outsourcing agreement | Partner identification is never reused | Contracts under §17, quality checks |
| R04 | - | - | Retention or deletion | Retention schedule, deletion log | Append-only evidence tables. Retention not modelled (named gap) | Retention schedules, WORM storage, deletion jobs |
| R05 | Provides the tax identification number | Reminds the customer to have it ready. Never transmits or stores it | The number, or that the record is complete | The recorded number with the identity data | Format checked, then discarded (D11). The gate is stricter than §154, which provides a BZSt query when the number is missing | Encrypted storage with access control, the BZSt query, the flag and report under §154(2c) |
| R06 | Issues an exemption order (optional). Files any church-tax objection (Sperrvermerk) with the BZSt | Explains the options | The amount, or the church-tax status | Exemption order on the official form; result of the church-tax query | Optional amount, not checked. An objection field on the bank's form, a simplification: the law has the customer file it with the BZSt (assumption 9) | Official form, the church-tax query, reporting under §45d |
| R07 | Gives the self-certification completely and correctly | Explains the self-certification | Tax residences or US status | Self-certification, checked for plausibility against the opening data | One residence country and a US-person declaration | All residences with their tax numbers, the plausibility check, reporting to the BZSt (assumption 10) |
| R08 | Answers the knowledge and experience questions, reads a warning and decides | Explains the questions and what a warning means, from verified facts | Whether a product is appropriate; suppressing or overriding a warning | Answers, assessment, warning, acknowledgement | Rules `synthetic-2026.11`. A warning needs the customer's acknowledgement and never rejects the account. Recorded for later non-advised orders; there is no order step | Compliance-owned rule sets per product, the order step |
| R09 | Takes the initiative for the order and receives the notice | Says that execution-only is not offered | That an order qualifies as execution-only | Compliance's decision with the conditions | Not offered. The exemption is `UNRESOLVED` and never assumed | Compliance's decision, including the client-initiative question |
| R10 | If advised: gives the information and receives the suitability statement | Points the customer to an advising partner bank | A personal recommendation in the bank's name. The bank gives an agent no suitability verdict | Suitability statement (advising firm) | Advice is not offered. Requests for another service are refused and audited | None for this journey. Referral to a partner bank |
| R11 | Receives the cost information | Presents the bank's verified figures with their source | Costs other than the bank's verified figures, presented as the bank's | Proof of the cost information given | Fees in the signed, versioned catalog. Disclosure receipts | Ex-ante and ex-post cost information for orders |
| R12 | Receives the KID in good time before an order | Presents the KID reference and digest from the catalog | The KID content, or that the KID was provided | KID version and digest, timestamp of provision | A mock risk document `kid-depot-basic@1.0` with a pinned digest. Synthetic text, not a KID | KIDs from the KVGs' document management, provided before the order |
| R13 | Is told about inducements before the service | Shows the customer's choice of bank. Carries a partner's signed referral proof | The attribution, or who receives commission | Inducement disclosure, attribution decision | Signed referral proofs, the bank's register, conflicts to a person. The commission figure is demonstration only and never payable | Partner contracts, disclosure, documented quality enhancement |
| R14 | Is told about recording in advance | - | What is recorded | Records and recordings, kept five years | Audit events with correlation ids. No recording of agent conversations | Recording and retention, if the agent channel carries orders |
| R15 | Gives consent and signs in the required form | Shows the exact action and the package hash, sends the customer to the bank's page | Consent or signature for the customer, or the required form | Consent receipt with the action digest; signed package hash; signature evidence at the required level | Consent receipts bound to the digest. Test signer at `TEST_NO_LEGAL_EFFECT`. A QES setting without a named QTSP is refused at start-up | Legal decision on the form of each declaration. A QTSP contract only where a QES is required |
| R16 | Presents attributes from the wallet, voluntarily | Guides the customer to the wallet step | Accepting a wallet presentation | The verified presentation | Not implemented | Relying-party registration, presentation verification |
| R17 | Gives consent where consent is the basis; exercises their rights | Passes no more data than needed | Automated decisions with legal effect; purposes of processing | Records of processing, data-protection impact assessment | The tax identifier is discarded. Statuses and digests are stored, not documents. No automated rejection. A model named as decider is refused | Impact assessment, records of processing, a lawful basis per field, Art. 9 handling |
| R18 | - | - | Provider selection or outsourcing | Outsourcing register, ICT third-party register, contracts, exit plans | Ports and labelled stand-ins only | Contracts, registers, exit plans, resilience tests |

### The boundaries in short

These are the boundaries the case study asks about. Each one names the rows above that support it.

- **Appropriateness at account opening.** WpHG §63(10) attaches the assessment to non-advised investment services, not to
  the opening of an account (R08). The prototype collects the answers and records them for later orders, but no answer
  rejects the account. The reference provider also collects the answers at opening in its remote form and assesses
  appropriateness for complex funds. Whether the answers may be reused for a later order needs legal validation.
- **Advice and suitability** apply only to advice and portfolio management (R10). Neither is offered. The bank answers an
  independent agent with facts, never with a suitability verdict.
- **Execution-only** needs all its conditions (R09). The prototype never assumes it.
- **Warnings, not rejection.** The law asks for a warning when an assessment is negative or incomplete, not for a refusal
  (R08). A knowledge gap produces a warning that the customer acknowledges.
- **Consent is not a signature.** The consent receipt binds the customer to an exact action. The form of the contract is a
  separate legal question (R15). No source found prescribes a form for the depot contract itself, so a QES is not assumed.
- **KYC and AML stay with the obliged entity**, the depot bank. Reliance on a partner bank or a provider leaves it
  responsible (R01 to R03).
- **Delegation does not replace the customer's own acts.** Identification, the tax number, the self-certification,
  answers, consent and signature stay with the customer (R01, R05, R07, R08, R15). The agent prepares and explains.
- **Branch conflicts and commission** are governed by contracts and WpHG §70, not by code (R13). Conflicts go to a
  person and never stop the account.
- **A person decides what cannot be automated**: unresolved applicability, review cases, attribution conflicts, failed
  proofing or signing ([technical-concept-2030.md](technical-concept-2030.md#23-who-authorizes-whom)).

## Sources, and what each supports

**Read on 2026-11-07**, at the official sources. Every page was read through an automated fetch that returns excerpts, so
re-read the exact wording before quoting it. "Version shown" is the amendment note on the page.

| Source | Version shown | URL | Supports |
|---|---|---|---|
| GwG §8, §10, §11, §12, §13, §17 | Last amended by Art. 12(4) of the act of 29.6.2026 (BGBl. I No. 197) | [§8](https://www.gesetze-im-internet.de/gwg_2017/__8.html), [§10](https://www.gesetze-im-internet.de/gwg_2017/__10.html), [§11](https://www.gesetze-im-internet.de/gwg_2017/__11.html), [§12](https://www.gesetze-im-internet.de/gwg_2017/__12.html), [§13](https://www.gesetze-im-internet.de/gwg_2017/__13.html), [§17](https://www.gesetze-im-internet.de/gwg_2017/__17.html) | R01 to R04 |
| PAuswG §18 | Not captured | [§18](https://www.gesetze-im-internet.de/pauswg/__18.html) | R01: online ID |
| BaFin circular 3/2017 (GW), video identification | Dated 10.04.2017, in force 15.06.2017; no amendment shown | [Circular](https://www.bafin.de/SharedDocs/Veroeffentlichungen/DE/Rundschreiben/2017/rs_1703_gw_videoident.html), [BaFin evaluation of 09.05.2022](https://www.bafin.de/SharedDocs/Veroeffentlichungen/DE/Auslegungsentscheidung/A/ae_videoident_rs_3_2017.html) | R01: video identification continues as a "bridge technology" |
| AO §154, §139b, §93c | Last amended by Art. 6 of the act of 22.10.2026 (BGBl. I No. 320) | [§154](https://www.gesetze-im-internet.de/ao_1977/__154.html), [§139b](https://www.gesetze-im-internet.de/ao_1977/__139b.html), [§93c](https://www.gesetze-im-internet.de/ao_1977/__93c.html) | R05, R06 |
| EStG §44a, §45d, §51a | Last amended by Art. 1 of the act of 15.10.2026 (BGBl. I No. 307) | [§44a](https://www.gesetze-im-internet.de/estg/__44a.html), [§45d](https://www.gesetze-im-internet.de/estg/__45d.html), [§51a](https://www.gesetze-im-internet.de/estg/__51a.html) | R06 |
| FKAustG §3a, §13 | Last amended by Art. 3 of the act of 22.12.2025 (BGBl. I No. 352) | [§13](https://www.gesetze-im-internet.de/fkaustg/__13.html), [§3a](https://www.gesetze-im-internet.de/fkaustg/__3a.html), [BZSt CRS page](https://www.bzst.de/DE/Privatpersonen/Selbstauskuenfte/CommonReportingStandard/commonreportingstandard_node.html) | R07 |
| FATCA-USA-UmsV §5 | Last amended by Art. 11 of the act of 29.6.2026 (BGBl. I No. 197) | [§5](https://www.gesetze-im-internet.de/fatca-usa-umsv/__5.html) | R07 |
| WpHG §63, §64, §70, §83 | Last amended by Art. 11 of the act of 26.5.2026 (BGBl. I No. 156) | [§63](https://www.gesetze-im-internet.de/wphg/__63.html), [§64](https://www.gesetze-im-internet.de/wphg/__64.html), [§70](https://www.gesetze-im-internet.de/wphg/__70.html), [§83](https://www.gesetze-im-internet.de/wphg/__83.html) | R08 to R11, R13, R14 |
| Delegated Regulation (EU) 2017/565, Art. 50, 54 to 57 | Original text (OJ L 87, 31.3.2017). A consolidated version of 02/08/2022 exists and was not read | [EUR-Lex](https://eur-lex.europa.eu/eli/reg_del/2017/565/oj) | R08 to R11 |
| MiFID II, Directive 2014/65/EU, Art. 24(9), 25(2) to (4) | Original text and the consolidated version of 06/06/2026 | [EUR-Lex](https://eur-lex.europa.eu/eli/dir/2014/65/oj), [consolidated](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:02014L0065-20260606) | R08 to R10, R13 |
| PRIIPs Regulation (EU) 1286/2014, Art. 13(1), 32(1); Regulation (EU) 2021/2259 | Original texts | [1286/2014](https://eur-lex.europa.eu/eli/reg/2014/1286/oj), [2021/2259](https://eur-lex.europa.eu/eli/reg/2021/2259/oj) | R12 |
| KAGB §164, §297 | Last amended by Art. 3 of the act of 9.4.2026 (BGBl. I No. 97) | [§164](https://www.gesetze-im-internet.de/kagb/__164.html), [§297](https://www.gesetze-im-internet.de/kagb/__297.html) | R12 |
| eIDAS Regulation (EU) 910/2014, Art. 3(12), 25 | Original text and the consolidated version of 18.10.2024 | [EUR-Lex](https://eur-lex.europa.eu/eli/reg/2014/910/oj), [consolidated](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:02014R0910-20241018) | R01 (QES route), R15 |
| Regulation (EU) 2024/1183 (European Digital Identity Framework) | OJ L 2024/1183, 30.4.2024 | [EUR-Lex](https://eur-lex.europa.eu/eli/reg/2024/1183/oj) | R16 |
| BGB §126b; DepotG §10, §13 | DepotG last amended by Art. 24 of the act of 23.10.2024 (BGBl. I No. 323) | [§126b BGB](https://www.gesetze-im-internet.de/bgb/__126b.html), [DepotG](https://www.gesetze-im-internet.de/wpapg/BJNR001710937.html), [§10](https://www.gesetze-im-internet.de/wpapg/__10.html), [§13](https://www.gesetze-im-internet.de/wpapg/__13.html) | R15 |
| GDPR, Regulation (EU) 2016/679, Art. 5, 6, 9, 22 | Original text | [EUR-Lex](https://eur-lex.europa.eu/eli/reg/2016/679/oj) | R17 |
| DORA, Regulation (EU) 2022/2554, Art. 2(1), 64 | Original text (OJ L 333, 27.12.2022) | [EUR-Lex](https://eur-lex.europa.eu/eli/reg/2022/2554/oj) | R18 |
| KWG §25b | Last amended by Art. 9 of the act of 12.5.2026 (BGBl. I No. 139) | [§25b](https://www.gesetze-im-internet.de/kredwg/__25b.html) | R18 |
| BaFin circular 06/2026 (BA), MaRisk | Dated 30.06.2026. The PDF could not be read | [Circular page](https://www.bafin.de/SharedDocs/Downloads/DE/Rundschreiben/dl_rs_0626_9_marisk.html), [announcement](https://www.bafin.de/SharedDocs/Veroeffentlichungen/DE/Meldung/2026/meldung_2026_06_30_9_marisk_novelle.html) | R18: that the circular exists, and its date |
| AML Regulation (EU) 2024/1624, Art. 90 | OJ L of 2024 | [EUR-Lex](https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=OJ:L_202401624) | Dated rows: application from 10 July 2027 |

**Read on 2026-11-06**, from gesetze-im-internet.de, for the first version of this matrix:

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

On 2026-11-06 the EU texts could not be read, and this section listed them as resting on secondary sources. They were read
on 2026-11-07 (table above).

**Still not read.** The matrix does not rely on these, and **legal must check them**:

| Source | Why it matters | Status |
|---|---|---|
| [ESMA guidelines on appropriateness and execution-only](https://www.esma.europa.eu/node/100352) | How supervisors read R08 and R09 | Found, not read |
| MaComp (BaFin) | Conduct rules for R08 to R14 | Not read |
| BaFin MaRisk AT 9 text | Outsourcing detail for R18 | The PDF could not be read |
| EBA guidelines on remote customer onboarding | Remote identification for R01 | Not read |
| Annex I of the FATCA agreement; BMF letter of 15.06.2022 on CRS and FATCA | Procedures and thresholds for R07 | Not read; the BMF server answered 503 |
| BMF draft of 28.10.2026 implementing the sixth AML directive | Successor of the GwG | Search results only; the BMF server answered 503 |
| An ordinance on video identification under GwG §13(2) | Successor of circular 3/2017 | A 2024 draft appears only in third-party sources; no enactment found |
| Implementing acts under Regulation (EU) 2024/1183 | The deadline in R16 | Dates not checked |
| Consolidated versions of PRIIPs, Regulation 2017/565, GDPR, DORA | Later amendments | Only the original texts were read |

## Assumptions needing legal and compliance confirmation

These are **proposals of this project, not requirements of the law.**

1. Which legal entity holds each duty, and under which licence.
2. Tax information is a gate before confirmation. The fields and the timing are a prototype decision, and the church-tax field may touch GDPR Art. 9.
3. Knowledge and experience answers given at account opening and reused for a later non-advised order (WpHG section 63(10)): whether that order may rely on them, and for how long they stay valid. This covers the reuse only. The account opening itself is treated as not triggering the appropriateness test (the matrix above), and no answer rejects the account.
4. A conflict in attribution does not stop the account.
5. Which signature form each contract needs. QES is not assumed.
6. Whether any agent-initiated flow affects the customer-initiative condition of section 63(11).
7. Retention periods, and how they fit data minimization.
8. Whether delegation to an agent has effects under civil law, GwG, or for electronic signatures.
9. The church-tax objection on the bank's form is a prototype simplification. Under EStG §51a(2e) the customer files the
   objection (Sperrvermerk) with the BZSt, and the bank informs the customer about its query and that right.
10. The self-certification records one residence country and a US-person declaration. FKAustG §13 asks for every tax
    residence, checked for plausibility against the opening data.
11. Requiring the tax identification number before confirmation is stricter than AO §154(2b), which provides a query to the
    BZSt when the customer does not give it. This is a product decision.
12. Allocation of every row to an entity of the reference case (R01 to R18, "Accountable" and "Executed by") rests on public
    documents and needs the provider's confirmation.

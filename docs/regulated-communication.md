# Area 6: trustworthy customer communication

| Item | Content |
|---|---|
| **Requirement** | Trustworthy communication without a website visit |
| **Existing before** | Approved claims at the tool boundary and a claim API. Free-form LLM text was not checked |
| **Added** | `CommunicationGuard`, `VerifiedLlmAgent`, structured answers, a public check |
| **Boundaries** | `app/services/regulated_communication.py`, `app/services/verified_llm.py`, `POST /v1/communication/verify`, A2A `product-information`, `product-comparison` |
| **In the journey** | The agent reads a verified fact with its source. The LLM wrapper is exercised with a fake model, not in the demo |
| **Demonstration** | `python -m pytest tests/test_trust_communication.py`; the demo's `verified_fact` line |
| **Positive tests** | Supported statements pass unchanged and carry evidence |
| **Negative tests** | See below |
| **External dependency** | A paid model run, not authorized in this work |
| **Limitation** | See "What this cannot guarantee" |
| **Classification** | LOCAL VERIFIED with a fake model. Behaviour with a real model NOT RUN |

**The historical 20 of 20 evaluation does not cover this.** `app/agents/llm.py` is unchanged so that its recorded digest stays valid; the
wrapper sits outside it, on the bank's side of the tool gateway.

## The rule

The model never verifies itself. Text is released only if each checkable value matches a current, approved fact. Otherwise:

| Verdict | When | The customer sees |
|---|---|---|
| `RELEASE` | Nothing contradicts the facts | The text, with evidence for what it cited |
| `EVIDENCE_INSUFFICIENT` | A value, claim or reference without support | A fixed statement that it cannot be presented as fact |
| `ESCALATE_TO_HUMAN` | Guarantee, return promise, recommendation, instruction-like text | A fixed route to a person |

**High-impact facts** should use `verified_answer`, a template over approved facts. The bank answers with the value, statement, version
and source, or says that the evidence is insufficient. It does not generate text.

## Adversarial cases tested

| Case | Result |
|---|---|
| Invented fee, different amount, comma form | Withheld |
| Correct citation with a false number, a real fee attached to the wrong fee type | Withheld |
| "No fees", "free of charge" where a fee exists | Withheld |
| Amount in words | Withheld as not verifiable |
| Interest, return or percentage | Withheld |
| Guaranteed return, "cannot lose", promised approval | To a person |
| BaFin, licensed, regulated, deposit protection | Withheld |
| Comparison without data | Withheld. Structured comparison needs two approved values, and never ranks |
| Unavailable or unknown product | Withheld |
| Wrong document version, unknown document, altered digest | Withheld |
| Expired source | No financial statement is released |
| Personal recommendation | To a person |
| Prompt injection echoed by a model | To a person |

## What this cannot guarantee

This is pattern and value matching, and it reads no meaning.

- **Misleading prose without a checkable value passes.** A test pins this as a known limit.
- **Currency:** the amount is compared, not the currency, so "USD 4.90" passes against "4.90 EUR". A test pins this too.
- **Wording:** only listed phrases count. Other wording of a guarantee can pass.
- **Third-party agents:** the bank cannot force them through this check. `POST /v1/communication/verify` is an offer.
- **No model has run through this boundary.**

So it reduces the risk and does not remove it. Investment-related text still needs human compliance review before production use.

**Factual information is kept apart from personalized advice:** every structured fact says "Not investment advice", and a recommendation request goes
to a person.

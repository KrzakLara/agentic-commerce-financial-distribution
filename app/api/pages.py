"""Minimal bank-controlled customer pages (server-rendered HTML, no JavaScript).

Everything dynamic is passed through `html.escape`. Responses carry anti-framing, no-store and
a strict content-security-policy (forms may only post back to this origin). Every form carries
a CSRF token bound to the browser's (pre-)session and to this handoff (Phase 03).

These pages are a PROTOTYPE of the bank's confirmation ceremony. The customer sign-in on them
is a DEV-ONLY simulation (see `app.api.customer_auth`), not identity verification.

Phase 04: the review page and the confirmation page carry a "Speak to a person" form
(request a human). It posts to this origin with the session's CSRF token for exactly this
application; its acknowledgement is the same neutral text in every case.

The look is a generic, conservative retail-banking style: no real bank's name, logo or design.
The stylesheet is inline, so the pages need no other request and work offline. It reaches the
acknowledgement page too, where a test forbids words like `review` and `case` in the whole HTML,
so class names and CSS values must avoid them (`uppercase` contains `case`).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from html import escape

from fastapi.responses import HTMLResponse

from app.domain.catalog import Channel, ChannelKind, Product
from app.domain.customer_information import AppropriatenessRecord, TaxInformationRecord
from app.domain.enums import ApplicationStatus
from app.services.appropriateness import (
    PROTOTYPE_NOTICE,
    QUESTIONNAIRE,
    AppropriatenessOutcome,
    ExperienceBand,
    customer_notice_for,
)
from app.services.onboarding_service import HandoffReview
from app.services.tax_information import TaxFieldStatus

_STYLE = """
:root{--navy:#12355b;--navy-dark:#0c2744;--ink:#1f2933;--muted:#56626d;--line:#d5dbe1;--panel:#f5f7f9;--page:#eef1f4}
*{box-sizing:border-box}
body{display:flex;flex-direction:column;min-height:100vh;margin:0;background:var(--page);color:var(--ink);font-family:"Segoe UI","Helvetica Neue",Arial,sans-serif;line-height:1.45}
.wrap{max-width:60rem;margin:0 auto;padding:0 1.25rem}
.top{background:#fff;border-top:4px solid var(--navy);border-bottom:1px solid var(--line)}
.top .wrap{display:flex;justify-content:space-between;align-items:center;gap:1rem;padding-top:.75rem;padding-bottom:.75rem}
.brand{font-size:1.1rem;font-weight:600;color:var(--navy)}
.top-note{font-size:.9rem;color:var(--muted);text-align:right}
main.wrap{flex:1;width:100%;padding-top:1.25rem;padding-bottom:1.5rem}
h1{margin:1.25rem 0 .75rem;font-size:1.6rem;font-weight:600;color:var(--navy)}
.banner{background:#fdf8e6;border:1px solid #e3d49a;border-left:4px solid #b7950b;padding:.55rem .9rem;font-size:.88rem}
.steps{list-style:none;display:grid;grid-template-columns:repeat(3,1fr);gap:.5rem;margin:1.25rem 0 0;padding:0}
.steps li{display:flex;align-items:center;gap:.55rem;padding:.5rem .15rem;border-bottom:3px solid var(--line);color:var(--muted);font-size:.95rem}
.num{display:flex;flex:none;align-items:center;justify-content:center;width:1.6rem;height:1.6rem;border:1px solid #9aa6b2;border-radius:50%;background:#fff;font-size:.85rem}
.steps .now{border-bottom-color:var(--navy);color:var(--navy);font-weight:600}
.now .num{border-color:var(--navy);background:var(--navy);color:#fff}
.steps .done{border-bottom-color:#7f95ad;color:var(--ink)}
.done .num{border-color:#7f95ad;background:#e4eaf1;color:var(--navy)}
.status{margin:0 0 1rem;background:#eaf0f6;border:1px solid #c6d3e0;padding:.6rem .9rem}
.agent{margin:1rem 0;background:#fff;border:1px solid var(--line);border-left:4px solid var(--navy);padding:.6rem .9rem}
.card{margin:.8rem 0;background:#fff;border:1px solid var(--line);border-radius:3px}
.card h2{margin:0;padding:.55rem 1rem;background:var(--panel);border-bottom:1px solid var(--line);font-size:1rem;font-weight:600;color:var(--navy)}
.card-body{padding:.9rem 1rem}
.card-body p{margin:0 0 .7rem}
.card-body ol{margin:0}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:.8rem;margin:.8rem 0}
.cols .card{margin:0}
.facts{border-collapse:collapse;width:100%}
.facts th,.facts td{padding:.36rem 1rem;border-bottom:1px solid #e8ecf0;text-align:left;vertical-align:top}
.facts tr:last-child th,.facts tr:last-child td{border-bottom:0}
.facts th{width:13rem;font-weight:500;color:var(--muted)}
.cols .facts th{width:11rem}
code{font-family:Consolas,"Courier New",monospace;font-size:.84rem;word-break:break-all}
.risk{padding:.7rem 1rem;border-top:1px solid var(--line);background:var(--panel);font-size:.92rem}
.decision{border-top:3px solid var(--navy)}
label{font-weight:600}
input[type=text]{display:block;width:100%;max-width:22rem;margin:.3rem 0 .9rem;padding:.5rem .6rem;border:1px solid #8c98a4;border-radius:3px;font:inherit}
select{padding:.42rem;border:1px solid #8c98a4;border-radius:3px;background:#fff;font:inherit;font-weight:400}
button{padding:.6rem 1.4rem;border:1px solid var(--navy);border-radius:3px;background:var(--navy);color:#fff;font:inherit;font-weight:600;cursor:pointer}
button:hover{background:var(--navy-dark)}
button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid #2f6db0;outline-offset:2px}
button.secondary{background:#fff;color:var(--navy);font-weight:500}
button.secondary:hover{background:#eaf0f6}
.human{margin:1rem 0;padding:.9rem 1rem;background:#fff;border:1px solid var(--line);border-radius:3px}
.human p{margin-top:0}
.ok,.err{margin:1rem 0;padding:.8rem 1rem;border:1px solid}
.ok{color:#14532d;background:#eef7f1;border-color:#b5d8c2;border-left:4px solid #2f7d4f}
.err{color:#8a1c2b;background:#fbeff0;border-color:#e6bcc2;border-left:4px solid #a8303f}
.foot{background:#fff;border-top:1px solid var(--line);padding:.8rem 0;color:var(--muted);font-size:.82rem}
.hint{color:var(--muted);font-size:.9rem}
.choice label{margin-right:1.2rem;font-weight:400}
.notice{background:#fdf8e6;border:1px solid #e3d49a;border-left:4px solid #b7950b;padding:.6rem .9rem}
details{border-top:1px solid var(--line)}
summary{padding:.55rem 1rem;color:var(--navy);cursor:pointer}
@media (max-width:48rem){.cols{grid-template-columns:1fr}.facts th,.cols .facts th{width:40%}.steps li{font-size:.85rem}}
"""

_PORTAL_NAME = "Depot Onboarding Portal"
_DEFAULT_TOP_NOTE = "Prototype (MOCK)"

_BANNER = (
    '<div class="banner"><strong>DEVELOPMENT PROTOTYPE - MOCK.</strong> Synthetic data only. '
    "The sign-in on this page is a <em>simulation</em>: it is not real identity verification, "
    "and nothing here opens a real account or creates a legally valid contract.</div>"
)

_FOOTER = (
    "Development prototype with synthetic data. Providers, products and results are mock values; "
    "no real account or legally valid contract is created."
)

# Where the customer is: (step, status). Fixed text on purpose, never the live workflow state,
# which would tell the customer whether a compliance review is running (no tipping-off).
STEP_SIGN_IN = ("Step 1 of 3: Sign in.", "Application details stay hidden until you sign in.")
STEP_CONFIRM = (
    "Step 2 of 3: Review and confirm.",
    "Application status: waiting for your confirmation.",
)
STEP_CONFIRMED = (
    "Step 3 of 3: Confirmation recorded.",
    "Application status: in progress with the bank. The account is not open yet: only the signed "
    "confirmation of the depot provider (MOCK) can open it.",
)
# The one status that depends on the live state: the bank refuses a confirmation unless the
# application awaits it, so the page must not offer one. This reveals no check result: the checks
# only start after the confirmation, and by then this link is used up and shows nothing.
STEP_CONFIRM_UNAVAILABLE = (STEP_CONFIRM[0], "Application status: confirmation is not available.")
_CONFIRM_UNAVAILABLE = (
    '<div class="card-body"><p>Confirmation is not available at the moment. If you asked to speak '
    "with a person, this application stays paused until the bank's staff resume it. "
    "This link can be used again after that, while it is still valid.</p></div>"
)
# The customer still has to supply information. Depends on what they entered, not on any check.
STEP_CONFIRM_INFORMATION = (
    STEP_CONFIRM[0],
    "Application status: waiting for your information and your confirmation.",
)
_INFORMATION_FIRST = (
    '<div class="card-body"><p>Please complete the tax information and the knowledge and experience '
    "section above. You can confirm here afterwards.</p></div>"
)
ANCHOR_TAX = "tax-information"
ANCHOR_KNOWLEDGE = "knowledge-and-experience"
TAX_FIELD_LABELS = {
    "tax_residency_country": "Tax residency country",
    "synthetic_tax_id": "Synthetic tax ID",
    "us_person_declared": "US person declared",
    "exemption_order_amount_eur": "Exemption order amount",
    "church_tax_objection": "Church tax objection",
}
TAX_STATUS_LABELS = {
    TaxFieldStatus.COLLECTED.value: "Accepted",
    TaxFieldStatus.MISSING.value: "Missing",
    TaxFieldStatus.INVALID.value: "Invalid",
    TaxFieldStatus.NOT_PROVIDED_OPTIONAL.value: "Not stated (optional)",
}
ASSESSMENT_LABELS = {
    AppropriatenessOutcome.ASSESSMENT_OK.value: "No warning from your answers",
    AppropriatenessOutcome.WARNING_REQUIRED.value: "Warning",
    AppropriatenessOutcome.INSUFFICIENT_INFORMATION.value: "Not enough information to assess",
}
TRUE_OR_FALSE = (("true", "True"), ("false", "False"))
EXPERIENCE_CHOICES = (
    (ExperienceBand.NONE.value, "None"),
    (ExperienceBand.ONE_TO_FIVE.value, "1 to 5"),
    (ExperienceBand.MORE_THAN_FIVE.value, "More than 5"),
)
_STEPS = (
    ("Sign in", STEP_SIGN_IN),
    ("Review and confirm", STEP_CONFIRM),
    ("Confirmation recorded", STEP_CONFIRMED),
)

# Fixed text as well: the same on every confirmation, whatever the checks found.
_NEXT_STEPS = (
    "Your confirmation is recorded.",
    "The bank continues processing your application.",
    "The account opens only after the signed confirmation of the depot provider (MOCK).",
)

_AGENT_NOTICE = (
    '<p class="agent"><strong>Prepared by an AI agent - your decision.</strong> This page refuses '
    "the agent's credentials, so the agent cannot confirm it for you. The customer identity itself "
    "is simulated in this prototype.</p>"
)

_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    # Not "no-referrer": with it, browsers send `Origin: null` on these same-origin form posts and
    # the cross-site check refuses every form (finding F-8). Other origins still get no referrer.
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"
    ),
}


@dataclass(frozen=True)
class InformationPanel:
    """What the review page shows about the customer's tax and knowledge-and-experience steps."""

    handoff_id: str
    token: str
    tax_csrf: str
    assessment_csrf: str
    tax_record: TaxInformationRecord | None
    assessment: AppropriatenessRecord | None
    ready: bool
    tax_statuses: Mapping[str, str] | None = None  # the field results of a refused attempt


def _document(
    title: str,
    body: str,
    *,
    step: tuple[str, str] | None = None,
    top_note: str = _DEFAULT_TOP_NOTE,
) -> str:
    steps = _stepper(step) if step else ""
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{escape(title)}</title><style>{_STYLE}</style></head><body>"
        f'<header class="top"><div class="wrap"><span class="brand">{_PORTAL_NAME}</span>'
        f'<span class="top-note">{escape(top_note)}</span></div></header>'
        f'<main class="wrap">{_BANNER}{steps}<h1>{escape(title)}</h1>{body}</main>'
        f'<footer class="foot"><div class="wrap">{_FOOTER}</div></footer>'
        "</body></html>"
    )


def html_response(content: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(content, status_code=status_code, headers=_SECURITY_HEADERS)


def _stepper(current: tuple[str, str]) -> str:
    """The three steps of the flow, with the current one highlighted."""
    position = next(i for i, (_, step) in enumerate(_STEPS) if step[0] == current[0])
    items = []
    for index, (label, _) in enumerate(_STEPS):
        if index < position:
            items.append(f'<li class="done"><span class="num">&#10003;</span>{escape(label)}</li>')
        elif index == position:
            items.append(
                f'<li class="now" aria-current="step"><span class="num">{index + 1}</span>'
                f"{escape(label)}</li>"
            )
        else:
            items.append(f'<li><span class="num">{index + 1}</span>{escape(label)}</li>')
    return f'<ol class="steps">{"".join(items)}</ol>'


def _progress(step: tuple[str, str]) -> str:
    return f'<p class="status"><strong>{escape(step[0])}</strong> {escape(step[1])}</p>'


def _facts(rows: list[tuple[str, str]]) -> str:
    """Label and value rows. Values are HTML that the caller has already escaped."""
    cells = "".join(f'<tr><th scope="row">{k}</th><td>{v}</td></tr>' for k, v in rows)
    return f'<table class="facts">{cells}</table>'


def _card(title: str, content: str, css: str = "card", anchor: str | None = None) -> str:
    target = f' id="{anchor}"' if anchor else ""
    return f'<section class="{css}"{target}><h2>{escape(title)}</h2>{content}</section>'


def sign_in_page(
    handoff_id: str, token: str, error: str | None = None, *, login_csrf: str = ""
) -> str:
    notice = f'<p class="err">{escape(error)}</p>' if error else ""
    form = (
        "<p>Please sign in as the customer to review and confirm this request.</p>"
        f"{notice}"
        "<p><em>Simulated sign-in (development only). In production this step is the bank's "
        "own strong customer authentication.</em></p>"
        '<form method="post" action="/customer/dev-login">'
        f'<input type="hidden" name="handoff_id" value="{escape(handoff_id, quote=True)}">'
        f'<input type="hidden" name="token" value="{escape(token, quote=True)}">'
        f'<input type="hidden" name="login_csrf" value="{escape(login_csrf, quote=True)}">'
        '<label>Customer ID <input type="text" name="customer_id" autocomplete="off" required></label>'
        '<button type="submit">Sign in (simulated)</button></form>'
    )
    card = _card("Customer sign-in", f'<div class="card-body">{form}</div>')
    return _document("Bank sign-in", f"{_progress(STEP_SIGN_IN)}{card}", step=STEP_SIGN_IN)


def review_page(
    *,
    review: HandoffReview,
    customer_id: str,
    token: str,
    provider_name: str,
    product_name: str,
    channel_label: str,
    csrf_token: str = "",
    product: Product | None = None,
    channel: Channel | None = None,
    human_csrf_token: str = "",
    information: InformationPanel | None = None,
) -> str:
    action = review.handoff.action
    summary = [
        ("Signed in as (simulated)", escape(customer_id)),
        ("Provider", escape(provider_name)),
        ("Product", escape(product_name)),
        ("Distribution channel", escape(channel_label)),
        *_partner_rows(channel),
        ("Application reference", f"<code>{escape(action.application_id)}</code>"),
        ("Valid until", escape(review.handoff.expires_at.isoformat(timespec="seconds"))),
    ]
    requested = [
        (
            "Exact requested action",
            "Open a securities account (Wertpapierdepot) for you with the provider above, "
            "for this product, through this channel, and record your confirmation of the "
            "contract and the disclosure below.",
        ),
    ]
    disclosure = _facts(
        [
            (
                "Disclosure document",
                f"{escape(action.document_id)} - version <strong>{escape(action.document_version)}</strong>",
            ),
            ("SHA-256", f"<code>{escape(action.document_digest)}</code>"),
        ]
    )
    digests = [("Action digest", f"<code>{escape(review.handoff.action_digest)}</code>")]
    if action.terms_digest is not None:
        digests.insert(0, ("Terms digest", f"<code>{escape(action.terms_digest)}</code>"))
    can_confirm = review.application.status is ApplicationStatus.AWAITING_AUTHORIZATION
    step = STEP_CONFIRM if can_confirm else STEP_CONFIRM_UNAVAILABLE
    decision = _CONFIRM_UNAVAILABLE
    information_cards = _information_cards(information) if information and can_confirm else ""
    if can_confirm and information and not information.ready:
        step = STEP_CONFIRM_INFORMATION
        decision = _INFORMATION_FIRST
    elif can_confirm:
        decision = (
            f'<form class="card-body" method="post" action="/customer/handoffs/{escape(review.handoff.handoff_id, quote=True)}/confirm">'
            f'<input type="hidden" name="token" value="{escape(token, quote=True)}">'
            f'<input type="hidden" name="action_digest" value="{escape(review.handoff.action_digest, quote=True)}">'
            f'<input type="hidden" name="csrf_token" value="{escape(csrf_token, quote=True)}">'
            "<p>By pressing the button you confirm exactly the action shown above.</p>"
            '<button type="submit">Confirm and authorize</button></form>'
        )
    body = (
        f"{_progress(step)}{_AGENT_NOTICE}"
        f"{_card('Application summary', _facts(summary))}"
        f"{_card('Requested action', _facts(requested))}"
        f"{_product_cards(product)}"
        f"{_card('Disclosure', disclosure + _risk_text(review.document_text))}"
        f"{_card('Digests', _facts(digests))}"
        f"{information_cards}"
        f"{_card('Your decision', decision, 'card decision')}"
    )
    if human_csrf_token:
        body += human_request_form(action.application_id, human_csrf_token)
    return _document(
        "Confirm your account opening request", body, step=step, top_note=provider_name
    )


def _hidden_fields(panel: InformationPanel, csrf: str) -> str:
    return (
        f'<input type="hidden" name="token" value="{escape(panel.token, quote=True)}">'
        f'<input type="hidden" name="csrf_token" value="{escape(csrf, quote=True)}">'
    )


def _radio_group(name: str, question: str, choices: tuple[tuple[str, str], ...]) -> str:
    radios = "".join(
        f'<label><input type="radio" name="{name}" value="{escape(value, quote=True)}"> '
        f"{escape(label)}</label>"
        for value, label in choices
    )
    return f'<p class="choice"><span>{escape(question)}</span><br>{radios}</p>'


def _tax_form(panel: InformationPanel) -> str:
    action = f"/customer/handoffs/{escape(panel.handoff_id, quote=True)}/tax"
    yes_no = (("yes", "Yes"), ("no", "No"))
    return (
        f'<form class="card-body" method="post" action="{action}">'
        f"{_hidden_fields(panel, panel.tax_csrf)}"
        "<label>Tax residency country (two letters, such as DE) "
        '<input type="text" name="tax_residency_country" maxlength="2" autocomplete="off"></label>'
        "<label>Synthetic tax ID "
        '<input type="text" name="synthetic_tax_id" maxlength="40" autocomplete="off" '
        'placeholder="SYNTH-TAX-123456"></label>'
        '<p class="hint">Synthetic format only: SYNTH-TAX- and six digits. A real tax ID is '
        "refused. Please do not enter real data.</p>"
        f"{_radio_group('us_person_declared', 'Do you declare that you are a US person?', yes_no)}"
        "<label>Exemption order amount in EUR (optional, not checked) "
        '<input type="text" name="exemption_order_amount_eur" maxlength="9" autocomplete="off">'
        "</label>"
        f"{_radio_group('church_tax_objection', 'Church tax objection (optional)', yes_no)}"
        '<button type="submit" class="secondary">Check and save tax information</button></form>'
    )


def _yes_no_text(value: bool | None) -> str:
    return "Not stated" if value is None else ("Yes" if value else "No")


def _tax_card(panel: InformationPanel) -> str:
    record = panel.tax_record
    rows = [
        ("Data source", "Customer-provided on the bank's page"),
        (
            "Local validation",
            "Complete: required fields present and format accepted" if record else "Not complete",
        ),
        ("External verification", "Not implemented: no tax authority is contacted"),
    ]
    if record:
        amount = record.exemption_order_amount_eur
        rows += [
            ("Tax residency country", escape(record.tax_residency_country)),
            ("Synthetic tax ID", "Format accepted, value not stored"),
            ("US person declared", _yes_no_text(record.us_person_declared)),
            ("Exemption order amount", "Not stated" if amount is None else f"{amount} EUR"),
            ("Church tax objection", _yes_no_text(record.church_tax_objection)),
        ]
    feedback = ""
    if panel.tax_statuses:
        feedback = (
            '<p class="err">The information is not complete. Nothing was saved from this attempt. '
            "Please check the fields below.</p>"
            + _facts(
                [
                    (TAX_FIELD_LABELS[name], TAX_STATUS_LABELS[status])
                    for name, status in panel.tax_statuses.items()
                ]
            )
        )
    form = _tax_form(panel)
    if record:
        form = f"<details><summary>Change tax information</summary>{form}</details>"
    return _card("Tax information (synthetic)", _facts(rows) + feedback + form, anchor=ANCHOR_TAX)


def _assessment_form(panel: InformationPanel) -> str:
    action = f"/customer/handoffs/{escape(panel.handoff_id, quote=True)}/assessment"
    questions = "".join(
        _radio_group(name, text, TRUE_OR_FALSE if name != "experience" else EXPERIENCE_CHOICES)
        for name, text in QUESTIONNAIRE
    )
    return (
        f'<form class="card-body" method="post" action="{action}">'
        f"{_hidden_fields(panel, panel.assessment_csrf)}"
        f'<input type="hidden" name="intent" value="answer">{questions}'
        '<button type="submit" class="secondary">Save my answers</button></form>'
    )


def _assessment_card(panel: InformationPanel) -> str:
    record = panel.assessment
    intro = (
        '<div class="card-body"><p class="hint">These questions prepare later securities orders. '
        "Opening this account executes no order, and no result here stops the account opening. "
        f"{escape(PROTOTYPE_NOTICE)}</p></div>"
    )
    if record is None:
        return _card(
            "Knowledge and experience (synthetic)",
            intro + _assessment_form(panel),
            anchor=ANCHOR_KNOWLEDGE,
        )
    rows = [
        ("Result", ASSESSMENT_LABELS[record.outcome]),
        ("Rule set", escape(record.rule_set_version)),
        (
            "Your acknowledgement",
            "Not needed"
            if record.outcome == AppropriatenessOutcome.ASSESSMENT_OK.value
            else ("Recorded" if record.acknowledged_at else "Needed"),
        ),
    ]
    notice_text = customer_notice_for(record.outcome)
    notice = f'<p class="notice">{escape(notice_text)}</p>' if notice_text else ""
    acknowledge = ""
    if notice_text and record.acknowledged_at is None:
        action = f"/customer/handoffs/{escape(panel.handoff_id, quote=True)}/assessment"
        acknowledge = (
            f'<form class="card-body" method="post" action="{action}">'
            f"{_hidden_fields(panel, panel.assessment_csrf)}"
            '<input type="hidden" name="intent" value="acknowledge">'
            '<button type="submit" class="secondary">'
            "I have read this notice and want to continue</button></form>"
        )
    change = f"<details><summary>Change my answers</summary>{_assessment_form(panel)}</details>"
    return _card(
        "Knowledge and experience (synthetic)",
        f'{intro}{_facts(rows)}<div class="card-body">{notice}</div>{acknowledge}{change}',
        anchor=ANCHOR_KNOWLEDGE,
    )


def _information_cards(panel: InformationPanel) -> str:
    return _tax_card(panel) + _assessment_card(panel)


def _risk_text(document_text: str) -> str:
    return (
        '<div class="risk"><strong>Risk disclosure (MOCK text)</strong><br>'
        f"{escape(document_text)}</div>"
    )


def _partner_rows(channel: Channel | None) -> list[tuple[str, str]]:
    """The distribution attribution (Phase 04), only for a registered partner bank."""
    if channel is None or channel.kind is not ChannelKind.PARTNER_BANK:
        return []
    return [
        (
            "Distribution partner",
            f"{escape(channel.partner_name or channel.partner_id or '-')} - this account "
            "opening is attributed to this registered partner bank",
        )
    ]


def _product_cards(product: Product | None) -> str:
    """The exact, approved product terms (Phase 04).

    Every value comes from the verified catalog and is escaped: provider-supplied text is
    shown as data, never interpreted."""
    if product is None:
        return ""
    effective = product.effective_from.isoformat() if product.effective_from else "-"
    sources = "<br>".join(
        f"{escape(s.title)} - {escape(s.document_id)} v{escape(s.document_version)}"
        for s in product.sources
    )
    information = [
        ("Product version", f"{escape(product.catalog_version)} (effective {escape(effective)})"),
        ("Conditions", escape(product.conditions or "-")),
        ("Sources", sources or "-"),
    ]
    fees = [(escape(f.label), escape(f.amount)) for f in product.fees] or [("Fees", "-")]
    risk = [
        (
            "Risk information",
            f"{escape(product.risk_class or '-')}: {escape(product.risk_summary or '-')}",
        ),
        (
            "Note",
            "Non-personalized product information (MOCK). This is not investment advice "
            "and not a suitability assessment.",
        ),
    ]
    return (
        '<div class="cols">'
        f"{_card('Product information', _facts(information))}"
        f"{_card('Fees (approved price list)', _facts(fees))}"
        "</div>"
        f"{_card('Risk and notes', _facts(risk))}"
    )


def result_page(
    title: str,
    message: str,
    ok: bool,
    human_request: tuple[str, str] | None = None,
    step: tuple[str, str] | None = None,
) -> str:
    """A short outcome page; `human_request` = (application_id, CSRF token) adds the
    "Speak to a person" form (e.g. after a confirmation); `step` adds the progress line."""
    css = "ok" if ok else "err"
    body = f'{_progress(step) if step else ""}<p class="{css}">{escape(message)}</p>'
    if step == STEP_CONFIRMED:
        items = "".join(f"<li>{escape(line)}</li>" for line in _NEXT_STEPS)
        body += _card("What happens next", f'<div class="card-body"><ol>{items}</ol></div>')
    if human_request is not None:
        body += human_request_form(*human_request)
    return _document(title, body, step=step)


# The fixed request categories (no free text, so no personal or sensitive data is collected).
HUMAN_REQUEST_CHOICES = (
    ("QUESTION_ABOUT_PRODUCT", "Question about the product"),
    ("HELP_WITH_PROCESS", "Help with the process"),
    ("DISAGREE_WITH_AUTOMATED_RESULT", "I disagree with an automated result"),
    ("OTHER", "Other"),
)
HUMAN_REQUEST_BUTTON = "Speak to a person"

# Fixed text, the same in every case: it promises no assignment and no response time.
_HUMAN_STATUS = (
    "Human assistance requested.",
    "This is a recorded request, not a staff assignment or a confirmed follow-up.",
)
_HUMAN_NEXT_STEPS = (
    "Your request is listed as open for the bank's staff.",
    "This page does not confirm that a member of staff has been assigned, or whether or when "
    "you will be contacted.",
    "Human assistance does not bypass the bank's mandatory checks. They still apply.",
)


def human_request_form(application_id: str, csrf_token: str) -> str:
    """The customer's request for a human, bound to the session and this application."""
    options = "".join(
        f'<option value="{escape(value, quote=True)}">{escape(label)}</option>'
        for value, label in HUMAN_REQUEST_CHOICES
    )
    return (
        '<form class="human" method="post" '
        f'action="/customer/applications/{escape(application_id, quote=True)}/request-human">'
        f'<input type="hidden" name="csrf_token" value="{escape(csrf_token, quote=True)}">'
        "<p><strong>Would you prefer to speak to a person?</strong> You can ask for a member of "
        "the bank's staff at any time. Asking for a person does not skip any of the bank's "
        "checks.</p>"
        f'<label>Topic <select name="category">{options}</select></label> '
        f'<button type="submit" class="secondary">{escape(HUMAN_REQUEST_BUTTON)}</button>'
        "</form>"
    )


def human_request_ack_page(message: str) -> str:
    """The same neutral acknowledgement in every case - it never says whether a review exists."""
    items = "".join(f"<li>{escape(line)}</li>" for line in _HUMAN_NEXT_STEPS)
    next_steps = _card("What happens next", f'<div class="card-body"><ol>{items}</ol></div>')
    body = (
        f'<p class="status"><strong>{escape(_HUMAN_STATUS[0])}</strong> '
        f"{escape(_HUMAN_STATUS[1])}</p>"
        f'<p class="ok">{escape(message)}</p>{next_steps}'
    )
    return _document("Request received", body)

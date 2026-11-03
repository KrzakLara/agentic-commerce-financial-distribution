"""Minimal bank-controlled customer pages (server-rendered HTML, no JavaScript).

Everything dynamic is passed through `html.escape`. Responses carry anti-framing, no-store and
a strict content-security-policy (forms may only post back to this origin). Every form carries
a CSRF token bound to the browser's (pre-)session and to this handoff (Phase 03).

These pages are a PROTOTYPE of the bank's confirmation ceremony. The customer sign-in on them
is a DEV-ONLY simulation (see `app.api.customer_auth`), not identity verification.

Phase 04: the review page and the confirmation page carry a "Mit einer Person sprechen" form
(request a human). It posts to this origin with the session's CSRF token for exactly this
application; its acknowledgement is the same neutral text in every case.
"""

from html import escape

from fastapi.responses import HTMLResponse

from app.domain.catalog import Channel, ChannelKind, Product
from app.services.onboarding_service import HandoffReview

_STYLE = """
body{font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:46rem;margin:2rem auto;padding:0 1rem;color:#1b1f23}
.banner{background:#fff3cd;border:1px solid #e0b100;padding:.6rem .9rem;border-radius:6px;font-size:.9rem}
table{border-collapse:collapse;width:100%;margin:1rem 0}
th,td{border:1px solid #d0d7de;padding:.5rem .7rem;text-align:left;vertical-align:top}
th{background:#f6f8fa;width:13rem}
code{font-size:.85rem;word-break:break-all}
.risk{background:#f6f8fa;padding:.7rem;border-left:4px solid #8c959f;font-size:.9rem}
button{font-size:1rem;padding:.6rem 1.2rem;border-radius:6px;border:0;background:#1f6feb;color:#fff;cursor:pointer}
button.secondary{background:#fff;color:#1f6feb;border:1px solid #1f6feb}
.human{margin-top:1.5rem;padding-top:1rem;border-top:1px solid #d0d7de}
select{font-size:1rem;padding:.35rem}
input[type=text]{font-size:1rem;padding:.4rem;width:16rem}
.ok{color:#116329}.err{color:#a40e26}
"""

_BANNER = (
    '<div class="banner"><strong>DEVELOPMENT PROTOTYPE - MOCK.</strong> Synthetic data only. '
    "The sign-in on this page is a <em>simulation</em>: it is not real identity verification, "
    "and nothing here opens a real account or creates a legally valid contract.</div>"
)

_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"
    ),
}


def _document(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>{escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body>{_BANNER}<h1>{escape(title)}</h1>{body}</body></html>"
    )


def html_response(content: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(content, status_code=status_code, headers=_SECURITY_HEADERS)


def sign_in_page(
    handoff_id: str, token: str, error: str | None = None, *, login_csrf: str = ""
) -> str:
    notice = f'<p class="err">{escape(error)}</p>' if error else ""
    body = (
        "<p>Please sign in as the customer to review and confirm this request.</p>"
        f"{notice}"
        "<p><em>Simulated sign-in (development only). In production this step is the bank's "
        "own strong customer authentication.</em></p>"
        '<form method="post" action="/customer/dev-login">'
        f'<input type="hidden" name="handoff_id" value="{escape(handoff_id, quote=True)}">'
        f'<input type="hidden" name="token" value="{escape(token, quote=True)}">'
        f'<input type="hidden" name="login_csrf" value="{escape(login_csrf, quote=True)}">'
        '<label>Customer ID <input type="text" name="customer_id" autocomplete="off" required></label> '
        '<button type="submit">Sign in (simulated)</button></form>'
    )
    return _document("Bank sign-in", body)


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
) -> str:
    action = review.handoff.action
    rows = [
        ("Signed in as (simulated)", escape(customer_id)),
        ("Provider", escape(provider_name)),
        ("Product", escape(product_name)),
        ("Distribution channel", escape(channel_label)),
        ("Application reference", f"<code>{escape(action.application_id)}</code>"),
        (
            "Exact requested action",
            "Open a securities account (Wertpapierdepot) for you with the provider above, "
            "for this product, through this channel, and record your confirmation of the "
            "contract and the disclosure below. The AI agent that prepared this request "
            "cannot confirm it for you.",
        ),
        (
            "Disclosure document",
            f"{escape(action.document_id)} - version <strong>{escape(action.document_version)}</strong>"
            f"<br>SHA-256: <code>{escape(action.document_digest)}</code>",
        ),
        ("Valid until", escape(review.handoff.expires_at.isoformat(timespec="seconds"))),
        ("Action digest", f"<code>{escape(review.handoff.action_digest)}</code>"),
    ]
    rows[4:4] = _terms_rows(product, channel, action.terms_digest)
    table = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows)
    body = (
        f"<table>{table}</table>"
        f'<div class="risk"><strong>Risk disclosure (MOCK text)</strong><br>'
        f"{escape(review.document_text)}</div>"
        f'<form method="post" action="/customer/handoffs/{escape(review.handoff.handoff_id, quote=True)}/confirm">'
        f'<input type="hidden" name="token" value="{escape(token, quote=True)}">'
        f'<input type="hidden" name="action_digest" value="{escape(review.handoff.action_digest, quote=True)}">'
        f'<input type="hidden" name="csrf_token" value="{escape(csrf_token, quote=True)}">'
        "<p>By pressing the button you confirm exactly the action shown above.</p>"
        '<button type="submit">Confirm and authorize</button></form>'
    )
    if human_csrf_token:
        body += human_request_form(action.application_id, human_csrf_token)
    return _document("Confirm your account opening request", body)


def _terms_rows(
    product: Product | None, channel: Channel | None, terms_digest: str | None
) -> list[tuple[str, str]]:
    """The exact, approved product terms and the distribution attribution (Phase 04).

    Every value comes from the verified catalog and is escaped: provider-supplied text is
    shown as data, never interpreted."""
    rows: list[tuple[str, str]] = []
    if product is not None:
        fees = "<br>".join(f"{escape(f.label)}: {escape(f.amount)}" for f in product.fees)
        sources = "<br>".join(
            f"{escape(s.title)} - {escape(s.document_id)} v{escape(s.document_version)}"
            for s in product.sources
        )
        effective = product.effective_from.isoformat() if product.effective_from else "-"
        rows += [
            (
                "Product version",
                f"{escape(product.catalog_version)} (effective {escape(effective)})",
            ),
            ("Fees (approved price list)", fees or "-"),
            ("Conditions", escape(product.conditions or "-")),
            (
                "Risk information",
                f"{escape(product.risk_class or '-')}: {escape(product.risk_summary or '-')}",
            ),
            ("Sources", sources or "-"),
        ]
    if terms_digest is not None:
        rows.append(("Terms digest", f"<code>{escape(terms_digest)}</code>"))
    if channel is not None and channel.kind is ChannelKind.PARTNER_BANK:
        rows.append(
            (
                "Distribution partner",
                f"{escape(channel.partner_name or channel.partner_id or '-')} - this account "
                "opening is attributed to this registered partner bank",
            )
        )
    if product is not None:
        rows.append(
            (
                "Note",
                "Non-personalized product information (MOCK). This is not investment advice "
                "and not a suitability assessment.",
            )
        )
    return rows


def result_page(
    title: str, message: str, ok: bool, human_request: tuple[str, str] | None = None
) -> str:
    """A short outcome page; `human_request` = (application_id, CSRF token) adds the
    "Mit einer Person sprechen" form (e.g. after a confirmation)."""
    css = "ok" if ok else "err"
    body = f'<p class="{css}">{escape(message)}</p>'
    if human_request is not None:
        body += human_request_form(*human_request)
    return _document(title, body)


# The fixed request categories (no free text, so no personal or sensitive data is collected).
HUMAN_REQUEST_CHOICES = (
    ("QUESTION_ABOUT_PRODUCT", "Frage zum Produkt"),
    ("HELP_WITH_PROCESS", "Hilfe beim Ablauf"),
    (
        "DISAGREE_WITH_AUTOMATED_RESULT",
        "Ich bin mit einem automatischen Ergebnis nicht einverstanden",
    ),
    ("OTHER", "Sonstiges"),
)
HUMAN_REQUEST_BUTTON = "Mit einer Person sprechen"


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
        "<p><strong>Lieber mit einem Menschen sprechen?</strong> Sie koennen jederzeit eine "
        "Mitarbeiterin oder einen Mitarbeiter der Bank hinzuziehen. (You can ask for a person at "
        "any time; this does not skip any of the bank's checks.)</p>"
        f'<label>Anliegen <select name="category">{options}</select></label> '
        f'<button type="submit" class="secondary">{escape(HUMAN_REQUEST_BUTTON)}</button>'
        "</form>"
    )


def human_request_ack_page(message: str) -> str:
    """The same neutral acknowledgement in every case - it never says whether a review exists."""
    body = (
        '<p class="ok">Ihre Anfrage wurde aufgenommen. Eine Mitarbeiterin oder ein Mitarbeiter '
        "der Bank meldet sich ueber die ueblichen Kanaele der Bank bei Ihnen.</p>"
        f"<p>{escape(message)}</p>"
    )
    return _document("Anfrage erhalten", body)

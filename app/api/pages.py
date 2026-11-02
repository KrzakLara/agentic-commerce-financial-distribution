"""Minimal bank-controlled customer pages (server-rendered HTML, no JavaScript).

Everything dynamic is passed through `html.escape`. Responses carry anti-framing, no-store and
a strict content-security-policy (forms may only post back to this origin).

These pages are a PROTOTYPE of the bank's confirmation ceremony. The customer sign-in on them
is a DEV-ONLY simulation (see `app.api.customer_auth`), not identity verification.
"""

from html import escape

from fastapi.responses import HTMLResponse

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


def sign_in_page(handoff_id: str, token: str, error: str | None = None) -> str:
    notice = f'<p class="err">{escape(error)}</p>' if error else ""
    body = (
        "<p>Please sign in as the customer to review and confirm this request.</p>"
        f"{notice}"
        "<p><em>Simulated sign-in (development only). In production this step is the bank's "
        "own strong customer authentication.</em></p>"
        '<form method="post" action="/customer/dev-login">'
        f'<input type="hidden" name="handoff_id" value="{escape(handoff_id, quote=True)}">'
        f'<input type="hidden" name="token" value="{escape(token, quote=True)}">'
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
    table = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows)
    body = (
        f"<table>{table}</table>"
        f'<div class="risk"><strong>Risk disclosure (MOCK text)</strong><br>'
        f"{escape(review.document_text)}</div>"
        f'<form method="post" action="/customer/handoffs/{escape(review.handoff.handoff_id, quote=True)}/confirm">'
        f'<input type="hidden" name="token" value="{escape(token, quote=True)}">'
        f'<input type="hidden" name="action_digest" value="{escape(review.handoff.action_digest, quote=True)}">'
        "<p>By pressing the button you confirm exactly the action shown above.</p>"
        '<button type="submit">Confirm and authorize</button></form>'
    )
    return _document("Confirm your account opening request", body)


def result_page(title: str, message: str, ok: bool) -> str:
    css = "ok" if ok else "err"
    return _document(title, f'<p class="{css}">{escape(message)}</p>')

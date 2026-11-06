"""Bank-controlled pages of the trust integration, in the existing look.

Reuses the layout of `app.api.pages`. Every dynamic value is escaped. Provider and agent names are
shown as data, never as instructions.
"""

from collections.abc import Mapping, Sequence
from html import escape
from typing import Any

from app.api.pages import _card, _document, _facts
from app.repositories.trust_orm import DelegationRow

_SCOPE_TEXT = {
    "create_draft": "Prepare one application",
    "request_handoff": "Ask you to confirm it here on the bank's page",
    "read_status": "See its general status",
    "submit_after_consent": "Hand it on after you confirmed and signed",
}


def _hidden(**fields: str) -> str:
    return "".join(
        f'<input type="hidden" name="{name}" value="{escape(value, quote=True)}">'
        for name, value in fields.items()
    )


def delegation_page(row: DelegationRow, agent_name: str, request_token: str, csrf: str) -> str:
    details = row.authorization_details
    rows = [
        ("Agent", escape(agent_name)),
        ("Registered client", f"<code>{escape(row.client_id)}</code>"),
        ("Provider", escape(details.get("provider_id", ""))),
        ("Product", escape(details.get("product_id", ""))),
        ("Channel", escape(details.get("channel_id", ""))),
    ]
    allowed = "".join(f"<li>{escape(_SCOPE_TEXT.get(s, s))}</li>" for s in row.scopes)
    decision = (
        '<form class="card-body" method="post" action="/customer/delegations/authorize">'
        f"{_hidden(request=request_token, csrf_token=csrf)}"
        "<p>You can withdraw this at any time. The agent cannot confirm or sign for you, and it "
        "cannot decide any check.</p>"
        '<button type="submit" name="decision" value="approve">Allow this agent</button> '
        '<button type="submit" class="secondary" name="decision" value="deny">Do not allow'
        "</button></form>"
    )
    body = (
        f"{_card('Who is asking', _facts(rows))}"
        f"{_card('What the agent may do', f'<div class=card-body><ol>{allowed}</ol></div>')}"
        f"{_card('Your decision', decision, 'card decision')}"
    )
    return _document("Allow an AI agent to prepare your application", body)


def delegation_list_page(rows: Sequence[DelegationRow], csrf_for: Any) -> str:  # noqa: ANN401
    cards = ""
    for row in rows:
        revoke = ""
        if row.status not in {"REVOKED", "DENIED"}:
            revoke = (
                f'<form class="card-body" method="post" '
                f'action="/customer/delegations/{escape(row.delegation_id, quote=True)}/revoke">'
                f"{_hidden(csrf_token=csrf_for(row.delegation_id))}"
                '<button type="submit" class="secondary">Withdraw</button></form>'
            )
        facts = _facts(
            [
                ("Registered client", f"<code>{escape(row.client_id)}</code>"),
                ("Status", escape(row.status)),
                ("Valid until", escape(row.expires_at.isoformat() if row.expires_at else "-")),
            ]
        )
        cards += _card(f"Delegation {row.delegation_id[:12]}", facts + revoke)
    return _document("AI agents you allowed", cards or "<p>You have not allowed any agent.</p>")


def continue_page(return_path: str) -> str:
    """After a login. A same-site click, because the Strict session cookie is withheld on redirects
    that began at another site."""
    target = escape(return_path, quote=True)
    body = (
        '<p class="ok">You are signed in. Signing in proves who you are to the sign-in service. '
        "It is not identity verification for the account.</p>"
        f'<p><a id="continue" href="{target}">Continue</a></p>'
    )
    return _document("Signed in", body)


def provider_handover_page(title: str, url: str, explanation: str) -> str:
    """A link to the provider, not a redirect: the pages' CSP (`form-action 'self'`) stops a form
    post that ends at another origin, and it should stay that strict."""
    target = escape(url, quote=True)
    body = (
        f'<p class="status">{escape(explanation)}</p>'
        f'<p><a id="provider-link" href="{target}" rel="noreferrer">'
        "Continue to the provider</a></p>"
    )
    return _document(title, body)


def identity_card(case_status: str | None, handoff_id: str, token: str, csrf: str) -> str:
    """The review page's identity step."""
    labels = {
        None: "Not started",
        "PENDING": "Waiting for the provider's result",
        "VERIFIED": "Verified by the synthetic test provider: no legal or regulatory value",
        "REJECTED": "Not verified",
        "EXPIRED": "Expired",
        "MANUAL_REVIEW": "A person has to check this",
    }
    facts = _facts(
        [
            ("Status", escape(labels.get(case_status, "Unknown"))),
            ("Provider", "Synthetic test provider. Not eID, VideoIdent or an EUDI wallet"),
            ("Your data", "No document image or biometric data is collected in this prototype"),
        ]
    )
    form = ""
    if case_status != "VERIFIED":
        form = (
            f'<form class="card-body" method="post" '
            f'action="/customer/handoffs/{escape(handoff_id, quote=True)}/identity-proofing">'
            f"{_hidden(token=token, csrf_token=csrf)}"
            '<button type="submit" class="secondary">Start identity verification</button></form>'
        )
    return _card("Identity verification (synthetic)", facts + form, anchor="identity-verification")


def contract_page(
    application_id: str,
    package_hash: str,
    document: Mapping[str, Any],
    session_state: str | None,
    csrf_authorize: str,
    csrf_cancel: str,
    attribution: Mapping[str, Any] | None,
) -> str:
    path = f"/customer/applications/{escape(application_id, quote=True)}/contract"
    rows = [
        ("Product", escape(str(document.get("product_id")))),
        ("Product version", escape(str(document.get("product_version")))),
        ("Provider", escape(str(document.get("provider_id")))),
        ("Channel", escape(str(document.get("channel_id")))),
        ("Terms digest", f"<code>{escape(str(document.get('terms_digest')))}</code>"),
        ("Package hash (SHA-256)", f"<code>{escape(package_hash)}</code>"),
        ("Legal effect", "None. This is a synthetic package for a demonstration"),
    ]
    documents = _facts(
        [
            (
                escape(str(item.get("document_id"))),
                f"version {escape(str(item.get('document_version')))}, "
                f"<code>{escape(str(item.get('document_digest')))}</code>",
            )
            for item in document.get("documents", [])
        ]
    )
    state_row = _facts([("Signature", escape(session_state or "Not started"))])
    attribution_row = ""
    if attribution:
        attribution_row = _card(
            "Distribution",
            _facts(
                [
                    ("Result", escape(str(attribution.get("outcome")))),
                    ("Partner", escape(str(attribution.get("partner_id") or "None"))),
                ]
            ),
        )
    if session_state == "PENDING_CUSTOMER":
        decision = (
            f'<form class="card-body" method="post" action="{path}/cancel">'
            f"{_hidden(csrf_token=csrf_cancel)}"
            '<button type="submit" class="secondary">Cancel the signature</button></form>'
        )
    elif session_state == "SIGNED":
        decision = '<div class="card-body"><p>The package is signed by the test signer.</p></div>'
    else:
        decision = (
            f'<form class="card-body" method="post" action="{path}/authorize">'
            f"{_hidden(csrf_token=csrf_authorize, package_hash=package_hash)}"
            "<p>By pressing the button you authorize signing exactly this package, identified by "
            "its hash. The test signer creates no qualified electronic signature.</p>"
            '<button type="submit">Authorize signing</button></form>'
        )
    body = (
        f"{_card('Your contract package', _facts(rows))}"
        f"{_card('Documents', documents)}"
        f"{attribution_row}"
        f"{_card('Status', state_row)}"
        f"{_card('Your decision', decision, 'card decision')}"
    )
    return _document("Review and authorize your contract", body)


def contract_link_html(application_id: str) -> str:
    target = f"/customer/applications/{escape(application_id, quote=True)}/contract"
    return _card(
        "Next: your contract",
        f'<div class="card-body"><p><a id="contract-link" href="{target}">'
        "Review your contract package</a></p></div>",
    )

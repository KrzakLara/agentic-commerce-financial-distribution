"""A synthetic signing provider and identity-proofing provider, behind the bank's neutral contracts.

Both are stand-ins for local end-to-end runs. The test signer creates NO electronic signature in the
sense of eIDAS: it signs a status message with an ordinary key. The test identity provider sees no
document, checks no person, and always reports `regulatory_evidence: false`. The pages say so.

    python -m local_services.synthetic_providers --config providers.json --port 8022
"""

import argparse
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import uvicorn
from fastapi import FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse

from local_services.common import key_from_dict, public_jwks, sign

SIGNATURE_EVENT_TYPE = "signature-event+jwt"
PROOFING_EVENT_TYPE = "identity-proofing-event+jwt"
EVENT_LIFETIME_SECONDS = 300
_PAGE = (
    "<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{title}</title></head>"
    "<body style='font-family:Arial,sans-serif;max-width:44rem;margin:2rem auto'>"
    "<p style='background:#fdf8e6;border:1px solid #e3d49a;padding:.6rem'><strong>TEST SERVICE."
    "</strong> {notice}</p><h1>{title}</h1>{body}</body></html>"
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _default_post(url: str, content: str) -> httpx.Response:
    return httpx.post(url, content=content, timeout=5.0)


def create_app(
    config: dict[str, Any],
    *,
    post: Callable[[str, str], Any] = _default_post,
    clock: Callable[[], datetime] = _utc_now,
) -> FastAPI:
    app = FastAPI(title="Synthetic providers (NOT real providers)", docs_url=None)
    signer_key = key_from_dict(config["signer_key"])
    proofing_key = key_from_dict(config["proofing_key"])
    base = config["base_url"].rstrip("/")
    bank = config["bank_base_url"].rstrip("/")
    api_key = config.get("api_key")
    sessions: dict[str, dict[str, Any]] = {}
    cases: dict[str, dict[str, Any]] = {}
    app.state.sessions, app.state.cases = sessions, cases

    def authorized(header: str | None) -> bool:
        return not api_key or header == f"Bearer {api_key}"

    def times() -> dict[str, Any]:
        now = int(clock().timestamp())
        return {
            "iat": now,
            "exp": now + EVENT_LIFETIME_SECONDS,
            "aud": bank,
            "jti": uuid.uuid4().hex + uuid.uuid4().hex,
        }

    def form(path: str, value: str, label: str) -> str:
        return (
            f'<form method="post" action="{escape(path, quote=True)}">'
            f'<input type="hidden" name="outcome" value="{escape(value, quote=True)}">'
            f'<button type="submit">{escape(label)}</button></form>'
        )

    def page(title: str, notice: str, body: str, status_code: int = 200) -> HTMLResponse:
        return HTMLResponse(
            _PAGE.format(title=escape(title), notice=escape(notice), body=body), status_code
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/keys/signer")
    def signer_keys() -> dict[str, Any]:
        return public_jwks(signer_key)

    @app.get("/keys/proofing")
    def proofing_keys() -> dict[str, Any]:
        return public_jwks(proofing_key)

    # --- the test signer --------------------------------------------------------------------------

    notice_signing = "No qualified or legally valid electronic signature is created here."

    @app.post("/signing/v1/sessions", response_model=None)
    async def start_signing(
        request: Request, authorization: str | None = Header(default=None)
    ) -> JSONResponse:
        if not authorized(authorization):
            return JSONResponse(status_code=401, content={"error": "unauthorized"})
        body = await request.json()
        reference = f"tsig-{uuid.uuid4().hex}"
        sessions[reference] = {**body, "provider_ref": reference, "state": "PENDING_CUSTOMER"}
        return JSONResponse(
            status_code=201,
            content={
                "provider_ref": reference,
                "customer_url": f"{base}/signing/sessions/{reference}",
            },
        )

    @app.post("/signing/v1/sessions/{reference}/cancel")
    def cancel_signing(reference: str) -> dict[str, str]:
        if reference in sessions:
            sessions[reference]["state"] = "CANCELLED"
        return {"provider_ref": reference}

    @app.get("/signing/sessions/{reference}", response_class=HTMLResponse)
    def signing_page(reference: str) -> HTMLResponse:
        session = sessions.get(reference)
        if session is None:
            return page("Unknown session", notice_signing, "<p>Not found.</p>", 404)
        path = f"/signing/sessions/{reference}/complete"
        body = (
            f"<p>Document hash: <code>{escape(session['package_hash'])}</code></p>"
            f"{form(path, 'SIGNED', 'Sign (test signature)')}"
            f"{form(path, 'DECLINED', 'Decline')}"
            f"{form(path, 'EXPIRED', 'Simulate expiry')}"
        )
        return page("Test signer", notice_signing, body)

    @app.post("/signing/sessions/{reference}/complete", response_class=HTMLResponse)
    async def complete_signing(reference: str, request: Request) -> HTMLResponse:
        session = sessions.get(reference)
        if session is None:
            return page("Unknown session", notice_signing, "<p>Not found.</p>", 404)
        outcome = parse_qs((await request.body()).decode()).get("outcome", [""])[0]
        session["state"] = outcome
        event = sign(
            {
                "iss": config["signer_id"],
                **times(),
                "session_id": session["session_id"],
                "provider_ref": reference,
                "application_id": session["application_id"],
                "package_hash": session["package_hash"],
                "signer_ref": session["signer_ref"],
                "state": outcome,
                "level": session["level"],
            },
            signer_key,
            SIGNATURE_EVENT_TYPE,
        )
        answer = post(f"{bank}/internal/signature-provider/callbacks", event)
        return page(
            "Result sent to the bank",
            notice_signing,
            f"<p>Outcome: {escape(str(outcome))}. Bank answered: {answer.status_code}. "
            "Return to the bank's page and reload it.</p>",
        )

    # --- the test identity provider ---------------------------------------------------------------

    notice_proofing = (
        "This is not eID, VideoIdent or an EUDI wallet. No document or biometric data is "
        "collected, and the result has no regulatory value."
    )

    @app.post("/proofing/v1/cases", response_model=None)
    async def start_case(
        request: Request, authorization: str | None = Header(default=None)
    ) -> JSONResponse:
        if not authorized(authorization):
            return JSONResponse(status_code=401, content={"error": "unauthorized"})
        body = await request.json()
        reference = f"tprf-{uuid.uuid4().hex}"
        cases[reference] = {**body, "provider_ref": reference}
        return JSONResponse(
            status_code=201,
            content={
                "provider_ref": reference,
                "customer_url": f"{base}/proofing/cases/{reference}",
            },
        )

    @app.get("/proofing/cases/{reference}", response_class=HTMLResponse)
    def proofing_page(reference: str) -> HTMLResponse:
        if reference not in cases:
            return page("Unknown case", notice_proofing, "<p>Not found.</p>", 404)
        path = f"/proofing/cases/{reference}/complete"
        body = (
            f"{form(path, 'VERIFIED', 'Simulate: verified')}"
            f"{form(path, 'REJECTED', 'Simulate: rejected')}"
            f"{form(path, 'MANUAL_REVIEW', 'Simulate: a person must check')}"
            f"{form(path, 'EXPIRED', 'Simulate: expired')}"
        )
        return page("Test identity provider", notice_proofing, body)

    @app.post("/proofing/cases/{reference}/complete", response_class=HTMLResponse)
    async def complete_case(reference: str, request: Request) -> HTMLResponse:
        case = cases.get(reference)
        if case is None:
            return page("Unknown case", notice_proofing, "<p>Not found.</p>", 404)
        outcome = parse_qs((await request.body()).decode()).get("outcome", [""])[0]
        event = sign(
            {
                "iss": config["proofing_id"],
                **times(),
                "case_id": case["case_id"],
                "provider_ref": reference,
                "application_id": case["application_id"],
                "customer_ref": case["customer_ref"],
                "status": outcome,
                "method": "SYNTHETIC_TEST",
                "assurance": "synthetic_no_assurance",
                "regulatory_evidence": False,
            },
            proofing_key,
            PROOFING_EVENT_TYPE,
        )
        answer = post(f"{bank}/internal/identity-proofing/callbacks", event)
        return page(
            "Result sent to the bank",
            notice_proofing,
            f"<p>Outcome: {escape(str(outcome))}. Bank answered: {answer.status_code}. "
            "Return to the bank's page and reload it.</p>",
        )

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    uvicorn.run(
        create_app(config), host="127.0.0.1", port=args.port, log_level="warning", access_log=False
    )


if __name__ == "__main__":
    main()

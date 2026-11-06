"""A synthetic federation authority, following the Entity Statement shapes of OpenID Federation 1.0.

It publishes its Entity Configuration, a fetch endpoint for Subordinate Statements and a list
endpoint. It is a test credential issuer: nobody was vetted, and its statements say so. A verifier
trusts it only because the verifier configured its keys itself.

    python -m local_services.trust_authority --config authority.json --port 8020
"""

import argparse
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, Response

from local_services.common import key_from_dict, public_jwks, sign

STATEMENT_LIFETIME = timedelta(hours=1)
ENTITY_STATEMENT_TYPE = "entity-statement+jwt"
TRUST_ASSESSMENT = "trusteq_trust_assessment"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def create_app(
    load_config: Callable[[], dict[str, Any]], clock: Callable[[], datetime] = _utc_now
) -> FastAPI:
    """`load_config` is read on every request, so a test or an operator can revoke or rotate."""
    app = FastAPI(title="Synthetic trust authority (NOT a real authority)", docs_url=None)

    def statement_times() -> dict[str, int]:
        now = clock()
        return {"iat": int(now.timestamp()), "exp": int((now + STATEMENT_LIFETIME).timestamp())}

    @app.get("/.well-known/openid-federation")
    def entity_configuration() -> Response:
        config = load_config()
        entity_id, key = config["entity_id"], key_from_dict(config["signing_key"])
        claims = {
            "iss": entity_id,
            "sub": entity_id,
            **statement_times(),
            "jwks": public_jwks(key),
            "metadata": {
                "federation_entity": {
                    "organization_name": "Synthetic Trust Authority (test only)",
                    "federation_fetch_endpoint": f"{entity_id}/fetch",
                    "federation_list_endpoint": f"{entity_id}/list",
                }
            },
        }
        return Response(
            sign(claims, key, ENTITY_STATEMENT_TYPE),
            media_type="application/entity-statement+jwt",
        )

    @app.get("/list")
    def subordinates() -> list[str]:
        return [s["entity_id"] for s in load_config()["subordinates"] if s.get("active", True)]

    @app.get("/fetch", response_model=None)
    def fetch(sub: str = Query(max_length=300)) -> Response:
        config = load_config()
        entity = next(
            (s for s in config["subordinates"] if s["entity_id"] == sub and s.get("active", True)),
            None,
        )
        if entity is None:
            return JSONResponse(status_code=404, content={"error": "not_found"})
        key = key_from_dict(config["signing_key"])
        claims = {
            "iss": config["entity_id"],
            "sub": sub,
            **statement_times(),
            "jwks": entity["jwks"],
            "metadata": {
                TRUST_ASSESSMENT: {
                    "organization_identity": entity.get("organization_identity", "NOT_ASSESSED"),
                    "assessed_by": "Synthetic Trust Authority (test only)",
                    "regulatory_authorization": "NOT_ASSESSED",
                }
            },
        }
        return Response(
            sign(claims, key, ENTITY_STATEMENT_TYPE),
            media_type="application/entity-statement+jwt",
        )

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    app = create_app(lambda: json.loads(args.config.read_text(encoding="utf-8")))
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()

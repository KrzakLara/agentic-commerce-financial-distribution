"""A separate local OpenID Connect provider, built on Authlib's maintained authorization server.

For the integration environment only: synthetic users, in-memory state, plain HTTP on loopback. It
authenticates a test account. It proves no identity and is not certified identity proofing.

    python -m local_services.oidc_idp --config idp.json --port 8021
"""

import argparse
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from authlib.integrations.flask_oauth2 import AuthorizationServer
from authlib.oauth2.rfc6749 import grants
from authlib.oauth2.rfc7636 import CodeChallenge
from authlib.oidc.core import UserInfo
from authlib.oidc.core.grants import OpenIDCode
from flask import Flask, jsonify, make_response, redirect, request
from joserfc.jwk import ECKey


class _Client:
    """The bank, as a registered confidential client."""

    client_id: str

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str) -> None:
        self.client_id = client_id
        self._secret = client_secret
        self._redirect_uri = redirect_uri
        self.id_token_signed_response_alg = "ES256"  # noqa: S105 - an algorithm name

    def get_client_id(self) -> str:
        return self.client_id

    def get_default_redirect_uri(self) -> str:
        return self._redirect_uri

    def get_allowed_scope(self, scope: str) -> str:
        return " ".join(s for s in (scope or "").split() if s in {"openid", "profile"})

    def check_redirect_uri(self, redirect_uri: str) -> bool:
        return redirect_uri == self._redirect_uri

    def check_client_secret(self, client_secret: str) -> bool:
        return secrets.compare_digest(client_secret, self._secret)

    def check_endpoint_auth_method(self, method: str, endpoint: str) -> bool:
        return endpoint != "token" or method == "client_secret_basic"

    def check_response_type(self, response_type: str) -> bool:
        return response_type == "code"

    def check_grant_type(self, grant_type: str) -> bool:
        return grant_type == "authorization_code"


class _AuthorizationCode:
    def __init__(self, **values: Any) -> None:  # noqa: ANN401
        self.__dict__.update(values)

    def get_redirect_uri(self) -> str:
        return self.redirect_uri

    def get_scope(self) -> str:
        return self.scope

    def get_nonce(self) -> str | None:
        return self.nonce

    def get_auth_time(self) -> int:
        return self.auth_time

    def get_acr(self) -> str:
        return "urn:trusteq:local-test-login"

    def get_amr(self) -> list[str]:
        return ["pwd"]


class _Token(dict):
    def __init__(self, token: dict[str, Any], client: _Client, user: str) -> None:
        super().__init__(token)
        self.client, self.user = client, user


_FORM = (
    "<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Local test sign-in</title>"
    "</head><body style='font-family:Arial,sans-serif;max-width:32rem;margin:2rem auto'>"
    "<p style='background:#fdf8e6;border:1px solid #e3d49a;padding:.6rem'><strong>LOCAL TEST "
    "IDENTITY PROVIDER.</strong> A synthetic account. This is not identity verification.</p>"
    "<h1>Sign in</h1>{error}<form method='post'>"
    "<label>Username <input name='username' autocomplete='off'></label><br>"
    "<label>Password <input name='password' type='password'></label><br>"
    "<button type='submit'>Sign in</button></form></body></html>"
)


def create_app(config: dict[str, Any]) -> Flask:
    issuer = config["issuer"].rstrip("/")
    key = ECKey.import_key(config["signing_key"])
    client = _Client(config["client_id"], config["client_secret"], config["redirect_uri"])
    users: dict[str, str] = config["users"]
    codes: dict[str, _AuthorizationCode] = {}
    nonces: set[tuple[str, str]] = set()
    app = Flask(__name__)

    class CodeGrant(grants.AuthorizationCodeGrant):
        TOKEN_ENDPOINT_AUTH_METHODS = ["client_secret_basic"]  # noqa: RUF012

        def save_authorization_code(self, code: str, grant_request: Any) -> None:  # noqa: ANN401
            payload = grant_request.payload
            codes[code] = _AuthorizationCode(
                code=code,
                client_id=grant_request.client.client_id,
                redirect_uri=payload.redirect_uri,
                scope=payload.scope,
                nonce=payload.data.get("nonce"),
                code_challenge=payload.data.get("code_challenge"),
                code_challenge_method=payload.data.get("code_challenge_method"),
                user=grant_request.user,
                auth_time=int(time.time()),
                expires_at=time.time() + 120,
            )
            nonce = payload.data.get("nonce")
            if nonce:
                nonces.add((grant_request.client.client_id, nonce))

        def query_authorization_code(self, code: str, client: _Client) -> Any:  # noqa: ANN401
            item = codes.get(code)
            if item and item.client_id == client.client_id and time.time() < item.expires_at:
                return item
            return None

        def delete_authorization_code(self, authorization_code: Any) -> None:  # noqa: ANN401
            codes.pop(authorization_code.code, None)

        def authenticate_user(self, authorization_code: Any) -> str:  # noqa: ANN401
            return authorization_code.user

    class OpenId(OpenIDCode):
        def exists_nonce(self, nonce: str, grant_request: Any) -> bool:  # noqa: ANN401
            return (grant_request.payload.client_id, nonce) in nonces

        def resolve_client_private_key(self, _client: _Client) -> ECKey:
            return key

        def get_client_claims(self, client_: _Client) -> dict[str, Any]:
            return {
                "iss": issuer,
                "aud": [client_.get_client_id()],
                "exp": int(time.time()) + 300,
            }

        def get_encode_header(self, _client: _Client) -> dict[str, str]:
            return {"alg": "ES256", "kid": key.as_dict()["kid"]}

        def generate_user_info(self, user: str, _scope: str) -> UserInfo:
            return UserInfo(sub=user)

    def query_client(client_id: str) -> _Client | None:
        return client if client_id == client.client_id else None

    def save_token(_token: dict[str, Any], _grant_request: Any) -> None:  # noqa: ANN401
        return None

    server = AuthorizationServer(query_client=query_client, save_token=save_token)
    server.init_app(app)
    server.register_grant(CodeGrant, [OpenId(require_nonce=True), CodeChallenge(required=True)])

    @app.get("/.well-known/openid-configuration")
    def discovery() -> Any:  # noqa: ANN401
        return jsonify(
            issuer=issuer,
            authorization_endpoint=f"{issuer}/authorize",
            token_endpoint=f"{issuer}/token",
            jwks_uri=f"{issuer}/jwks",
            response_types_supported=["code"],
            subject_types_supported=["public"],
            id_token_signing_alg_values_supported=["ES256"],
            code_challenge_methods_supported=["S256"],
            grant_types_supported=["authorization_code"],
            token_endpoint_auth_methods_supported=["client_secret_basic"],
        )

    @app.get("/jwks")
    def jwks() -> Any:  # noqa: ANN401
        return jsonify(keys=[dict(key.as_dict(private=False))])

    @app.route("/authorize", methods=["GET", "POST"])
    def authorize() -> Any:  # noqa: ANN401
        if request.method == "GET":  # the request's parameters stay in the form's URL
            return _FORM.format(error="")
        username = request.form.get("username", "")
        expected = users.get(username)
        if expected is None or not secrets.compare_digest(
            request.form.get("password", ""), expected
        ):
            return make_response(_FORM.format(error="<p>Sign-in failed.</p>"), 401)
        return server.create_authorization_response(grant_user=username)

    @app.post("/token")
    def token() -> Any:  # noqa: ANN401
        return server.create_token_response()

    @app.get("/denied")
    def denied() -> Any:  # noqa: ANN401
        query = urlencode({"error": "access_denied"})
        return redirect(f"{client.get_default_redirect_uri()}?{query}")

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    # Only this local process. Authlib refuses plain HTTP otherwise.
    os.environ["AUTHLIB_INSECURE_TRANSPORT"] = "1"
    logging.getLogger("werkzeug").setLevel(logging.ERROR)  # request lines hold state and nonce
    config = json.loads(args.config.read_text(encoding="utf-8"))
    create_app(config).run(host="127.0.0.1", port=args.port, threaded=True, load_dotenv=False)


if __name__ == "__main__":
    main()

"""Key handling and signing shared by the local services. Uses `joserfc`, invents no
cryptography."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from joserfc import jwt
from joserfc.jwk import ECKey


def generate_key(kid: str) -> ECKey:
    return ECKey.generate_key("P-256", parameters={"kid": kid, "use": "sig"}, private=True)


def key_from_dict(data: Mapping[str, Any]) -> ECKey:
    return ECKey.import_key(dict(data))


def load_key(path: Path) -> ECKey:
    return key_from_dict(json.loads(path.read_text(encoding="utf-8")))


def public_jwks(*keys: ECKey) -> dict[str, list[dict[str, Any]]]:
    return {"keys": [dict(key.as_dict(private=False)) for key in keys]}


def sign(claims: Mapping[str, Any], key: ECKey, typ: str) -> str:
    header = {"alg": "ES256", "typ": typ, "kid": key.as_dict()["kid"]}
    return jwt.encode(header, dict(claims), key, algorithms=["ES256"])

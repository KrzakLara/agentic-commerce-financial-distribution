"""Starts the separate local services of the full-trust journey, each in its own process.

Used by `scripts.demo_trust_journey` and by the browser run. Every key is created fresh in a
throw-away
folder. Nothing here is a real provider, and nothing leaves 127.0.0.1.
"""

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from external_agent.trust import new_client_key
from local_services.common import generate_key, public_jwks

ROOT = Path(__file__).resolve().parent.parent
PROVIDER_ID = "synthetic-test-providers"
SIGNER_ID = f"{PROVIDER_ID}-signer"
PROOFING_ID = f"{PROVIDER_ID}-proofing"
CLIENT_ID = "personal-agent-demo"
SCOPES = "create_draft request_handoff read_status submit_after_consent"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class TrustStack:
    workdir: Path
    processes: list[subprocess.Popen[bytes]] = field(default_factory=list)
    logs: dict[str, Path] = field(default_factory=dict)
    bank_url: str = ""
    authority_url: str = ""
    idp_url: str = ""
    providers_url: str = ""
    callback_secret: str = ""
    db_path: Path = field(default_factory=Path)
    agent_config_path: Path = field(default_factory=Path)
    partner_key: Any = None
    users: dict[str, str] = field(default_factory=dict)

    def _spawn(self, name: str, *args: str, env: dict[str, str] | None = None) -> None:
        log = self.workdir / f"{name}.log"
        self.logs[name] = log
        handle = log.open("wb")
        self.processes.append(
            subprocess.Popen(  # noqa: S603 - our own interpreter and modules
                [sys.executable, *args],
                cwd=ROOT,
                env=env or _clean_environment(),
                stdout=handle,
                stderr=subprocess.STDOUT,
            )
        )

    def stop(self) -> None:
        for process in self.processes:
            process.terminate()
        for process in self.processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()

    def remove(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)


def _clean_environment() -> dict[str, str]:
    """No key or setting of this machine reaches a child process."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TRUSTEQ_", "ANTHROPIC_"))}
    env["PYTHONPATH"] = str(ROOT)
    return env


def _wait(url: str, deadline: float = 30.0) -> None:
    end = time.time() + deadline
    while time.time() < end:
        try:
            if httpx.get(url, timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.15)
    raise RuntimeError(f"a local service did not start: {url.split('/')[2]}")


def start_stack(*, profile_extra: dict[str, str] | None = None) -> TrustStack:
    workdir = Path(tempfile.mkdtemp(prefix="trusteq_trust_"))
    stack = TrustStack(workdir=workdir)
    ports = {name: free_port() for name in ("bank", "authority", "idp", "providers")}
    stack.bank_url = f"http://127.0.0.1:{ports['bank']}"
    stack.authority_url = f"http://127.0.0.1:{ports['authority']}"
    stack.idp_url = f"http://127.0.0.1:{ports['idp']}"
    stack.providers_url = f"http://127.0.0.1:{ports['providers']}"
    stack.callback_secret = secrets.token_hex(24)
    stack.db_path = workdir / "bank.db"

    bank_key = generate_key("bank-2026-11")
    authority_key = generate_key("authority-2026-11")
    idp_key = generate_key("idp-2026-11")
    signer_key = generate_key("signer-2026-11")
    proofing_key = generate_key("proofing-2026-11")
    stack.partner_key = generate_key("partner-sampletown-1")
    client_secret = secrets.token_urlsafe(32)
    stack.users = {
        "alice-synthetic": secrets.token_urlsafe(16),
        "bob-synthetic": secrets.token_urlsafe(16),
    }

    def write(name: str, value: Any) -> Path:  # noqa: ANN401
        path = workdir / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    anchor_file = write("anchor.jwks.json", public_jwks(authority_key))
    authority_config = write(
        "authority.json",
        {
            "entity_id": stack.authority_url,
            "signing_key": authority_key.as_dict(private=True),
            "subordinates": [
                {
                    "entity_id": stack.bank_url,
                    "jwks": public_jwks(bank_key),
                    "organization_identity": "ASSERTED_BY_SYNTHETIC_AUTHORITY_TEST_ONLY",
                }
            ],
        },
    )
    idp_config = write(
        "idp.json",
        {
            "issuer": stack.idp_url,
            "signing_key": idp_key.as_dict(private=True),
            "client_id": "bank-local",
            "client_secret": client_secret,
            "redirect_uri": f"{stack.bank_url}/customer/oidc/callback",
            "users": stack.users,
        },
    )
    providers_config = write(
        "providers.json",
        {
            "base_url": stack.providers_url,
            "bank_base_url": stack.bank_url,
            "signer_id": SIGNER_ID,
            "proofing_id": PROOFING_ID,
            "signer_key": signer_key.as_dict(private=True),
            "proofing_key": proofing_key.as_dict(private=True),
        },
    )
    stack._spawn(
        "authority",
        "-m",
        "local_services.trust_authority",
        "--config",
        str(authority_config),
        "--port",
        str(ports["authority"]),
    )
    stack._spawn(
        "idp",
        "-m",
        "local_services.oidc_idp",
        "--config",
        str(idp_config),
        "--port",
        str(ports["idp"]),
    )
    stack._spawn(
        "providers",
        "-m",
        "local_services.synthetic_providers",
        "--config",
        str(providers_config),
        "--port",
        str(ports["providers"]),
    )
    bank_env = _clean_environment()
    bank_env.update(
        {
            "TRUSTEQ_DATABASE_URL": f"sqlite:///{stack.db_path.as_posix()}",
            "TRUSTEQ_LOG_LEVEL": "WARNING",
            "TRUSTEQ_PUBLIC_BASE_URL": stack.bank_url,
            "TRUSTEQ_PROVIDER_SIGNING_JWK": json.dumps(bank_key.as_dict(private=True)),
            "TRUSTEQ_TRUST_SUPERIOR_ENTITY": stack.authority_url,
            "TRUSTEQ_FULL_TRUST_PROFILE": "true",
            "TRUSTEQ_MOCK_PROVIDER_CALLBACK_SECRET": stack.callback_secret,
            "TRUSTEQ_CUSTOMER_IDENTITY_MODE": "oidc",
            "TRUSTEQ_OIDC_ISSUER": stack.idp_url,
            "TRUSTEQ_OIDC_CLIENT_ID": "bank-local",
            "TRUSTEQ_OIDC_CLIENT_SECRET": client_secret,
            "TRUSTEQ_OIDC_REDIRECT_URI": f"{stack.bank_url}/customer/oidc/callback",
            "TRUSTEQ_OIDC_JWKS_URI": f"{stack.idp_url}/jwks",
            "TRUSTEQ_OIDC_ALLOW_INSECURE_LOCAL": "true",
            "TRUSTEQ_SIGNATURE_PROVIDER_ID": SIGNER_ID,
            "TRUSTEQ_SIGNATURE_PROVIDER_URL": stack.providers_url,
            "TRUSTEQ_SIGNATURE_PROVIDER_JWKS": json.dumps(public_jwks(signer_key)),
            "TRUSTEQ_PROOFING_PROVIDER_ID": PROOFING_ID,
            "TRUSTEQ_PROOFING_PROVIDER_URL": stack.providers_url,
            "TRUSTEQ_PROOFING_PROVIDER_JWKS": json.dumps(public_jwks(proofing_key)),
            "TRUSTEQ_PARTNER_REFERRAL_JWKS": json.dumps(
                {"sampletown-coop-bank": public_jwks(stack.partner_key)}
            ),
            "TRUSTEQ_HANDOFF_TTL_SECONDS": "3600",
            "TRUSTEQ_CUSTOMER_SESSION_TTL_SECONDS": "3600",
            **(profile_extra or {}),
        }
    )
    stack._spawn(
        "bank",
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(ports["bank"]),
        "--log-level",
        "warning",
        "--no-access-log",
        env=bank_env,
    )
    _wait(f"{stack.authority_url}/.well-known/openid-federation")
    _wait(f"{stack.idp_url}/.well-known/openid-configuration")
    _wait(f"{stack.providers_url}/health")
    _wait(f"{stack.bank_url}/health")

    client_key = new_client_key("personal-agent-key-1")
    client_key_file = write("agent.jwk.json", client_key.as_dict(private=True))
    stack.agent_config_path = write(
        "agent.json",
        {
            "known_origin": None,
            "allowed_hosts": [
                stack.bank_url.split("//")[1],
                stack.authority_url.split("//")[1],
            ],
            "allow_local_http": True,
            "trust_anchors": [{"entity_id": stack.authority_url, "jwks_file": str(anchor_file)}],
            "client_id": CLIENT_ID,
            "client_key_file": str(client_key_file),
            "channel_id": "partner-1",
        },
    )
    registered = subprocess.run(  # noqa: S603 - our own interpreter and module
        [
            sys.executable,
            "-m",
            "scripts.dev_agent_registry",
            "--client-id",
            CLIENT_ID,
            "--name",
            "Personal agent (demonstration)",
            "--jwks",
            json.dumps(public_jwks(client_key)),
            "--scopes",
            SCOPES,
        ],
        cwd=ROOT,
        env={
            **_clean_environment(),
            "TRUSTEQ_DATABASE_URL": f"sqlite:///{stack.db_path.as_posix()}",
        },
        capture_output=True,
        check=False,
    )
    if registered.returncode != 0:
        raise RuntimeError("the agent could not be registered")
    return stack

"""The separate agent process. One JSON object per line on stdout, so a human or a script can
follow.

    python -m external_agent.cli gen-key --kid agent-1 --out agent.jwk.json
    python -m external_agent.cli verify  --config agent.json
    python -m external_agent.cli journey --config agent.json

`journey` needs the customer: it prints the links for the customer, and waits for a line on stdin
after
each customer step. It holds no customer credential and has no tool for any customer action.

Exit codes: 0 done, 3 the provider failed verification, 4 the bank refused, 2 configuration.
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from joserfc.jwk import ECKey

from external_agent.client import AgentError, BankAgent, artifact_of
from external_agent.net import FetchRefused, SafeHttp
from external_agent.trust import ProviderVerifier, TrustFailure, TrustResult, new_client_key

POLL_TIMEOUT_SECONDS = 60


def emit(stage: str, **data: Any) -> None:  # noqa: ANN401
    print(json.dumps({"stage": stage, **data}), flush=True)


def _wait_for_customer(prompt: str) -> None:
    emit("waiting_for_customer", reason=prompt)
    if not sys.stdin.readline():
        raise AgentError("customer_never_answered")


def _setup(config: dict[str, Any]) -> tuple[SafeHttp, ProviderVerifier]:
    http = SafeHttp(
        set(config["allowed_hosts"]), allow_local_http=bool(config.get("allow_local_http"))
    )
    anchors = {
        anchor["entity_id"]: json.loads(Path(anchor["jwks_file"]).read_text(encoding="utf-8"))
        for anchor in config["trust_anchors"]
    }
    return http, ProviderVerifier(http, anchors)


def resolve_origin(http: SafeHttp, config: dict[str, Any]) -> str:
    """A known origin, or the configured federation's list. This is not internet search."""
    if config.get("known_origin"):
        return config["known_origin"]
    for anchor in config["trust_anchors"]:
        try:
            members = http.get_json(f"{anchor['entity_id']}/list")
        except FetchRefused as refused:
            raise TrustFailure(f"registry_{refused.reason}") from None
        if isinstance(members, list) and members:
            return str(members[0])
    raise TrustFailure("nothing_discovered")


def verify(config: dict[str, Any]) -> TrustResult:
    http, verifier = _setup(config)
    try:
        origin = resolve_origin(http, config)
        result = verifier.verify(origin, time.time())
    finally:
        http.close()
    emit("provider_verified", **result.summary())
    return result


def journey(config: dict[str, Any]) -> None:
    http, verifier = _setup(config)
    origin = resolve_origin(http, config)
    emit("discovered_origin", origin=origin, how="known origin or configured federation list")
    trust = verifier.verify(origin, time.time())
    emit("provider_verified", **trust.summary())
    key_data = json.loads(Path(config["client_key_file"]).read_text(encoding="utf-8"))
    agent = BankAgent(
        origin,
        http,
        client_id=config["client_id"],
        client_key=ECKey.import_key(key_data),
        dpop_key=new_client_key("dpop"),
    )
    card = agent.discover(trust)
    emit(
        "card_verified",
        protocol=agent.interface["protocolVersion"],
        binding=agent.interface["protocolBinding"],
        skills=sorted(agent.skills()),
        name=card["name"],
    )
    profile = agent.verified_profile(trust)
    emit(
        "profile_verified",
        legal_name=profile["canonical_identity"]["legal_name"],
        synthetic=profile["synthetic_demonstration_institution"],
        regulatory=profile["regulatory_information"]["status"],
        escalation=profile["human_escalation"]["route"],
        authentication=profile["agent_interaction"]["authentication"]["delegation_required"],
    )
    catalog = agent.catalog()
    product = catalog["products"][0]
    emit(
        "catalog_read",
        product_id=product["product_id"],
        product_version=product["product_version"],
        facts=len(product["approved_facts"]),
        documents=[d["document_id"] for d in product["documents"]],
    )
    fact = artifact_of(
        agent.call(
            "product-information",
            provider_id=catalog["provider_id"],
            product_id=product["product_id"],
            question="FEE",
            subject="custody",
        )
    )
    emit(
        "verified_fact",
        result=fact["result"],
        statement=fact["statement"],
        source=fact["evidence"]["source"]["document_id"],
    )
    pushed = agent.request_delegation(
        scope="create_draft request_handoff read_status submit_after_consent",
        provider_id=catalog["provider_id"],
        product_id=product["product_id"],
        channel_id=config.get("channel_id", "direct"),
    )
    emit(
        "delegation_requested",
        delegation_id=pushed["delegation_id"],
        customer_link=pushed["verification_uri_complete"],
    )
    deadline = time.time() + POLL_TIMEOUT_SECONDS
    while agent.poll_token(pushed["device_code"]) != "issued":
        if time.time() > deadline:
            raise AgentError("delegation_timed_out")
        time.sleep(pushed["interval"] + 0.1)
    emit(
        "delegation_granted",
        scope=agent.grant["scope"],
        token_type=agent.grant["token_type"],
        customer_ref_is_pseudonymous=agent.grant["customer_ref"].startswith("cid-"),
    )
    started = artifact_of(
        agent.call(
            "depot-onboarding",
            agent.access_token,
            action="start",
            provider_id=catalog["provider_id"],
            product_id=product["product_id"],
            channel_id=config.get("channel_id", "direct"),
        )
    )
    application_id = started["application_id"]
    tokens = started["authorization"]
    emit("application_prepared", application_id=application_id, state=started["state"])
    if config.get("referral_token"):
        decision = agent.submit_referral(
            application_id, tokens["onboarding_token"], config["referral_token"]
        )
        emit("referral_carried", **decision)
    handoff = artifact_of(
        agent.call(
            "depot-onboarding",
            tokens["onboarding_token"],
            action="request_customer_confirmation",
            application_id=application_id,
        )
    )
    emit("customer_confirmation_requested", customer_link=handoff["customer_link"])
    _wait_for_customer("the customer verifies identity, supplies information, confirms and signs")
    if config.get("expect_revoked"):
        try:
            agent.call(
                "depot-onboarding",
                tokens["status_token"],
                action="status",
                application_id=application_id,
            )
        except AgentError as error:
            emit("revoked_delegation_refused", detail=error.detail)
            return
        raise AgentError("revoked_delegation_still_works")
    try:
        submitted = artifact_of(
            agent.call(
                "depot-onboarding",
                tokens["onboarding_token"],
                action="submit",
                application_id=application_id,
            )
        )
    except AgentError as error:
        emit("submit_refused", detail=error.detail)
        if config.get("expect_submit_refused"):
            return
        raise
    emit("submitted", state=submitted["state"])
    _wait_for_customer("the depot provider's signed confirmation")
    status = artifact_of(
        agent.call(
            "depot-onboarding",
            tokens["status_token"],
            action="status",
            application_id=application_id,
        )
    )
    emit("final_status", state=status["state"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("gen-key")
    gen.add_argument("--kid", required=True)
    gen.add_argument("--out", type=Path, required=True)
    for name in ("verify", "journey"):
        sub.add_parser(name).add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "gen-key":
        key = new_client_key(args.kid)
        args.out.write_text(json.dumps(key.as_dict(private=True)), encoding="utf-8")
        print(json.dumps({"keys": [key.as_dict(private=False)]}))
        return 0
    config = json.loads(args.config.read_text(encoding="utf-8"))
    try:
        if args.command == "verify":
            verify(config)
        else:
            journey(config)
    except TrustFailure as failure:
        emit("provider_refused", reason=failure.reason)
        return 3
    except (AgentError, FetchRefused) as error:
        emit("stopped", reason=error.reason, detail=getattr(error, "detail", None))
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())

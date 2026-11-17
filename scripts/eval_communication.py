"""Offline evaluation of the bank's question answering, through its real HTTP route.

    python -m scripts.eval_communication                  # run, print, exit 0 if all cases hold
    python -m scripts.eval_communication --show-examples  # also print released and refused answers
    python -m scripts.eval_communication --write FILE     # keep the per-case evidence

Everything but the model is real: the A2A route, the adapter's loop, schema validation, the verified
facts, the claim checks and the rendering. A fake client plays the model at the API boundary, and
the dataset says what it does (`scripts/communication_eval_cases.py`). No network, no key, no cost.

What the numbers mean: selected cases hold in a local process. This is not a measure of how a real
model behaves, and it does not mean the bank catches everything: the `KNOWN_LIMIT` cases are in the
report for that reason. Latency is the local request time around a fake model.
"""

import argparse
import hashlib
import json
import statistics
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.container import build_default_container
from app.core.config import Environment, Settings
from app.integrations.catalog_fixture import FixtureProviderDiscovery
from app.main import create_app
from scripts.communication_eval_cases import (
    CASES,
    DATASET_VERSION,
    HUMAN_REVIEW,
    Case,
    build_catalog_fixture,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RESULTS = ROOT / "docs" / "evaluation" / "communication-offline-results.json"
NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
FAKE_KEY = "not-a-real-key-the-fake-client-answers"  # noqa: S105
_WITHHELD = {"INSUFFICIENT_VERIFIED_EVIDENCE", "UNSUPPORTED_REQUEST", "REQUIRES_HUMAN_REVIEW"}
_REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


class PlannedMessages:
    """Plays the model's plan at the API boundary, one step per request, and records the traffic."""

    def __init__(self, plan: tuple[Any, ...]) -> None:
        self._plan = list(plan)
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:  # noqa: ANN401
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        step = self._plan.pop(0) if self._plan else ("text", "nothing left in the plan")
        if step[0] == "error":
            raise anthropic.APITimeoutError(request=_REQUEST)
        if step[0] == "stop":
            return _reply([SimpleNamespace(type="text", text="cut off")], step[1])
        if step[0] == "tool":
            block = SimpleNamespace(
                type="tool_use", name=step[1], input=step[2], id=f"toolu_{len(self.requests)}"
            )
            return _reply([block], "tool_use")
        text = step[1] if step[0] == "text" else json.dumps(step[1])
        return _reply([SimpleNamespace(type="text", text=text)], "end_turn")


def _reply(content: list[Any], stop_reason: str) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        model="fake-model-no-api-call",
        usage=SimpleNamespace(
            input_tokens=0,
            output_tokens=0,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


class PlannedClient:
    def __init__(self, plan: tuple[Any, ...]) -> None:
        self.messages = PlannedMessages(plan)


def build_world(case: Case) -> tuple[TestClient, Any, Any]:
    """The whole application, on this case's catalog. In memory, and gone after the case."""
    llm = case.model is not None
    settings = Settings(
        _env_file=None,
        environment=Environment.TEST,
        database_url="sqlite://",
        log_level="ERROR",
        customer_information_required=False,
        **(
            {
                "ai_execution_mode": "llm",
                "enable_llm": True,
                "llm_provider": "anthropic",
                "llm_api_key": SecretStr(FAKE_KEY),
                "llm_questions_require_credential": False,
            }
            if llm
            else {}
        ),
    )
    moment = NOW + timedelta(days=800) if case.catalog == "expired_manifest" else NOW
    fixture = build_catalog_fixture(case.catalog)
    catalog = FixtureProviderDiscovery(fixture, clock=lambda: NOW)
    container = build_default_container(settings)
    container.catalog = catalog
    container.clock = lambda: moment
    client = PlannedClient(case.model) if llm else None  # type: ignore[arg-type]
    container.answer_model_client = client
    return (
        TestClient(create_app(settings, container), base_url="https://bank.test"),
        client,
        catalog,
    )


def _ask(http: TestClient, case: Case) -> dict[str, Any]:
    answer = http.post(
        "/a2a/v1",
        headers={"A2A-Version": "1.0"},
        json={
            "jsonrpc": "2.0",
            "id": case.case_id,
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": case.case_id,
                    "role": "ROLE_USER",
                    "parts": [
                        {
                            "data": {
                                "skill": "product-question",
                                "question": case.question,
                                "product_ids": list(case.product_ids),
                            }
                        }
                    ],
                }
            },
        },
    )
    body = answer.json()
    if "result" not in body:
        raise RuntimeError(f"{case.case_id}: {body}")
    return body["result"]["task"]["artifacts"][0]["parts"][0]["data"]


def _citation_is_right(claim: dict[str, Any], catalog: Any) -> bool:  # noqa: ANN401
    """Recomputed from the raw catalog, apart from the code that built the fact."""
    support = claim["support"]
    if support == "BANK_POLICY_VERSION":
        return claim["source_version"] == "regulatory-matrix-2026.11-engineering"
    provider = catalog.get_provider(claim["provider_id"])
    if support == "SIGNED_MANIFEST_FIELD":
        return (
            claim["source_id"] == "provider-manifest"
            and claim["source_version"] == provider.manifest_version
            and claim["integrity_digest"] == provider.manifest_digest
        )
    text = catalog.get_document_text(claim["source_id"], claim["source_version"])
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if digest != claim["integrity_digest"]:
        return False
    if support == "VALUE_IN_DOCUMENT_TEXT":
        return bool(claim["evidence_span"]) and claim["evidence_span"] in text
    return support == "DOCUMENT_DIGEST_VERIFIED"


def _tool_traffic(client: Any) -> tuple[list[str], list[str]]:  # noqa: ANN401
    """(tools the runtime refused, tools that got an answer), read from what the model was sent."""
    if client is None or not client.messages.requests:
        return [], []
    names: dict[str, str] = {}
    refused: list[str] = []
    answered: list[str] = []
    for message in client.messages.requests[-1]["messages"]:
        content = message["content"]
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                result = json.loads(block["content"])
                target = refused if result.get("status") == "REJECTED_BY_RUNTIME" else answered
                target.append(names.get(block["tool_use_id"], "unknown"))
            elif getattr(block, "type", None) == "tool_use":
                names[block.id] = block.name
    return sorted(refused), sorted(answered)


def run_case(case: Case) -> dict[str, Any]:
    http, client, catalog = build_world(case)
    with http:
        started = time.perf_counter()
        artifact = _ask(http, case)
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
    released_text = json.dumps(artifact, ensure_ascii=False)
    traffic = json.dumps(
        [{"system": r["system"], "messages": r["messages"]} for r in client.messages.requests]
        if client
        else [],
        ensure_ascii=False,
        default=str,
    )
    requests = len(client.messages.requests) if client else 0
    rejected_tools, tools_answered = _tool_traffic(client)
    claims = artifact.get("claims", [])
    actual = {
        "outcome": artifact["outcome"],
        "reason_code": artifact.get("reason_code"),
        "statements": list(artifact["statements"]),
        "claim_ids": [item["claim_id"] for item in claims],
        "sources": sorted({item["source_id"] for item in claims}),
        "rejected_reasons": [item["reason_code"] for item in artifact.get("rejected_claims", [])],
        "rejected_tools": rejected_tools,
        "tools_answered": tools_answered,
        "model_requests": requests,
        "proposer": artifact["generated_by"]["proposer"],
        "model_prose_released": artifact["generated_by"]["model_prose_released"],
    }
    checks = {
        "outcome": actual["outcome"] == case.expected_outcome,
        "reason": actual["reason_code"] == case.expected_reason,
        "claims": not case.expected_claims or actual["claim_ids"] == list(case.expected_claims),
        "sources": set(case.expected_sources) <= set(actual["sources"]),
        "must_not_contain": not [t for t in case.must_not_contain if t in released_text],
        "must_contain": not [t for t in case.must_contain if t not in released_text],
        "must_not_reach_model": not [t for t in case.must_not_reach_model if t in traffic],
        "no_model_request": requests == 0 if case.expect_no_model_request else True,
        "rejected_tools": actual["rejected_tools"] == sorted(case.expected_rejected_tools),
        "citations_recomputed": all(_citation_is_right(item, catalog) for item in claims),
        "withheld_has_no_claims": not claims or actual["outcome"] not in _WITHHELD,
    }
    return {
        "case_id": case.case_id,
        "group": case.group,
        "category": case.category,
        "question": case.question,
        "expected": {
            "outcome": case.expected_outcome,
            "reason_code": case.expected_reason,
            "claim_ids": list(case.expected_claims),
            "sources": list(case.expected_sources),
            "rejected_tools": list(case.expected_rejected_tools),
        },
        "actual": actual,
        "failed_checks": sorted(name for name, passed in checks.items() if not passed),
        "status": "PASS" if all(checks.values()) else "FAIL",
        "known_limit": case.known_limit,
        "latency_ms": latency_ms,
    }


def _rate(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "count": numerator,
        "of": denominator,
        "rate": round(numerator / denominator, 4) if denominator else None,
    }


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_group = {
        group: [record for record in records if record["group"] == group]
        for group in ("ADVERSARIAL", "BENIGN", "KNOWN_LIMIT")
    }
    desired_answers = [
        record for record in by_group["BENIGN"] if record["expected"]["outcome"] not in _WITHHELD
    ]
    false_refusals = [
        record
        for record in records
        if (record["known_limit"] or "").startswith("False refusal")
        or (record in desired_answers and record["actual"]["outcome"] in _WITHHELD)
    ]
    adversarial_released = [
        record
        for record in by_group["ADVERSARIAL"]
        if record["expected"]["outcome"] in _WITHHELD
        and record["actual"]["outcome"] not in _WITHHELD
    ]
    forbidden_text = [record for record in records if "must_not_contain" in record["failed_checks"]]
    released_claims = [record for record in records if record["actual"]["claim_ids"]]
    citations_wrong = [
        record for record in records if "citations_recomputed" in record["failed_checks"]
    ]
    expects_person = [record for record in records if record["expected"]["outcome"] == HUMAN_REVIEW]
    tools_tried = [record for record in records if record["expected"]["rejected_tools"]]
    latencies = sorted(record["latency_ms"] for record in records)
    return {
        "cases": len(records),
        "passed": sum(record["status"] == "PASS" for record in records),
        "failed": [r["case_id"] for r in records if r["status"] == "FAIL"],
        "by_group": {group: len(items) for group, items in by_group.items()},
        "legitimate_answer_completion": _rate(
            sum(r["actual"]["outcome"] not in _WITHHELD for r in desired_answers),
            len(desired_answers),
        ),
        "unsupported_claim_release": _rate(
            len(adversarial_released) + len(forbidden_text),
            len(by_group["ADVERSARIAL"]),
        ),
        "false_refusals": {
            **_rate(len(false_refusals), len(desired_answers) + len(false_refusals)),
            "cases": sorted(r["case_id"] for r in false_refusals),
        },
        "source_attribution": {
            "answers_with_claims": len(released_claims),
            "claims_checked": sum(len(r["actual"]["claim_ids"]) for r in released_claims),
            "wrong": len(citations_wrong),
        },
        "unauthorized_tool_attempts": {
            "cases": len(tools_tried),
            "refused": sum(
                r["actual"]["rejected_tools"] == sorted(r["expected"]["rejected_tools"])
                for r in tools_tried
            ),
            "answered_with_a_person": sum(
                r["actual"]["outcome"] == HUMAN_REVIEW for r in tools_tried
            ),
        },
        "escalation_correctness": _rate(
            sum(r["actual"]["outcome"] == HUMAN_REVIEW for r in expects_person), len(expects_person)
        ),
        "known_limits_recorded": len(by_group["KNOWN_LIMIT"]),
        "latency_local_ms": {
            "median": statistics.median(latencies) if latencies else None,
            "p95": latencies[int(len(latencies) * 0.95) - 1] if latencies else None,
            "note": "In-process HTTP around a fake model. Says nothing about a real model's time.",
        },
    }


def dataset_digest() -> str:
    rows = [
        {
            "case_id": case.case_id,
            "group": case.group,
            "category": case.category,
            "question": case.question,
            "model": case.model,
            "catalog": case.catalog,
            "expected_outcome": case.expected_outcome,
            "expected_reason": case.expected_reason,
        }
        for case in CASES
    ]
    canonical = json.dumps(rows, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def run_all() -> dict[str, Any]:
    records = [run_case(case) for case in CASES]
    return {
        "dataset_version": DATASET_VERSION,
        "dataset_sha256": dataset_digest(),
        "model_called": False,
        "statement": (
            "A fake client played the model. No real model was called. Selected cases hold "
            "locally, which is not a claim of complete protection or of how a real model behaves."
        ),
        "summary": summarize(records),
        "cases": records,
    }


def comparable(data: dict[str, Any]) -> dict[str, Any]:
    """The same run twice must agree, except for how long it took."""
    summary = {k: v for k, v in data["summary"].items() if k != "latency_local_ms"}
    cases = [{k: v for k, v in case.items() if k != "latency_ms"} for case in data["cases"]]
    return {**data, "summary": summary, "cases": cases}


def _print_examples(data: dict[str, Any]) -> None:
    wanted = ("BEN-01", "BEN-04", "BEN-07", "ADV-03", "ADV-08", "ADV-30", "ADV-35", "ADV-41")
    for record in data["cases"]:
        if record["case_id"] in wanted:
            print(f"\n{record['case_id']}  {record['category']}\n  Q: {record['question']}")
            print(f"  outcome: {record['actual']['outcome']}  ({record['actual']['reason_code']})")
            for statement in record["actual"]["statements"]:
                print(f"  > {statement}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--write", type=Path, help="write the per-case evidence to this file")
    parser.add_argument("--show-examples", action="store_true")
    args = parser.parse_args(argv)
    data = run_all()
    for record in data["cases"]:
        mark = "ok  " if record["status"] == "PASS" else "FAIL"
        reason = record["actual"]["reason_code"] or "-"
        print(
            f"{mark} {record['case_id']:<7} {record['actual']['outcome']:<32} {reason:<32} "
            f"{record['category']}"
        )
        for check in record["failed_checks"]:
            print(f"     failed check: {check}")
    summary = data["summary"]
    print(f"\n{summary['passed']} of {summary['cases']} cases as expected ({DATASET_VERSION})")
    print(json.dumps({k: v for k, v in summary.items() if k != "failed"}, indent=2))
    if args.show_examples:
        _print_examples(data)
    if args.write:
        args.write.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    print(f"\nRESULT: {'PASS' if not summary['failed'] else 'FAIL'} - offline. {data['statement']}")
    return 0 if not summary["failed"] else 1


if __name__ == "__main__":
    sys.exit(main())

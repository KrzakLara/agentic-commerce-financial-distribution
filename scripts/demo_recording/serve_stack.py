"""Run the full-trust stack for the recording driver; the driver plays the customer in a browser.

Protocol: one JSON object per line. The helper prints events on stdout and reads commands on
stdin: ``{"do": "agent"}``, ``{"do": "after_delegation"}``,
``{"do": "after_signature", "application_id": ...}`` and ``{"do": "stop"}``. The agent is the
real agent process of ``scripts.demo_trust_journey``. Everything external is synthetic.

    python -m scripts.demo_recording.serve_stack
"""

import contextlib
import json
import sqlite3
import sys
from typing import Any

from scripts.demo_trust_journey import AgentProcess, agent_config, provider_opens, referral_for
from scripts.trust_stack import TrustStack, start_stack


def emit(event: str, **fields: Any) -> None:  # noqa: ANN401
    print(json.dumps({"event": event, **fields}), flush=True)


def audit_trail(stack: TrustStack, application_id: str) -> list[dict[str, str]]:
    """The application's audit events in order, read-only: the bank's trusted operator view."""
    uri = f"file:{stack.db_path.as_posix()}?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=15)) as db:
        rows = db.execute(
            "SELECT event_type, actor_type, outcome FROM audit_events "
            "WHERE application_id = ? ORDER BY occurred_at, rowid",
            (application_id,),
        ).fetchall()
    return [{"event": row[0], "actor": row[1], "outcome": row[2]} for row in rows]


def main() -> int:
    stack = start_stack()
    emit("up", bank=stack.bank_url, users=stack.users, idp=stack.idp_url)
    agent: AgentProcess | None = None
    try:
        for line in sys.stdin:
            command = json.loads(line)
            if command["do"] == "stop":
                break
            if command["do"] == "agent":
                config = agent_config(stack, referral_token=referral_for(stack))
                agent = AgentProcess("journey", config)
                emit("delegation", **agent.read_until("delegation_requested"))
            elif command["do"] == "after_delegation" and agent is not None:
                emit("handoff", **agent.read_until("customer_confirmation_requested"))
            elif command["do"] == "after_signature" and agent is not None:
                application_id = command["application_id"]
                agent.tell_customer_done()
                agent.read_until("submitted")
                code = provider_opens(stack, application_id)
                agent.tell_customer_done()
                final = agent.read_until("final_status")
                emit("final", provider=code, state=final["state"], application_id=application_id)
                emit("audit", events=audit_trail(stack, application_id))
    finally:
        if agent is not None and agent.process.poll() is None:
            agent.process.kill()
        stack.stop()
        stack.remove()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

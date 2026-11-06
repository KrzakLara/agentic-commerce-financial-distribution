"""Trusted operator console: register an external agent's public keys. DEV/TEST only.

There is no HTTP endpoint for this, so an agent cannot register itself.

    python -m scripts.dev_agent_registry --client-id agent-1 --name "My agent" \
        --jwks '{"keys": [...]}' --scopes "create_draft request_handoff read_status"
"""

import argparse
import json
import sys
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.config import Settings
from app.repositories.database import create_db_engine, create_session_factory, init_database
from app.repositories.sqlalchemy_repositories import SqlAlchemyUnitOfWork
from app.repositories.trust_orm import PartnerRelationshipRow
from app.services.delegation_service import register_agent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--jwks", required=True)
    parser.add_argument("--scopes", required=True)
    args = parser.parse_args()
    settings = Settings()
    if not settings.dev_operator_cli_active:
        print("REFUSED: the operator console is off in production.", file=sys.stderr)
        return 2
    engine = create_db_engine(settings.database_url)
    init_database(engine)
    factory = create_session_factory(engine)
    with SqlAlchemyUnitOfWork(factory) as uow:
        register_agent(
            uow,
            client_id=args.client_id,
            display_name=args.name,
            jwks=json.loads(args.jwks),
            allowed_scopes=frozenset(args.scopes.split()),
            now=datetime.now(UTC),
        )
        uow.commit()
        uow.trust.scalars(select(PartnerRelationshipRow)).first()  # the schema is usable
    engine.dispose()
    print(f"registered {args.client_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

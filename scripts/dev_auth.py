"""TRUSTED dev/test bootstrap for agent authorization (Phase 03). Not an API, not for production.

    python -m scripts.dev_auth bootstrap --agent demo-agent-1 --customer demo-customer-1
    python -m scripts.dev_auth app-tokens --application-id app-...
    python -m scripts.dev_auth revoke --jti jti-...

This is the operator-side stand-in for "the bank grants an agent a narrow starting mandate".
It writes directly to the configured database (``TRUSTEQ_DATABASE_URL``) through the normal
repositories; there is deliberately NO HTTP endpoint that issues tokens, so no remote caller
can mint itself an identity or a scope.

* ``bootstrap``  issue a grant for ONE agent acting for ONE customer, scope ``create_draft``,
                 usable for exactly one application, short-lived. Prints the bearer token ONCE.
* ``app-tokens`` re-issue an existing, still active application's onboarding + status tokens
                 (lost token, or an application created before Phase 03). Never widens scopes.
* ``revoke``     revoke one token server-side by its ``jti``.

Refuses to run unless ``Settings.dev_agent_bootstrap_active`` (never true in production).
Controlled development authorization - not production OAuth 2.0 / FAPI 2.0.
"""

import argparse
import sys
from collections.abc import Sequence

from app.core.config import Settings
from app.core.exceptions import AppError
from app.repositories.database import create_db_engine, create_session_factory, init_database
from app.repositories.sqlalchemy_repositories import SqlAlchemyUnitOfWork
from app.services.authorization_service import AuthorizationService, IssuedToken

BANNER = "CONTROLLED DEVELOPMENT AUTHORIZATION - not production OAuth/FAPI. Synthetic data only."


def _print_token(label: str, issued: IssuedToken) -> None:
    print(f"{label}: scope={' '.join(sorted(issued.scopes))}")
    print(f"  jti={issued.jti}  expires={issued.expires_at.isoformat(timespec='seconds')}")
    print(f"  {issued.token}")


def main(argv: Sequence[str] | None = None, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    boot = commands.add_parser("bootstrap", help="issue an agent's first, narrow grant")
    boot.add_argument("--agent", required=True, help="agent actor id")
    boot.add_argument("--customer", required=True, help="customer reference the agent acts for")
    boot.add_argument("--ttl", type=int, default=None, help="lifetime in seconds")
    reissue = commands.add_parser("app-tokens", help="re-issue an application's tokens")
    reissue.add_argument("--application-id", required=True)
    revoke = commands.add_parser("revoke", help="revoke one token by jti")
    revoke.add_argument("--jti", required=True)
    args = parser.parse_args(argv)

    settings = settings or Settings()
    if not settings.dev_agent_bootstrap_active:
        print(
            "refused: the dev agent bootstrap is disabled (or this is production)", file=sys.stderr
        )
        return 2
    engine = create_db_engine(settings.database_url)
    try:
        init_database(engine)  # idempotent: also lets you bootstrap before the first server start
        factory = create_session_factory(engine)
        authorization = AuthorizationService(
            lambda: SqlAlchemyUnitOfWork(factory),
            bootstrap_enabled=True,
            bootstrap_ttl_seconds=settings.agent_bootstrap_ttl_seconds,
            status_ttl_seconds=settings.agent_status_ttl_seconds,
        )
        print(BANNER)
        if args.command == "bootstrap":
            issued = authorization.issue_bootstrap(
                agent_actor_id=args.agent,
                customer_id=args.customer,
                correlation_id="dev-cli",
                ttl_seconds=args.ttl,
            )
            print(f"bootstrap grant for agent {args.agent!r} acting for customer {args.customer!r}")
            print("(one application; the token is shown ONCE - only its SHA-256 hash is stored)")
            _print_token("bootstrap token", issued)
        elif args.command == "app-tokens":
            credentials = authorization.reissue_for_application(
                application_id=args.application_id, correlation_id="dev-cli"
            )
            print(f"re-issued tokens for {args.application_id} (shown ONCE)")
            _print_token("onboarding token", credentials.onboarding)
            _print_token("status token", credentials.status)
        else:
            if not authorization.revoke_token(args.jti, correlation_id="dev-cli"):
                print(f"no live token with jti {args.jti!r}", file=sys.stderr)
                return 1
            print(f"revoked {args.jti}")
    except (AppError, ValueError) as error:  # e.g. an invalid id, or an inactive application
        print(f"refused: {error}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())

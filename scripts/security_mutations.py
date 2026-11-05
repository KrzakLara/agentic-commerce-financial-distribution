"""Targeted mutation testing of the Phase 03 and Phase 04 security controls.

    python -m scripts.security_mutations [--list] [--only M01,M07]

Each mutant deliberately breaks ONE control (e.g. "revoked tokens are accepted", "the CSRF
check is skipped"). For every mutant the harness:

1. copies app/, tests/, scripts/, docs/ and the project config into a fresh temporary folder
   (your working tree is never modified),
2. applies the source change there (each pattern must match exactly once and still compile),
3. runs the test suite on the copy (``pytest -x``, the slow demo subprocess tests excluded),
4. reports KILLED if any test fails - i.e. the tests noticed the weakened control.

Before any mutant it checks that the copy imports ITS OWN ``app`` package (the project is an
editable install) and that the unmodified copy passes, so a result can never come from the
wrong code. Exit code 0 only if every mutant was killed. Equivalent mutants (changes that
cannot alter observable behaviour) are deliberately not listed, e.g. removing the token-format
pre-check (the hash lookup rejects the same tokens) or removing the "bootstrap already used"
pre-check (the atomic compare-and-set binding refuses the same request with the same audited
reason; M10 shows the binding itself is tested).
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COPY_ITEMS = ("app", "tests", "scripts", "docs", "pyproject.toml", ".gitignore", ".env.example")
PYTEST = [
    "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider",
    "--deselect", "tests/test_demo_script.py",
    "--deselect", "tests/test_demo_scripts_phase04.py",
]  # fmt: skip


@dataclass(frozen=True)
class Mutant:
    key: str
    control: str
    path: str
    edits: tuple[tuple[str, str], ...]  # (exact original text, replacement), in one file


AUTHZ = "app/services/authorization_service.py"
APPS = "app/services/application_service.py"
FLOW = "app/services/onboarding_service.py"
CUSTOMER = "app/api/routes/customer.py"
DB = "app/repositories/database.py"
ORM = "app/repositories/orm.py"
REVIEW = "app/services/review_service.py"
CATALOG = "app/integrations/catalog_fixture.py"


def m(key: str, control: str, path: str, *edits: tuple[str, str]) -> Mutant:
    return Mutant(key, control, path, edits)


MUTANTS = [
    # --- token verification -----------------------------------------------------------
    m("M01", "revoked tokens are accepted", AUTHZ,
      ('if token.revoked_at is not None:\n        return "token_revoked"',
       'if False:\n        return "token_revoked"')),
    m("M02", "tokens of a revoked grant are accepted", AUTHZ,
      ('if mandate.revoked_at is not None:\n        return "grant_revoked"',
       'if False:\n        return "grant_revoked"')),
    m("M03", "the issuer is not validated", AUTHZ,
      ("if token.issuer != AUTHORIZATION_ISSUER or mandate.issuer != AUTHORIZATION_ISSUER:",
       "if False:")),
    m("M04", "the audience is not validated", AUTHZ,
      ("if token.audience != audience or mandate.audience != audience:", "if False:")),
    m("M05", "expired tokens are accepted", AUTHZ,
      ('if now >= token.expires_at:\n        return "token_expired"',
       'if False:\n        return "token_expired"')),
    m("M06", "tokens of an expired grant are accepted", AUTHZ,
      ('if now >= mandate.expires_at:\n        return "grant_expired"',
       'if False:\n        return "grant_expired"')),
    m("M07", "a token may carry more scopes than its grant", AUTHZ,
      ("if not token.scopes <= mandate.scopes:", "if False:")),
    m("M08", "the scope check is skipped", AUTHZ,
      ("if scope in principal.scopes:\n            return", "if True:\n            return")),
    # --- grants, ownership, resource binding --------------------------------------------
    m("M09", "single use is not enforced (the bootstrap grant is never bound)", APPS,
      ("if not uow.mandates.bind_bootstrap(", "if False and not uow.mandates.bind_bootstrap(")),
    m("M10", "the atomic binding's result is ignored (only the racy pre-check left)", APPS,
      ("principal.mandate_id, application.application_id\n                    ):",
       "principal.mandate_id, application.application_id\n                    ) and False:")),
    m("M11", "the agent may create for a customer its grant does not name", APPS,
      ("if command.customer_ref != principal.customer_id:", "if False:")),
    m("M12", "a token works on another application of the same agent", APPS,
      ("elif principal.application_id != application_id:", "elif False:")),
    m("M13", "ownership is not checked (only the token binding)", APPS,
      ("if application.agent_actor_id != principal.agent_actor_id:", "if False:")),
    # --- revocation at terminal states ---------------------------------------------------
    m("M14", "terminal states do not revoke the grants", APPS,
      ("if uow.mandates.revoke_for_application(app_id, REVOKED_AT_COMPLETION, now):",
       "if False:")),
    m("M15", "terminal states do not revoke the tokens", APPS,
      ("tokens = uow.agent_tokens.revoke_for_application(app_id, REVOKED_AT_COMPLETION, now)",
       "tokens = 0")),
    m("M16", "terminal states leave customer handoffs usable", APPS,
      ("handoffs = uow.handoffs.revoke_active(app_id, now)", "handoffs = 0")),
    # --- the customer channel --------------------------------------------------------------
    m("M17", "the confirm form's CSRF token is not checked", CUSTOMER,
      ("if not csrf_matches(customer.session_secret, CSRF_CONFIRM, handoff_id, "
       'form.get("csrf_token")):', "if False:")),
    m("M18", "the sign-in form's (login) CSRF token is not checked", CUSTOMER,
      ("    if not csrf_matches(\n        request.cookies.get(LOGIN_COOKIE),",
       "    if False and not csrf_matches(\n        request.cookies.get(LOGIN_COOKIE),")),
    m("M19", "cross-site browser requests are not refused", CUSTOMER,
      ('    fetch_site = request.headers.get("sec-fetch-site")\n',
       '    return False\n    fetch_site = request.headers.get("sec-fetch-site")\n')),
    m("M20", "agent credentials are accepted on the customer JSON ceremony", CUSTOMER,
      ("    if agent_credentials_presented(request):\n"
       "        _refuse_agent_credentials(request, handoff_id, service, authorization, "
       "correlation_id)\n        raise AuthenticationRequiredError()\n", "")),
    m("M21", "customer sessions never expire", "app/domain/customer_session.py",
      ("return self.revoked_at is None and self.created_at <= now < self.expires_at",
       "return self.revoked_at is None")),
    m("M22", "signing in again does not revoke the previous session",
      "app/services/customer_session_service.py",
      ("replaced = uow.customer_sessions.revoke(previous.session_id, now)", "replaced = False")),
    m("M23", "the session cookies are readable by scripts (no HttpOnly)", CUSTOMER,
      ("max_age=settings.customer_session_ttl_seconds,\n            httponly=True,",
       "max_age=settings.customer_session_ttl_seconds,\n            httponly=False,")),
    m("M24", "the session cookie is never Secure, even on HTTPS", "app/api/customer_auth.py",
      ('return request.url.scheme == "https"', "return False")),
    # --- recovery, migration, agent runtime ---------------------------------------------
    m("M25", "the resume re-requests checks that already have evidence", FLOW,
      ("record = uow.checks.get(app_id, check)", "record = None")),
    m("M26", "the v2->v3 upgrade loses the old revocation trigger", DB,
      ('triggers=("trg_mandates_revocation_is_final",),', "triggers=(),")),
    m("M27", "schema upgrades silently drop old rows (grants, check results)", DB,
      ('f"SELECT {step.select} FROM {step.legacy}"',
       'f"SELECT {step.select} FROM {step.legacy} WHERE 0"'),
      ("    if missing:\n", "    if False:\n")),
    m("M28", "the agent runtime leaks its token into tool results",
      "app/integrations/rest_gateway.py",
      ('data={"application_id": view.application_id, "state": view.state.value},\n'
       "        )\n\n    def _request_handoff",
       'data={"application_id": view.application_id, "state": view.state.value,\n'
       '                  "token": self._credentials.bootstrap},\n'
       "        )\n\n    def _request_handoff")),
    # --- Phase 04: human review ------------------------------------------------------------
    m("M29", "an agent token is accepted on request-human", CUSTOMER,
      ("    if agent_credentials_presented(request):\n"
       "        deny(\n            AuditActorType.AGENT,",
       "    if False:\n        deny(\n            AuditActorType.AGENT,")),
    m("M30", "any customer may ask for a person on any application", REVIEW,
      ("if application is None or application.customer_ref != customer_id:",
       "if application is None:")),
    m("M31", "an operator may RESUME past a compliance review (skip the re-run)", REVIEW,
      ("    decisions = {ReviewDecision.REJECT}\n",
       "    decisions = {ReviewDecision.REJECT, ReviewDecision.RESUME}\n")),
    m("M32", "a reviewer may REJECT without a supervisor", REVIEW,
      ("elif decision is ReviewDecision.REJECT and operator.role is not OperatorRole.SUPERVISOR:",
       "elif False:")),
    m("M33", "RE_EVALUATE does not re-run the check (the old result stands)", FLOW,
      ("return tuple(item.category for item in case.evidence)", "return None")),
    m("M34", "a check that does not pass parks without a review case", FLOW,
      ("            self._open_check_review(uow, application, check, record, cid)\n",
       "            pass\n")),
    m("M35", "an application may have two active review cases", ORM,
      ('"uq_review_cases_one_active_per_application",\n            "application_id",\n'
       "            unique=True,",
       '"uq_review_cases_one_active_per_application",\n            "application_id",\n'
       "            unique=False,")),
    m("M36", "RE_EVALUATE is allowed while requested information is outstanding", REVIEW,
      ("if case.check_attempt is not None and case.status is not "
       "ReviewCaseStatus.AWAITING_INFORMATION:",
       "if case.check_attempt is not None:")),
    m("M37", "an operator principal is not checked against the directory (role escalation)",
      REVIEW,
      ("if resolve_dev_operator(operator.operator_id) != operator:",
       "if resolve_dev_operator(operator.operator_id) is None:")),
    m("M38", "the customer's acknowledgement reveals an existing review", REVIEW,
      ("                uow.commit()\n                return HumanRequestAck(application_id)",
       "                uow.commit()\n                return HumanRequestAck(application_id, "
       '"Added to the ongoing review of your application.")')),
    m("M39", "a closed review case can be reopened", ORM,
      ("WHEN OLD.status = 'CLOSED'", "WHEN 0")),
    m("M40", "check attempts can be overwritten", ORM,
      ('    (CheckResultRow.__table__, "check_results"),\n', "")),
    # --- Phase 04: verified provider discovery --------------------------------------------
    m("M41", "the manifest signature is not verified", CATALOG,
      ('if not hmac.compare_digest(str(signature.get("value")), expected):', "if False:")),
    m("M42", "any trusted key may sign for any provider", CATALOG,
      ("if anchor.key_id != approved.trust_anchor or provider_id not in anchor.provider_ids:",
       "if False:")),
    m("M43", "the verified domain is not compared with the register", CATALOG,
      ("if domain != approved.verified_domain:", "if False:")),
    m("M44", "a substituted API endpoint is accepted", CATALOG,
      ("if endpoint not in approved.api_endpoints or not _endpoint_on_domain(endpoint, domain):",
       "if False:")),
    m("M45", "a stale (rolled-back) manifest version is accepted", CATALOG,
      ("if version < approved.minimum_manifest_version:", "if False:")),
    m("M46", "manifest validity is only checked at startup, not at use",
      "app/services/catalog_policy.py",
      ("def provider_is_current(provider: Provider, now: datetime) -> bool:\n",
       "def provider_is_current(provider: Provider, now: datetime) -> bool:\n    return True\n")),
    # --- Phase 04: disclosures, terms and attribution -------------------------------------
    m("M47", "inactive channels (ended partnerships) can be used", "app/domain/catalog.py",
      ("return self.active and (not self.product_ids or product_id in self.product_ids)",
       "return not self.product_ids or product_id in self.product_ids")),
    m("M48", "the approval does not bind the product terms", FLOW,
      ("terms_digest=product.terms_digest,", "terms_digest=None,")),
    m("M49", "the approval does not bind the attributed partner", FLOW,
      ("terms_digest=product.terms_digest,\n            partner_id=offer.channel.partner_id,",
       "terms_digest=product.terms_digest,\n            partner_id=None,")),
    m("M50", "submit does not re-check the approval against the live catalog", FLOW,
      ("handoff is None or not self._still_current(application, handoff)", "False")),
    m("M51", "the disclosure receipt is not bound to terms and action", FLOW,
      ("terms_digest=action.terms_digest,\n"
       "                    action_digest=handoff.action_digest,",
       "terms_digest=None,\n                    action_digest=None,")),
    m("M52", "the channel attribution can be changed after consent", ORM,
      ('    (ChannelAttributionRow.__table__, "channel_attributions"),\n', "")),
    # --- Phase 04 finalization: customer intervention needs the session + CSRF --------------
    m("M53", "request-human trusts the self-asserted customer header instead of the session",
      CUSTOMER,
      ("    customer = session_customer(request, settings, sessions, cookie)\n"
       "    if customer is None or customer.session_secret is None:",
       '    header = request.headers.get("X-Dev-Customer-Id")\n'
       "    customer = CustomerContext(header) if is_valid_customer_id(header) else None\n"
       "    if customer is None:"),
      ("    if not csrf_matches(customer.session_secret, CSRF_HUMAN, application_id, "
       "presented_csrf):",
       "    if False:")),
    m("M54", "the request-human CSRF token is not checked", CUSTOMER,
      ("    if not csrf_matches(customer.session_secret, CSRF_HUMAN, application_id, "
       "presented_csrf):",
       "    if False:")),
    m("M55", "cross-site browser requests may ask for a person", CUSTOMER,
      ('    if _cross_site(request):\n'
       '        deny(AuditActorType.CUSTOMER, None, "cross_site_request")',
       '    if False:\n        deny(AuditActorType.CUSTOMER, None, "cross_site_request")')),
    # --- gap closure, finding F-1: the JSON confirmation needs the session + CSRF -----------
    m("M56", "the JSON confirmation trusts the self-asserted customer header again", CUSTOMER,
      ("    customer = session_customer(request, settings, sessions, API_SESSION_COOKIE)\n"
       "    if customer is None or customer.session_secret is None:",
       '    header = request.headers.get("X-Dev-Customer-Id")\n'
       "    customer = CustomerContext(header) if is_valid_customer_id(header) else None\n"
       "    if customer is None:"),
      ("    if not csrf_matches(customer.session_secret, CSRF_CONFIRM, handoff_id, csrf_header):",
       "    if False:")),
    m("M57", "the JSON confirmation's CSRF token is not checked", CUSTOMER,
      ("    if not csrf_matches(customer.session_secret, CSRF_CONFIRM, handoff_id, csrf_header):",
       "    if False:")),
    m("M58", "the JSON confirmation accepts cross-site browser requests", CUSTOMER,
      ("    if _cross_site(request):\n        raise CrossSiteRequestError()\n"
       "    customer = session_customer(request, settings, sessions, API_SESSION_COOKIE)",
       "    customer = session_customer(request, settings, sessions, API_SESSION_COOKIE)")),
]  # fmt: skip


def make_copy() -> Path:
    workdir = Path(tempfile.mkdtemp(prefix="trusteq_mut_"))
    for item in COPY_ITEMS:
        source = ROOT / item
        if source.is_dir():
            shutil.copytree(source, workdir / item, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(source, workdir / item)
    return workdir


def run(cmd: list[str], cwd: Path, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("TRUSTEQ_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(  # noqa: S603 - fixed argument list, no shell
        [sys.executable, *cmd],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def first_failure(output: str) -> str:
    for kind in ("FAILED", "ERROR"):
        match = re.search(rf"^{kind} (\S+)", output, flags=re.MULTILINE)
        if match:
            return match.group(1)
    return "(non-zero exit)"


def apply(workdir: Path, mutant: Mutant) -> str | None:
    """Mutate the copy. Returns an error text if the mutant cannot be applied cleanly."""
    target = workdir / mutant.path
    source = target.read_text(encoding="utf-8")
    for old, new in mutant.edits:
        if source.count(old) != 1:
            return f"pattern found {source.count(old)}x (expected exactly once)"
        source = source.replace(old, new)
    try:
        compile(source, str(target), "exec")
    except SyntaxError as error:
        return f"mutant does not compile: {error}"
    target.write_text(source, encoding="utf-8")
    return None


def verify_baseline() -> str | None:
    """None if an unmodified copy imports itself and passes; else the reason to abort."""
    copy = make_copy()
    try:
        probe = run(
            ["-c", "import app, pathlib; print(pathlib.Path(app.__file__).resolve())"], copy
        )
        if not probe.stdout.strip().startswith(str(copy.resolve())):
            return f"the copy imports the wrong app package: {probe.stdout.strip()}"
        started = time.perf_counter()
        clean = run(PYTEST, copy)
        if clean.returncode != 0:
            return "the unmodified copy does not pass:\n" + clean.stdout[-2000:]
        passed = re.search(r"(\d+) passed", clean.stdout)
        print(
            f"baseline: {passed.group(1) if passed else '?'} tests pass on the unmodified copy "
            f"({time.perf_counter() - started:.0f}s); the copy imports its own app package\n"
        )
        return None
    finally:
        shutil.rmtree(copy, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--list", action="store_true", help="only list the mutants")
    parser.add_argument("--only", default="", help="comma-separated mutant keys")
    args = parser.parse_args()
    selected = [mu for mu in MUTANTS if not args.only or mu.key in args.only.split(",")]
    if args.list:
        for mu in selected:
            print(f"{mu.key}  {mu.control}  ({mu.path})")
        return 0
    problem = verify_baseline()
    if problem:
        print(f"ABORT: {problem}")
        return 2

    killed = 0
    for mutant in selected:
        workdir = make_copy()
        try:
            problem = apply(workdir, mutant)
            if problem:
                print(f"{mutant.key}  NOT APPLIED  {mutant.control}: {problem}")
                continue
            started = time.perf_counter()
            result = run(PYTEST, workdir)
            seconds = time.perf_counter() - started
            if result.returncode != 0:
                killed += 1
                where = first_failure(result.stdout)
                print(f"{mutant.key}  KILLED    {mutant.control}  <- {where} ({seconds:.0f}s)")
            else:
                print(f"{mutant.key}  SURVIVED  {mutant.control}  ({seconds:.0f}s)")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
    print(f"\n{killed}/{len(selected)} security mutants detected by the test suite")
    return 0 if killed == len(selected) else 1


if __name__ == "__main__":
    sys.exit(main())

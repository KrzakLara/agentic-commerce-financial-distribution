"""What a browser does on the bank's customer pages, for the demo, the matrix and the tests.

It reads the forms from the page's real HTML and posts exactly what they contain, so it can only do
what the page offers. Synthetic data only. Nothing here prints a value.
"""

from collections.abc import Mapping
from html.parser import HTMLParser

import httpx

SYNTHETIC_TAX_ID = "SYNTH-TAX-700421"  # not the page's placeholder: a leak would show
REAL_LOOKING_TAX_ID = "12345678901"  # 11 digits, invented: the shape of a real ID, always refused
TAX_COMPLETE = {
    "tax_residency_country": "DE",
    "synthetic_tax_id": SYNTHETIC_TAX_ID,
    "us_person_declared": "no",
}
# The form values of the three synthetic profiles of `app.services.appropriateness`.
ASSESSMENT_PROFILES: dict[str, dict[str, str]] = {
    "experienced": {
        "knows_capital_loss": "true",
        "knows_no_guarantee": "false",
        "experience": "MORE_THAN_FIVE",
    },
    "novice": {
        "knows_capital_loss": "true",
        "knows_no_guarantee": "true",
        "experience": "NONE",
    },
    "incomplete": {"knows_capital_loss": "true"},
}


class _Forms(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        if tag == "form":
            self.forms.append({"_action": values.get("action", "")})
        elif tag == "input" and values.get("type") == "hidden" and self.forms:
            self.forms[-1][values.get("name", "")] = values.get("value", "")


def page_form(page: str, action_end: str, intent: str | None = None) -> dict[str, str] | None:
    """The form on `page` whose action ends with `action_end`: its action and hidden fields."""
    parser = _Forms()
    parser.feed(page)
    for form in parser.forms:
        if form["_action"].endswith(action_end) and (
            intent is None or form.get("intent") == intent
        ):
            return form
    return None


def _post(
    browser: httpx.Client, form: Mapping[str, str], values: Mapping[str, str]
) -> httpx.Response:
    fields = {key: value for key, value in form.items() if key != "_action"}
    return browser.post(form["_action"], data={**fields, **values}, follow_redirects=False)


def link_of(handoff_id: str, nonce: str) -> str:
    return f"/customer/handoffs/{handoff_id}?token={nonce}"


def sign_in(browser: httpx.Client, link: str, customer_id: str) -> httpx.Response:
    """Open the link, submit the DEV sign-in form it shows, and return the page behind it."""
    form = page_form(browser.get(link).text, "/customer/dev-login")
    if form is None:
        raise ValueError("the page offers no sign-in form")
    response = _post(browser, form, {"customer_id": customer_id})
    if response.status_code != 303:
        raise ValueError(f"sign-in failed: {response.status_code}")
    return browser.get(link)


def post_tax(browser: httpx.Client, link: str, values: Mapping[str, str]) -> httpx.Response:
    form = page_form(browser.get(link).text, "/tax")
    if form is None:
        raise ValueError("the page offers no tax form")
    return _post(browser, form, values)


def post_answers(browser: httpx.Client, link: str, values: Mapping[str, str]) -> httpx.Response:
    form = page_form(browser.get(link).text, "/assessment", "answer")
    if form is None:
        raise ValueError("the page offers no questionnaire")
    return _post(browser, form, values)


def post_acknowledgement(browser: httpx.Client, link: str) -> httpx.Response:
    form = page_form(browser.get(link).text, "/assessment", "acknowledge")
    if form is None:
        raise ValueError("the page offers no acknowledgement")
    return _post(browser, form, {})


def post_confirmation(browser: httpx.Client, link: str) -> httpx.Response:
    """Press Confirm, if the page offers it. It offers it only when the bank would accept it."""
    form = page_form(browser.get(link).text, "/confirm")
    if form is None:
        raise ValueError("the page offers no confirmation")
    return _post(browser, form, {})


def complete_customer_information(
    browser: httpx.Client, link: str, profile: str = "experienced"
) -> None:
    """The tax form and the questionnaire, with the acknowledgement a notice needs."""
    if post_tax(browser, link, TAX_COMPLETE).status_code != 303:
        raise ValueError("the tax information was refused")
    if post_answers(browser, link, ASSESSMENT_PROFILES[profile]).status_code != 303:
        raise ValueError("the answers were refused")
    if page_form(browser.get(link).text, "/assessment", "acknowledge") is not None:
        post_acknowledgement(browser, link)

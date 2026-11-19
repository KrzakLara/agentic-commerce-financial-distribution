# Diagrams

Editable Mermaid sources for the architecture and process diagrams. The `.mmd` files are the sources of truth, and the
documents embed exact copies.

| # | Diagram | Shows | Embedded in |
|---|---|---|---|
| 02 | [2030 system context](02-system-context-2030.mmd) | Customer and agent outside the provider's trust boundary, layers A to G inside, separate regulated parties outside, prototype status per element | [../technical-concept-2030.md](../technical-concept-2030.md) |
| 03 | [Full-trust E2E sequence](03-full-trust-e2e-sequence.mmd) | The prototyped scenario from discovery to `OPENED`, every synthetic party labelled | [../technical-concept-2030.md](../technical-concept-2030.md) |
| 04 | [Decision and escalation boundaries](04-decision-escalation-boundaries.mmd) | Who decides at each critical point, and every path to a person | [../technical-concept-2030.md](../technical-concept-2030.md) |
| 05 | [AS-IS to TO-BE](05-as-is-to-be-comparison.mmd) | What changes by 2030, and what deliberately stays with the customer and the bank | [../technical-concept-2030.md](../technical-concept-2030.md) |
| 07 | [Trust boundaries](07-trust-boundaries.mmd) | Trust zones T0 to T3 and the controls at each crossing | Standalone; see [../trust-boundaries.md](../trust-boundaries.md) for the zones in text |

**Colours** in the architecture diagrams:
- **Architecture diagrams (02, 07):**
  - grey box with a blue border: implemented and tested locally;
  - white box with a dashed gold border: a labelled synthetic or mock stand-in;
  - dark blue: the customer and the agent.
- **Diagram 04** colours each step by who decides, with its own key.
- **Diagram 05** uses dark blue for what stays with the customer and the bank.

**Rendering.** GitHub and most Markdown viewers render Mermaid directly. All diagrams were parsed and rendered without
errors with Mermaid 11.4.1 in headless Chrome on 2026-11-07. The check also confirms that every embedded copy matches
its source file.

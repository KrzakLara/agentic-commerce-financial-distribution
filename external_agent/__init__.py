"""An independent personal-agent client. It never imports the bank's `app` package.

It reaches the bank only over HTTP, starting from a known origin or a federation list, and it trusts
only the anchors it was configured with. It is deterministic and calls no model: it demonstrates
interoperability with the published interface, not an AI product.
"""

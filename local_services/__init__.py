"""Separate local services for the integration environment. SYNTHETIC, not real providers.

They run as their own processes and never import the bank's `app` package, so the integration
crosses
a real boundary. None of them is a regulated service: the trust authority verifies nobody, the
identity provider proves no identity, and the test signer creates no electronic signature with legal
effect.
"""

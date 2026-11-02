"""Ports (interfaces) to external and regulated systems (ports-and-adapters, ADR ADR-02).

Each module defines a small `typing.Protocol`. The banking core depends only on these
interfaces, so adapters (mock now, real or MCP/A2A later) can change without touching
the approved business core. Nothing here performs a real financial operation.
"""

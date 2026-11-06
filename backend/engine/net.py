"""
Network/TLS setup for corporate environments.

Many corporate networks (e.g. ContiTech's) run TLS-inspecting proxies that re-sign HTTPS traffic with a
private root CA. Browsers trust it because IT installed that root in the OS certificate store — but
Python's HTTP stack verifies against the bundled `certifi` CA list, which does NOT contain the corporate
root, so every outbound API call fails with `APIConnectionError` / `CERTIFICATE_VERIFY_FAILED`.

`truststore` fixes this the right way: it makes Python's `ssl` verify against the **operating system's**
trust store (which already has the corporate root), so verification still happens — we are not disabling
TLS — it just trusts the same CAs the browser does. We inject it once, lazily, best-effort: if truststore
isn't installed or injection fails, we leave the default certifi behavior untouched rather than crash.
"""

from __future__ import annotations

_injected = False


def enable_os_trust_store() -> bool:
    """Route Python's TLS verification through the OS trust store. Idempotent and best-effort.

    Returns True if the OS trust store is active (now or already), False if truststore is unavailable.
    """
    global _injected
    if _injected:
        return True
    try:
        import truststore  # corporate-CA aware; verifies against the OS store

        truststore.inject_into_ssl()
        _injected = True
        return True
    except Exception:  # noqa: BLE001 — truststore missing or injection failed → keep certifi default
        return False

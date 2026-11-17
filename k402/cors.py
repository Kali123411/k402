# Browser agents: a page (the Kaspa Lab market page, or any web agent) talking to a k402 registry or provider
# needs CORS, and the x402 headers must be allowed in (PAYMENT-SIGNATURE, X-K402-Payment) and exposed out
# (PAYMENT-REQUIRED, PAYMENT-RESPONSE) or the browser cannot even read the quote. Operators name the origins.
from __future__ import annotations

from typing import Iterable

ALLOW_HEADERS = ["Content-Type", "PAYMENT-SIGNATURE", "X-K402-Payment"]
EXPOSE_HEADERS = ["PAYMENT-REQUIRED", "PAYMENT-RESPONSE"]


def allow_cors(app, origins: Iterable[str], *, max_age: int = 600) -> None:
    """Add FastAPI's CORSMiddleware to `app` for exactly `origins` (no wildcard), with the x402 headers allowed
    and exposed. Call it on a registry app (`create_registry_app(cors_origins=…)` does) and on a provider app."""
    from fastapi.middleware.cors import CORSMiddleware
    origins = [o.strip().rstrip("/") for o in origins if o and o.strip()]
    if not origins:
        return
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST", "OPTIONS"],
                       allow_headers=ALLOW_HEADERS, expose_headers=EXPOSE_HEADERS, max_age=max_age)


def origins_from_env(value: str | None) -> list:
    """`CORS_ORIGINS="https://a.example, http://localhost:3000"` → a list; empty/None → []."""
    return [o.strip() for o in (value or "").split(",") if o.strip()]


__all__ = ["allow_cors", "origins_from_env", "ALLOW_HEADERS", "EXPOSE_HEADERS"]

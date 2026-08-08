"""Same-origin hosting for the protocol-decoupled Web C build."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response

_RESERVED_ROOTS = {"api", "docs", "healthz", "openapi.json", "redoc"}
_CSP = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self' data:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)


def _security_headers(response: Response, *, strict_transport: bool) -> None:
    response.headers["Content-Security-Policy"] = _CSP
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    if strict_transport:
        response.headers["Strict-Transport-Security"] = (
            "max-age=63072000; includeSubDomains"
        )


def _asset(root: Path, requested_path: str) -> Path | None:
    candidate = (root / requested_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def install_web_hosting(app: FastAPI, static_dir: Path) -> None:
    """Install security headers and a final SPA fallback after all API routers."""

    root = static_dir.expanduser().resolve()
    strict_transport = str(app.state.settings.public_base_url).startswith("https")

    @app.middleware("http")
    async def web_security_headers(request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        _security_headers(response, strict_transport=strict_transport)
        return response

    @app.get("/", include_in_schema=False)
    @app.get("/{full_path:path}", include_in_schema=False)
    async def web_client(full_path: str = "") -> Response:
        first_segment = full_path.partition("/")[0]
        if first_segment in _RESERVED_ROOTS:
            return JSONResponse(status_code=404, content={"detail": "Not Found"})

        if full_path:
            asset = _asset(root, full_path)
            if asset is not None:
                response = FileResponse(asset)
                if first_segment == "assets":
                    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
                else:
                    response.headers["Cache-Control"] = "no-cache"
                return response
            if first_segment == "assets" or "." in Path(full_path).name:
                return JSONResponse(status_code=404, content={"detail": "Not Found"})

        index = _asset(root, "index.html")
        if index is None:
            return PlainTextResponse(
                "Web C assets are unavailable; build apps/clients/web first.",
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

"""avalpha web console — a members-only ops UI over the portfolio DB.

Served by uvicorn behind a Cloudflare Tunnel + Access. See app.create_app.
"""

from avalpha.web.app import create_app

__all__ = ["create_app"]

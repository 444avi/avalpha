"""Cloudflare Access authentication.

Cloudflare Access sits in front of the tunnel and blocks any request from a
non-member at the edge — by the time a request reaches this app it has already
been authenticated. We still verify the signed Access JWT here as
defence-in-depth (so the app is safe even if it is ever reached without going
through Access) and to learn *which* member is acting, for attribution.

Access forwards the JWT in the ``Cf-Access-Jwt-Assertion`` header (and a
``CF_Authorization`` cookie). We validate its signature against the team's
public keys, check the audience tag of our Access application, and read the
member's email from the ``email`` claim.

Configuration comes from the environment (set by systemd on the instance):

    CF_ACCESS_TEAM_DOMAIN   e.g. yourteam.cloudflareaccess.com
    CF_ACCESS_AUD           the Access application's Application Audience tag

For local development, set ``AVALPHA_WEB_DEV_USER=you@example.com`` and leave
the CF_* variables unset — every request is then treated as that member. This
bypass is refused whenever CF_ACCESS_* is configured, so it can never weaken a
real deployment.
"""

import os
from dataclasses import dataclass

import jwt


class AccessDenied(Exception):
    """Raised when a request carries no valid Cloudflare Access identity."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass
class AccessConfig:
    team_domain: str | None
    aud: str | None
    dev_user: str | None

    @property
    def enabled(self) -> bool:
        return bool(self.team_domain and self.aud)

    @property
    def issuer(self) -> str:
        return f"https://{self.team_domain}"

    @property
    def certs_url(self) -> str:
        return f"https://{self.team_domain}/cdn-cgi/access/certs"


def load_access_config() -> AccessConfig:
    team = os.environ.get("CF_ACCESS_TEAM_DOMAIN", "").strip() or None
    aud = os.environ.get("CF_ACCESS_AUD", "").strip() or None
    dev_user = os.environ.get("AVALPHA_WEB_DEV_USER", "").strip() or None
    return AccessConfig(team_domain=team, aud=aud, dev_user=dev_user)


class Authenticator:
    """Validates Cloudflare Access JWTs, caching the signing keys."""

    def __init__(self, config: AccessConfig):
        self.config = config
        self._jwks: jwt.PyJWKClient | None = None
        if config.enabled:
            # PyJWKClient caches fetched keys and refreshes on unknown kid.
            self._jwks = jwt.PyJWKClient(config.certs_url)

    def member_email(self, token: str | None) -> str:
        """Return the verified member email, or raise AccessDenied."""
        cfg = self.config

        if not cfg.enabled:
            # Development mode: no Access in front, trust the configured user.
            if cfg.dev_user:
                return cfg.dev_user
            raise AccessDenied(
                "Cloudflare Access is not configured (set CF_ACCESS_TEAM_DOMAIN "
                "and CF_ACCESS_AUD), and no AVALPHA_WEB_DEV_USER dev bypass is set."
            )

        if not token:
            raise AccessDenied("no Cloudflare Access token on the request")

        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=cfg.aud,
                issuer=cfg.issuer,
            )
        except jwt.PyJWTError as e:
            raise AccessDenied(f"invalid Access token: {e}") from e

        email = claims.get("email")
        if not email:
            raise AccessDenied("Access token carries no email claim")
        return email


def extract_token(headers, cookies) -> str | None:
    """Pull the Access JWT from the header, falling back to the cookie."""
    token = headers.get("cf-access-jwt-assertion")
    if token:
        return token
    return cookies.get("CF_Authorization")

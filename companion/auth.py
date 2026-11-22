"""OAuth resource-server verification. No learner identity comes from tool arguments."""

import asyncio
import hmac
from dataclasses import dataclass
from urllib.parse import urlsplit

import jwt
from mcp.server.auth.provider import AccessToken, TokenVerifier


@dataclass(frozen=True)
class OAuthConfig:
    issuer: str
    audience: str
    jwks_url: str
    subject: str

    def __post_init__(self):
        for value in (self.issuer, self.audience, self.jwks_url):
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError("OAuth issuer, audience and JWKS URLs must use HTTPS")
        if not self.subject:
            raise ValueError("Configure the single learner's OAuth subject")


class Verifier(TokenVerifier):
    def __init__(self, config, jwks=None):
        self.config = config
        self.jwks = jwks or jwt.PyJWKClient(
            config.jwks_url, cache_keys=True, timeout=10
        )

    async def verify_token(self, token):
        try:
            key = await asyncio.to_thread(self.jwks.get_signing_key_from_jwt, token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256", "ES256"],
                audience=self.config.audience,
                issuer=self.config.issuer,
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
            if claims["sub"] != self.config.subject:
                return None
            scope = claims.get("scope", "")
            if not isinstance(scope, str):
                return None
            return AccessToken(
                token=token,
                client_id=str(
                    claims.get("client_id") or claims.get("azp") or "oauth-client"
                ),
                subject=claims["sub"],
                scopes=scope.split(),
                expires_at=claims["exp"],
                resource=self.config.audience,
            )
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            return None


def collector_authorized(header, token):
    return bool(
        token
        and len(token) >= 32
        and header
        and hmac.compare_digest(header, "Bearer " + token)
    )

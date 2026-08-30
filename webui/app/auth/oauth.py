"""OAuth 2.0 and OpenID Connect sign-in, built on Authlib.

Two provider shapes are supported:

* **OpenID Connect** -- the provider publishes a discovery document, Authlib
  reads the endpoints and JWKS from it, and the ID token carries the user's
  claims. This covers Keycloak, Authentik, Entra ID, Google, Okta and friends.
* **Plain OAuth 2.0** -- no discovery, no ID token. The endpoints are given
  explicitly and the user's details come from a userinfo request. GitHub is
  the common case.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from authlib.integrations.flask_client import OAuth
from flask import Flask, current_app, session, url_for

from ..config import AUTH_OAUTH, OAuthProvider
from .provisioning import IdentityClaim

log = logging.getLogger(__name__)

_EXTENSION_KEY = "pdnsadmin.oauth"
_NONCE_SESSION_KEY = "oauth_nonce"


class OAuthError(Exception):
    """The provider refused, or answered with something unusable."""


def init_app(app: Flask) -> None:
    """Register every configured provider with Authlib."""
    providers = app.config["AUTH"].oauth_providers
    if not providers:
        return

    oauth = OAuth(app)
    for provider in providers:
        kwargs: dict[str, Any] = {
            "name": provider.name,
            "client_id": provider.client_id,
            "client_secret": provider.client_secret,
            "client_kwargs": {"scope": provider.scopes},
        }
        if provider.is_oidc:
            kwargs["server_metadata_url"] = provider.discovery_url
        else:
            kwargs["authorize_url"] = provider.authorize_url
            kwargs["access_token_url"] = provider.token_url
            kwargs["api_base_url"] = provider.userinfo_url
            if provider.jwks_url:
                kwargs["jwks_uri"] = provider.jwks_url
        oauth.register(**kwargs)
        log.info(
            "registered OAuth provider %s (%s)",
            provider.name,
            "OpenID Connect" if provider.is_oidc else "OAuth 2.0",
        )

    app.extensions[_EXTENSION_KEY] = oauth


def _client(provider: OAuthProvider):
    oauth: OAuth | None = current_app.extensions.get(_EXTENSION_KEY)
    if oauth is None:
        raise OAuthError("No OAuth providers are configured.")
    client = oauth.create_client(provider.name)
    if client is None:
        raise OAuthError(f"OAuth provider {provider.name!r} is not registered.")
    return client


def redirect_uri(provider: OAuthProvider) -> str:
    """Absolute callback URL, which must match what the provider has registered.

    BASE_URL wins when set: behind a reverse proxy Flask often sees http and an
    internal hostname, and handing the provider that URL fails the exact-match
    check every OAuth implementation performs.
    """
    base_url = current_app.config.get("BASE_URL")
    path = url_for("auth.oauth_callback", provider=provider.name)
    if base_url:
        return f"{base_url}{path}"
    return url_for("auth.oauth_callback", provider=provider.name, _external=True)


def start_login(provider: OAuthProvider):
    """Redirect the browser to the provider's authorisation endpoint."""
    client = _client(provider)
    kwargs: dict[str, Any] = {}
    if provider.is_oidc:
        # Binds the ID token to this browser session, so a token obtained
        # elsewhere cannot be replayed here.
        nonce = secrets.token_urlsafe(24)
        session[_NONCE_SESSION_KEY] = nonce
        kwargs["nonce"] = nonce
    return client.authorize_redirect(redirect_uri(provider), **kwargs)


def _claim(source: dict[str, Any], key: str, *fallbacks: str) -> str:
    for candidate in (key, *fallbacks):
        if not candidate:
            continue
        value = source.get(candidate)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return ""


def _groups(source: dict[str, Any], key: str) -> list[str]:
    value = source.get(key)
    if isinstance(value, str):
        # Some providers hand back a single space- or comma-separated string.
        return [item for item in value.replace(",", " ").split() if item]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    return []


def complete_login(provider: OAuthProvider) -> IdentityClaim:
    """Exchange the authorisation code and build the identity claim."""
    client = _client(provider)
    nonce = session.pop(_NONCE_SESSION_KEY, None)

    try:
        token = client.authorize_access_token()
    except Exception as exc:  # Authlib raises a family of errors here
        log.warning("OAuth token exchange failed for %s: %s", provider.name, exc)
        raise OAuthError(f"{provider.display_name} did not complete the sign-in: {exc}") from exc

    userinfo: dict[str, Any] = {}
    if provider.is_oidc:
        # Authlib decodes and validates the ID token when the openid scope is
        # requested, including the nonce we planted above.
        userinfo = dict(token.get("userinfo") or {})
        if not userinfo:
            try:
                userinfo = dict(client.parse_id_token(token, nonce=nonce) or {})
            except Exception as exc:  # pragma: no cover - provider dependent
                log.debug("could not parse id_token for %s: %s", provider.name, exc)

    if not userinfo:
        url = provider.userinfo_url or "userinfo"
        try:
            response = client.get(url, token=token)
            response.raise_for_status()
            userinfo = dict(response.json())
        except Exception as exc:
            log.warning("OAuth userinfo request failed for %s: %s", provider.name, exc)
            raise OAuthError(f"{provider.display_name} did not return the user's details.") from exc

    username = _claim(
        userinfo,
        provider.username_claim,
        "preferred_username",
        "login",
        "email",
        "sub",
    )
    if not username:
        raise OAuthError(
            f"{provider.display_name} returned no usable username. Adjust "
            f"OAUTH_{provider.name.upper()}_USERNAME_CLAIM to match a claim it does send."
        )

    return IdentityClaim(
        username=username,
        auth_source=AUTH_OAUTH,
        provider=provider.name,
        external_id=_claim(userinfo, "sub", "id", "user_id") or None,
        email=_claim(userinfo, provider.email_claim, "email"),
        display_name=_claim(userinfo, provider.name_claim, "name", "display_name"),
        groups=_groups(userinfo, provider.groups_claim) or _groups(userinfo, "roles"),
    )

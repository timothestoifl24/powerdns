"""SAML 2.0 service provider, built on python3-saml (OneLogin).

The panel acts as the SP. Two ways to describe the IdP:

* ``SAML_IDP_METADATA_URL`` -- fetched and parsed at first use, then cached for
  the life of the process.
* the explicit ``SAML_IDP_ENTITY_ID`` / ``_SSO_URL`` / ``_X509_CERT`` trio.

Signature validation is on by default (``SAML_STRICT``). Turning it off makes
assertions forgeable, so it exists only for diagnosing a broken integration.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from flask import Request, current_app, url_for
from onelogin.saml2.auth import OneLogin_Saml2_Auth
from onelogin.saml2.idp_metadata_parser import OneLogin_Saml2_IdPMetadataParser
from onelogin.saml2.settings import OneLogin_Saml2_Settings

from ..config import AUTH_SAML, SamlConfig
from .provisioning import IdentityClaim

log = logging.getLogger(__name__)

_METADATA_CACHE: dict[str, dict[str, Any]] = {}


class SamlError(Exception):
    """The assertion was rejected, or the SP is misconfigured."""


def _base_url() -> str:
    configured = current_app.config.get("BASE_URL")
    if configured:
        return configured
    # Falls back to what the request says; correct only when no proxy rewrites
    # the host, which is why BASE_URL is documented as required for SAML.
    return url_for("dashboard.index", _external=True).rstrip("/")


def sp_urls() -> dict[str, str]:
    base = _base_url()
    return {
        "entity_id": f"{base}{url_for('auth.saml_metadata')}",
        "acs": f"{base}{url_for('auth.saml_acs')}",
        "sls": f"{base}{url_for('auth.saml_sls')}",
    }


def _idp_settings(config: SamlConfig) -> dict[str, Any]:
    """IdP half of the settings, from metadata or from explicit configuration."""
    if config.idp_metadata_url:
        cached = _METADATA_CACHE.get(config.idp_metadata_url)
        if cached is None:
            try:
                parsed = OneLogin_Saml2_IdPMetadataParser.parse_remote(
                    config.idp_metadata_url, validate_cert=True
                )
            except Exception as exc:
                raise SamlError(
                    f"Could not read the IdP metadata at {config.idp_metadata_url}: {exc}"
                ) from exc
            cached = parsed.get("idp") or {}
            if not cached:
                raise SamlError(
                    f"The document at {config.idp_metadata_url} contains no IdP descriptor."
                )
            _METADATA_CACHE[config.idp_metadata_url] = cached
            log.info("loaded SAML IdP metadata from %s", config.idp_metadata_url)
        return dict(cached)

    idp: dict[str, Any] = {
        "entityId": config.idp_entity_id,
        "singleSignOnService": {
            "url": config.idp_sso_url,
            "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
        },
        "x509cert": config.idp_x509_cert,
    }
    if config.idp_slo_url:
        idp["singleLogoutService"] = {
            "url": config.idp_slo_url,
            "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
        }
    return idp


def build_settings(config: SamlConfig) -> dict[str, Any]:
    urls = sp_urls()
    return {
        "strict": config.strict,
        "debug": False,
        "sp": {
            "entityId": config.sp_entity_id or urls["entity_id"],
            "assertionConsumerService": {
                "url": urls["acs"],
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
            },
            "singleLogoutService": {
                "url": urls["sls"],
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
            },
            "NameIDFormat": config.name_id_format,
            "x509cert": config.sp_x509_cert,
            "privateKey": config.sp_private_key,
        },
        "idp": _idp_settings(config),
        "security": {
            "nameIdEncrypted": False,
            "authnRequestsSigned": bool(config.sp_private_key),
            "logoutRequestSigned": bool(config.sp_private_key),
            "logoutResponseSigned": bool(config.sp_private_key),
            "wantMessagesSigned": config.want_messages_signed,
            "wantAssertionsSigned": config.want_assertions_signed,
            "wantNameIdEncrypted": config.want_name_id_encrypted,
            "wantAssertionsEncrypted": False,
            "requestedAuthnContext": False,
            "signatureAlgorithm": "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256",
            "digestAlgorithm": "http://www.w3.org/2001/04/xmlenc#sha256",
        },
    }


def _request_data(request: Request) -> dict[str, Any]:
    """Translate a Flask request into what python3-saml expects."""
    url = urlparse(_base_url())
    https = "on" if url.scheme == "https" else "off"
    return {
        "https": https,
        "http_host": url.netloc or request.host,
        "server_port": str(url.port or (443 if https == "on" else 80)),
        "script_name": request.path,
        "get_data": request.args.copy(),
        # python3-saml reads SAMLResponse/RelayState from here.
        "post_data": request.form.copy(),
    }


def build_auth(config: SamlConfig, request: Request) -> OneLogin_Saml2_Auth:
    try:
        return OneLogin_Saml2_Auth(_request_data(request), build_settings(config))
    except SamlError:
        raise
    except Exception as exc:
        raise SamlError(f"The SAML configuration is not usable: {exc}") from exc


def metadata_xml(config: SamlConfig) -> str:
    """SP metadata to hand to the identity provider."""
    settings = OneLogin_Saml2_Settings(build_settings(config), sp_validation_only=True)
    xml = settings.get_sp_metadata()
    errors = settings.validate_metadata(xml)
    if errors:
        raise SamlError("Generated SP metadata is invalid: " + ", ".join(errors))
    return xml.decode("utf-8") if isinstance(xml, bytes) else xml


def start_login(config: SamlConfig, request: Request, relay_state: str | None = None) -> str:
    auth = build_auth(config, request)
    return auth.login(return_to=relay_state)


def complete_login(config: SamlConfig, request: Request) -> IdentityClaim:
    """Validate the assertion and build the identity claim."""
    auth = build_auth(config, request)
    auth.process_response()

    errors = auth.get_errors()
    if errors:
        # get_last_error_reason carries the signature/condition detail.
        reason = auth.get_last_error_reason() or ""
        log.warning("SAML response rejected: %s (%s)", errors, reason)
        raise SamlError(f"The SAML response was rejected: {', '.join(errors)}. {reason}".strip())
    if not auth.is_authenticated():
        raise SamlError("The identity provider did not authenticate this user.")

    attributes = auth.get_attributes() or {}
    name_id = auth.get_nameid() or ""

    def first(attribute_name: str, default: str = "") -> str:
        values = attributes.get(attribute_name) or []
        if isinstance(values, (list, tuple)):
            return str(values[0]) if values else default
        return str(values) if values else default

    def many(attribute_name: str) -> list[str]:
        values = attributes.get(attribute_name) or []
        if isinstance(values, (list, tuple)):
            return [str(item) for item in values if str(item).strip()]
        return [str(values)] if str(values).strip() else []

    # NameID is the fallback username: some IdPs send no attributes at all.
    username = first(config.attr_username) or name_id
    if not username:
        raise SamlError(
            "The assertion contained neither a NameID nor the attribute named by "
            f"SAML_ATTR_USERNAME ({config.attr_username!r})."
        )

    return IdentityClaim(
        username=username,
        auth_source=AUTH_SAML,
        provider="saml",
        external_id=name_id or username,
        email=first(config.attr_email),
        display_name=first(config.attr_name),
        groups=many(config.attr_groups),
    )

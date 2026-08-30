"""Authentication backends: local, LDAP, OAuth 2.0 / OIDC and SAML 2.0.

Each backend's job is to turn a credential into an :class:`IdentityClaim`.
Everything after that -- creating or updating the account, applying the
group-to-role mapping, refusing users with no entitlement -- is shared, and
lives in :mod:`app.auth.provisioning`.
"""

from .provisioning import IdentityClaim, ProvisioningError, resolve_identity

__all__ = ["IdentityClaim", "ProvisioningError", "resolve_identity"]

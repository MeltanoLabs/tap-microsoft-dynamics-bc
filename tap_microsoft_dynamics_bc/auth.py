"""Microsoft Entra ID client-credentials authentication for Business Central."""

from __future__ import annotations

import sys

from singer_sdk.authenticators import OAuthAuthenticator

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

DEFAULT_AUTH_URL = "https://login.microsoftonline.com"
DEFAULT_API_URL = "https://api.businesscentral.dynamics.com"


class BusinessCentralAuthenticator(OAuthAuthenticator):
    """Service-to-service (S2S) authenticator.

    Uses the OAuth 2.0 client credentials flow against the customer's Microsoft
    Entra tenant, as described in "Using Service-to-Service Authentication" in
    the Business Central documentation. The registered application needs the
    ``Dynamics 365 Business Central / API.ReadWrite.All`` application
    permission and a Business Central permission set; read-only access is
    enforced by that permission set.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        auth_url: str = DEFAULT_AUTH_URL,
        api_url: str = DEFAULT_API_URL,
    ) -> None:
        """Create the authenticator.

        Args:
            tenant_id: Microsoft Entra tenant ID (GUID) or primary domain.
            client_id: Application (client) ID of the Entra app registration.
            client_secret: Client secret value of the Entra app registration.
            auth_url: Entra authority host.
            api_url: Business Central API host, used to derive the token scope.
        """
        super().__init__(
            client_id=client_id,
            client_secret=client_secret,
            auth_endpoint=f"{auth_url.rstrip('/')}/{tenant_id}/oauth2/v2.0/token",
            oauth_scopes=f"{api_url.rstrip('/')}/.default",
        )

    @override
    @property
    def oauth_request_body(self) -> dict:
        """Return the client-credentials token request body."""
        return {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": self.oauth_scopes,
        }

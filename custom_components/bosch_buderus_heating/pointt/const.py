"""Protocol constants for the observed PointT cloud API."""

from typing import Final

POINTT_BASE_URL: Final = (
    "https://pointt-api.bosch-thermotechnology.com/pointt-api/api/v1"
)
OAUTH_AUTHORIZE_URL: Final = "https://singlekey-id.com/auth/connect/authorize"
OAUTH_TOKEN_URL: Final = "https://singlekey-id.com/auth/connect/token"
OAUTH_CLIENT_ID: Final = "762162C0-FA2D-4540-AE66-6489F189FADC"
OAUTH_SCOPES: Final = (
    "openid",
    "email",
    "profile",
    "offline_access",
    "pointt.gateway.claiming",
    "pointt.gateway.removal",
    "pointt.gateway.list",
    "pointt.gateway.users",
    "pointt.gateway.resource.dashapp",
    "pointt.castt.flow.token-exchange",
    "bacon",
    "hcc.tariff.read",
)

MAX_BULK_PATHS: Final = 30
DEFAULT_CONCURRENCY: Final = 3
DEFAULT_CONNECT_TIMEOUT: Final = 10.0
DEFAULT_TOTAL_TIMEOUT: Final = 30.0
# PointT expects requests to look like the official DashApp client. A generic
# integration user agent is rejected by some API paths even after a successful
# SingleKey ID token exchange.
DEFAULT_USER_AGENT: Final = "DashApp/3.7.0 (iOS-Release)"
MAX_RESPONSE_BYTES: Final = 2 * 1024 * 1024

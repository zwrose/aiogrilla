"""Client-side constants for the Grilla cloud service (not secret; may be rotated)."""

from __future__ import annotations

REGION = "us-east-2"
USER_POOL_ID = "us-east-2_l53UXbXVM"
USER_POOL_CLIENT_ID = "50v5lb75o5i6r0rn4q1rap9vps"
IDENTITY_POOL_ID = "us-east-2:bf67d1fc-96d7-4921-86f8-ce9c2eb1b415"
COGNITO_LOGIN_KEY = f"cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}"

IOT_ENDPOINT = "a2j9sbhiey57ml-ats.iot.us-east-2.amazonaws.com"

# REST discovery endpoint (host/path/params may change)
API_GRILLS_BY_OWNER = "https://v3v2ce8vjg.execute-api.us-east-2.amazonaws.com/default"

TOPIC_GRILL_STATE = "grilla/{device_id}/grill_state"  # the only authorized subscribe topic

PROBE_UNPLUGGED = 65535  # observed sentinel (probe2); ASSUMED for probe1
TEMP_MAX_PLAUSIBLE_F = 1000  # out-of-range guard -> treat as unplugged/None
STALENESS_SECONDS = 300  # no grill_state this long while connected => mark UNAVAILABLE
CRED_REFRESH_RATIO = 0.8  # renew IAM creds at 80% of their TTL

# --- Public decode tables (part of aiogrilla's public API) -----------------
# These map the vendor's model/error codes to human-readable names. aiogrilla
# does NOT consume them internally — they exist for CONSUMERS (e.g. the Home
# Assistant integration) to present model and error codes. They are exported
# from `aiogrilla` and covered by tests; do not remove them as "unused". Extend
# as new codes are confirmed. Mode/status display names are
# intentionally NOT here — those are presentation and belong to the UI layer
# (Home Assistant uses its own translations).

MODEL_NAMES = {
    "grilla": "Grilla",
    "silverbac": "Silverbac",
    "silverbac20": "Silverbac 2.0",
    "silverbacxl": "Silverbac XL",
    "chimp": "Chimp",
    "chimp20": "Chimp 2.0",
    "kong": "Kong",
    "mammoth": "Mammoth",
    "pieRo": "Pie-Ro",
}

# Error/display codes -> "Friendly (CODE)". Seeded with confirmed codes; consumers
# fall back to the raw code for anything not listed here.
ERROR_CODE_NAMES = {
    "FHI": "Food probe too high (FHI)",
    "C15": "High-temp cooldown (C15)",
}

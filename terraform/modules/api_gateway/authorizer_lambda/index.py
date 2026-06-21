"""
Lambda authoriser for HTTP API v2.
Validates the X-Api-Key header against keys stored in Secrets Manager.

WHY Lambda authoriser (not usage plans)?
  HTTP API v2 removed native usage-plan-based API key auth (that's only on REST API v1).
  The recommended pattern for HTTP API key auth is a Lambda authoriser that:
    1. Reads valid keys from Secrets Manager (cached for 300s by API Gateway)
    2. Returns allow/deny as a simple boolean
  The 300s TTL on the authoriser result means Secrets Manager is called once per
  5 minutes per unique key, not once per request.
"""

import json
import logging
import os

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

SECRET_NAME = os.environ["SECRET_NAME"]
AWS_REGION  = os.environ.get("AWS_REGION", "us-east-1")

_cached_keys: dict[str, str] | None = None


def _get_valid_keys() -> dict[str, str]:
    global _cached_keys
    if _cached_keys is not None:
        return _cached_keys

    sm   = boto3.client("secretsmanager", region_name=AWS_REGION)
    resp = sm.get_secret_value(SecretId=SECRET_NAME)
    _cached_keys = json.loads(resp["SecretString"])
    return _cached_keys


def handler(event: dict, context) -> bool:
    """
    HTTP API v2 simple response authoriser.
    Return True to allow, False to deny.
    API Gateway caches this result for `authorizer_result_ttl_in_seconds`.
    """
    headers = event.get("headers", {})
    api_key = headers.get("x-api-key") or headers.get("X-Api-Key", "")

    if not api_key:
        log.warning("Request with missing X-Api-Key header")
        return False

    try:
        valid_keys = _get_valid_keys()
        if api_key in valid_keys.values():
            log.info(f"Valid API key accepted (caller: {_key_name(api_key, valid_keys)})")
            return True
        else:
            log.warning(f"Invalid API key: {api_key[:8]}...")
            return False
    except Exception as e:
        log.error(f"Authoriser error: {e}")
        return False


def _key_name(key: str, valid_keys: dict) -> str:
    for name, val in valid_keys.items():
        if val == key:
            return name
    return "unknown"

"""
Webhook Receiver Lambda
========================
Receives HTTP POST webhooks from Segment, Amplitude, and Adjust.
Validates the signature, normalizes the event format, and publishes to Kinesis.

Why normalize? Each platform has a different JSON structure:
  - Segment: { "type": "track", "userId": ..., "event": ..., "properties": {...} }
  - Amplitude: { "events": [{ "event_type": ..., "user_id": ..., "event_properties": {...} }] }

We map all of these to our internal CustomerEvent schema before Kinesis ingestion.
This means downstream consumers (Glue, ML training) see a consistent schema
regardless of which analytics platform sent the event.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import boto3

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

kinesis_client = boto3.client("kinesis", region_name=os.environ.get("AWS_REGION_NAME", "us-east-1"))
secrets_client = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION_NAME", "us-east-1"))

STREAM_NAME = os.environ["KINESIS_STREAM_NAME"]
WEBHOOK_SECRET_ARN = os.environ["WEBHOOK_SECRET_ARN"]

# Cache the webhook secret — secrets don't change often
_webhook_secret_cache: dict | None = None


def _get_webhook_secrets() -> dict:
    global _webhook_secret_cache
    if _webhook_secret_cache is None:
        response = secrets_client.get_secret_value(SecretId=WEBHOOK_SECRET_ARN)
        _webhook_secret_cache = json.loads(response["SecretString"])
    return _webhook_secret_cache


def _verify_segment_signature(payload: bytes, signature_header: str, secret: str) -> bool:
    """Validate Segment webhook HMAC-SHA1 signature."""
    if not signature_header:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), payload, hashlib.sha1
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _verify_amplitude_signature(payload: bytes, signature_header: str, secret: str) -> bool:
    """Validate Amplitude webhook signature (SHA-256)."""
    if not signature_header:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _normalize_segment_event(raw: dict) -> list[dict]:
    """
    Convert Segment track/identify/page events → our internal schema.
    Segment sends one event per webhook call.
    """
    event_type_map = {
        "track":    raw.get("event", "feature_usage"),
        "identify": "profile_update",
        "page":     "session_start",
        "screen":   "session_start",
        "group":    "profile_update",
    }

    segment_type = raw.get("type", "track")
    props = raw.get("properties", {})

    return [{
        "event_id":           raw.get("messageId", str(uuid.uuid4())),
        "customer_id":        raw.get("userId") or raw.get("anonymousId", "anonymous"),
        "event_type":         event_type_map.get(segment_type, "feature_usage"),
        "timestamp":          raw.get("timestamp", datetime.now(timezone.utc).isoformat()),
        "device":             raw.get("context", {}).get("device", {}).get("type", "unknown"),
        "session_id":         raw.get("context", {}).get("sessionId"),
        "session_duration":   props.get("session_duration", 0),
        "transaction_amount": props.get("revenue", props.get("transaction_amount", 0.0)),
        "feature_flags":      props.get("feature_flags", {}),
        "cohort":             raw.get("context", {}).get("traits", {}).get("cohort"),
        "plan":               raw.get("context", {}).get("traits", {}).get("plan"),
        "customer_state":     None,  # Unknown from external source
        "metadata": {
            "source":         "segment",
            "schema_version": "1.0",
            "original_type":  segment_type,
        },
    }]


def _normalize_amplitude_event(raw: dict) -> list[dict]:
    """
    Convert Amplitude event batch → our internal schema.
    Amplitude sends arrays of events in a single webhook call.
    """
    normalized = []
    for evt in raw.get("events", []):
        props = evt.get("event_properties", {})
        user_props = evt.get("user_properties", {})

        # Map Amplitude event types to our taxonomy
        event_type = evt.get("event_type", "feature_usage").lower().replace(" ", "_")
        if event_type in ("session_start", "[amplitude] start session"):
            event_type = "session_start"
        elif event_type in ("session_end", "[amplitude] end session"):
            event_type = "session_end"

        normalized.append({
            "event_id":           str(evt.get("event_id", uuid.uuid4())),
            "customer_id":        str(evt.get("user_id") or evt.get("device_id", "anonymous")),
            "event_type":         event_type,
            "timestamp":          datetime.fromtimestamp(
                                    evt.get("time", 0) / 1000, tz=timezone.utc
                                  ).isoformat(),
            "device":             evt.get("platform", "unknown").lower(),
            "session_id":         str(evt.get("session_id")),
            "session_duration":   props.get("session_duration", 0),
            "transaction_amount": props.get("revenue", 0.0),
            "feature_flags":      props.get("feature_flags", {}),
            "cohort":             user_props.get("cohort"),
            "plan":               user_props.get("plan"),
            "customer_state":     None,
            "metadata": {
                "source":         "amplitude",
                "schema_version": "1.0",
                "amplitude_event_type": evt.get("event_type"),
            },
        })
    return normalized


def _publish_to_kinesis(events: list[dict]) -> tuple[int, int]:
    """Publish normalized events to Kinesis. Returns (success, failure) counts."""
    if not events:
        return 0, 0

    records = [
        {
            "Data":         json.dumps(evt).encode("utf-8"),
            "PartitionKey": evt.get("customer_id", "unknown"),
        }
        for evt in events
    ]

    # PutRecords batch limit = 500 records
    success = failure = 0
    for i in range(0, len(records), 500):
        batch = records[i : i + 500]
        resp = kinesis_client.put_records(StreamName=STREAM_NAME, Records=batch)
        failure += resp["FailedRecordCount"]
        success += len(batch) - resp["FailedRecordCount"]

    return success, failure


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def lambda_handler(event: dict, context) -> dict:
    """
    Route webhook POST to the correct normalizer based on path.
    API Gateway v2 (HTTP API) passes the raw path in event["rawPath"].
    """
    path = event.get("rawPath", "")
    method = event.get("requestContext", {}).get("http", {}).get("method", "POST")

    if method != "POST":
        return _response(405, {"error": "Method not allowed"})

    # Decode body (API Gateway v2 base64-encodes binary bodies)
    body_str = event.get("body", "")
    if event.get("isBase64Encoded"):
        body_bytes = base64.b64decode(body_str)
    else:
        body_bytes = body_str.encode("utf-8") if isinstance(body_str, str) else body_str

    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    secrets = _get_webhook_secrets()

    # Route and validate by source
    if "/segment" in path:
        sig = headers.get("x-signature")
        if not _verify_segment_signature(body_bytes, sig, secrets.get("segment_secret", "")):
            log.warning("Segment signature validation failed")
            return _response(401, {"error": "Invalid signature"})

        try:
            raw = json.loads(body_bytes)
        except json.JSONDecodeError as e:
            return _response(400, {"error": f"Invalid JSON: {e}"})

        events = _normalize_segment_event(raw)

    elif "/amplitude" in path:
        sig = headers.get("x-amplitude-signature")
        if not _verify_amplitude_signature(body_bytes, sig, secrets.get("amplitude_secret", "")):
            log.warning("Amplitude signature validation failed")
            return _response(401, {"error": "Invalid signature"})

        try:
            raw = json.loads(body_bytes)
        except json.JSONDecodeError as e:
            return _response(400, {"error": f"Invalid JSON: {e}"})

        events = _normalize_amplitude_event(raw)

    else:
        return _response(404, {"error": f"Unknown webhook path: {path}"})

    success, failure = _publish_to_kinesis(events)
    log.info(f"Webhook processed: {success} published, {failure} failed | path={path}")

    if failure > 0:
        # Return 207 Multi-Status — partial success
        return _response(207, {
            "message": f"Partially published: {success}/{len(events)} events",
            "failed": failure,
        })

    return _response(200, {
        "message": f"Published {success} events",
        "stream": STREAM_NAME,
    })

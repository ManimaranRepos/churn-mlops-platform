"""
SNS → Slack forwarder Lambda.

AlertManager sends alerts to the alertmanager-sns-forwarder sidecar → SNS.
SNS invokes this Lambda → Slack webhook.

WHY Lambda instead of AlertManager's native Slack receiver?
  AlertManager's Slack receiver requires a public HTTPS endpoint for the
  webhook to call back. Our AlertManager runs inside the EKS cluster with no
  public ingress. Instead:
    AlertManager → SNS (via HTTPS, crosses VPC boundary easily) → Lambda → Slack
  The Lambda runs in the VPC but Slack webhooks are outbound — no public
  ingress needed.

Alert payload from AlertManager webhook format:
  {
    "receiver": "slack-critical",
    "status": "firing",
    "alerts": [
      {
        "status": "firing",
        "labels": { "alertname": "InferenceHighErrorRate", "severity": "critical", ... },
        "annotations": { "summary": "...", "description": "..." },
        "startsAt": "2024-01-01T00:00:00Z",
        "endsAt": "0001-01-01T00:00:00Z"
      }
    ],
    "groupLabels": { "alertname": "InferenceHighErrorRate" },
    "commonLabels": { ... },
    "commonAnnotations": { ... },
    "externalURL": "http://alertmanager:9093"
  }
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from datetime import datetime, timezone

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

AWS_REGION      = os.environ.get("AWS_REGION", "us-east-1")
SECRET_NAME     = os.environ["SLACK_WEBHOOK_SECRET_NAME"]

# Channel routing by SNS topic suffix
CHANNEL_MAP = {
    "critical": os.environ.get("SLACK_CHANNEL_CRITICAL", "#alerts-critical"),
    "warning":  os.environ.get("SLACK_CHANNEL_WARNING",  "#alerts-warning"),
    "info":     os.environ.get("SLACK_CHANNEL_INFO",      "#ml-platform"),
}

EMOJI_MAP = {
    "critical": ":red_circle:",
    "warning":  ":large_yellow_circle:",
    "info":     ":large_green_circle:",
    "resolved": ":white_check_mark:",
}

_webhook_url: str | None = None


def _get_webhook_url() -> str:
    global _webhook_url
    if _webhook_url:
        return _webhook_url
    sm   = boto3.client("secretsmanager", region_name=AWS_REGION)
    resp = sm.get_secret_value(SecretId=SECRET_NAME)
    data = json.loads(resp["SecretString"])
    _webhook_url = data["webhook_url"]
    return _webhook_url


def _severity_from_topic(topic_arn: str) -> str:
    if "critical" in topic_arn:
        return "critical"
    if "warning" in topic_arn:
        return "warning"
    return "info"


def _format_alert(alert: dict, severity: str) -> dict:
    """Build a Slack Block Kit message for one alert."""
    status      = alert.get("status", "firing")
    labels      = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    alert_name  = labels.get("alertname", "Unknown Alert")
    summary     = annotations.get("summary", alert_name)
    description = annotations.get("description", "")
    runbook     = annotations.get("runbook", "")
    dashboard   = annotations.get("dashboard", "")
    namespace   = labels.get("namespace", "")
    env         = labels.get("env", labels.get("environment", ""))

    emoji = EMOJI_MAP.get("resolved" if status == "resolved" else severity, ":bell:")
    color = {"critical": "#FF0000", "warning": "#FFA500", "info": "#36a64f"}.get(severity, "#808080")

    if status == "resolved":
        color = "#36a64f"

    started_at = alert.get("startsAt", "")
    if started_at:
        try:
            dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            started_at = dt.strftime("%Y-%m-%d %H:%M UTC")
        except ValueError:
            pass

    blocks = [
        {
            "type": "header",
            "text": { "type": "plain_text", "text": f"{emoji} {summary}" }
        },
        {
            "type": "section",
            "fields": [
                { "type": "mrkdwn", "text": f"*Status:*\n{'RESOLVED' if status == 'resolved' else 'FIRING'}" },
                { "type": "mrkdwn", "text": f"*Severity:*\n{severity.upper()}" },
                { "type": "mrkdwn", "text": f"*Environment:*\n{env or 'unknown'}" },
                { "type": "mrkdwn", "text": f"*Namespace:*\n{namespace or 'cluster-wide'}" },
                { "type": "mrkdwn", "text": f"*Started:*\n{started_at}" },
            ]
        }
    ]

    if description:
        blocks.append({
            "type": "section",
            "text": { "type": "mrkdwn", "text": f"*Details:*\n{description[:500]}" }
        })

    actions = []
    if runbook:
        actions.append({ "type": "button", "text": { "type": "plain_text", "text": "Runbook" }, "url": runbook })
    if dashboard:
        actions.append({ "type": "button", "text": { "type": "plain_text", "text": "Dashboard" }, "url": dashboard })
    if actions:
        blocks.append({ "type": "actions", "elements": actions })

    return {
        "attachments": [
            {
                "color": color,
                "blocks": blocks,
                "fallback": f"[{severity.upper()}] {summary}"
            }
        ]
    }


def _post_to_slack(payload: dict, channel: str) -> None:
    webhook_url = _get_webhook_url()
    payload["channel"] = channel

    data    = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as resp:
        body = resp.read().decode()
        if resp.status != 200:
            raise RuntimeError(f"Slack returned {resp.status}: {body}")
        log.info(f"Posted to Slack channel {channel}: {resp.status}")


def handler(event: dict, context) -> dict:
    """
    Lambda handler invoked by SNS subscription.
    Each SNS message is one AlertManager webhook payload (may contain multiple alerts).
    """
    for record in event.get("Records", []):
        try:
            topic_arn = record.get("Sns", {}).get("TopicArn", "")
            severity  = _severity_from_topic(topic_arn)
            channel   = CHANNEL_MAP.get(severity, CHANNEL_MAP["warning"])

            message_str = record.get("Sns", {}).get("Message", "{}")
            payload     = json.loads(message_str)

            alerts = payload.get("alerts", [])
            log.info(f"Processing {len(alerts)} alert(s) from SNS topic {topic_arn}")

            for alert in alerts:
                slack_msg = _format_alert(alert, severity)
                _post_to_slack(slack_msg, channel)

        except Exception as e:
            log.error(f"Failed to forward alert to Slack: {e}", exc_info=True)
            # Don't raise — Lambda retry would re-send the same (already-sent) alerts

    return {"statusCode": 200, "body": "ok"}

"""
Security Hub Findings → Slack forwarder

Invoked by EventBridge when Security Hub imports a CRITICAL or HIGH finding.
Formats the finding into a Slack Block Kit message and posts to the security
alerts channel via the shared webhook URL in Secrets Manager.

WHY format here (not rely on SNS email)?
  SNS email sends raw JSON — unreadable in an incident.
  Slack Block Kit gives engineers the title, affected resource, remediation
  guidance, and a link to Security Hub in <5 seconds of reading.

Finding structure (Security Hub ASFF format):
  findings[0].Title
  findings[0].Description
  findings[0].Severity.Label  (CRITICAL|HIGH|MEDIUM|LOW|INFORMATIONAL)
  findings[0].Resources[0].Type + .Id  (e.g. "AwsS3Bucket", "arn:aws:s3:::my-bucket")
  findings[0].Remediation.Recommendation.Text
  findings[0].SourceUrl  (link back to Security Hub finding)
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

AWS_REGION             = os.environ.get("AWS_REGION", "us-east-1")
SNS_TOPIC_ARN          = os.environ.get("SNS_TOPIC_ARN", "")
SLACK_WEBHOOK_SECRET   = os.environ.get("SLACK_WEBHOOK_SECRET_NAME", "")
ENVIRONMENT            = os.environ.get("ENVIRONMENT", "dev")

SEVERITY_EMOJI = {
    "CRITICAL": ":rotating_light:",
    "HIGH":     ":red_circle:",
    "MEDIUM":   ":large_yellow_circle:",
    "LOW":      ":large_green_circle:",
}
SEVERITY_COLOR = {
    "CRITICAL": "#8B0000",
    "HIGH":     "#FF0000",
    "MEDIUM":   "#FFA500",
    "LOW":      "#36a64f",
}

_webhook_cache: str | None = None


def _get_webhook_url() -> str:
    global _webhook_cache
    if _webhook_cache:
        return _webhook_cache
    if not SLACK_WEBHOOK_SECRET:
        return ""
    sm   = boto3.client("secretsmanager", region_name=AWS_REGION)
    data = json.loads(sm.get_secret_value(SecretId=SLACK_WEBHOOK_SECRET)["SecretString"])
    _webhook_cache = data.get("webhook_url", "")
    return _webhook_cache


def _format_finding(finding: dict) -> dict:
    severity    = finding.get("Severity", {}).get("Label", "UNKNOWN")
    title       = finding.get("Title", "Security Finding")
    description = finding.get("Description", "")[:500]
    source      = finding.get("ProductArn", "").split("/")[-1]
    source_url  = finding.get("SourceUrl", "")

    resources   = finding.get("Resources", [{}])
    resource    = resources[0] if resources else {}
    res_type    = resource.get("Type", "Unknown")
    res_id      = resource.get("Id", "Unknown")

    remediation = (
        finding.get("Remediation", {})
               .get("Recommendation", {})
               .get("Text", "See Security Hub for remediation guidance.")
    )

    emoji = SEVERITY_EMOJI.get(severity, ":bell:")
    color = SEVERITY_COLOR.get(severity, "#808080")

    blocks = [
        {
            "type": "header",
            "text": { "type": "plain_text", "text": f"{emoji} [{severity}] {title}" }
        },
        {
            "type": "section",
            "fields": [
                { "type": "mrkdwn", "text": f"*Severity:*\n{severity}" },
                { "type": "mrkdwn", "text": f"*Source:*\n{source}" },
                { "type": "mrkdwn", "text": f"*Resource Type:*\n{res_type}" },
                { "type": "mrkdwn", "text": f"*Environment:*\n{ENVIRONMENT}" },
            ]
        },
        {
            "type": "section",
            "text": { "type": "mrkdwn", "text": f"*Resource:*\n`{res_id}`" }
        },
        {
            "type": "section",
            "text": { "type": "mrkdwn", "text": f"*Description:*\n{description}" }
        },
        {
            "type": "section",
            "text": { "type": "mrkdwn", "text": f"*Remediation:*\n{remediation}" }
        },
    ]

    if source_url:
        blocks.append({
            "type": "actions",
            "elements": [{
                "type": "button",
                "text": { "type": "plain_text", "text": "View in Security Hub" },
                "url": source_url,
                "style": "danger" if severity == "CRITICAL" else "primary",
            }]
        })

    return {
        "attachments": [{
            "color":    color,
            "blocks":   blocks,
            "fallback": f"[{severity}] {title} — {res_id}",
        }]
    }


def _post_slack(payload: dict) -> None:
    url = _get_webhook_url()
    if not url:
        log.warning("No Slack webhook URL configured — skipping Slack notification")
        return
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status != 200:
            log.error(f"Slack returned {resp.status}: {resp.read().decode()}")


def _publish_sns(finding: dict) -> None:
    if not SNS_TOPIC_ARN:
        return
    severity = finding.get("Severity", {}).get("Label", "UNKNOWN")
    title    = finding.get("Title", "Security Finding")
    try:
        boto3.client("sns", region_name=AWS_REGION).publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=f"[{ENVIRONMENT.upper()}] Security Finding: {severity} — {title}",
            Message=json.dumps(finding, indent=2),
        )
    except Exception as e:
        log.error(f"SNS publish failed: {e}")


def handler(event: dict, context) -> dict:
    """
    EventBridge delivers a batch of Security Hub findings in one event.
    We process each finding independently.
    """
    log.info(f"Received event: {json.dumps(event)}")

    findings = event.get("detail", {}).get("findings", [])
    if not findings:
        log.info("No findings in event")
        return {"statusCode": 200, "processed": 0}

    processed = 0
    for finding in findings:
        try:
            severity = finding.get("Severity", {}).get("Label", "UNKNOWN")
            title    = finding.get("Title", "unknown")
            log.info(f"Processing finding: [{severity}] {title}")

            slack_msg = _format_finding(finding)
            _post_slack(slack_msg)
            _publish_sns(finding)
            processed += 1

        except Exception as e:
            log.error(f"Failed to process finding: {e}", exc_info=True)

    return {"statusCode": 200, "processed": processed}

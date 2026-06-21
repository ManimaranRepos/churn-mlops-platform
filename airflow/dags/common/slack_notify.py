"""
Slack notification callbacks for Airflow DAGs.

WHY Slack callbacks (not just relying on Airflow email alerts)?
  Email latency is 1-5 min; Slack is instant and has richer formatting.
  ML pipeline failures are time-sensitive — a training job failure means
  no new model ships that day. Team needs to know immediately.

  These callbacks are wired as on_failure_callback / on_success_callback
  on each DAG definition. One line per DAG, consistent messaging.
"""

import json
import logging
import os
from typing import Optional

import boto3
import urllib.request

log = logging.getLogger(__name__)

_SLACK_WEBHOOK_CACHE: Optional[str] = None


def _get_slack_webhook_url() -> str:
    """
    Fetch Slack webhook URL from Secrets Manager (cached for the pod lifetime).
    WHY cached? We don't want a Secrets Manager API call on every task callback.
    Pod restarts automatically clear the cache.
    """
    global _SLACK_WEBHOOK_CACHE
    if _SLACK_WEBHOOK_CACHE:
        return _SLACK_WEBHOOK_CACHE

    secret_name = f"{os.environ.get('PROJECT', 'churn-platform')}-{os.environ.get('ENVIRONMENT', 'dev')}/slack/webhook"
    client  = boto3.client("secretsmanager", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    secret  = json.loads(client.get_secret_value(SecretId=secret_name)["SecretString"])
    _SLACK_WEBHOOK_CACHE = secret["webhook_url"]
    return _SLACK_WEBHOOK_CACHE


def _send_slack_message(blocks: list) -> None:
    """POST a Block Kit message to Slack via webhook."""
    try:
        webhook_url = _get_slack_webhook_url()
        payload     = json.dumps({"blocks": blocks}).encode("utf-8")
        req         = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                log.warning(f"Slack webhook returned {resp.status}")
    except Exception as e:
        log.warning(f"Failed to send Slack notification: {e}")
        # Don't raise — a failed Slack notification should not fail the Airflow task


def on_dag_failure(context: dict) -> None:
    """Airflow on_failure_callback — sends alert when any task in the DAG fails."""
    dag_id   = context["dag"].dag_id
    task_id  = context["task_instance"].task_id
    run_id   = context["run_id"]
    exc      = context.get("exception", "Unknown error")
    log_url  = context["task_instance"].log_url

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": ":red_circle: Airflow Task Failed"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*DAG:* `{dag_id}`"},
                {"type": "mrkdwn", "text": f"*Task:* `{task_id}`"},
                {"type": "mrkdwn", "text": f"*Run:* `{run_id}`"},
                {"type": "mrkdwn", "text": f"*Error:* `{str(exc)[:200]}`"},
            ],
        },
        {
            "type": "actions",
            "elements": [{
                "type": "button",
                "text": {"type": "plain_text", "text": "View Logs"},
                "url":  log_url,
            }],
        },
    ]
    _send_slack_message(blocks)


def on_dag_success(context: dict) -> None:
    """Airflow on_success_callback — optional success notification for critical DAGs."""
    dag_id   = context["dag"].dag_id
    run_id   = context["run_id"]
    duration = context.get("dag_run", {}).get("duration", "?")

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": ":white_check_mark: Pipeline Complete"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*DAG:* `{dag_id}`"},
                {"type": "mrkdwn", "text": f"*Run:* `{run_id}`"},
                {"type": "mrkdwn", "text": f"*Duration:* {duration}"},
            ],
        },
    ]
    _send_slack_message(blocks)


def on_sla_miss(dag, task_list, blocking_task_list, slas, blocking_tis) -> None:
    """Called when a task exceeds its SLA."""
    dag_id = dag.dag_id
    tasks  = ", ".join(t.task_id for t in task_list)

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": ":warning: SLA Missed"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*DAG:* `{dag_id}`"},
                {"type": "mrkdwn", "text": f"*Tasks:* `{tasks}`"},
            ],
        },
    ]
    _send_slack_message(blocks)

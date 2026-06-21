output "airflow_role_arn"          { value = aws_iam_role.airflow.arn }
output "fernet_key_secret_arn"     { value = aws_secretsmanager_secret.airflow_fernet_key.arn }
output "db_credentials_secret_arn" { value = aws_secretsmanager_secret.airflow_db.arn }
output "dag_events_queue_url"      { value = aws_sqs_queue.dag_events.url }
output "log_group_name"            { value = aws_cloudwatch_log_group.airflow.name }

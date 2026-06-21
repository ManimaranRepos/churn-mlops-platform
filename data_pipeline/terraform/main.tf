# =============================================================================
# DATA PIPELINE TERRAFORM — Kinesis, Firehose, Glue, Athena
# =============================================================================
# Resource creation order:
#   SQS (DLQ) → Lambda → Kinesis Stream → Firehose → Glue Catalog →
#   Glue Crawler → Glue Job → Athena Workgroup
# =============================================================================

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
  }
}

locals {
  name_prefix = "${var.project}-${var.environment}"
}

# =============================================================================
# SQS DEAD LETTER QUEUES
# When Firehose or Lambda fails to process a record, it goes here.
# Without a DLQ: failed records are silently dropped. With a DLQ: we can
# inspect failures, fix the bug, and replay the records.
# =============================================================================
resource "aws_sqs_queue" "firehose_dlq" {
  name                      = "${local.name_prefix}-firehose-dlq"
  message_retention_seconds = 1209600 # 14 days — enough time to notice and fix issues
  kms_master_key_id         = var.kms_s3_key_arn

  tags = { Name = "${local.name_prefix}-firehose-dlq" }
}

resource "aws_sqs_queue" "lambda_dlq" {
  name                      = "${local.name_prefix}-lambda-dlq"
  message_retention_seconds = 1209600
  kms_master_key_id         = var.kms_s3_key_arn

  tags = { Name = "${local.name_prefix}-lambda-dlq" }
}

# =============================================================================
# LAMBDA — Firehose Record Transformer
# Firehose calls this Lambda for every batch of records before writing to S3.
# We convert JSON → Parquet here for storage efficiency and Athena performance.
# Parquet is columnar: Athena only reads the columns you SELECT, not full rows.
# =============================================================================

# Package the Lambda function code
data "archive_file" "firehose_transformer" {
  type        = "zip"
  source_dir  = "${path.root}/../kinesis/lambda/firehose_transformer"
  output_path = "${path.module}/builds/firehose_transformer.zip"
}

resource "aws_lambda_function" "firehose_transformer" {
  function_name = "${local.name_prefix}-firehose-transformer"
  description   = "Converts Kinesis JSON events to Parquet for S3/Athena"

  filename         = data.archive_file.firehose_transformer.output_path
  source_code_hash = data.archive_file.firehose_transformer.output_base64sha256
  handler          = "handler.lambda_handler"
  runtime          = "python3.11"

  role    = var.lambda_role_arn
  timeout = 300 # 5 min — Firehose batches can be large
  memory_size = 512 # Parquet conversion is memory-intensive

  # Lambda runs inside the VPC so it can access internal services
  vpc_config {
    subnet_ids         = var.private_subnet_ids
    security_group_ids = [aws_security_group.lambda.id]
  }

  # Dead letter queue — if Lambda crashes, the failed batch goes here
  dead_letter_config {
    target_arn = aws_sqs_queue.lambda_dlq.arn
  }

  environment {
    variables = {
      ENVIRONMENT = var.environment
      LOG_LEVEL   = var.environment == "prod" ? "WARNING" : "DEBUG"
    }
  }

  # Reserved concurrency: prevent this Lambda from consuming all account concurrency
  # and starving other Lambda functions (e.g., inference Lambda in Phase 7)
  reserved_concurrent_executions = 10

  tags = { Name = "${local.name_prefix}-firehose-transformer" }
}

# =============================================================================
# LAMBDA — Webhook Receiver (Segment/Amplitude/Adjust integration)
# Receives POST requests from 3rd-party analytics platforms and
# normalizes their event formats before publishing to Kinesis.
# =============================================================================
data "archive_file" "webhook_receiver" {
  type        = "zip"
  source_dir  = "${path.root}/../kinesis/lambda/webhook_receiver"
  output_path = "${path.module}/builds/webhook_receiver.zip"
}

resource "aws_lambda_function" "webhook_receiver" {
  function_name = "${local.name_prefix}-webhook-receiver"
  description   = "Receives Segment/Amplitude webhooks and publishes to Kinesis"

  filename         = data.archive_file.webhook_receiver.output_path
  source_code_hash = data.archive_file.webhook_receiver.output_base64sha256
  handler          = "handler.lambda_handler"
  runtime          = "python3.11"

  role        = var.lambda_role_arn
  timeout     = 30
  memory_size = 256

  vpc_config {
    subnet_ids         = var.private_subnet_ids
    security_group_ids = [aws_security_group.lambda.id]
  }

  dead_letter_config {
    target_arn = aws_sqs_queue.lambda_dlq.arn
  }

  environment {
    variables = {
      KINESIS_STREAM_NAME = "${local.name_prefix}-events"
      AWS_REGION_NAME     = var.aws_region
      ENVIRONMENT         = var.environment
      # Webhook secret for validating Segment/Amplitude signatures
      WEBHOOK_SECRET_ARN  = aws_secretsmanager_secret.webhook_secret.arn
    }
  }

  tags = { Name = "${local.name_prefix}-webhook-receiver" }
}

# Secret for webhook signature validation (prevents spoofed events)
resource "aws_secretsmanager_secret" "webhook_secret" {
  name       = "${local.name_prefix}/webhooks/signing-secret"
  kms_key_id = var.kms_s3_key_arn

  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "webhook_secret" {
  secret_id     = aws_secretsmanager_secret.webhook_secret.id
  secret_string = jsonencode({ segment_secret = "REPLACE_ME", amplitude_secret = "REPLACE_ME" })
  lifecycle { ignore_changes = [secret_string] }
}

# API Gateway → Lambda (webhook receiver)
resource "aws_apigatewayv2_api" "webhook" {
  name          = "${local.name_prefix}-webhook-api"
  protocol_type = "HTTP"
  description   = "Webhook endpoint for Segment/Amplitude/Adjust events"

  cors_configuration {
    allow_origins = ["*"]
    allow_methods = ["POST"]
    allow_headers = ["Content-Type", "X-Signature-256", "X-Amplitude-Signature"]
  }
}

resource "aws_apigatewayv2_stage" "webhook" {
  api_id      = aws_apigatewayv2_api.webhook.id
  name        = var.environment
  auto_deploy = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_gateway.arn
    format = jsonencode({
      requestId      = "$context.requestId"
      sourceIp       = "$context.identity.sourceIp"
      requestTime    = "$context.requestTime"
      protocol       = "$context.protocol"
      httpMethod     = "$context.httpMethod"
      resourcePath   = "$context.resourcePath"
      routeKey       = "$context.routeKey"
      status         = "$context.status"
      responseLength = "$context.responseLength"
      integrationError = "$context.integrationErrorMessage"
    })
  }
}

resource "aws_apigatewayv2_integration" "webhook_lambda" {
  api_id             = aws_apigatewayv2_api.webhook.id
  integration_type   = "AWS_PROXY"
  integration_uri    = aws_lambda_function.webhook_receiver.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "segment" {
  api_id    = aws_apigatewayv2_api.webhook.id
  route_key = "POST /webhooks/segment"
  target    = "integrations/${aws_apigatewayv2_integration.webhook_lambda.id}"
}

resource "aws_apigatewayv2_route" "amplitude" {
  api_id    = aws_apigatewayv2_api.webhook.id
  route_key = "POST /webhooks/amplitude"
  target    = "integrations/${aws_apigatewayv2_integration.webhook_lambda.id}"
}

resource "aws_lambda_permission" "webhook_api" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.webhook_receiver.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.webhook.execution_arn}/*/*"
}

resource "aws_cloudwatch_log_group" "api_gateway" {
  name              = "/aws/apigateway/${local.name_prefix}-webhook"
  retention_in_days = 14
}

# =============================================================================
# KINESIS DATA STREAM — The central event bus
# All customer events flow through here before landing in S3.
# Using PROVISIONED mode (not ON_DEMAND) so we control cost predictably.
# ON_DEMAND auto-scales but can 10x your cost during traffic spikes.
# =============================================================================
resource "aws_kinesis_stream" "events" {
  name        = "${local.name_prefix}-events"
  shard_count = var.kinesis_shard_count

  # Retain records for 24h in dev (replay window — useful for debugging)
  # 7 days in prod (gives time to recover from downstream outages)
  retention_period = var.kinesis_retention_hours

  # Enhanced fanout: allows multiple consumers at 2MB/s each (vs shared 2MB/s total)
  # Needed when both Firehose AND a real-time processing Lambda read from the stream
  stream_mode_details {
    stream_mode = "PROVISIONED"
  }

  encryption_type = "KMS"
  kms_key_id      = var.kms_s3_key_arn

  tags = { Name = "${local.name_prefix}-events" }
}

# =============================================================================
# KINESIS FIREHOSE — Streams events to S3 with buffering + transformation
# Why Firehose over writing to S3 directly?
# - Firehose handles buffering (accumulates records before writing)
# - Fewer, larger S3 files = dramatically faster Athena queries
# - Built-in retry with DLQ for failed records
# - Can call our Lambda to convert JSON → Parquet in-flight
# =============================================================================
resource "aws_kinesis_firehose_delivery_stream" "events_to_s3" {
  name        = "${local.name_prefix}-events-to-s3"
  destination = "extended_s3"

  kinesis_source_configuration {
    kinesis_stream_arn = aws_kinesis_stream.events.arn
    role_arn           = aws_iam_role.firehose.arn
  }

  extended_s3_configuration {
    role_arn   = aws_iam_role.firehose.arn
    bucket_arn = var.raw_bucket_arn

    # Hive-style partitioning — Athena and Glue understand this partition format
    # Glue crawler will automatically detect year/month/day/hour as partition keys
    prefix              = "events/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/hour=!{timestamp:HH}/"
    error_output_prefix = "errors/!{firehose:error-output-type}/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/"

    # Buffer: accumulate 64MB OR 5 minutes before flushing (whichever comes first)
    # Small buffers = many tiny files = slow Athena. Large buffers = data freshness lag.
    # 64MB / 5min is a good balance for POC-scale event volumes.
    buffering_size     = var.firehose_buffer_size_mb
    buffering_interval = var.firehose_buffer_interval_seconds

    compression_format = "UNCOMPRESSED" # Lambda transformer outputs Parquet (already compressed)

    # Encrypt data at rest in S3 using our CMK
    s3_backup_mode = "FailedDataOnly"
    encryption_configuration {
      kms_encryption_config {
        aws_kms_key_arn = var.kms_s3_key_arn
      }
    }

    # Lambda transform: called on every batch before writing to S3
    processing_configuration {
      enabled = true
      processors {
        type = "Lambda"
        parameters {
          parameter_name  = "LambdaArn"
          parameter_value = "${aws_lambda_function.firehose_transformer.arn}:$LATEST"
        }
        parameters {
          parameter_name  = "BufferSizeInMBs"     # How much to send to Lambda per invocation
          parameter_value = "3"
        }
        parameters {
          parameter_name  = "BufferIntervalInSeconds"
          parameter_value = "60"
        }
        parameters {
          parameter_name  = "NumberOfRetries"
          parameter_value = "3"
        }
      }
    }

    cloudwatch_logging_options {
      enabled         = true
      log_group_name  = "/aws/firehose/${local.name_prefix}"
      log_stream_name = "S3Delivery"
    }
  }

  tags = { Name = "${local.name_prefix}-events-to-s3" }
}

resource "aws_cloudwatch_log_group" "firehose" {
  name              = "/aws/firehose/${local.name_prefix}"
  retention_in_days = 14
}

# Firehose IAM role — needs to read from Kinesis and write to S3
resource "aws_iam_role" "firehose" {
  name = "${local.name_prefix}-firehose-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "firehose.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "firehose" {
  role = aws_iam_role.firehose.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "KinesisRead"
        Effect = "Allow"
        Action = [
          "kinesis:DescribeStream", "kinesis:GetShardIterator",
          "kinesis:GetRecords", "kinesis:ListShards"
        ]
        Resource = aws_kinesis_stream.events.arn
      },
      {
        Sid    = "S3Write"
        Effect = "Allow"
        Action = ["s3:AbortMultipartUpload", "s3:GetBucketLocation",
                  "s3:GetObject", "s3:ListBucket", "s3:ListBucketMultipartUploads",
                  "s3:PutObject"]
        Resource = [var.raw_bucket_arn, "${var.raw_bucket_arn}/*"]
      },
      {
        Sid      = "KMSAccess"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = [var.kms_s3_key_arn]
      },
      {
        Sid    = "LambdaInvoke"
        Effect = "Allow"
        Action = ["lambda:InvokeFunction", "lambda:GetFunctionConfiguration"]
        Resource = aws_lambda_function.firehose_transformer.arn
      },
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = ["logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.firehose.arn}:*"
      }
    ]
  })
}

# Lambda permission for Firehose to invoke it
resource "aws_lambda_permission" "firehose_invoke" {
  statement_id  = "AllowFirehoseInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.firehose_transformer.function_name
  principal     = "firehose.amazonaws.com"
  source_arn    = aws_kinesis_firehose_delivery_stream.events_to_s3.arn
}

# =============================================================================
# GLUE DATA CATALOG — Metadata layer over S3
# Glue Catalog = the "schema registry" for the data lake.
# Without it: Athena doesn't know column names, types, or partition layout.
# With it: SELECT * FROM events WHERE date = '2024-01-15' just works.
# =============================================================================
resource "aws_glue_catalog_database" "raw" {
  name        = "${replace(local.name_prefix, "-", "_")}_raw"
  description = "Raw event data — JSON as landed from Kinesis Firehose"
}

resource "aws_glue_catalog_database" "curated" {
  name        = "${replace(local.name_prefix, "-", "_")}_curated"
  description = "Curated Iceberg tables — cleaned, typed, partitioned"
}

# =============================================================================
# GLUE CRAWLER — Auto-discovers schema from S3
# Instead of manually defining every column, the crawler reads a sample of
# your data and infers the schema. Run it after new data lands in S3.
# =============================================================================
resource "aws_glue_crawler" "raw_events" {
  name          = "${local.name_prefix}-raw-events-crawler"
  role          = var.glue_role_arn
  database_name = aws_glue_catalog_database.raw.name
  description   = "Discovers schema of raw JSON events in S3"

  s3_target {
    path = "s3://${var.raw_bucket_name}/events/"
    # Skip empty partitions — common in dev where not all hours have data
    exclusions = ["**.json.tmp"]
  }

  # Schedule: crawl daily at 1am (after the nightly batch of events lands)
  schedule = "cron(0 1 * * ? *)"

  schema_change_policy {
    # UPDATE_IN_DATABASE: when new columns appear, add them to the catalog
    # Don't delete old columns — downstream queries might still reference them
    update_behavior = "UPDATE_IN_DATABASE"
    delete_behavior = "LOG" # Log deletions but don't remove from catalog
  }

  # Partition discovery: finds new year/month/day/hour partition folders automatically
  recrawl_policy {
    recrawl_behavior = "CRAWL_NEW_FOLDERS_ONLY" # Efficient: skip already-catalogued data
  }

  configuration = jsonencode({
    Version = 1.0
    CrawlerOutput = {
      Partitions = { AddOrUpdateBehavior = "InheritFromTable" }
      Tables     = { AddOrUpdateBehavior = "MergeNewColumns" }
    }
    Grouping = {
      TableGroupingPolicy     = "CombineCompatibleSchemas"
      TableLevelConfiguration = 3 # year/month/day = 3 levels before grouping
    }
  })

  tags = { Name = "${local.name_prefix}-raw-crawler" }
}

resource "aws_glue_crawler" "curated" {
  name          = "${local.name_prefix}-curated-crawler"
  role          = var.glue_role_arn
  database_name = aws_glue_catalog_database.curated.name
  description   = "Discovers Iceberg tables in processed S3 bucket"

  s3_target {
    path = "s3://${var.processed_bucket_name}/iceberg/"
  }

  schedule = "cron(0 2 * * ? *)"

  schema_change_policy {
    update_behavior = "UPDATE_IN_DATABASE"
    delete_behavior = "LOG"
  }

  tags = { Name = "${local.name_prefix}-curated-crawler" }
}

# =============================================================================
# GLUE ETL JOB — raw JSON → curated Iceberg tables
# This is the heart of the ETL pipeline:
#   - Reads raw JSON from S3
#   - Casts and validates every field
#   - Writes Iceberg format (enables time-travel and schema evolution)
#   - Updates the Glue catalog partition metadata
# =============================================================================
resource "aws_glue_job" "raw_to_curated" {
  name        = "${local.name_prefix}-raw-to-curated"
  description = "Transform raw JSON events → curated Iceberg tables"
  role_arn    = var.glue_role_arn

  # Glue 4.0 = Spark 3.3, Python 3.10, built-in Iceberg support
  glue_version = "4.0"
  worker_type  = var.glue_worker_type
  number_of_workers = var.glue_num_workers

  command {
    name            = "glueetl"
    script_location = "s3://${var.processed_bucket_name}/glue-scripts/raw_to_curated.py"
    python_version  = "3"
  }

  default_arguments = {
    "--job-language"                     = "python"
    "--job-bookmark-option"              = "job-bookmark-enable"  # Only process new data each run
    "--enable-metrics"                   = "true"
    "--enable-spark-ui"                  = "true"
    "--spark-event-logs-path"            = "s3://${var.logs_bucket_name}/spark-ui/${local.name_prefix}/"
    "--enable-continuous-cloudwatch-log" = "true"
    "--enable-glue-datacatalog"          = "true"
    "--datalake-formats"                 = "iceberg"   # Enable Iceberg support
    "--conf"                             = "spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions --conf spark.sql.catalog.glue_catalog=org.apache.iceberg.spark.SparkCatalog --conf spark.sql.catalog.glue_catalog.warehouse=s3://${var.processed_bucket_name}/iceberg/ --conf spark.sql.catalog.glue_catalog.catalog-impl=org.apache.iceberg.aws.glue.GlueCatalog --conf spark.sql.catalog.glue_catalog.io-impl=org.apache.iceberg.aws.s3.S3FileIO"
    "--RAW_BUCKET"                       = var.raw_bucket_name
    "--PROCESSED_BUCKET"                 = var.processed_bucket_name
    "--GLUE_DATABASE"                    = aws_glue_catalog_database.curated.name
    "--AWS_REGION"                       = var.aws_region
    "--TempDir"                          = "s3://${var.processed_bucket_name}/glue-temp/"
  }

  execution_property {
    # Don't run more than 1 concurrent ETL job — prevents race conditions
    # when two runs try to write to the same Iceberg table partition
    max_concurrent_runs = 1
  }

  timeout = 60 # 60 minutes — fail if ETL takes longer (prevents runaway costs)

  tags = { Name = "${local.name_prefix}-raw-to-curated" }
}

# Glue Job for feature engineering (feeds the ML training pipeline)
resource "aws_glue_job" "feature_engineering" {
  name        = "${local.name_prefix}-feature-engineering"
  description = "Compute ML features from curated events → feature store"
  role_arn    = var.glue_role_arn
  glue_version = "4.0"
  worker_type  = var.glue_worker_type
  number_of_workers = var.glue_num_workers

  command {
    name            = "glueetl"
    script_location = "s3://${var.processed_bucket_name}/glue-scripts/feature_engineering.py"
    python_version  = "3"
  }

  default_arguments = {
    "--job-bookmark-option"              = "job-bookmark-enable"
    "--enable-metrics"                   = "true"
    "--enable-continuous-cloudwatch-log" = "true"
    "--enable-glue-datacatalog"          = "true"
    "--datalake-formats"                 = "iceberg"
    "--conf"                             = "spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions --conf spark.sql.catalog.glue_catalog=org.apache.iceberg.spark.SparkCatalog --conf spark.sql.catalog.glue_catalog.warehouse=s3://${var.processed_bucket_name}/iceberg/ --conf spark.sql.catalog.glue_catalog.catalog-impl=org.apache.iceberg.aws.glue.GlueCatalog"
    "--PROCESSED_BUCKET"                 = var.processed_bucket_name
    "--GLUE_DATABASE"                    = aws_glue_catalog_database.curated.name
    "--AWS_REGION"                       = var.aws_region
    "--TempDir"                          = "s3://${var.processed_bucket_name}/glue-temp/"
  }

  execution_property { max_concurrent_runs = 1 }
  timeout = 60

  tags = { Name = "${local.name_prefix}-feature-engineering" }
}

# =============================================================================
# GLUE TRIGGERS — Schedule when ETL jobs run
# raw-to-curated runs at 2am daily (after crawler at 1am discovers schema)
# feature-engineering runs at 4am (after curated data is ready)
# =============================================================================
resource "aws_glue_trigger" "raw_to_curated_schedule" {
  name     = "${local.name_prefix}-raw-to-curated-schedule"
  type     = "SCHEDULED"
  schedule = "cron(0 2 * * ? *)"
  enabled  = true

  actions {
    job_name = aws_glue_job.raw_to_curated.name
  }
}

# Chain: raw-to-curated SUCCESS → start feature-engineering
resource "aws_glue_trigger" "feature_engineering_conditional" {
  name = "${local.name_prefix}-feature-engineering-trigger"
  type = "CONDITIONAL"

  predicate {
    conditions {
      job_name = aws_glue_job.raw_to_curated.name
      state    = "SUCCEEDED"
    }
  }

  actions {
    job_name = aws_glue_job.feature_engineering.name
  }
}

# =============================================================================
# ATHENA — SQL query engine over S3
# =============================================================================
resource "aws_athena_workgroup" "main" {
  name        = "${local.name_prefix}-workgroup"
  description = "Athena workgroup for churn platform analytics"

  configuration {
    # Enforce query result encryption — raw query results contain customer data
    result_configuration {
      output_location = "s3://${var.processed_bucket_name}/athena-results/"

      encryption_configuration {
        encryption_option = "SSE_KMS"
        kms_key           = var.kms_s3_key_arn
      }
    }

    # Cost control: kill queries that scan more than 10GB
    # A full table scan of years of data would cost $50+ and likely be a mistake
    bytes_scanned_cutoff_per_query     = 10737418240 # 10 GB
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = true
    requester_pays_enabled             = false

    engine_version {
      selected_engine_version = "Athena engine version 3"
    }
  }
}

# Named queries — reusable SQL for common analytics
resource "aws_athena_named_query" "daily_event_counts" {
  name        = "daily-event-counts"
  workgroup   = aws_athena_workgroup.main.id
  database    = aws_glue_catalog_database.curated.name
  description = "Daily event count by type — use for monitoring data pipeline health"
  query       = file("${path.root}/../athena/views/daily_event_counts.sql")
}

resource "aws_athena_named_query" "churn_risk_by_cohort" {
  name        = "churn-risk-by-cohort"
  workgroup   = aws_athena_workgroup.main.id
  database    = aws_glue_catalog_database.curated.name
  description = "Churn risk distribution per customer acquisition cohort"
  query       = file("${path.root}/../athena/views/churn_risk_by_cohort.sql")
}

# =============================================================================
# SECURITY GROUPS
# =============================================================================
resource "aws_security_group" "lambda" {
  name        = "${local.name_prefix}-lambda-sg"
  description = "Security group for Lambda functions in the data pipeline"
  vpc_id      = var.vpc_id

  # Lambda needs outbound to: Kinesis, S3 (via endpoint), Secrets Manager
  egress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "HTTPS to AWS services via VPC endpoints"
  }

  tags = { Name = "${local.name_prefix}-lambda-sg" }
}

# =============================================================================
# EVENTBRIDGE RULE — Trigger Glue crawler after Firehose writes data
# Firehose emits a CloudWatch metric when it successfully writes to S3.
# We use this to trigger the Glue crawler, keeping catalog fresh.
# =============================================================================
resource "aws_cloudwatch_event_rule" "firehose_success" {
  name        = "${local.name_prefix}-firehose-success"
  description = "Trigger Glue crawler when Firehose writes to S3"

  event_pattern = jsonencode({
    source      = ["aws.firehose"]
    detail-type = ["Kinesis Data Firehose Delivery Stream Data"]
    detail = {
      deliveryStreamName = [aws_kinesis_firehose_delivery_stream.events_to_s3.name]
    }
  })
}

resource "aws_iam_role" "eventbridge_glue" {
  name = "${local.name_prefix}-eventbridge-glue-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "eventbridge_glue" {
  role = aws_iam_role.eventbridge_glue.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["glue:StartCrawler"]
      Resource = aws_glue_crawler.raw_events.arn
    }]
  })
}

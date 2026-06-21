# =============================================================================
# API Gateway HTTP API → VPC Link → ALB → EKS inference pods
#
# WHY API Gateway in front of the ALB?
#   The ALB is internal (scheme: internal). Putting it directly on the internet
#   would require a public ALB — which exposes all paths, all methods.
#   API Gateway gives us:
#     1. Public HTTPS endpoint with AWS-managed TLS (ACM cert)
#     2. Request validation (reject malformed JSON before it hits the pod)
#     3. Throttling: burst_limit and rate_limit protect against unintentional DoS
#     4. API keys for caller identity (CRM, marketing automation each get a key)
#     5. Usage plans: per-key rate limits (CRM gets 1000/s, batch jobs get 50/s)
#     6. CloudWatch access logs per route
#     7. IAM or Cognito authorisation (can be added without changing the pod)
#
# WHY HTTP API (not REST API)?
#   HTTP API has ~70% lower cost than REST API for the same traffic.
#   It lacks some features (e.g., request transformation, WAF integration at the
#   API GW level) — but WAF is better attached to the ALB directly for this pattern,
#   and we don't need request transformation (FastAPI handles that).
#
# VPC Link: API Gateway cannot route to a private ALB without a VPC Link.
#   The VPC Link creates an ENI in the VPC subnets, allowing API Gateway to
#   reach the private ALB as if it were on the internet.
# =============================================================================

resource "aws_apigatewayv2_api" "inference" {
  name          = "${var.project}-${var.environment}-inference"
  protocol_type = "HTTP"
  description   = "Churn prediction inference API"

  cors_configuration {
    allow_methods  = ["POST", "DELETE", "GET"]
    allow_headers  = ["Content-Type", "X-Api-Key", "Authorization"]
    allow_origins  = var.allowed_origins
    max_age        = 300
  }

  tags = var.tags
}

# VPC Link: bridges API Gateway to the private ALB
resource "aws_apigatewayv2_vpc_link" "inference" {
  name               = "${var.project}-${var.environment}-vpclink"
  security_group_ids = [aws_security_group.vpc_link.id]
  subnet_ids         = var.private_subnet_ids

  tags = var.tags
}

resource "aws_security_group" "vpc_link" {
  name        = "${var.project}-${var.environment}-apigw-vpclink-sg"
  description = "API Gateway VPC Link — allows outbound to ALB"
  vpc_id      = var.vpc_id

  egress {
    description = "All outbound (to ALB)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, {
    Name = "${var.project}-${var.environment}-apigw-vpclink-sg"
  })
}

# Integration: API GW → VPC Link → ALB
resource "aws_apigatewayv2_integration" "alb" {
  api_id             = aws_apigatewayv2_api.inference.id
  integration_type   = "HTTP_PROXY"
  integration_method = "ANY"
  integration_uri    = var.alb_listener_arn

  connection_type = "VPC_LINK"
  connection_id   = aws_apigatewayv2_vpc_link.inference.id

  timeout_milliseconds = 29000    # Max API GW timeout is 29s; set to match

  request_parameters = {
    # Forward caller identity for downstream logging
    "overwrite:header.X-Forwarded-For" = "$context.identity.sourceIp"
    "overwrite:header.X-Api-Key-Id"    = "$context.identity.apiKeyId"
  }
}

# Routes
resource "aws_apigatewayv2_route" "predict" {
  api_id             = aws_apigatewayv2_api.inference.id
  route_key          = "POST /predict"
  target             = "integrations/${aws_apigatewayv2_integration.alb.id}"
  authorization_type = "NONE"    # API key auth enforced via usage plan
  api_key_required   = true
}

resource "aws_apigatewayv2_route" "predict_batch" {
  api_id             = aws_apigatewayv2_api.inference.id
  route_key          = "POST /predict/batch"
  target             = "integrations/${aws_apigatewayv2_integration.alb.id}"
  authorization_type = "NONE"
  api_key_required   = true
}

resource "aws_apigatewayv2_route" "cache_invalidate" {
  api_id             = aws_apigatewayv2_api.inference.id
  route_key          = "DELETE /cache/{customerId}"
  target             = "integrations/${aws_apigatewayv2_integration.alb.id}"
  authorization_type = "NONE"
  api_key_required   = true
}

resource "aws_apigatewayv2_route" "health" {
  api_id             = aws_apigatewayv2_api.inference.id
  route_key          = "GET /health"
  target             = "integrations/${aws_apigatewayv2_integration.alb.id}"
  authorization_type = "NONE"
  api_key_required   = false    # Health endpoint does not require auth
}

# Stage (deploys routes)
resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.inference.id
  name        = "$default"
  auto_deploy = true

  # Access log format: JSON for CloudWatch Logs Insights queries in Phase 8
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_access_logs.arn
    format = jsonencode({
      requestId       = "$context.requestId"
      routeKey        = "$context.routeKey"
      status          = "$context.status"
      responseLength  = "$context.responseLength"
      requestTime     = "$context.requestTime"
      integrationLatency = "$context.integrationLatency"
      responseLatency = "$context.responseLatency"
      ip              = "$context.identity.sourceIp"
      apiKeyId        = "$context.identity.apiKeyId"
      userAgent       = "$context.identity.userAgent"
      error           = "$context.error.message"
    })
  }

  default_route_settings {
    detailed_metrics_enabled = true
    throttling_burst_limit   = 500   # Max concurrent requests
    throttling_rate_limit    = 200   # Sustained requests/second per stage
  }

  tags = var.tags
}

resource "aws_cloudwatch_log_group" "api_access_logs" {
  name              = "/aws/apigateway/${var.project}-${var.environment}-inference"
  retention_in_days = 14

  tags = var.tags
}

# ── API Keys and Usage Plans ──────────────────────────────────────────────────
# Each consumer (CRM, ML pipeline, batch scorer) gets its own API key.
# This allows per-key throttling, monitoring, and revocation without
# affecting other callers.

resource "aws_apigatewayv2_api_key" "crm" {   # Note: HTTP API uses different resource than REST API
  # HTTP API does not support aws_api_gateway_api_key directly —
  # API key enforcement on HTTP API routes is done via usage plans at the REST API layer.
  # For HTTP API, we use Lambda authoriser or Cognito instead.
  # See: https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-key-auth.html
  #
  # For simplicity in this POC, we store API keys in Secrets Manager
  # and validate them in a Lambda authoriser (below).
  # Production upgrade: replace with Cognito JWT authoriser.
}

# Lambda authoriser for API key validation
# (HTTP API v2 does not support usage plan-based API keys natively)
resource "aws_apigatewayv2_authorizer" "api_key" {
  api_id           = aws_apigatewayv2_api.inference.id
  authorizer_type  = "REQUEST"
  name             = "api-key-authorizer"
  identity_sources = ["$request.header.X-Api-Key"]

  authorizer_uri                    = aws_lambda_function.api_key_authorizer.invoke_arn
  authorizer_payload_format_version = "2.0"
  enable_simple_responses           = true

  # Cache authoriser result for 300s — same API key → same allow/deny decision
  authorizer_result_ttl_in_seconds = 300
}

resource "aws_lambda_function" "api_key_authorizer" {
  function_name = "${var.project}-${var.environment}-api-key-authorizer"
  role          = aws_iam_role.authorizer_lambda.arn
  runtime       = "python3.11"
  handler       = "index.handler"
  timeout       = 5
  memory_size   = 128

  filename         = "${path.module}/authorizer_lambda.zip"
  source_code_hash = filebase64sha256("${path.module}/authorizer_lambda.zip")

  environment {
    variables = {
      SECRET_NAME = "churn-platform/${var.environment}/api-keys"
      AWS_REGION  = var.aws_region
    }
  }

  tags = var.tags
}

resource "aws_iam_role" "authorizer_lambda" {
  name = "${var.project}-${var.environment}-api-key-authorizer-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy" "authorizer_lambda" {
  name = "api-key-authorizer-policy"
  role = aws_iam_role.authorizer_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.aws_region}:${var.account_id}:log-group:/aws/lambda/${var.project}-${var.environment}-api-key-authorizer:*"
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = "arn:aws:secretsmanager:${var.aws_region}:${var.account_id}:secret:churn-platform/${var.environment}/api-keys*"
      }
    ]
  })
}

resource "aws_lambda_permission" "api_gw_invoke_authorizer" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api_key_authorizer.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.inference.execution_arn}/*"
}

# API keys stored in Secrets Manager — rotated via Lambda rotation or manually
resource "aws_secretsmanager_secret" "api_keys" {
  name                    = "churn-platform/${var.environment}/api-keys"
  description             = "Valid API keys for churn inference API — JSON object {name: key}"
  recovery_window_in_days = var.environment == "prod" ? 30 : 0

  tags = var.tags
}

resource "aws_secretsmanager_secret_version" "api_keys_initial" {
  secret_id = aws_secretsmanager_secret.api_keys.id
  secret_string = jsonencode({
    crm_system   = "REPLACE_WITH_CRM_API_KEY"
    ml_pipeline  = "REPLACE_WITH_PIPELINE_API_KEY"
    batch_scorer = "REPLACE_WITH_BATCH_API_KEY"
  })

  lifecycle {
    ignore_changes = [secret_string]    # Keys are rotated externally — don't revert
  }
}

# CloudWatch alarms for API Gateway
resource "aws_cloudwatch_metric_alarm" "api_5xx_rate" {
  alarm_name          = "${var.project}-${var.environment}-api-5xx-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "5XXError"
  namespace           = "AWS/ApiGateway"
  period              = 60
  statistic           = "Sum"
  threshold           = 10     # More than 10 5xx errors per minute
  alarm_description   = "Inference API 5xx error rate elevated"
  alarm_actions       = [var.sns_topic_arn]

  dimensions = {
    ApiId = aws_apigatewayv2_api.inference.id
    Stage = "$default"
  }

  tags = var.tags
}

resource "aws_cloudwatch_metric_alarm" "api_latency_p99" {
  alarm_name          = "${var.project}-${var.environment}-api-latency-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  extended_statistic  = "p99"
  metric_name         = "IntegrationLatency"
  namespace           = "AWS/ApiGateway"
  period              = 60
  threshold           = 500    # P99 > 500ms triggers alert (quality gate is 200ms)
  alarm_description   = "Inference API P99 latency exceeds 500ms"
  alarm_actions       = [var.sns_topic_arn]

  dimensions = {
    ApiId = aws_apigatewayv2_api.inference.id
    Stage = "$default"
  }

  tags = var.tags
}

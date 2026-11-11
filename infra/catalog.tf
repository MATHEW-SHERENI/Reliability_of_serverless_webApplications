# Catalog API (module M1-M3) behind an API Gateway HTTP API (midterm Figure 1, arrows 1-3).

# ---------- Lambda function ----------

data "archive_file" "catalog" {
  type        = "zip"
  source_dir  = "${path.module}/../api"
  output_path = "${path.module}/../build/catalog.zip"
  excludes    = ["__pycache__"]
}

resource "aws_iam_role" "catalog" {
  name               = "${var.project}-catalog-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

# Identity policy: what the Catalog function itself may do.
data "aws_iam_policy_document" "catalog" {
  statement {
    sid       = "CallDependencyUrl"
    actions   = ["lambda:InvokeFunctionUrl", "lambda:InvokeFunction"]
    resources = [aws_lambda_function.dependency.arn]
  }
  statement {
    sid       = "WriteLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.catalog.arn}:*"]
  }
}

resource "aws_iam_role_policy" "catalog" {
  name   = "${var.project}-catalog-access"
  role   = aws_iam_role.catalog.id
  policy = data.aws_iam_policy_document.catalog.json
}

resource "aws_cloudwatch_log_group" "catalog" {
  name              = "/aws/lambda/${var.project}-catalog"
  retention_in_days = 14
}

resource "aws_lambda_function" "catalog" {
  function_name                  = "${var.project}-catalog"
  role                           = aws_iam_role.catalog.arn
  runtime                        = "python3.12"
  handler                        = "app.handler"
  filename                       = data.archive_file.catalog.output_path
  source_code_hash               = data.archive_file.catalog.output_base64sha256
  memory_size                    = 256
  timeout                        = 5
  reserved_concurrent_executions = 400

  environment {
    variables = {
      DEPENDENCY_URL     = aws_lambda_function_url.dependency.function_url
      ATTEMPT_TIMEOUT_MS = "1000"
      MECH_RETRY         = "false"
      MECH_CACHE         = "false"
    }
  }

  depends_on = [aws_iam_role_policy.catalog, aws_cloudwatch_log_group.catalog]
}

# ---------- API Gateway HTTP API ----------

resource "aws_apigatewayv2_api" "catalog" {
  name          = "${var.project}-catalog-api"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "catalog" {
  api_id                 = aws_apigatewayv2_api.catalog.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.catalog.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "get_product" {
  api_id    = aws_apigatewayv2_api.catalog.id
  route_key = "GET /products/{id}"
  target    = "integrations/${aws_apigatewayv2_integration.catalog.id}"
}

resource "aws_apigatewayv2_route" "create_order" {
  api_id    = aws_apigatewayv2_api.catalog.id
  route_key = "POST /orders"
  target    = "integrations/${aws_apigatewayv2_integration.catalog.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.catalog.id
  name        = "$default"
  auto_deploy = true
}

# Resource-based policy: allow API Gateway (this API only) to invoke the Catalog function.
resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowHttpApiInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.catalog.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.catalog.execution_arn}/*/*"
}

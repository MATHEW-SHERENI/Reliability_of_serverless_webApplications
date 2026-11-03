# Dependency Lambda (module M4) with its own IAM role and function URL.

# 1. Package dep/ into a zip file; re-zipped whenever the code changes.
data "archive_file" "dependency" {
  type        = "zip"
  source_dir  = "${path.module}/../dep"
  output_path = "${path.module}/../build/dependency.zip"
  excludes    = ["__pycache__"]
}

# 2. Allow the Lambda service to use this role.
data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "dependency" {
  name               = "${var.project}-dependency-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

# 3. Least privilege: exactly the actions this function needs, on exactly these resources.
data "aws_iam_policy_document" "dependency" {
  statement {
    sid       = "ReadAndReserveProducts"
    actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.products.arn]
  }
  statement {
    sid       = "WriteOrders"
    actions   = ["dynamodb:PutItem"]
    resources = [aws_dynamodb_table.orders.arn]
  }
  statement {
    sid       = "WriteLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.dependency.arn}:*"]
  }
}

resource "aws_iam_role_policy" "dependency" {
  name   = "${var.project}-dependency-access"
  role   = aws_iam_role.dependency.id
  policy = data.aws_iam_policy_document.dependency.json
}

# 4. Log group created by Terraform so logs expire after 14 days (Day 07 plan).
resource "aws_cloudwatch_log_group" "dependency" {
  name              = "/aws/lambda/${var.project}-dependency"
  retention_in_days = 14
}

# 5. The function itself (settings from midterm Table 3).
resource "aws_lambda_function" "dependency" {
  function_name                  = "${var.project}-dependency"
  role                           = aws_iam_role.dependency.arn
  runtime                        = "python3.12"
  handler                        = "handler.handler"
  filename                       = data.archive_file.dependency.output_path
  source_code_hash               = data.archive_file.dependency.output_base64sha256
  memory_size                    = 256
  timeout                        = 5
  reserved_concurrent_executions = 400

  environment {
    variables = {
      PRODUCTS_TABLE = aws_dynamodb_table.products.name
      ORDERS_TABLE   = aws_dynamodb_table.orders.name
    }
  }

  depends_on = [aws_iam_role_policy.dependency, aws_cloudwatch_log_group.dependency]
}

# 6. HTTPS address for the function; only IAM-signed requests are accepted.
resource "aws_lambda_function_url" "dependency" {
  function_name      = aws_lambda_function.dependency.function_name
  authorization_type = "AWS_IAM"
}

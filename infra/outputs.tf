output "products_table" {
  value = aws_dynamodb_table.products.name
}

output "orders_table" {
  value = aws_dynamodb_table.orders.name
}

output "idempotency_table" {
  value = aws_dynamodb_table.idempotency.name
}
output "dependency_function_name" {
  value = aws_lambda_function.dependency.function_name
}

output "dependency_url" {
  value = aws_lambda_function_url.dependency.function_url
}

output "catalog_function_name" {
  value = aws_lambda_function.catalog.function_name
}

output "api_url" {
  value = aws_apigatewayv2_stage.default.invoke_url
}

output "loadhost_id" {
  value = aws_instance.loadhost.id
}

output "loadhost_ami" {
  value = aws_instance.loadhost.ami
}

output "results_bucket" {
  value = local.results_bucket
}

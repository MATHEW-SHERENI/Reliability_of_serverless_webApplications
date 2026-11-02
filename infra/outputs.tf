output "products_table" {
  value = aws_dynamodb_table.products.name
}

output "orders_table" {
  value = aws_dynamodb_table.orders.name
}

output "idempotency_table" {
  value = aws_dynamodb_table.idempotency.name
}

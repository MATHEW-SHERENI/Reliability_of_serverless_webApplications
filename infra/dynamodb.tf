# Three tables from the data model in midterm Figure 2(b).
# PAY_PER_REQUEST = on-demand: no capacity planning, billed per request.

resource "aws_dynamodb_table" "products" {
  name         = "${var.project}-products"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "product_id"

  attribute {
    name = "product_id"
    type = "N"
  }
}

resource "aws_dynamodb_table" "orders" {
  name         = "${var.project}-orders"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "order_id"

  attribute {
    name = "order_id"
    type = "S"
  }
}

resource "aws_dynamodb_table" "idempotency" {
  name         = "${var.project}-idempotency"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "idem_key"

  attribute {
    name = "idem_key"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

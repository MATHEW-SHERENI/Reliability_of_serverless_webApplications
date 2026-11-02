"""Load the synthetic product catalog into DynamoDB (module M6, midterm Section 4.5).

The same seed always produces the same 1,000 products, so the catalog is
reproducible on any machine.
"""
import argparse
import random
from datetime import datetime, timezone
from decimal import Decimal

import boto3

STOCK = 1_000_000  # large enough that experiments never run out of stock


def make_products(count, seed):
    """Return a list of products; the same seed always gives the same list."""
    rng = random.Random(seed)
    now = datetime.now(timezone.utc).isoformat()
    products = []
    for product_id in range(1, count + 1):
        price = Decimal(str(round(rng.uniform(5, 500), 2)))
        products.append({
            "product_id": product_id,
            "name": f"Product {product_id:04d}",
            "price": price,
            "stock": STOCK,
            "version": 1,
            "updated_at": now,
        })
    return products


def write_products(table_name, products, region):
    """Write all products using DynamoDB's batch writer."""
    table = boto3.resource("dynamodb", region_name=region).Table(table_name)
    with table.batch_writer() as batch:
        for item in products:
            batch.put_item(Item=item)


def count_items(table_name, region):
    """Count items in the table, following pagination."""
    client = boto3.client("dynamodb", region_name=region)
    total = 0
    kwargs = {"TableName": table_name, "Select": "COUNT", "ConsistentRead": True}
    while True:
        page = client.scan(**kwargs)
        total += page["Count"]
        if "LastEvaluatedKey" not in page:
            return total
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def main():
    parser = argparse.ArgumentParser(description="Seed the product catalog.")
    parser.add_argument("--table", required=True, help="DynamoDB table name")
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=5360)
    parser.add_argument("--region", default="us-east-2")
    args = parser.parse_args()

    products = make_products(args.count, args.seed)
    write_products(args.table, products, args.region)
    total = count_items(args.table, args.region)
    print(f"Wrote {len(products)} products to {args.table}; table now holds {total} items.")


if __name__ == "__main__":
    main()

from decimal import Decimal

from orchestrate.seed_catalog import make_products


def without_timestamp(products):
    return [{k: v for k, v in p.items() if k != "updated_at"} for p in products]


def test_same_seed_gives_same_catalog():
    first = make_products(1000, seed=5360)
    second = make_products(1000, seed=5360)
    assert without_timestamp(first) == without_timestamp(second)


def test_catalog_has_1000_ids_and_valid_prices():
    products = make_products(1000, seed=5360)
    assert [p["product_id"] for p in products] == list(range(1, 1001))
    assert all(Decimal("5") <= p["price"] <= Decimal("500") for p in products)

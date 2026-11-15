"""Price updater (M6): changes 1% of prices every 10 s during a run.

Each change raises the product's version by one and is written to a version
log, so the analysis can tell whether a cached answer was stale: a cached read
is stale if its X-Data-Version is lower than the latest version logged before
it was served. Product choices and prices come from a seeded generator.
"""
import json
import random
import threading
import time
from decimal import Decimal

import boto3

PRODUCTS = 1000
SHARE = 0.01          # 1% of the catalog per round
INTERVAL_S = 10


def choose_updates(rng, products=PRODUCTS, share=SHARE):
    """One round: which products change and their new prices (5 to 500, cents)."""
    chosen = rng.sample(range(1, products + 1), max(1, round(products * share)))
    return [(product_id, Decimal(str(round(rng.uniform(5, 500), 2)))) for product_id in chosen]


class PriceUpdater(threading.Thread):
    """Runs in the background while k6 runs; stop() ends it and waits for it."""

    def __init__(self, table_name, seed, run_id, region="us-east-2", interval_s=INTERVAL_S,
                 table=None):
        super().__init__(daemon=True)
        self.table = table or boto3.resource("dynamodb", region_name=region).Table(table_name)
        self.rng = random.Random(f"{seed}-{run_id}")
        self.interval_s = interval_s
        self.log = []          # one entry per change: time, product, new version, price
        self.errors = 0
        # Not "_stop": threading.Thread on Python 3.12 has an internal _stop()
        # method that join() calls, and an attribute of that name replaces it.
        self._halt = threading.Event()

    def run(self):
        while not self._halt.wait(self.interval_s):
            for product_id, price in choose_updates(self.rng):
                try:
                    new = self.table.update_item(
                        Key={"product_id": product_id},
                        UpdateExpression="SET price = :p, version = version + :one, updated_at = :now",
                        ExpressionAttributeValues={":p": price, ":one": 1,
                                                   ":now": int(time.time())},
                        ReturnValues="UPDATED_NEW")["Attributes"]
                    self.log.append({"t": int(time.time() * 1000), "product_id": product_id,
                                     "version": int(new["version"]), "price": str(price)})
                except Exception:   # a failed update must not stop the run; it is counted
                    self.errors += 1

    def stop(self):
        self._halt.set()
        self.join()

    def write_log(self, path):
        with open(path, "w") as f:
            for entry in self.log:
                f.write(json.dumps(entry) + "\n")

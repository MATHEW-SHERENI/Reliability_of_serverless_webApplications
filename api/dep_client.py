"""HTTPS client for the dependency service (module M3).

Signs each request with AWS Signature Version 4 (the function URL requires
IAM auth), enforces a per-attempt timeout, and turns every failure into an
error class that says whether a retry could help.
"""
import json
import time

import botocore.session
import urllib3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest


class DependencyError(Exception):
    """Base class: every dependency failure says whether retrying could help."""
    retryable = False

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class DependencyTimeout(DependencyError):
    retryable = True


class DependencyServerError(DependencyError):
    retryable = True      # 5xx or broken connection: may succeed next time


class DependencyClientError(DependencyError):
    retryable = False     # 4xx: the request itself is the problem


class DependencyClient:
    def __init__(self, base_url, region, credentials=None, http=None):
        self.base_url = base_url.rstrip("/")
        self.region = region
        self.credentials = credentials or botocore.session.get_session().get_credentials()
        # One connection pool, created once and reused while the environment is warm.
        self.http = http or urllib3.PoolManager(maxsize=10, retries=False)
        self.last_latency_ms = None

    def get_item(self, product_id, timeout_s):
        return self._call("GET", f"/items/{product_id}", None, timeout_s)

    def reserve(self, product_id, quantity, timeout_s):
        body = json.dumps({"product_id": product_id, "quantity": quantity})
        return self._call("POST", "/reserve", body, timeout_s)

    def _signed_headers(self, method, url, body):
        request = AWSRequest(method=method, url=url, data=body,
                             headers={"Content-Type": "application/json"})
        SigV4Auth(self.credentials, "lambda", self.region).add_auth(request)
        return dict(request.headers.items())

    def _call(self, method, path, body, timeout_s):
        url = self.base_url + path
        headers = self._signed_headers(method, url, body)
        started = time.monotonic()
        try:
            response = self.http.request(method, url, body=body, headers=headers,
                                         timeout=urllib3.Timeout(total=timeout_s),
                                         retries=False)
        except urllib3.exceptions.TimeoutError as err:
            raise DependencyTimeout(f"{method} {path} timed out after {timeout_s}s") from err
        except urllib3.exceptions.HTTPError as err:
            raise DependencyServerError(f"{method} {path} failed: {err}") from err
        finally:
            self.last_latency_ms = (time.monotonic() - started) * 1000

        if response.status >= 500:
            raise DependencyServerError(f"{method} {path} returned {response.status}",
                                        status=response.status)
        if response.status >= 400:
            raise DependencyClientError(f"{method} {path} returned {response.status}",
                                        status=response.status)
        return json.loads(response.data)

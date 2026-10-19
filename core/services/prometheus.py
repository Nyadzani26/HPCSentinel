"""Read-only client for the Prometheus HTTP API (NFR-05, BR-12, design decision D-2).

Every request this module sends is an HTTP GET to a query endpoint. Nothing here can
change the monitored environment.
"""
from urllib.parse import urlparse

import requests


class PrometheusError(Exception):
    """Prometheus could not be reached or returned an error."""


class PrometheusClient:
    def __init__(self, base_url: str, timeout: float = 20.0):
        parsed = urlparse(base_url or "")
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise PrometheusError(f"The Prometheus address '{base_url}' is not a valid http or https address.")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def is_reachable(self) -> bool:
        try:
            response = requests.get(f"{self.base_url}/-/ready", timeout=self.timeout)
            return response.status_code == 200
        except requests.RequestException:
            return False

    def _get(self, path: str, params: dict):
        try:
            response = requests.get(f"{self.base_url}{path}", params=params, timeout=self.timeout)
            response.raise_for_status()
            body = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise PrometheusError(f"Request to Prometheus failed: {exc}") from exc
        if body.get("status") != "success":
            raise PrometheusError(f"Prometheus reported an error: {body.get('error', 'unknown error')}")
        return body["data"]

    def query_range(self, query: str, start: float, end: float, step: int, label: str = "instance"):
        """Return {label_value: [(timestamp, value), ...]} for a range query."""
        data = self._get("/api/v1/query_range", {"query": query, "start": start, "end": end, "step": step})
        series = {}
        for item in data.get("result", []):
            key = item["metric"].get(label)
            if key is None:
                continue
            series[key] = [(float(ts), float(val)) for ts, val in item["values"]]
        return series

    def query_instant(self, query: str, label: str = "instance"):
        """Return {label_value: value} for an instant query."""
        data = self._get("/api/v1/query", {"query": query})
        return {
            item["metric"][label]: float(item["value"][1])
            for item in data.get("result", [])
            if label in item["metric"]
        }

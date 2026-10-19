"""The Prometheus client is read-only and fails cleanly (NFR-05, NFR-08)."""
from unittest import mock

import requests
from django.test import SimpleTestCase

from core.services.prometheus import PrometheusClient, PrometheusError


def response(status=200, body=None):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = body or {}
    r.raise_for_status.side_effect = None if status < 400 else requests.HTTPError("bad")
    return r


class PrometheusClientTests(SimpleTestCase):
    def test_invalid_address_is_rejected(self):
        for bad in ("", "ftp://host", "localhost:9090", "http://"):
            with self.assertRaises(PrometheusError):
                PrometheusClient(bad)

    def test_reachable_and_unreachable(self):
        client = PrometheusClient("http://prom:9090/")
        with mock.patch("requests.get", return_value=response(200)):
            self.assertTrue(client.is_reachable())
        with mock.patch("requests.get", side_effect=requests.ConnectionError()):
            self.assertFalse(client.is_reachable())
        with mock.patch("requests.get", return_value=response(503)):
            self.assertFalse(client.is_reachable())

    def test_query_range_groups_series_by_label(self):
        body = {"status": "success", "data": {"result": [
            {"metric": {"instance": "a:9100"}, "values": [[1, "0.5"], [2, "0.6"]]},
            {"metric": {"instance": "b:9100"}, "values": [[1, "0.1"]]},
            {"metric": {}, "values": [[1, "9"]]},
        ]}}
        client = PrometheusClient("http://prom:9090")
        with mock.patch("requests.get", return_value=response(200, body)) as get:
            series = client.query_range("up", 0, 10, 15)
        self.assertEqual(series["a:9100"], [(1.0, 0.5), (2.0, 0.6)])
        self.assertEqual(set(series), {"a:9100", "b:9100"})
        self.assertEqual(get.call_args.args[0], "http://prom:9090/api/v1/query_range")

    def test_error_status_and_network_failure_raise(self):
        client = PrometheusClient("http://prom:9090")
        with mock.patch("requests.get", return_value=response(200, {"status": "error", "error": "bad query"})):
            with self.assertRaisesRegex(PrometheusError, "bad query"):
                client.query_instant("x")
        with mock.patch("requests.get", side_effect=requests.Timeout()):
            with self.assertRaises(PrometheusError):
                client.query_range("x", 0, 1, 15)

    def test_only_get_requests_are_ever_sent(self):  # NFR-05, BR-12
        client = PrometheusClient("http://prom:9090")
        with mock.patch("requests.get", return_value=response(200, {"status": "success", "data": {"result": []}})), \
             mock.patch("requests.post") as post, mock.patch("requests.put") as put, mock.patch("requests.delete") as delete:
            client.is_reachable()
            client.query_instant("up")
            client.query_range("up", 0, 1, 15)
        for method in (post, put, delete):
            method.assert_not_called()

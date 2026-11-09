import json
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from workbench.demo import create_demo
from workbench.models import test_connection as connection_probe
from workbench.network import network_error, opener
from workbench.review import adopt_page
from workbench.storage import Store


class BatchReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "tasks")
        task = create_demo(self.store)["task"]
        self.analysis = self.store.read(task, "analysis.json")
        self.runs = self.store.read(task, "suggestions.json")
        self.ids = [i["id"] for i in self.analysis["items"]]
        self.assertGreater(len(self.ids), 8)

    def test_current_page_fills_all_eight_even_if_only_first_was_filled(self):
        review = {self.ids[0]: {"text": "手动修改后的模块名称", "use": False, "runId": "manual"}}
        result = adopt_page(self.analysis, self.runs, review, self.ids[:8])
        self.assertEqual(result["adopted"], 8)
        self.assertFalse(result["skipped"])
        self.assertEqual(result["review"][self.ids[0]]["text"], "手动修改后的模块名称")
        for identifier in self.ids[:8]:
            self.assertTrue(result["review"][identifier]["text"])
            self.assertTrue(result["review"][identifier]["use"])
        self.assertFalse(set(self.ids[8:]) & result["review"].keys())

    def test_adopt_all_covers_other_pages_and_is_idempotent(self):
        page = adopt_page(self.analysis, self.runs, {}, self.ids[:8])
        result = adopt_page(self.analysis, self.runs, page["review"], self.ids)
        self.assertEqual(result["adopted"], len(self.ids))
        self.assertEqual(result["newlyAdopted"], len(self.ids) - 8)
        again = adopt_page(self.analysis, self.runs, result["review"], self.ids)
        self.assertEqual(again["newlyAdopted"], 0)
        self.assertEqual(again["review"], result["review"])

    def test_different_model_suggestions_need_explicit_source(self):
        row = {"id": self.ids[0], "suggestion": "另一种模块名称", "reason": "测试"}
        self.runs.append({"id": "other", "items": [row]})
        skipped = adopt_page(self.analysis, self.runs, {}, self.ids[:1])
        self.assertEqual(skipped["adopted"], 0)
        self.assertIn("多个不同", skipped["skipped"][0]["reason"])
        explicit = adopt_page(self.analysis, self.runs, {}, self.ids[:1], "other")
        self.assertEqual(explicit["review"][self.ids[0]]["text"], "另一种模块名称")

    def test_identical_suggestions_from_multiple_models_can_be_adopted(self):
        row = dict(self.runs[0]["items"][0])
        self.runs.append({"id": "same", "items": [row]})
        result = adopt_page(self.analysis, self.runs, {}, self.ids[:1])
        self.assertEqual(result["adopted"], 1)

    def test_missing_or_invalid_suggestion_does_not_block_valid_items(self):
        # An invalid imported draft is retained, not silently overwritten.
        review = {self.ids[1]: {"text": "有换行的\nSwagger 摘要", "use": False}}
        self.runs[0]["items"] = [row for row in self.runs[0]["items"] if row["id"] != self.ids[2]]
        result = adopt_page(self.analysis, self.runs, review, self.ids)
        self.assertEqual(result["adopted"], len(self.ids) - 2)
        self.assertEqual(len(result["skipped"]), 2)
        self.assertFalse(result["review"][self.ids[1]]["use"])

    def test_unknown_ids_rejected_before_any_adoption(self):
        with self.assertRaises(ValueError):
            adopt_page(self.analysis, self.runs, {}, [self.ids[0], "unknown"])


class NetworkDiagnosticTests(unittest.TestCase):
    def test_distinguishes_dns_tls_proxy_refused_and_timeout(self):
        cases = [(socket.gaierror(-2, "private detail"), "DNS"),
                 (ssl.SSLCertVerificationError(1, "private detail"), "证书"),
                 (ConnectionRefusedError(10061, "private detail"), "拒绝"),
                 (TimeoutError("private detail"), "超时")]
        for error, expected in cases:
            message = network_error(urllib.error.URLError(error), {"proxyMode": "direct"})
            self.assertIn(expected, message)
            self.assertNotIn("private detail", message)

    def test_tls_verification_is_enabled_with_bundled_certificates(self):
        from urllib.request import HTTPSHandler
        client = opener({"proxyMode": "direct"})
        handler = next(h for h in client.handlers if isinstance(h, HTTPSHandler))
        self.assertEqual(handler._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(handler._context.check_hostname)
        self.assertTrue(handler._context.get_ca_certs())

    def test_probe_reads_models_without_prompt_and_redacts_key(self):
        calls = []
        class Mock(BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append((self.path, self.headers.get("Authorization"), self.headers.get("Content-Length")))
                body = json.dumps({"data": [{"id": "model-test"}, {"id": "fake-secret-key"}]}).encode()
                self.send_response(200);self.send_header("Content-Length", str(len(body)));self.end_headers();self.wfile.write(body)
            def log_message(self, *_): pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        result = connection_probe({"baseUrl": f"http://127.0.0.1:{server.server_port}/v1", "provider": "compatible", "model": "model-test", "proxyMode": "direct"}, "fake-secret-key")
        self.assertTrue(result["success"])
        self.assertTrue(result["modelListed"])
        self.assertEqual(calls, [("/v1/models", "Bearer fake-secret-key", None)])
        self.assertNotIn("fake-secret-key", json.dumps(result))

    def test_dns_failure_returns_exportable_diagnostic(self):
        with patch("workbench.models.opener") as mocked:
            mocked.return_value.open.side_effect = urllib.error.URLError(socket.gaierror(-2, "host not found"))
            result = connection_probe({"provider": "compatible", "baseUrl": "https://example.invalid", "model": "test"}, "fake-secret-key")
        self.assertFalse(result["success"])
        self.assertIn("DNS", result["message"])
        self.assertNotIn("fake-secret-key", json.dumps(result))


if __name__ == "__main__":
    unittest.main()

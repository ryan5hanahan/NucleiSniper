"""Local-Kev checks against a fake System One server. Run: python3 -m unittest test_kev"""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import NucleiSniper as ns


class FakeKev(BaseHTTPRequestHandler):
    max_questions = 1000
    seen: list[dict] = []

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeKev.seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "n": len(payload["questions"])})
        if len(payload["questions"]) > FakeKev.max_questions:
            self._reply(422, {"detail": "state exceeds 65536 tokens"})
            return
        answers = {key: {"type": "score", "score": 0.5, "confidence": 0.9, "probabilities": {}} for key in payload["questions"]}
        self._reply(200, {"answers": answers, "usage": {"input_tokens": 10, "output_tokens": 1}})

    def _reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def template(i):
    return ns.TemplateSummary(f"t{i}", f"id-{i}", f"name {i}", "", "info", [], ["http"], {}, [], [], f"/t/{i}.yaml", "")


class KevTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeKev)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.endpoint = f"http://127.0.0.1:{cls.server.server_port}/v1/systemone"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeKev.seen = []
        FakeKev.max_questions = 1000

    def score(self, batch):
        ranked, usage, _ = ns.evaluate_batch(1, 1, {}, batch, "kev-4b", None, 5, 0, quiet=True, endpoint=self.endpoint)
        return ranked, usage

    def test_posts_to_endpoint_without_auth(self):
        ranked, _ = self.score([template(i) for i in range(3)])
        self.assertEqual(len(ranked), 3)
        self.assertEqual(FakeKev.seen, [{"path": "/v1/systemone", "auth": None, "n": 3}])

    def test_422_token_error_splits_batch(self):
        FakeKev.max_questions = 2
        batch = [template(i) for i in range(5)]
        ranked, usage = self.score(batch)
        self.assertEqual(sorted(r.file_path for r in ranked), sorted(t.file_path for t in batch))
        self.assertEqual(FakeKev.seen[0]["n"], 5)
        self.assertEqual(usage["input_tokens"], 10 * sum(1 for s in FakeKev.seen if s["n"] <= 2))


if __name__ == "__main__":
    unittest.main()

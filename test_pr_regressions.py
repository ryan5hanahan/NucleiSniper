"""Regression checks for endpoint-isolated resumes and the Kev eval harness."""

import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import NucleiSniper as ns


class ResumeTest(unittest.TestCase):
    def test_cli_resumes_only_matching_endpoint_and_model(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            (root / "test.yaml").write_text(
                'id: test\ninfo:\n  name: Test\n  severity: info\n'
                'http:\n  - method: GET\n    path: ["{{BaseURL}}/"]\n'
            )
            url = "http://fixture.test/"
            local = "http://127.0.0.1:8009/v1/systemone"
            target = {"status_code": 200, "final_url": url, "detected_technologies": []}
            posted = []

            def post(endpoint, headers, json, timeout):
                posted.append((endpoint, json["model"]))
                response = ns.requests.Response()
                response.status_code = 200
                score = 0.25 if endpoint == ns.TYPESAFE_ENDPOINT else 3.5
                response._content = __import__("json").dumps({
                    "answers": {key: {"type": "score", "score": score, "confidence": 0.9}
                                for key in json["questions"]},
                    "usage": {"input_tokens": 10, "output_tokens": 1},
                }).encode()
                return response

            argv = ["NucleiSniper.py", url, "-t", str(root), "--no-scan",
                    "--no-prefilter", "--resume", "--skip-version-check",
                    "-o", str(root / "report.json")]
            with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}, clear=True), \
                    patch.object(ns, "profile_url_job", return_value=(url, target, None, 0.0)), \
                    patch.object(ns.requests, "post", side_effect=post), \
                    patch.object(ns, "_tqdm", None), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                for endpoint, model, resumed, score in [
                    (ns.TYPESAFE_ENDPOINT, "jev-latest", 0, 0.25),
                    (local, "jev-latest", 0, 3.5),
                    (ns.TYPESAFE_ENDPOINT, "jev-latest", 1, 0.25),
                    (local, "jev-latest", 1, 3.5),
                    (local, "kev-4b", 0, 3.5),
                ]:
                    with self.subTest(endpoint=endpoint, model=model, resumed=resumed), \
                            patch.object(sys, "argv", argv + ["--endpoint", endpoint, "--model", model]):
                        self.assertEqual(ns.main(), 0)
                        row = json.loads((root / "report.json").read_text())["results"][0]
                        self.assertEqual(row["resumed_count"], resumed)
                        self.assertEqual(row["all_results"][0]["score"], score)
            self.assertEqual(posted, [(ns.TYPESAFE_ENDPOINT, "jev-latest"),
                                      (local, "jev-latest"), (local, "kev-4b")])

    def test_legacy_scores_are_preserved_only_for_hosted_endpoint(self):
        with sqlite3.connect(":memory:") as conn:
            conn.execute("""
                CREATE TABLE scores (
                    url TEXT, model TEXT, file_path TEXT, template_id TEXT,
                    name TEXT, score REAL, confidence REAL, probabilities TEXT,
                    severity TEXT, tags TEXT, PRIMARY KEY (url, model, file_path)
                )
            """)
            conn.execute("INSERT INTO scores VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         ("http://fixture.test/", "jev-latest", "/test.yaml", "test",
                          "Test", 0.25, 0.9, "{}", "info", "[]"))
            conn.commit()
            ns.ensure_scores(conn)
            ns.ensure_scores(conn)
            self.assertEqual(ns.cached_paths(conn, "http://fixture.test/", "jev-latest"), {"/test.yaml"})
            self.assertEqual(ns.load_ranked(conn, "http://fixture.test/", "jev-latest")[0].score, 0.25)
            self.assertEqual(ns.cached_paths(conn, "http://fixture.test/", "jev-latest",
                                             "http://127.0.0.1:8009/v1/systemone"), set())


class EvalReadinessTest(unittest.TestCase):
    def run_eval(self, key, ready):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            (root / "eval/templates").mkdir(parents=True)
            (root / "bin").mkdir()
            (root / "eval_kev.sh").write_text(Path(__file__).with_name("eval_kev.sh").read_text())
            scripts = {
                "uv": '#!/bin/sh\nexec /bin/sleep 30\n',
                "curl": f'#!{sys.executable}\n' + '''import json, os, sys
from pathlib import Path
Path(os.environ["PROBE_LOG"]).write_text(json.dumps(sys.argv[1:]))
expected = "Authorization: Bearer " + os.environ["KEV_API_KEY"]
authorized = expected in sys.argv if os.environ["KEV_API_KEY"] else "-H" not in sys.argv
sys.exit(0 if authorized and os.environ["PROBE_READY"] == "1" else 22)
''',
                "python-stub": f'#!{sys.executable}\n' + '''import json, os, sys, time
from pathlib import Path
if sys.argv[1:3] == ["-m", "http.server"]:
    time.sleep(30)
elif sys.argv[1] == "NucleiSniper.py":
    report = {"results": [{"batch_input_tokens": [10], "all_results": [],
                           "evaluated_count": 0, "failed_batches": 0, "batch_count": 1}]}
    Path(sys.argv[sys.argv.index("-o") + 1]).write_text(json.dumps(report))
else:
    os.execv(sys.executable, [sys.executable] + sys.argv[1:])
''',
            }
            for name, body in scripts.items():
                path = root / "bin" / name
                path.write_text(body)
                path.chmod(0o755)
            env = dict(os.environ, PATH=str(root / "bin") + ":" + os.environ["PATH"],
                       KEV_DIR=str(root), PYTHON=str(root / "bin/python-stub"),
                       ONLY="kev-4b", KEV_API_KEY=key, KEV_START_TIMEOUT="0",
                       PROBE_LOG=str(root / "curl.json"), PROBE_READY="1" if ready else "0")
            result = subprocess.run(["/bin/bash", str(root / "eval_kev.sh")], env=env,
                                    capture_output=True, text=True, timeout=5)
            return result, json.loads((root / "curl.json").read_text())

    def test_authenticated_readiness_reaches_scoring(self):
        result, args = self.run_eval("test-key", True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Authorization: Bearer test-key", args)
        self.assertIn("kev-4b-b50", result.stdout)

    def test_open_server_readiness_omits_auth(self):
        result, args = self.run_eval("", True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("-H", args)

    def test_unready_server_exits_at_deadline(self):
        result, args = self.run_eval("test-key", False)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("not ready within 0 seconds", result.stdout)
        self.assertIn("--connect-timeout", args)
        self.assertIn("--max-time", args)


if __name__ == "__main__":
    unittest.main()

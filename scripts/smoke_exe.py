"""Run the packaged artifact end-to-end via HTTP; no Python imports from the app."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def main():
    exe = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix="jwb-exe-") as folder:
        data_dir = Path(folder)
        sock = socket.socket();sock.bind(("127.0.0.1", 0));port = sock.getsockname()[1];sock.close()
        # The artifact should work without Python or this venv on PATH.
        git_exe = shutil.which("git")
        if not git_exe:
            raise SystemExit("Git required for packaged end-to-end smoke test")
        env = {**os.environ, "PATH": str(Path(git_exe).parent) + os.pathsep + str(Path(os.environ["SystemRoot"]) / "System32")}
        process = subprocess.Popen([str(exe), "--no-browser", "--port", str(port), "--data-dir", str(data_dir)], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        url = f"http://127.0.0.1:{port}"
        token = None
        try:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError("EXE exited early: " + process.stdout.read().decode(errors="replace"))
                try:
                    with urllib.request.urlopen(url, timeout=1) as response:
                        page = response.read().decode()
                    token = re.search(r'window.WORKBENCH_TOKEN="([^"]+)"', page).group(1)
                    break
                except (OSError, AttributeError):
                    time.sleep(.2)
            if not token:
                raise RuntimeError("EXE did not start its local server")

            def request(path, body=None, binary=False):
                headers = {"X-Workbench-Token": token}
                if body is not None:
                    headers["Content-Type"] = "application/json"
                req = urllib.request.Request(url + path, headers=headers, data=json.dumps(body).encode() if body is not None else None)
                with urllib.request.urlopen(req, timeout=30) as response:
                    return response.read() if binary else json.load(response)

            with urllib.request.urlopen(url + "/app.js", timeout=5) as response:
                assert b"renderReview" in response.read()
            task = request("/api/demo", {})["task"]
            view = request("/api/task?task=" + task)
            assert view["analysis"]["items"] and view["runs"]
            rows = {row["id"]: {"text": row["suggestion"], "use": True} for row in view["runs"][0]["items"]}
            identifiers = list(rows)
            current_page = request("/api/adopt-page", {"task": task, "itemIds": identifiers[:8]})
            assert current_page["adopted"] == 8 and not current_page["skipped"]
            all_items = request("/api/adopt-page", {"task": task, "itemIds": identifiers})
            assert all_items["adopted"] == len(rows) and all_items["newlyAdopted"] == len(rows) - 8
            class ModelList(BaseHTTPRequestHandler):
                def do_GET(self):
                    body = b'{"data":[{"id":"smoke-model"}]}'
                    self.send_response(200);self.send_header("Content-Length", str(len(body)));self.end_headers();self.wfile.write(body)
                def log_message(self, *_): pass
            mock = ThreadingHTTPServer(("127.0.0.1", 0), ModelList)
            threading.Thread(target=mock.serve_forever, daemon=True).start()
            try:
                probe = request("/api/test-connection", {"profile": {"provider": "compatible", "baseUrl": f"http://127.0.0.1:{mock.server_port}", "model": "smoke-model", "proxyMode": "direct"}, "apiKey": "fake-key"})
                for _ in range(100):
                    progress = request("/api/job?id=" + probe["job"])
                    if progress["status"] != "running":
                        break
                    time.sleep(.1)
                assert progress["status"] == "done" and progress["result"]["success"], progress
                assert "fake-key" not in json.dumps(progress)
            finally:
                mock.shutdown();mock.server_close()
            root = view["local"]["projectRoot"]
            note = Path(root) / "preexisting-notes.txt"
            note.write_bytes(b"preserve existing work")
            preview = request("/api/preview", {"task": task, "projectRoot": root})
            assert not preview["errors"] and not preview["git"]["ready"], preview
            receipt = request("/api/apply", {"task": task, "token": preview["token"], "acknowledgeGitWarning": True})
            assert "@Schema" in receipt["gitDiff"] and "/**" in receipt["gitDiff"]
            assert "@Schema" in receipt["toolDiff"] and note.read_bytes() == b"preserve existing work"
            bundle = request("/api/export?task=" + task, binary=True)
            imported = request("/api/import", {"filename": "task.zip", "data": base64.b64encode(bundle).decode()})["task"]
            imported_view = request("/api/task?task=" + imported)
            assert not imported_view["local"]["projectRoot"]
            assert imported_view["review"] == request("/api/task?task=" + task)["review"]
            result = request("/api/restore", {"task": task, "run": receipt["id"]})
            assert result["restored"] == len(receipt["files"])
            status_after_restore = request("/api/git-status", {"projectRoot": root})
            assert status_after_restore["changes"] == preview["git"]["changes"]
            assert note.read_bytes() == b"preserve existing work"
            # Verify the shared data directory cannot be opened by a second EXE instance.
            second = subprocess.run([str(exe), "--no-browser", "--port", str(port+1), "--data-dir", str(data_dir)], env=env, capture_output=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            assert second.returncode != 0, "Second instance unexpectedly opened the same data directory"
            print(json.dumps({"exe": str(exe), "status": "passed", "scannedItems": len(rows),
                              "modifiedFiles": len(receipt["files"]), "bundleBytes": len(bundle),
                              "withoutPythonOnPath": True,
                              "checks": ["startup", "static assets", "AST scan", "current-page adoption", "all-item adoption", "bundled CA and connection diagnostic", "Git warning acknowledged", "dirty worktree apply", "real Git diff and tool diff", "bundle import/export", "restore preserves original dirty files", "single-instance lock"]}, ensure_ascii=False, indent=2))
        finally:
            # Stop the onefile child as well as its launcher before cleaning its data.
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            process.wait(timeout=15)
            if process.stdout:
                process.stdout.close()


if __name__ == "__main__":
    main()

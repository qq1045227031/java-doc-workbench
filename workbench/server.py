from __future__ import annotations

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
import traceback
import urllib.request
from urllib.parse import parse_qs, urlsplit
import uuid

from .changes import apply_preview, build_preview, git_status, preview_signature, restore
from .demo import create_demo
from .java_source import digest
from .models import SYSTEM, call_model, endpoint, validate_suggestion, test_connection
from .scanner import now, scan
from .review import adopt_page
from .storage import Store, encode, parse_review, review_markdown


class Workbench:
    def __init__(self, data_dir, static_dir):
        self.store = Store(Path(data_dir) / "tasks")
        self.static_dir = Path(static_dir)
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.jobs = {}

    def busy(self, task):
        return any(j["status"] == "running" and j.get("task") == task for j in self.jobs.values())

    def start(self, task, function):
        with self.lock:
            if task and self.busy(task):
                raise ValueError("该任务正在生成中，请等待完成或停止后再操作")
            identifier = uuid.uuid4().hex
            job = {"id": identifier, "task": task, "status": "running", "message": "准备中", "completed": 0, "total": 0, "cancel": False}
            self.jobs[identifier] = job

        def worker():
            try:
                result = function(job)
                with self.lock:
                    job.update(status="cancelled" if job["cancel"] else "done", result=result, message="已停止，已完成结果已保存" if job["cancel"] else "处理完成")
            except Exception as exc:
                # Error messages from model calls are sanitized in models.py.
                with self.lock:
                    job.update(status="error", error=str(exc), message="处理未完成；已完成批次保留，可重试")
        threading.Thread(target=worker, daemon=True).start()
        return {"job": identifier}

    def get_task(self, task):
        analysis = self.store.read(task, "analysis.json")
        local = self.store.read(task, "local.json", {})
        receipts = []
        for file in sorted((self.store.path(task) / "backups").glob("*/receipt.json")):
            receipt = json.loads(file.read_text(encoding="utf-8"))
            receipts.append({k: receipt[k] for k in ("id", "status", "createdAt", "head")})
        return {"id": task, "analysis": analysis, "local": local, "runs": self.store.read(task, "suggestions.json", []),
                "review": self.store.read(task, "review.json", {}), "receipts": receipts}

    def translate(self, task, profile, api_key, item_ids=None):
        safe_profile = {key: profile.get(key) for key in ("provider", "baseUrl", "model", "jsonMode")}
        endpoint(safe_profile.get("baseUrl") or "", safe_profile.get("provider"))
        batch_size = int(profile.get("batchSize", 5))
        if not 1 <= batch_size <= 20:
            raise ValueError("每批数量必须在 1～20 之间")
        fingerprint = digest(encode({"profile": safe_profile, "prompt": SYSTEM}))[:24]
        # Network settings do not change generated-content identity, preserving resume.
        safe_profile.update(proxyMode=profile.get("proxyMode", "system"), timeoutSeconds=profile.get("timeoutSeconds", 180))
        analysis = self.store.read(task, "analysis.json")
        allowed = {i["id"] for i in analysis["items"]}
        if item_ids is not None and (not isinstance(item_ids, list) or set(item_ids) - allowed):
            raise ValueError("生成范围包含未知 id")

        def work(job):
            with self.lock:
                runs = self.store.read(task, "suggestions.json", [])
                run = next((r for r in runs if r.get("id") == fingerprint), None)
                if run is None:
                    run = {"id": fingerprint, "profile": safe_profile, "createdAt": now(), "items": [], "usage": [], "errors": []}
                    runs.append(run)
                done = {r["id"] for r in run["items"]}
                items = [i for i in analysis["items"] if i["id"] not in done and (item_ids is None or i["id"] in item_ids)]
                job.update(total=len(items), completed=0)
            for index in range(0, len(items), batch_size):
                if job["cancel"]:
                    break
                batch = items[index:index + batch_size]
                with self.lock:
                    job["message"] = f"正在生成 {index + 1}～{min(index + batch_size, len(items))} / {len(items)}"
                try:
                    suggestions, usage = call_model(batch, safe_profile, api_key)
                except ValueError as exc:
                    with self.lock:
                        run["errors"].append({"at": now(), "ids": [i["id"] for i in batch], "message": str(exc)})
                        self.store.write(task, "suggestions.json", runs)
                    raise
                with self.lock:
                    run["items"].extend(suggestions)
                    run["usage"].append(usage)
                    self.store.write(task, "suggestions.json", runs)
                    job["completed"] = index + len(batch)
            return {"task": task, "run": fingerprint}
        return self.start(task, work)


def make_handler(app: Workbench):
    class Handler(BaseHTTPRequestHandler):
        server_version = "JavaWorkbench/1.0"

        def log_message(self, fmt, *args):
            pass  # Keys and request bodies must never enter access logs.

        def send_bytes(self, data, content_type="application/json; charset=utf-8", filename=None):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'nonce-" + app.token + "'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(data)

        def error(self, exc, status=400):
            body = encode({"error": str(exc)})
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def local_request(self):
            host = self.headers.get("Host", "")
            expected = f"127.0.0.1:{self.server.server_port}"
            if host != expected:
                raise ValueError("请使用程序提供的 127.0.0.1 本机地址")
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + expected:
                raise ValueError("不接受其他网页发起的请求")

        def do_GET(self):
            try:
                self.local_request()
                url = urlsplit(self.path)
                query = parse_qs(url.query)
                task = query.get("task", [""])[0]
                if url.path == "/":
                    html = (app.static_dir / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", app.token)
                    return self.send_bytes(html.encode(), "text/html; charset=utf-8")
                if url.path in {"/app.js", "/style.css"}:
                    return self.send_bytes((app.static_dir / url.path[1:]).read_bytes(), "text/javascript; charset=utf-8" if url.path.endswith("js") else "text/css; charset=utf-8")
                if self.headers.get("X-Workbench-Token") != app.token:
                    raise ValueError("会话已失效，请刷新页面")
                with app.lock:
                    if url.path == "/api/info":
                        result = {"tasks": app.store.list(), "dataDir": str(app.store.directory.parent), "version": "1.1.1", "jobs": [dict(j) for j in app.jobs.values() if j["status"] == "running"]}
                    elif url.path == "/api/task":
                        result = app.get_task(task)
                    elif url.path == "/api/job":
                        identifier = query.get("id", [""])[0]
                        if identifier not in app.jobs:
                            raise ValueError("任务进度不存在，请重新加载任务")
                        result = dict(app.jobs[identifier])
                    elif url.path == "/api/export":
                        return self.send_bytes(app.store.export(task), "application/zip", "java-description-task.zip")
                    elif url.path == "/api/download":
                        name = query.get("name", [""])[0]
                        if name == "review.md":
                            data = review_markdown(app.store.read(task, "analysis.json"), app.store.read(task, "review.json", {})).encode()
                        elif name in {"suggestions.json", "review.json"}:
                            data = encode(app.store.stage_document(task, name.split(".")[0]))
                        elif name == "analysis.json":
                            data = encode(app.store.read(task, name))
                        elif name == "changes.diff":
                            preview = app.store.read(task, "preview.json", {})
                            data = "\n".join(f["diff"] for f in preview.get("files", [])).encode()
                        elif name in {"git.diff", "tool.diff"}:
                            run = query.get("run", [""])[0]
                            if len(run) != 32 or any(c not in "0123456789abcdef" for c in run):
                                raise ValueError("变更编号无效")
                            folder = app.store.path(task) / "backups" / run
                            if name == "tool.diff" and not (folder / name).exists():
                                receipt = json.loads((folder / "receipt.json").read_text(encoding="utf-8"))
                                data = "\n".join(f.get("diff", "") for f in receipt["files"]).encode("utf-8")
                            else:
                                data = (folder / name).read_bytes()
                        else:
                            raise ValueError("不支持下载此文件")
                        return self.send_bytes(data, "application/octet-stream", name)
                    else:
                        return self.error("页面不存在", 404)
                self.send_bytes(encode(result))
            except (ValueError, OSError, KeyError, TypeError) as exc:
                self.error(exc)

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 115_000_000:
                    raise ValueError("请求为空或超过大小上限")
                # Consume the bounded request body before rejecting its origin/token.
                # Closing Windows sockets with unread POST data can reset the connection
                # before a caller receives the useful error response.
                raw = self.rfile.read(length)
                self.local_request()
                if self.headers.get("X-Workbench-Token") != app.token:
                    raise ValueError("会话已失效，请刷新页面")
                body = json.loads(raw)
                route = urlsplit(self.path).path
                task = body.get("task", "")
                with app.lock:
                    if route not in {"/api/translate", "/api/cancel", "/api/git-status", "/api/scan", "/api/import"} and task and app.busy(task):
                        raise ValueError("正在生成，请停止或等待完成后编辑")
                    if route == "/api/demo":
                        result = create_demo(app.store)
                    elif route == "/api/scan":
                        def work(job):
                            spec = body.get("spec")
                            source = body.get("source", "").strip()
                            if source:
                                request = urllib.request.Request(source, headers={"Accept": "application/json"})
                                if urlsplit(source).scheme not in {"http", "https"}:
                                    raise ValueError("OpenAPI 地址必须以 http:// 或 https:// 开头")
                                with urllib.request.urlopen(request, timeout=30) as response:
                                    raw = response.read(40_000_001)
                                if len(raw) > 40_000_000:
                                    raise ValueError("OpenAPI 文件超过 40MB")
                                spec = json.loads(raw.decode("utf-8-sig"))
                            if spec is not None and (not isinstance(spec, dict) or not spec.get("openapi")):
                                raise ValueError("仅支持 OpenAPI 3 JSON，请检查文件或地址")
                            job["message"] = "正在分析 Java 源码"
                            analysis = scan(body.get("projectRoot", ""), body.get("roots", []), body.get("modes", []), spec, bool(body.get("includeSnippets", True)))
                            with app.lock:
                                identifier = app.store.create(analysis, body.get("title") or "Java 说明任务", body.get("projectRoot", ""))
                            return {"task": identifier}
                        result = app.start(None, work)
                    elif route == "/api/import":
                        raw = base64.b64decode(body["data"], validate=True)
                        if body.get("filename", "").lower().endswith(".json"):
                            document = json.loads(raw.decode("utf-8-sig"))
                            if isinstance(document, dict) and document.get("kind") in {"suggestions", "review"}:
                                if not task:
                                    raise ValueError("请先选择或导入配套分析任务，再导入建议/审核 JSON")
                                if app.busy(task):
                                    raise ValueError("请等待当前任务生成完成再导入")
                                result = {"task": app.store.import_stage(task, document)}
                            else:
                                result = {"task": app.store.import_bundle(raw, body.get("filename", ""))}
                        else:
                            result = {"task": app.store.import_bundle(raw, body.get("filename", ""))}
                    elif route == "/api/test-connection":
                        def probe(job):
                            job["message"] = "正在测试模型列表接口（不发送源码）"
                            return test_connection(body["profile"], body.get("apiKey", ""))
                        result = app.start(None, probe)
                    elif route == "/api/translate":
                        result = app.translate(task, body["profile"], body.get("apiKey", ""), body.get("itemIds"))
                    elif route == "/api/cancel":
                        job = app.jobs.get(body.get("job"))
                        if not job:
                            raise ValueError("生成任务不存在")
                        job["cancel"] = True
                        result = {"message": "将在当前请求结束后停止，已完成批次保留"}
                    elif route == "/api/adopt-page":
                        result = adopt_page(app.store.read(task, "analysis.json"),
                            app.store.read(task, "suggestions.json", []), app.store.read(task, "review.json", {}),
                            body.get("itemIds"), body.get("sourceRun", "auto"))
                        app.store.write(task, "review.json", result["review"])
                    elif route == "/api/review":
                        analysis = app.store.read(task, "analysis.json")
                        by_id = {i["id"]: i for i in analysis["items"]}
                        rows = body.get("rows")
                        if not isinstance(rows, dict) or set(rows) - by_id.keys():
                            raise ValueError("审核项包含未知 id")
                        review = app.store.read(task, "review.json", {})
                        for identifier, row in rows.items():
                            if not isinstance(row, dict) or type(row.get("use")) is not bool or not isinstance(row.get("text"), str):
                                raise ValueError("审核项格式不正确")
                            if row["use"]:
                                validate_suggestion(by_id[identifier], row["text"])
                            review[identifier] = {"text": row["text"][:12000], "use": row["use"], "runId": str(row.get("runId", "manual"))[:100]}
                        app.store.write(task, "review.json", review)
                        result = {"saved": len(rows)}
                    elif route == "/api/import-review":
                        analysis = app.store.read(task, "analysis.json")
                        rows = parse_review(body["content"], analysis)
                        by_id = {i["id"]: i for i in analysis["items"]}
                        for identifier, row in rows.items():
                            if row["use"]:
                                validate_suggestion(by_id[identifier], row["text"])
                        app.store.write(task, "review.json", rows)
                        result = {"saved": len(rows)}
                    elif route == "/api/git-status":
                        result = git_status(body.get("projectRoot", ""))
                    elif route == "/api/preview":
                        analysis = app.store.read(task, "analysis.json")
                        review = app.store.read(task, "review.json", {})
                        root = body.get("projectRoot", "")
                        files, errors = build_preview(analysis, review, root)
                        status = git_status(root)
                        public_files = [{k: v for k, v in f.items() if not k.startswith("_")} for f in files]
                        result = {"token": preview_signature(files, review, root), "files": public_files, "errors": errors,
                                  "git": status, "projectRoot": str(Path(root).resolve())}
                        app.store.write(task, "preview.json", result)
                        local = app.store.read(task, "local.json", {})
                        local["projectRoot"] = root
                        app.store.write(task, "local.json", local)
                    elif route == "/api/apply":
                        preview = app.store.read(task, "preview.json", {})
                        if not preview or preview.get("token") != body.get("token"):
                            raise ValueError("请先生成并查看本次差异预览")
                        analysis = app.store.read(task, "analysis.json")
                        review = app.store.read(task, "review.json", {})
                        root = preview["projectRoot"]
                        files, errors = build_preview(analysis, review, root)
                        if errors or preview_signature(files, review, root) != preview["token"]:
                            raise ValueError("审核结果或源码已变化，请重新预览并处理冲突")
                        result = apply_preview(app.store.path(task), root, files, preview.get("git", {}).get("head"),
                                               body.get("acknowledgeGitWarning") is True)
                        app.store.write(task, "preview.json", {})
                    elif route == "/api/restore":
                        result = restore(app.store.path(task), body["run"])
                    else:
                        return self.error("接口不存在", 404)
                self.send_bytes(encode(result))
            except Exception as exc:
                if not isinstance(exc, (ValueError, OSError, KeyError, TypeError)):
                    traceback.print_exc()
                self.error(exc)
    return Handler


def create_server(data_dir, static_dir, port=8765):
    app = Workbench(data_dir, static_dir)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    server.daemon_threads = True
    return app, server

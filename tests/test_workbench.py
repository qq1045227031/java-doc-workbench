from __future__ import annotations

import base64
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile

from workbench.changes import apply_preview, build_preview, git, git_status, preview_signature, restore
from workbench.demo import create_demo
from workbench.java_source import JavaFile, digest, safe_path
from workbench.models import call_model, parse_response, validate_suggestion
from workbench.scanner import scan
from workbench.server import Workbench, create_server
from workbench.storage import Store, parse_review, review_markdown


class TemporaryProject(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        self.store = Store(self.base / "tasks")

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
        return path

    def baseline(self):
        git(self.root, "init")
        git(self.root, "add", "--", ".")
        git(self.root, "-c", "user.name=Workbench Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", "commit", "-m", "baseline")

    def analysis(self, modes=None, spec=None):
        return scan(str(self.root), ["."], modes or ["swagger", "javadoc"], spec)


class ParserTests(TemporaryProject):
    def test_multiline_overloads_nested_generics_and_comments(self):
        path = self.write("C.java", '''package a;
/** Existing class documentation. */
public class C {
    @GetMapping(
        value = "/one"
    )
    public String find(String id) { return "} /* not a comment */"; }
    public String find(int id) { return null; }
    public <T> T[] list(T[] input, String... names) throws java.io.IOException { return input; }
    public static class Inner { public long id; }
}''')
        jf = JavaFile("C.java", path.read_bytes())
        self.assertIn("method:a.C#find(String)", jf.declarations)
        self.assertIn("method:a.C#find(int)", jf.declarations)
        self.assertIn("field:a.C.Inner#id", jf.declarations)
        declaration = jf.declarations["method:a.C#list(T[],String...)"]
        self.assertEqual(declaration.parameter_names, ["input", "names"])
        self.assertEqual(declaration.type_parameters, ["T"])
        self.assertEqual(declaration.throws, ["java.io.IOException"])
        analysis = self.analysis(["javadoc"])
        self.assertNotIn("type:a.C", [i["selector"] for i in analysis["items"]])

    def test_duplicate_simple_class_names_skip_schema_instead_of_overwrite(self):
        self.write("a/User.java", "package a; public class User { public String name; }")
        self.write("b/User.java", "package b; public class User { public String name; }")
        spec = {"components": {"schemas": {"User": {"properties": {"name": {"type": "string"}}}}}}
        analysis = self.analysis(["swagger"], spec)
        self.assertFalse(analysis["items"])
        self.assertIn("不唯一", analysis["skipped"][0]["reason"])

    def test_inheritance_and_json_property(self):
        self.write("a/Base.java", "package a; public class Base { @JsonProperty(\"person_id\") protected String id; }")
        self.write("b/Person.java", "package b; import a.Base; public class Person extends Base { private boolean isActive; }")
        spec = {"components": {"schemas": {"Person": {"properties": {"person_id": {}, "active": {}}}}}}
        items = self.analysis(["swagger"], spec)["items"]
        self.assertEqual(len(items), 2)
        self.assertIn("field:a.Base#id", [i["selector"] for i in items])
        self.assertIn("field:b.Person#isActive", [i["selector"] for i in items])

    def test_record_and_interface_javadoc_parsed(self):
        self.write("R.java", "package x; public record R(String id) { public String normalized() { return id; } }")
        self.write("Api.java", "package x; public interface Api { String get(String id); }")
        items = self.analysis(["javadoc"])["items"]
        self.assertIn("type:x.R", [i["selector"] for i in items])
        self.assertIn("method:x.Api#get(String)", [i["selector"] for i in items])

    def test_field_javadoc_has_no_return_tag(self):
        self.write("C.java", "public class C { private String name; }")
        item = next(i for i in self.analysis(["javadoc"])["items"] if i["selector"].startswith("field:"))
        self.assertEqual(item["context"]["returns"], "")
        validate_suggestion(item, "姓名")
        with self.assertRaises(ValueError):
            validate_suggestion(item, "姓名\n@return 姓名")

    def test_invalid_utf8_and_java_are_reported(self):
        self.write("Bad.java", b"\xff public class Bad {}")
        self.write("Broken.java", "public class Broken { void method( }")
        self.write("Good.java", "public class Good {}")
        analysis = self.analysis(["javadoc"])
        self.assertEqual(analysis["scannedFiles"], 1)
        self.assertEqual(len(analysis["warnings"]), 2)

    def test_existing_swagger_values_and_multifield_preserved(self):
        self.write("C.java", '''package x;
import io.swagger.v3.oas.annotations.media.Schema;
public class C {
    @Schema(description = "现有说明") private String existing;
    private String first, second;
}''')
        spec = {"components": {"schemas": {"C": {"properties": {"existing": {}, "first": {}, "second": {}}}}}}
        analysis = self.analysis(["swagger"], spec)
        self.assertFalse(analysis["items"])
        self.assertEqual(len(analysis["skipped"]), 2)

    def test_overloaded_controller_and_multiline_annotations(self):
        self.write("C.java", '''package x;
@RestController public class C {
    @GetMapping(
        path = "/a"
    )
    public String find(String id) { return id; }
    @PostMapping("/b") public String find(int id) { return ""; }
}''')
        items = self.analysis(["swagger"])["items"]
        self.assertEqual(len(items), 3)
        self.assertEqual(len({i["id"] for i in items}), 3)


class EditTests(TemporaryProject):
    def test_combined_javadoc_and_swagger_default_package_bom_crlf(self):
        self.write("C.java", b'\xef\xbb\xbf@RestController\r\npublic class C {\r\n    @GetMapping("/x")\r\n    public String get(String id) { return id; }\r\n}\r\n')
        analysis = self.analysis()
        review = {}
        for item in analysis["items"]:
            value = "说明"
            if item["kind"] == "javadoc":
                value += "\n@param id 标识\n@return 结果" if item["context"]["parameters"] else ""
            review[item["id"]] = {"text": value, "use": True}
        files, errors = build_preview(analysis, review, str(self.root))
        self.assertFalse(errors, errors)
        updated = files[0]["_after"]
        self.assertTrue(updated.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\n", updated.replace(b"\r\n", b""))
        self.assertLess(updated.index(b"import "), updated.index(b"/**"))
        self.assertIn(b"return id;", updated)
        self.assertEqual(self.root.joinpath("C.java").read_bytes(), files[0]["_before"])

    def test_replace_empty_attribute_preserves_other_arguments(self):
        self.write("C.java", '''package x;
import io.swagger.v3.oas.annotations.media.Schema;
public class C {
    @Schema(description = "", requiredMode = Schema.RequiredMode.REQUIRED)
    private String name;
    @Schema(hidden = true) private String hiddenName;
}''')
        spec = {"components": {"schemas": {"C": {"properties": {"name": {}, "hiddenName": {}}}}}}
        analysis = self.analysis(["swagger"], spec)
        review = {i["id"]: {"text": '显示名 "A" \\ path', "use": True} for i in analysis["items"]}
        files, errors = build_preview(analysis, review, str(self.root))
        self.assertFalse(errors, errors)
        updated = files[0]["_after"].decode()
        self.assertIn("requiredMode = Schema.RequiredMode.REQUIRED", updated)
        self.assertIn('hidden = true, description =', updated)
        self.assertIn('\\"A\\"', updated)
        self.assertEqual(updated.count("import io.swagger.v3.oas.annotations.media.Schema;"), 1)

    def test_changed_source_rejected(self):
        path = self.write("C.java", "public class C {}")
        analysis = self.analysis(["javadoc"])
        path.write_text("public class C { int id; }", encoding="utf-8")
        files, errors = build_preview(analysis, {analysis["items"][0]["id"]: {"text": "类说明", "use": True}}, str(self.root))
        self.assertFalse(files)
        self.assertIn("不同", errors[0]["reason"])

    def test_not_adopted_never_changes(self):
        self.write("C.java", "public class C {}")
        analysis = self.analysis(["javadoc"])
        files, errors = build_preview(analysis, {analysis["items"][0]["id"]: {"text": "类说明", "use": False}}, str(self.root))
        self.assertFalse(files)
        self.assertFalse(errors)

    def test_unicode_escape_cannot_escape_javadoc(self):
        self.write("C.java", "public class C {}")
        analysis = self.analysis(["javadoc"])
        files, errors = build_preview(analysis, {analysis["items"][0]["id"]: {"text": r"字面量 \u002a\u002f", "use": True}}, str(self.root))
        self.assertFalse(errors)
        self.assertNotIn(b"\\u002a", files[0]["_after"])

    def test_custom_annotation_name_conflict_uses_fully_qualified_name(self):
        self.write("C.java", 'package x; import custom.Schema; public class C { @Schema private String name; }')
        spec = {"components": {"schemas": {"C": {"properties": {"name": {}}}}}}
        analysis = self.analysis(["swagger"], spec)
        files, errors = build_preview(analysis, {analysis["items"][0]["id"]: {"text": "名称", "use": True}}, str(self.root))
        self.assertFalse(errors)
        self.assertIn(b"@io.swagger.v3.oas.annotations.media.Schema", files[0]["_after"])
        self.assertIn(b"@Schema private", files[0]["_after"])

    def test_path_traversal_rejected(self):
        for path in ["../Outside.java", "a/../../Outside.java", "C:/Outside.java", "a\\C.java", "/Outside.java"]:
            with self.assertRaises(ValueError):
                safe_path(self.root, path)


class ModelTests(TemporaryProject):
    def setUp(self):
        super().setUp()
        self.write("C.java", "public class C { public String find(String id) { return id; } }")
        self.items = self.analysis(["javadoc"])["items"]

    def test_strict_ids_and_batch_count(self):
        for rows in [[], [{"id": "unknown", "suggestion": "说明"}], [{"id": self.items[0]["id"], "suggestion": "说明"}] * 2]:
            with self.assertRaises(ValueError):
                parse_response(self.items, json.dumps({"items": rows}))

    def test_javadoc_parameters_and_return_are_not_invented(self):
        item = next(i for i in self.items if i["context"]["parameters"])
        self.assertEqual(validate_suggestion(item, "查询\n@param id 标识\n@return 结果"), "查询\n@param id 标识\n@return 结果")
        for value in ["查询", "查询\n@param other 标识\n@return 结果", "查询 */", "查询\n@param id 标识\n@return 结果\n@throws Exception 错误"]:
            with self.assertRaises(ValueError):
                validate_suggestion(item, value)

    def test_mock_api_protocol_json_and_authorization(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        received = []
        class Mock(BaseHTTPRequestHandler):
            def do_POST(inner):
                request = json.loads(inner.rfile.read(int(inner.headers["Content-Length"])))
                received.append((inner.path, inner.headers.get("Authorization"), request))
                items = json.loads(request["messages"][1]["content"].split("\n", 1)[1])
                rows = [{"id": i["id"], "suggestion": "类说明", "reason": "测试"} for i in items]
                payload = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"items": rows})}}], "usage": {"prompt_tokens": 10}}).encode()
                inner.send_response(200);inner.send_header("Content-Length", str(len(payload)));inner.end_headers();inner.wfile.write(payload)
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        rows, usage = call_model([self.items[0]], {"provider": "deepseek", "baseUrl": f"http://127.0.0.1:{server.server_port}/v1", "model": "test", "jsonMode": True}, "test-secret", attempts=1)
        self.assertEqual(rows[0]["suggestion"], "类说明")
        self.assertEqual(received[0][0], "/v1/chat/completions")
        self.assertEqual(received[0][1], "Bearer test-secret")
        self.assertEqual(received[0][2]["response_format"], {"type": "json_object"})
        self.assertEqual(usage["prompt_tokens"], 10)

    def test_resume_retains_successful_batches_and_does_not_save_key(self):
        from unittest.mock import patch
        app = Workbench(self.base / "app", self.base)
        task = app.store.create(self.analysis(["javadoc"]))
        profile = {"provider": "compatible", "baseUrl": "https://example.invalid/v1", "model": "test", "jsonMode": True, "batchSize": 1}
        calls = []
        def fake(batch, _, key):
            calls.append(batch[0]["id"])
            if len(calls) == 2:
                raise ValueError("模拟失败")
            return [{"id": batch[0]["id"], "suggestion": "类说明", "reason": "测试"}], {}
        with patch("workbench.server.call_model", side_effect=fake):
            result = app.translate(task, profile, "secret-do-not-persist")
            while app.jobs[result["job"]]["status"] == "running":
                time.sleep(.01)
        self.assertEqual(app.jobs[result["job"]]["status"], "error")
        self.assertEqual(len(app.store.read(task, "suggestions.json")[0]["items"]), 1)
        with patch("workbench.server.call_model", return_value=([{"id": self.items[1]["id"], "suggestion": "方法说明", "reason": "测试"}], {})) as mock:
            result = app.translate(task, profile, "secret-do-not-persist")
            while app.jobs[result["job"]]["status"] == "running":
                time.sleep(.01)
            self.assertEqual(mock.call_count, 1)
        self.assertEqual(len(app.store.read(task, "suggestions.json")[0]["items"]), 2)
        self.assertNotIn(b"secret-do-not-persist", app.store.export(task))
        self.assertNotIn("secret-do-not-persist", (app.store.path(task) / "suggestions.json").read_text(encoding="utf-8"))


class ArtifactTests(TemporaryProject):
    def test_standalone_stage_files_validate_pairing(self):
        task = create_demo(self.store)["task"]
        analysis = self.store.read(task, "analysis.json")
        remote = self.store.create(analysis)
        self.store.import_stage(remote, self.store.stage_document(task, "suggestions"))
        self.assertEqual(len(self.store.read(remote, "suggestions.json")), 1)
        identifier = analysis["items"][0]["id"]
        self.store.write(task, "review.json", {identifier: {"text": "模块说明", "use": True}})
        self.store.import_stage(remote, self.store.stage_document(task, "review"))
        self.assertTrue(self.store.read(remote, "review.json")[identifier]["use"])
        other = {**analysis, "createdAt": "different analysis"}
        other_task = self.store.create(other)
        with self.assertRaisesRegex(ValueError, "不属于"):
            self.store.import_stage(other_task, self.store.stage_document(task, "suggestions"))

    def test_markdown_from_other_analysis_rejected(self):
        self.write("C.java", "public class C {}")
        analysis = self.analysis(["javadoc"])
        markdown = review_markdown(analysis, {})
        with self.assertRaises(ValueError):
            parse_review(markdown, {**analysis, "taskId": "other"})

    def test_task_roundtrip_on_another_computer_without_project(self):
        self.write("C.java", "public class C {}")
        analysis = self.analysis(["javadoc"])
        task = self.store.create(analysis, "任务", str(self.root))
        identifier = analysis["items"][0]["id"]
        self.store.write(task, "review.json", {identifier: {"text": "类说明", "use": True}})
        remote = Store(self.base / "remote-tasks")
        imported = remote.import_bundle(self.store.export(task), "task.zip")
        self.assertEqual(remote.read(imported, "analysis.json"), analysis)
        self.assertTrue(remote.read(imported, "review.json")[identifier]["use"])
        self.assertEqual(remote.read(imported, "local.json")["projectRoot"], "")

    def test_markdown_roundtrip_multiline_backslash_pipe_and_dashes(self):
        self.write("C.java", "public class C {}")
        analysis = self.analysis(["javadoc"])
        value = "说明 --- A | B \\ 文件\n第二行"
        review = {analysis["items"][0]["id"]: {"text": value, "use": True}}
        parsed = parse_review(review_markdown(analysis, review), analysis)
        self.assertEqual(parsed[analysis["items"][0]["id"]]["text"], value)

    def test_unknown_zip_members_and_hash_mismatch_rejected(self):
        self.write("C.java", "public class C {}")
        task = self.store.create(self.analysis(["javadoc"]))
        for member in ["../outside.json", "local.json"]:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr(member, "{}")
            with self.assertRaises(ValueError):
                self.store.import_bundle(buffer.getvalue())
        buffer = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(self.store.export(task))) as original, zipfile.ZipFile(buffer, "w") as tampered:
            for name in original.namelist():
                data = original.read(name)
                tampered.writestr(name, data + b" " if name == "analysis.json" else data)
        with self.assertRaisesRegex(ValueError, "校验失败"):
            self.store.import_bundle(buffer.getvalue())


class GitWorkflowTests(TemporaryProject):
    def test_large_batch_keeps_all_files_in_real_git_diff(self):
        for index in range(96):
            name = "DocumentedType" + "LongName" * 7 + str(index)
            self.write(f"src/example/{name}.java", f"package example; public class {name} {{}}\n")
        self.baseline()
        analysis = self.analysis(["javadoc"])
        review = {i["id"]: {"text": "测试类型说明", "use": True} for i in analysis["items"]}
        files, errors = build_preview(analysis, review, str(self.root))
        self.assertFalse(errors)
        task = self.store.create(analysis)
        receipt = apply_preview(self.store.path(task), str(self.root), files, git_status(str(self.root))["head"])
        self.assertEqual(receipt["gitDiff"].count("diff --git "), 96)
        self.assertEqual(receipt["gitDiff"].count("测试类型说明"), 96)

    def setup_demo(self):
        task = create_demo(self.store)["task"]
        analysis = self.store.read(task, "analysis.json")
        rows = self.store.read(task, "suggestions.json")[0]["items"]
        review = {r["id"]: {"text": r["suggestion"], "use": True} for r in rows}
        root = self.store.read(task, "local.json")["projectRoot"]
        files, errors = build_preview(analysis, review, root)
        self.assertFalse(errors, errors)
        return task, analysis, root, files

    def test_real_git_diff_compilation_and_safe_restore(self):
        task, analysis, root, files = self.setup_demo()
        before = git_status(root)
        self.assertTrue(before["ready"], before)
        receipt = apply_preview(self.store.path(task), root, files, before["head"])
        self.assertIn("@Operation", receipt["gitDiff"])
        self.assertFalse(git_status(root)["ready"])
        if shutil.which("javac"):
            result = subprocess.run(["javac", "-encoding", "UTF-8", "-d", str(self.base / "compiled"), *map(str, Path(root).rglob("*.java"))], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        if shutil.which("javadoc"):
            result = subprocess.run(["javadoc", "-encoding", "UTF-8", "-private", "-quiet", "-Xdoclint:all", "-d", str(self.base / "javadoc"), *map(str, Path(root).rglob("*.java"))], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        result = restore(self.store.path(task), receipt["id"])
        self.assertEqual(result["restored"], len(files))
        self.assertTrue(git_status(root)["ready"])
        self.assertEqual([i["id"] for i in scan(root, ["src"], ["swagger", "javadoc"])["items"] if i["kind"] != "schemaField"],
                         [i["id"] for i in analysis["items"] if i["kind"] != "schemaField"])

    def test_dirty_and_untracked_worktree_require_acknowledgement(self):
        task, _, root, files = self.setup_demo()
        (Path(root) / "notes.txt").write_text("untracked")
        with self.assertRaises(ValueError):
            apply_preview(self.store.path(task), root, files, git_status(root)["head"])
        self.assertNotIn("@Operation", (Path(root) / "src/demo/PersonController.java").read_text(encoding="utf-8"))

    def test_staged_changes_do_not_bypass_source_fingerprint(self):
        task, _, root, files = self.setup_demo()
        path = Path(root) / "src/demo/Person.java"
        path.write_bytes(path.read_bytes() + b"\n")
        git(Path(root), "add", "--", "src/demo/Person.java")
        with self.assertRaises(ValueError):
            apply_preview(self.store.path(task), root, files, git_status(root)["head"])

    def test_unrelated_dirty_files_allowed_after_ack_and_preserved_on_restore(self):
        task, _, root, files = self.setup_demo()
        note = Path(root) / "untracked-notes.txt"
        note.write_bytes(b"existing unrelated notes")
        before = git_status(root)
        receipt = apply_preview(self.store.path(task), root, files, before["head"], True)
        self.assertIn("@Operation", receipt["toolDiff"])
        self.assertEqual(note.read_bytes(), b"existing unrelated notes")
        restore(self.store.path(task), receipt["id"])
        self.assertEqual(git_status(root)["changes"], before["changes"])
        self.assertEqual(note.read_bytes(), b"existing unrelated notes")

    def test_preexisting_staged_and_unstaged_edits_both_survive_apply_and_restore(self):
        task, _, root, _ = self.setup_demo()
        path = Path(root) / "src/demo/PersonController.java"
        path.write_bytes(path.read_bytes().replace("示例人员".encode(), "已暂存的业务修改".encode()))
        git(Path(root), "add", "--", "src/demo/PersonController.java")
        index_before = git(Path(root), "ls-files", "--stage", "-z", "--", "src/demo/PersonController.java")
        path.write_bytes(path.read_bytes() + b"// existing unstaged comment\n")
        original = path.read_bytes()
        analysis = scan(root, ["src"], ["swagger", "javadoc"])
        rows = self.store.read(task, "suggestions.json")[0]["items"]
        review = {r["id"]: {"text": r["suggestion"], "use": True} for r in rows}
        files, errors = build_preview(analysis, review, root)
        self.assertFalse(errors, errors)
        receipt = apply_preview(self.store.path(task), root, files, git_status(root)["head"], True)
        self.assertIn("已暂存的业务修改".encode(), path.read_bytes())
        self.assertIn(b"// existing unstaged comment", path.read_bytes())
        self.assertEqual(git(Path(root), "ls-files", "--stage", "-z", "--", "src/demo/PersonController.java"), index_before)
        changed_lines = "\n".join(line for line in receipt["toolDiff"].splitlines()
                                  if line.startswith(("+", "-")) and not line.startswith(("+++", "---")))
        self.assertNotIn("已暂存的业务修改", changed_lines)
        self.assertIn("已暂存的业务修改", receipt["gitDiff"])
        restore(self.store.path(task), receipt["id"])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(git(Path(root), "ls-files", "--stage", "-z", "--", "src/demo/PersonController.java"), index_before)

    def test_untracked_java_file_can_be_written_and_restored(self):
        self.write("README.txt", "baseline")
        self.baseline()
        path = self.write("New.java", "public class New {}\n")
        original = path.read_bytes()
        analysis = self.analysis(["javadoc"])
        task = self.store.create(analysis)
        review = {i["id"]: {"text": "新增类型", "use": True} for i in analysis["items"]}
        files, errors = build_preview(analysis, review, str(self.root))
        self.assertFalse(errors)
        receipt = apply_preview(self.store.path(task), str(self.root), files, git_status(str(self.root))["head"], True)
        self.assertIn("新增类型", receipt["toolDiff"])
        self.assertIn("未跟踪", receipt["gitDiffNote"])
        restore(self.store.path(task), receipt["id"])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(git(self.root, "ls-files", "--", "New.java"), "")

    def test_no_git_repository_still_supports_backup_and_diff(self):
        self.write("C.java", "public class C {}\n")
        analysis = self.analysis(["javadoc"])
        task = self.store.create(analysis)
        files, errors = build_preview(analysis, {i["id"]: {"text": "类型说明", "use": True} for i in analysis["items"]}, str(self.root))
        self.assertFalse(errors)
        status = git_status(str(self.root))
        self.assertFalse(status["available"])
        receipt = apply_preview(self.store.path(task), str(self.root), files, None, True)
        self.assertIn("类型说明", receipt["toolDiff"])
        restore(self.store.path(task), receipt["id"])
        self.assertEqual((self.root / "C.java").read_bytes(), b"public class C {}\n")

    def test_staging_tool_result_after_apply_prevents_restore(self):
        task, _, root, files = self.setup_demo()
        receipt = apply_preview(self.store.path(task), root, files, git_status(root)["head"])
        git(Path(root), "add", "--", files[0]["file"])
        with self.assertRaisesRegex(ValueError, "暂存内容"):
            restore(self.store.path(task), receipt["id"])

    def test_post_apply_edits_block_restore(self):
        task, _, root, files = self.setup_demo()
        receipt = apply_preview(self.store.path(task), root, files, git_status(root)["head"])
        path = Path(root) / files[0]["file"]
        path.write_bytes(path.read_bytes() + b"// user edit\n")
        with self.assertRaisesRegex(ValueError, "又被修改"):
            restore(self.store.path(task), receipt["id"])
        self.assertTrue(path.read_bytes().endswith(b"// user edit\n"))


class HttpTests(TemporaryProject):
    def test_dirty_git_apply_requires_explicit_ack_but_then_succeeds(self):
        def post(path, body):
            with self.request(path, body) as response:
                return json.load(response)
        task = post("/api/demo", {})["task"]
        view = self.app.get_task(task)
        root = view["local"]["projectRoot"]
        note = Path(root) / "existing.txt"
        note.write_text("preserve this", encoding="utf-8")
        post("/api/adopt-page", {"task": task, "itemIds": [i["id"] for i in view["analysis"]["items"]]})
        preview = post("/api/preview", {"task": task, "projectRoot": root})
        self.assertFalse(preview["git"]["ready"])
        self.assertFalse(preview["errors"])
        with self.assertRaises(urllib.error.HTTPError):
            post("/api/apply", {"task": task, "token": preview["token"]})
        receipt = post("/api/apply", {"task": task, "token": preview["token"], "acknowledgeGitWarning": True})
        self.assertIn("@Operation", receipt["toolDiff"])
        self.assertEqual(note.read_text(encoding="utf-8"), "preserve this")
        post("/api/restore", {"task": task, "run": receipt["id"]})

    def setUp(self):
        super().setUp()
        self.app, self.server = create_server(self.base / "data", Path(__file__).resolve().parents[1] / "static", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def request(self, path, body=None, token=True, origin=None):
        headers = {"X-Workbench-Token": self.app.token} if token else {}
        if origin:
            headers["Origin"] = origin
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.url + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        return urllib.request.urlopen(request, timeout=30)

    def test_local_page_and_no_unauthenticated_api(self):
        with self.request("/", token=False) as response:
            page = response.read().decode()
            self.assertIn(self.app.token, page)
            self.assertNotIn("__TOKEN__", page)
        with self.assertRaises(urllib.error.HTTPError):
            self.request("/api/info", token=False)
        with self.assertRaises(urllib.error.HTTPError):
            self.request("/api/demo", {}, origin="https://other.example")

    def test_http_end_to_end_demo_review_preview_apply_export_restore(self):
        def post(path, body):
            with self.request(path, body) as response:
                return json.load(response)
        task = post("/api/demo", {})["task"]
        with self.request("/api/task?task=" + task) as response:
            view = json.load(response)
        rows = {r["id"]: {"text": r["suggestion"], "use": True} for r in view["runs"][0]["items"]}
        post("/api/review", {"task": task, "rows": rows})
        preview = post("/api/preview", {"task": task, "projectRoot": view["local"]["projectRoot"]})
        self.assertFalse(preview["errors"])
        self.assertTrue(preview["git"]["ready"])
        receipt = post("/api/apply", {"task": task, "token": preview["token"]})
        self.assertIn("@Schema", receipt["gitDiff"])
        with self.request("/api/export?task=" + task) as response:
            data = response.read()
        imported = post("/api/import", {"filename": "bundle.zip", "data": base64.b64encode(data).decode()})["task"]
        self.assertEqual(len(self.app.store.read(imported, "review.json")), len(rows))
        post("/api/restore", {"task": task, "run": receipt["id"]})
        self.assertTrue(git_status(view["local"]["projectRoot"])["ready"])

    def test_changed_review_invalidates_preview(self):
        def post(path, body):
            with self.request(path, body) as response:
                return json.load(response)
        task = post("/api/demo", {})["task"]
        view = self.app.get_task(task)
        row = next(r for r in view["runs"][0]["items"] if next(i for i in view["analysis"]["items"] if i["id"] == r["id"])["kind"] == "controllerTag")
        post("/api/review", {"task": task, "rows": {row["id"]: {"text": row["suggestion"], "use": True}}})
        preview = post("/api/preview", {"task": task, "projectRoot": view["local"]["projectRoot"]})
        post("/api/review", {"task": task, "rows": {row["id"]: {"text": "更新说明", "use": True}}})
        with self.assertRaises(urllib.error.HTTPError):
            post("/api/apply", {"task": task, "token": preview["token"]})


if __name__ == "__main__":
    unittest.main()

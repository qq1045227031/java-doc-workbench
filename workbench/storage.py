from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import uuid
import zipfile

from .java_source import safe_path

FILES = {"analysis.json", "suggestions.json", "review.json", "review.md"}
MAX_BUNDLE = 80_000_000


def encode(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def atomic(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_bytes(data)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def validate_analysis(value):
    if not isinstance(value, dict) or value.get("format") != "java-description-workbench" or value.get("version") != 1:
        raise ValueError("文件不是受支持的工作台分析包（version=1）")
    if not isinstance(value.get("items"), list) or len(value["items"]) > 100000:
        raise ValueError("分析项格式错误或数量过多")
    seen = set()
    for item in value["items"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or item["id"] in seen:
            raise ValueError("分析项 id 缺失或重复")
        seen.add(item["id"])
        if item.get("kind") not in {"controllerTag", "operation", "schemaField", "javadoc"}:
            raise ValueError("未知说明类型")
        if not re.fullmatch(r"[0-9a-f]{64}", str(item.get("fileHash", ""))):
            raise ValueError("缺少源码指纹，无法安全回写")
        safe_path(Path.cwd().resolve(), item.get("file", ""))
        if not isinstance(item.get("selector"), str) or not isinstance(item.get("context"), dict):
            raise ValueError("缺少源码定位或上下文")
    return value


def table_escape(value):
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\r", "").replace("\n", "<br>")


def table_split(line):
    columns, current, escaped = [], [], False
    for char in line.strip()[1:-1]:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            columns.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if escaped:
        current.append("\\")
    columns.append("".join(current).strip())
    return [c.replace("<br>", "\n") for c in columns]


def review_markdown(analysis, review):
    lines = ["# Java 说明审核表", "", f"<!-- analysis-sha256:{hashlib.sha256(encode(analysis)).hexdigest()} -->", "", "修改“审核说明”和“采用”（是/否），不要修改 id。JavaDoc 换行使用 <br>。", "",
             "| id | 类型 | 目标 | 文件 | 审核说明 | 采用 |", "| --- | --- | --- | --- | --- | --- |"]
    for item in analysis["items"]:
        row = review.get(item["id"], {})
        values = [item["id"], item["label"], item["target"], item["file"], row.get("text", ""), "是" if row.get("use") else "否"]
        lines.append("| " + " | ".join(table_escape(v) for v in values) + " |")
    return "\n".join(lines) + "\n"


def parse_review(content, analysis):
    fingerprint = re.search(r"<!-- analysis-sha256:([0-9a-f]{64}) -->", content)
    if not fingerprint or fingerprint.group(1) != hashlib.sha256(encode(analysis)).hexdigest():
        raise ValueError("审核表不属于当前分析任务，或缺少文件指纹，请导入配套任务包")
    valid = {i["id"] for i in analysis["items"]}
    result, header = {}, None
    for line in content.splitlines():
        if not line.strip().startswith("|") or not line.strip().endswith("|"):
            continue
        columns = table_split(line)
        if all(re.fullmatch(r":?-+:?", c.replace(" ", "")) for c in columns):
            continue
        if header is None:
            header = columns
            if not {"id", "审核说明", "采用"}.issubset(header):
                raise ValueError("审核表缺少 id、审核说明、采用 列")
            continue
        if len(columns) != len(header):
            raise ValueError("审核表列数错误，请将内容中的 | 写为 \\|")
        row = dict(zip(header, columns))
        identifier = row["id"]
        if identifier not in valid or identifier in result:
            raise ValueError("审核表包含未知或重复 id")
        if row["采用"] not in {"是", "否"}:
            raise ValueError("采用列只能填写 是 或 否")
        result[identifier] = {"text": row["审核说明"], "use": row["采用"] == "是", "runId": "manual"}
    if header is None:
        raise ValueError("未找到审核表")
    return result


class Store:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)

    def path(self, task):
        if not re.fullmatch(r"[a-f0-9]{32}", task):
            raise ValueError("任务编号不正确")
        folder = self.directory / task
        if not folder.is_dir():
            raise ValueError("任务不存在")
        return folder

    def read(self, task, name, default=None):
        path = self.path(task) / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

    def write(self, task, name, value):
        atomic(self.path(task) / name, encode(value))

    def create(self, analysis, title="新任务", project_root=""):
        validate_analysis(analysis)
        task = uuid.uuid4().hex
        folder = self.directory / task
        folder.mkdir()
        self.write(task, "analysis.json", analysis)
        self.write(task, "local.json", {"title": title[:120], "projectRoot": project_root})
        self.write(task, "suggestions.json", [])
        self.write(task, "review.json", {})
        return task

    def list(self):
        result = []
        for folder in self.directory.iterdir():
            if not folder.is_dir() or not re.fullmatch(r"[a-f0-9]{32}", folder.name):
                continue
            try:
                analysis = self.read(folder.name, "analysis.json")
                local = self.read(folder.name, "local.json", {})
                result.append({"id": folder.name, "title": local.get("title", "任务"), "createdAt": analysis["createdAt"], "count": len(analysis["items"])})
            except (ValueError, KeyError, OSError):
                continue
        return sorted(result, key=lambda r: r["createdAt"], reverse=True)

    def export(self, task):
        analysis = self.read(task, "analysis.json")
        review = self.read(task, "review.json", {})
        contents = {name: encode(self.read(task, name, [] if name == "suggestions.json" else {})) for name in FILES if name.endswith(".json")}
        contents["review.md"] = review_markdown(analysis, review).encode()
        manifest = {"format": "java-description-workbench", "version": 1,
                    "sha256": {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}}
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in contents.items():
                archive.writestr(name, data)
            archive.writestr("manifest.json", encode(manifest))
        return buffer.getvalue()

    def stage_document(self, task, kind):
        analysis = self.read(task, "analysis.json")
        return {"format": "java-description-workbench", "version": 1, "kind": kind,
                "analysisHash": hashlib.sha256(encode(analysis)).hexdigest(),
                "data": self.read(task, "suggestions.json" if kind == "suggestions" else "review.json")}

    def import_stage(self, task, document):
        from .models import validate_suggestion
        analysis = self.read(task, "analysis.json")
        if document.get("format") != "java-description-workbench" or document.get("version") != 1 or document.get("analysisHash") != hashlib.sha256(encode(analysis)).hexdigest():
            raise ValueError("文件不属于当前分析任务，请先导入配套分析 JSON 或任务包")
        items = {i["id"]: i for i in analysis["items"]}
        data = document.get("data")
        if document.get("kind") == "suggestions":
            if not isinstance(data, list):
                raise ValueError("建议文件格式错误")
            runs = self.read(task, "suggestions.json", [])
            merged = {r["id"]: r for r in runs}
            seen_runs = set()
            for run in data:
                if not isinstance(run, dict) or not isinstance(run.get("id"), str) or run["id"] in seen_runs or not isinstance(run.get("profile"), dict) or not isinstance(run.get("items"), list):
                    raise ValueError("模型结果编号重复或格式错误")
                seen_runs.add(run["id"])
                seen = set()
                for row in run["items"]:
                    if not isinstance(row, dict) or row.get("id") not in items or row["id"] in seen:
                        raise ValueError("建议包含未知或重复 id")
                    seen.add(row["id"])
                    validate_suggestion(items[row["id"]], row.get("suggestion"))
                clean = {"id": run["id"], "profile": {key: str(run["profile"].get(key, ""))[:1000] for key in ("provider", "baseUrl", "model")},
                         "createdAt": str(run.get("createdAt", "")), "items": [{"id": r["id"], "suggestion": r["suggestion"], "reason": str(r.get("reason", ""))[:1500]} for r in run["items"]],
                         "usage": run.get("usage", []), "errors": run.get("errors", [])}
                if clean["id"] in merged:
                    original = merged[clean["id"]]
                    combined = {r["id"]: r for r in original["items"]}
                    for row in clean["items"]:
                        if row["id"] in combined and combined[row["id"]]["suggestion"] != row["suggestion"]:
                            raise ValueError("同一模型批次存在不同建议，请使用另一任务包分别审核")
                        combined[row["id"]] = row
                    clean["items"] = list(combined.values())
                merged[clean["id"]] = clean
            self.write(task, "suggestions.json", list(merged.values()))
        elif document.get("kind") == "review":
            if not isinstance(data, dict) or set(data) - items.keys():
                raise ValueError("审核文件包含未知 id")
            clean = {}
            for identifier, row in data.items():
                if not isinstance(row, dict) or not isinstance(row.get("text"), str) or type(row.get("use")) is not bool:
                    raise ValueError("审核项格式不正确")
                if row["use"]:
                    validate_suggestion(items[identifier], row["text"])
                clean[identifier] = {"text": row["text"][:12000], "use": row["use"], "runId": str(row.get("runId", "manual"))[:100]}
            self.write(task, "review.json", clean)
        else:
            raise ValueError("未知阶段文件类型")
        return task

    def import_bundle(self, data: bytes, filename=""):
        if len(data) > MAX_BUNDLE:
            raise ValueError("导入文件超过 80MB")
        if filename.lower().endswith(".json"):
            analysis = validate_analysis(json.loads(data.decode("utf-8-sig")))
            return self.create(analysis, "导入的分析任务")
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = archive.namelist()
                if len(set(names)) != len(names) or set(names) - (FILES | {"manifest.json"}) or "analysis.json" not in names:
                    raise ValueError("文件包包含重复、未知或缺失文件")
                if sum(i.file_size for i in archive.infolist()) > MAX_BUNDLE:
                    raise ValueError("解压后超过 80MB")
                contents = {name: archive.read(name) for name in names}
        except zipfile.BadZipFile:
            raise ValueError("不是有效的 ZIP 文件包") from None
        if "manifest.json" in contents:
            manifest = json.loads(contents["manifest.json"])
            for name, sha in manifest.get("sha256", {}).items():
                if name not in contents or hashlib.sha256(contents[name]).hexdigest() != sha:
                    raise ValueError(f"文件包校验失败：{name}；如手动编辑审核表，请单独导入 Markdown")
        analysis = validate_analysis(json.loads(contents["analysis.json"].decode("utf-8-sig")))
        runs = json.loads(contents.get("suggestions.json", b"[]"))
        review = json.loads(contents.get("review.json", b"{}"))
        ids = {i["id"] for i in analysis["items"]}
        if not isinstance(runs, list) or not isinstance(review, dict) or set(review) - ids:
            raise ValueError("建议或审核文件格式不正确")
        for run in runs:
            if not isinstance(run, dict) or not isinstance(run.get("items"), list):
                raise ValueError("建议文件格式不正确")
            if any(not isinstance(row, dict) or row.get("id") not in ids for row in run["items"]):
                raise ValueError("建议中存在未知 id")
        # Validate public stage payloads before creating any persistent task.
        # Then pass through the same strict validation and key filtering as standalone files.
        task = self.create(analysis, "导入的接力任务")
        analysis_hash = hashlib.sha256(encode(analysis)).hexdigest()
        for kind, data in (("suggestions", runs), ("review", review)):
            self.import_stage(task, {"format": "java-description-workbench", "version": 1,
                                    "analysisHash": analysis_hash, "kind": kind, "data": data})
        return task

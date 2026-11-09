from __future__ import annotations

from collections import defaultdict
import difflib
import json
import os
from pathlib import Path
import subprocess
import uuid

from .java_source import JavaFile, annotation_edits, apply_edits, digest, javadoc_edit, safe_path
from .models import validate_suggestion
from .scanner import now
from .storage import atomic, encode


def git(root: Path, *args):
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_LITERAL_PATHSPECS": "1"}
    try:
        result = subprocess.run(["git", "-c", "core.quotepath=false", "-C", str(root), *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except FileNotFoundError:
        raise ValueError("未找到 Git，请安装 Git for Windows 后重新启动工具") from None
    except subprocess.TimeoutExpired:
        raise ValueError("Git 检查超时") from None
    except OSError as exc:
        raise ValueError(f"Git 无法启动：{exc}") from None
    if result.returncode:
        raise ValueError("Git 操作失败：" + result.stderr.decode("utf-8", errors="replace")[:1000])
    return result.stdout.decode("utf-8", errors="replace")


def git_status(project_root: str):
    root = Path(project_root).resolve()
    if not root.is_dir():
        return {"ready": False, "available": False, "reason": "请选择本机项目根目录", "changes": []}
    try:
        repo = git(root, "rev-parse", "--show-toplevel").strip()
        try:
            head = git(root, "rev-parse", "HEAD").strip()
        except ValueError:
            head = None
        status = git(root, "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none")
        clean = not bool(status) and bool(head)
        return {"ready": clean, "available": True, "repo": repo, "head": head, "changes": status.splitlines(),
                "reason": "工作区干净，可以在预览后写入" if clean else "建议先提交或整理现有修改，便于对比和恢复。这只是提醒；可在最后确认框中确认提示后继续写入，工具不会暂存、提交或清理现有文件。"}
    except ValueError as exc:
        return {"ready": False, "available": False, "head": None, "reason": "Git 提醒：" + str(exc) + "。仍可确认后写入，保留原文件备份和本次变更 diff。", "changes": []}


def index_signature(repo: Path, path: Path):
    return digest(git(repo, "ls-files", "--stage", "-z", "--", path.relative_to(repo).as_posix()).encode("utf-8"))


def build_preview(analysis, review, project_root):
    root = Path(project_root).resolve()
    if not root.is_dir():
        raise ValueError("项目根目录不存在")
    grouped = defaultdict(list)
    errors = []
    for item in analysis["items"]:
        row = review.get(item["id"], {})
        if row.get("use") is not True:
            continue
        try:
            value = validate_suggestion(item, row.get("text"))
            grouped[item["file"]].append((item, value))
        except ValueError as exc:
            errors.append({"target": item["target"], "reason": str(exc)})
    files = []
    for relative, entries in grouped.items():
        try:
            path = safe_path(root, relative)
            data = path.read_bytes()
            if any(digest(data) != item["fileHash"] for item, _ in entries):
                raise ValueError("源码与扫描时不同，请重新扫描；不会按旧行号强行写入")
            jf = JavaFile(relative, data)
            edits, imports, ids = [], set(), []
            # JavaDoc must precede new annotations when both are inserted at the same offset.
            for item, value in sorted(entries, key=lambda pair: pair[0]["kind"] != "javadoc"):
                decl = jf.declarations.get(item["selector"])
                if not decl:
                    raise ValueError("无法通过完整声明签名定位，请重新扫描")
                expected_kind = {"controllerTag": {"type"}, "operation": {"method"}, "schemaField": {"field", "method"}, "javadoc": {"type", "field", "method"}}
                if decl.kind not in expected_kind[item["kind"]]:
                    raise ValueError("说明类型与源码声明不匹配")
                if item["kind"] == "javadoc":
                    edits.append(javadoc_edit(jf, decl, value.replace("\\u", "&#92;u")))
                else:
                    additions, imp = annotation_edits(jf, decl, item["kind"], value)
                    edits.extend(additions)
                    if imp:
                        imports.add(imp)
                ids.append(item["id"])
            updated = apply_edits(jf, edits, imports)
            difference = "".join(difflib.unified_diff(data.decode("utf-8-sig").splitlines(keepends=True),
                updated.decode("utf-8-sig").splitlines(keepends=True), fromfile="a/" + relative, tofile="b/" + relative))
            files.append({"file": relative, "beforeHash": digest(data), "afterHash": digest(updated),
                          "diff": difference, "ids": ids, "_before": data, "_after": updated})
        except (ValueError, OSError) as exc:
            errors.append({"target": relative, "reason": str(exc)})
    return files, errors


def preview_signature(files, review, root):
    return digest(encode({"root": str(Path(root).resolve()), "review": review,
        "files": [{k: v for k, v in f.items() if not k.startswith("_")} for f in files]}))


def apply_preview(task_folder: Path, project_root: str, files, expected_head, acknowledge_git_warning=False):
    status = git_status(project_root)
    if not status["ready"] and acknowledge_git_warning is not True:
        raise ValueError("Git 检查有提醒，请在最后确认框中勾选已知晓后继续；不要求必须提交代码。")
    if status.get("head") != expected_head:
        raise ValueError("Git HEAD 自预览后已变化，请重新预览")
    if not files:
        raise ValueError("没有采用项可写入")
    root = Path(project_root).resolve()
    repo = Path(status["repo"]).resolve() if status.get("available") else None
    index_before = {}
    for file in files:
        path = safe_path(root, file["file"])
        if repo:
            index_before[file["file"]] = index_signature(repo, path)
        if digest(path.read_bytes()) != file["beforeHash"]:
            raise ValueError("源码自预览后已变化，请重新扫描")
    run_id = uuid.uuid4().hex
    folder = task_folder / "backups" / run_id
    folder.mkdir(parents=True)
    receipt = {"id": run_id, "createdAt": now(), "projectRoot": str(root), "head": status.get("head"), "status": "prepared",
        "gitBefore": status, "gitWarningAcknowledged": acknowledge_git_warning,
        "files": [{**{k: v for k, v in f.items() if not k.startswith("_")},
                   "indexBefore": index_before.get(f["file"])} for f in files]}
    for file in files:
        backup = folder / "files" / file["file"]
        atomic(backup, file["_before"])
    atomic(folder / "receipt.json", encode(receipt))
    written = []
    try:
        for file in files:
            target = safe_path(root, file["file"])
            if digest(target.read_bytes()) != file["beforeHash"]:
                raise ValueError("写入期间源码发生变化，已停止")
            # Preserve source file permissions while replacing content.
            mode = target.stat().st_mode
            atomic(target, file["_after"])
            target.chmod(mode)
            written.append(file)
    except Exception:
        for file in reversed(written):
            target = safe_path(root, file["file"])
            if digest(target.read_bytes()) == file["afterHash"]:
                atomic(target, file["_before"])
        receipt["status"] = "failed"
        atomic(folder / "receipt.json", encode(receipt))
        raise
    receipt["status"] = "applied"
    # Source is already backed up if Git diff fails; the receipt still records applied status.
    atomic(folder / "receipt.json", encode(receipt))
    tool_diff = "\n".join(f["diff"] for f in files)
    atomic(folder / "tool.diff", tool_diff.encode("utf-8"))
    paths = [safe_path(root, f["file"]).relative_to(repo).as_posix() for f in files] if repo else []
    note = "Git diff 包含目标文件原有的未提交修改；仅本工具的变更请查看“本次变更 diff”。" if not status["ready"] else "Git diff 相对于写入前的 HEAD。"
    if any(signature == digest(b"") for signature in index_before.values()):
        note += " 未跟踪文件不会显示在 Git HEAD diff 中，但完整包含在本次变更 diff 中。"
    try:
        if not repo or not status.get("head"):
            raise ValueError("没有可用的 Git HEAD，显示本次写入前后的 diff")
        # Large Java projects can exceed Windows' command-line length limit.
        groups, group, length = [], [], 0
        for path in paths:
            if group and length + len(path) > 7000:
                groups.append(group)
                group, length = [], 0
            group.append(path)
            length += len(path) + 3
        if group:
            groups.append(group)
        diff = "".join(git(repo, "diff", "--no-ext-diff", "--no-textconv", "HEAD", "--", *group) for group in groups)
    except ValueError as exc:
        diff = "Git diff 获取失败：" + str(exc) + "\n\n" + tool_diff
        note = "未取得 Git HEAD diff，已保留完整的本次变更 diff。"
    atomic(folder / "git.diff", diff.encode("utf-8"))
    return {**receipt, "gitDiff": diff, "toolDiff": tool_diff, "gitDiffNote": note}


def restore(task_folder: Path, run_id: str):
    if not isinstance(run_id, str) or len(run_id) != 32 or any(c not in "0123456789abcdef" for c in run_id):
        raise ValueError("备份编号无效")
    folder = task_folder / "backups" / run_id
    receipt = json.loads((folder / "receipt.json").read_text(encoding="utf-8"))
    if receipt["status"] != "applied":
        raise ValueError("此备份已恢复或未完成写入")
    root = Path(receipt["projectRoot"]).resolve()
    status = git_status(str(root))
    old_git = receipt.get("gitBefore")
    if old_git is not None and bool(status.get("available")) != bool(old_git.get("available")):
        raise ValueError("Git 仓库状态已变化，请核对备份后手动恢复")
    if status.get("head") != receipt.get("head"):
        raise ValueError("写入后已有新的提交，请使用 Git 检查和恢复，不自动覆盖")
    # Do not overwrite staged changes or post-apply edits.
    for file in receipt["files"]:
        path = safe_path(root, file["file"])
        if digest(path.read_bytes()) != file["afterHash"]:
            raise ValueError("文件在写入后又被修改，不能自动恢复：" + file["file"])
        if status.get("available"):
            repo = Path(status["repo"]).resolve()
            if file.get("indexBefore") is not None:
                if index_signature(repo, path) != file["indexBefore"]:
                    raise ValueError("文件暂存内容在写入后发生变化，请先在 Git 中检查后手动处理")
            elif git(repo, "diff", "--cached", "--name-only", "--", path.relative_to(repo).as_posix()).strip():
                raise ValueError("文件已暂存，请先在 Git 中检查后手动处理")
        backup = (folder / "files" / file["file"]).read_bytes()
        if digest(backup) != file["beforeHash"]:
            raise ValueError("备份指纹校验失败")
    for file in receipt["files"]:
        path = safe_path(root, file["file"])
        mode = path.stat().st_mode
        atomic(path, (folder / "files" / file["file"]).read_bytes())
        path.chmod(mode)
    receipt["status"] = "restored"
    atomic(folder / "receipt.json", encode(receipt))
    return {"restored": len(receipt["files"])}

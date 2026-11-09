"""Parse declarations and make narrowly scoped byte edits; no model-authored Java."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Language, Parser
import tree_sitter_java

LANGUAGE = Language(tree_sitter_java.language())
TYPES = {"class_declaration", "interface_declaration", "enum_declaration", "record_declaration", "annotation_type_declaration"}
METHODS = {"method_declaration", "constructor_declaration", "compact_constructor_declaration"}
EXCLUDED = {".git", ".venv", "target", "build", "node_modules", ".idea", ".gradle"}
SWAGGER = {
    "controllerTag": ("io.swagger.v3.oas.annotations.tags.Tag", "name"),
    "operation": ("io.swagger.v3.oas.annotations.Operation", "summary"),
    "schemaField": ("io.swagger.v3.oas.annotations.media.Schema", "description"),
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parser() -> Parser:
    return Parser(LANGUAGE)


def text(node, data: bytes) -> str:
    return data[node.start_byte:node.end_byte].decode("utf-8") if node else ""


def child(node, name):
    return node.child_by_field_name(name)


def modifiers(node):
    return next((n for n in node.named_children if n.type == "modifiers"), None)


def annotations(node, data):
    mods = modifiers(node)
    return {text(child(n, "name"), data): n for n in (mods.named_children if mods else [])
            if n.type in {"annotation", "marker_annotation"}}


def annotation_named(node, name, data, imports=None, qualified=None):
    for key, value in annotations(node, data).items():
        if key == qualified:
            return value
        if key == name and (not qualified or not imports or imports.get(name, qualified) == qualified):
            return value
    return None


def attribute(annotation, name, data):
    if not annotation:
        return None
    args = child(annotation, "arguments")
    if args:
        for pair in args.named_children:
            if pair.type == "element_value_pair" and text(child(pair, "key"), data) == name:
                return child(pair, "value")
    return None


def literal(node, data):
    if node and node.type == "string_literal":
        try:
            return json.loads(text(node, data))
        except ValueError:
            pass
    return None


def has_value(node, name, data):
    value = attribute(node, name, data)
    return value is not None and (literal(value, data) is None or bool(literal(value, data).strip()))


def previous_doc(node):
    prev = node.prev_named_sibling
    return prev if prev and prev.type == "block_comment" and prev.text.startswith(b"/**") else None


@dataclass
class Declaration:
    key: str
    kind: str
    name: str
    class_name: str
    node: object
    owner: object
    parameter_names: list[str]
    return_type: str
    type_parameters: list[str]
    throws: list[str]


class JavaFile:
    def __init__(self, relative: str, data: bytes):
        data.decode("utf-8-sig")  # Refuse non-UTF8 files rather than corrupting them.
        self.relative, self.data = relative, data
        self.tree = parser().parse(data)
        if self.tree.root_node.has_error:
            raise ValueError(f"Java 语法无法可靠解析：{relative}")
        root = self.tree.root_node
        package = next((n for n in root.named_children if n.type == "package_declaration"), None)
        self.package = re.sub(r"^package\s+|;\s*$", "", text(package, data)).strip()
        self.import_nodes = [n for n in root.named_children if n.type == "import_declaration"]
        self.imports = {}
        for n in self.import_nodes:
            value = re.sub(r"^import\s+|;\s*$", "", text(n, data)).strip()
            if not value.startswith("static "):
                self.imports[value.rsplit(".", 1)[-1]] = value
        self.declarations: dict[str, Declaration] = {}
        self.classes = []
        for n in root.named_children:
            if n.type in TYPES:
                self._class(n, "")

    def _class(self, node, enclosing):
        name = text(child(node, "name"), self.data)
        class_name = ".".join(filter(None, [enclosing or self.package, name]))
        self.classes.append((class_name, node))
        params = child(node, "type_parameters")
        type_parameters = [text(child(p, "name") or p.named_children[0], self.data)
                           for p in (params.named_children if params else []) if p.type == "type_parameter"]
        self.declarations[f"type:{class_name}"] = Declaration(f"type:{class_name}", "type", name, class_name, node, node, [], "", type_parameters, [])
        body = child(node, "body")
        if not body:
            return
        # enum constants have their own bodies; only declarations in the type body are considered.
        members = list(body.named_children)
        for block in list(members):
            if block.type == "enum_body_declarations":
                members.extend(block.named_children)
        for member in members:
            if member.type in TYPES:
                self._class(member, class_name)
            elif member.type in METHODS:
                method = text(child(member, "name"), self.data) or name
                parameters = child(member, "parameters")
                parameter_names, parameter_types = [], []
                for p in (parameters.named_children if parameters else []):
                    if p.type not in {"formal_parameter", "spread_parameter"}:
                        continue
                    pname = child(p, "name")
                    if pname is None:
                        declarator = next((n for n in p.named_children if n.type == "variable_declarator"), None)
                        pname = child(declarator, "name") if declarator else None
                    parameter_names.append(text(pname, self.data))
                    ptype = text(child(p, "type"), self.data)
                    if not ptype:
                        ptype = next((text(n, self.data) for n in p.named_children if n.type not in {"modifiers", "variable_declarator"}), "?")
                    dimensions = child(p, "dimensions")
                    parameter_types.append(re.sub(r"\s+", "", ptype + text(dimensions, self.data)) + ("..." if p.type == "spread_parameter" else ""))
                key = f"method:{class_name}#{method}({','.join(parameter_types)})"
                throws = next((n for n in member.named_children if n.type == "throws"), None)
                mtp = child(member, "type_parameters")
                tparams = [text(child(p, "name") or p.named_children[0], self.data)
                           for p in (mtp.named_children if mtp else []) if p.type == "type_parameter"]
                if key in self.declarations:
                    raise ValueError(f"方法签名不唯一：{key}")
                self.declarations[key] = Declaration(key, "method", method, class_name, member, node,
                    parameter_names, text(child(member, "type"), self.data), tparams,
                    [text(n, self.data) for n in (throws.named_children if throws else [])])
            elif member.type in {"field_declaration", "constant_declaration"}:
                variables = [n for n in member.named_children if n.type == "variable_declarator"]
                for v in variables:
                    field = text(child(v, "name"), self.data)
                    key = f"field:{class_name}#{field}"
                    self.declarations[key] = Declaration(key, "field", field, class_name, member, node, [], text(child(member, "type"), self.data), [], [])

    def resolve_class(self, name):
        name = re.sub(r"<.*>", "", name).strip()
        if "." in name:
            return name
        return self.imports.get(name, ".".join(filter(None, [self.package, name])))

    def snippet(self, node, limit=1800):
        # A capped declaration fragment is explicitly visible in the exported analysis.
        return text(node, self.data)[:limit]


def safe_path(root: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts or "\\" in relative or ":" in relative:
        raise ValueError("文件路径必须是项目内的相对路径")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or path.suffix != ".java":
        raise ValueError(f"源码路径越界或不是 Java 文件：{relative}")
    return path


def read_project(root: Path, roots: list[str]):
    files, warnings = {}, []
    for entry in roots:
        folder = (root / entry).resolve()
        if not folder.is_relative_to(root) or not folder.is_dir():
            raise ValueError(f"扫描目录不存在或不在项目中：{entry}")
        for path in folder.rglob("*.java"):
            if EXCLUDED.intersection(path.relative_to(root).parts) or path.is_symlink():
                continue
            relative = path.relative_to(root).as_posix()
            if relative in files:
                continue
            try:
                safe_path(root, relative)
                if path.stat().st_size > 2_000_000:
                    raise ValueError("文件大于 2MB，已跳过")
                files[relative] = JavaFile(relative, path.read_bytes())
            except (ValueError, OSError) as exc:
                warnings.append(f"{relative}：{exc}")
    return files, warnings


def java_string(value: str):
    return json.dumps(value, ensure_ascii=False).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def annotation_edits(jf: JavaFile, decl: Declaration, kind: str, suggestion: str):
    qualified, prop = SWAGGER[kind]
    simple = qualified.rsplit(".", 1)[-1]
    existing = annotation_named(decl.node, simple, jf.data, jf.imports, qualified)
    value = java_string(suggestion)
    if existing:
        old = attribute(existing, prop, jf.data)
        if has_value(existing, prop, jf.data):
            raise ValueError("已有非空说明，重新扫描后再处理")
        if old:
            return [(old.start_byte, old.end_byte, value.encode("utf-8"))], None
        args = child(existing, "arguments")
        if args:
            prefix = ", " if args.named_child_count else ""
            return [(args.end_byte - 1, args.end_byte - 1, f"{prefix}{prop} = {value}".encode("utf-8"))], None
        return [(existing.end_byte, existing.end_byte, f"({prop} = {value})".encode("utf-8"))], None
    # A distinct custom annotation with the same short name must remain untouched.
    name = qualified if simple in jf.imports and jf.imports[simple] != qualified else simple
    before = jf.data[:decl.node.start_byte]
    indentation = before[before.rfind(b"\n") + 1:]
    indentation = indentation if not indentation.strip() else b""
    newline = b"\r\n" if b"\r\n" in jf.data else b"\n"
    addition = f"@{name}({prop} = {value})".encode("utf-8") + newline + indentation
    return [(decl.node.start_byte, decl.node.start_byte, addition)], qualified if name == simple else None


def javadoc_edit(jf: JavaFile, decl: Declaration, suggestion: str):
    if previous_doc(decl.node):
        raise ValueError("已有 JavaDoc，首版保留原注释，请重新扫描")
    if "*/" in suggestion or "\x00" in suggestion:
        raise ValueError("JavaDoc 包含非法结束符或空字符")
    before = jf.data[:decl.node.start_byte]
    indentation = before[before.rfind(b"\n") + 1:]
    indentation = indentation if not indentation.strip() else b""
    newline = "\r\n" if b"\r\n" in jf.data else "\n"
    indent = indentation.decode("utf-8")
    lines = ["/**"] + [" *" + (" " + line if line else "") for line in suggestion.splitlines()] + [" */"]
    addition = (newline + indent).join(lines) + newline + indent
    return decl.node.start_byte, decl.node.start_byte, addition.encode("utf-8")


def apply_edits(jf: JavaFile, edits, imports):
    missing = sorted(q for q in imports if q not in jf.imports.values())
    if missing:
        newline = b"\r\n" if b"\r\n" in jf.data else b"\n"
        package = next((n for n in jf.tree.root_node.named_children if n.type == "package_declaration"), None)
        pos = jf.import_nodes[-1].end_byte if jf.import_nodes else (package.end_byte if package else (3 if jf.data.startswith(b"\xef\xbb\xbf") else 0))
        addition = newline + newline.join(f"import {q};".encode() for q in missing) + newline
        # In a default-package file import insertion can share offset 0 with a type.
        # Imports must precede both its JavaDoc and its annotations.
        edits.insert(0, (pos, pos, addition))
    ordered = sorted(edits, key=lambda e: (e[0], e[1]))
    for a, b in zip(ordered, ordered[1:]):
        if a[1] > b[0]:
            raise ValueError("写入位置重叠，已停止")
    data = jf.data
    for start, end, replacement in reversed(ordered):
        data = data[:start] + replacement + data[end:]
    if parser().parse(data).root_node.has_error:
        raise ValueError(f"生成内容未通过 Java 语法检查：{jf.relative}")
    return data

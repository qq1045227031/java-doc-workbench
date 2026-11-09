from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import uuid

from .java_source import (SWAGGER, annotations, annotation_named, attribute, child, digest,
                          has_value, literal, modifiers, previous_doc, read_project, text)


def now():
    return datetime.now(timezone.utc).isoformat()


def scan(project_root: str, roots: list[str], modes: list[str], spec=None, include_snippets=True):
    root = Path(project_root).resolve()
    if not root.is_dir():
        raise ValueError("项目根目录不存在")
    if not modes or set(modes) - {"swagger", "javadoc"}:
        raise ValueError("请选择 Swagger 或 JavaDoc 模式")
    roots = roots or ["."]
    files, warnings = read_project(root, roots)
    if not files:
        raise ValueError("未找到可解析的 Java 源码，请检查目录和 UTF-8 编码")
    items, skipped = [], []
    classes = {}
    schema_classes = {}
    for jf in files.values():
        for fqn, node in jf.classes:
            if fqn in classes:
                warnings.append(f"完整类名重复，字段映射将跳过：{fqn}")
                classes[fqn] = None
            else:
                classes[fqn] = (jf, node)
            aliases = {fqn, fqn.rsplit(".", 1)[-1]}
            ann = annotation_named(node, "Schema", jf.data, jf.imports, SWAGGER["schemaField"][0])
            name = literal(attribute(ann, "name", jf.data), jf.data)
            if name:
                aliases.add(name)
            for alias in aliases:
                schema_classes.setdefault(alias, []).append((jf, fqn, node))

    def add(jf, declaration, kind, target=None, context=None):
        selector = declaration.key
        identifier = digest(f"{jf.relative}\n{selector}\n{kind}".encode())[:24]
        doc = previous_doc(declaration.node)
        current = text(doc, jf.data) if kind == "javadoc" else ""
        items.append({
            "id": identifier, "kind": kind,
            "label": {"controllerTag": "模块", "operation": "接口", "schemaField": "字段", "javadoc": "JavaDoc"}[kind],
            "file": jf.relative, "fileHash": digest(jf.data), "selector": selector,
            "line": declaration.node.start_point.row + 1, "target": target or selector.split(":", 1)[-1],
            "javaType": declaration.return_type, "current": current,
            "context": {"class": declaration.class_name, "name": declaration.name,
                        "parameters": declaration.parameter_names,
                        "declarationKind": declaration.kind,
                        "typeParameters": declaration.type_parameters, "returns": declaration.return_type if declaration.kind == "method" else "",
                        "throws": declaration.throws, **(context or {})},
            "snippet": jf.snippet(declaration.node) if include_snippets else "",
        })

    if "swagger" in modes:
        for jf in files.values():
            for decl in jf.declarations.values():
                owner_annotations = {s.rsplit(".", 1)[-1] for s in annotations(decl.owner, jf.data)}
                if not owner_annotations.intersection({"Controller", "RestController"}):
                    continue
                kind = "controllerTag" if decl.kind == "type" else ("operation" if decl.kind == "method" else None)
                if not kind:
                    continue
                if kind == "operation" and not any(n.rsplit(".", 1)[-1] in {"GetMapping", "PostMapping", "PutMapping", "DeleteMapping", "PatchMapping", "RequestMapping"} for n in annotations(decl.node, jf.data)):
                    continue
                qualified, prop = SWAGGER[kind]
                annotation = annotation_named(decl.node, qualified.rsplit(".", 1)[-1], jf.data, jf.imports, qualified)
                if not has_value(annotation, prop, jf.data):
                    mapping = [text(a, jf.data) for n, a in annotations(decl.node, jf.data).items() if n.endswith("Mapping")]
                    add(jf, decl, kind, context={"mapping": mapping})
        if spec is None:
            warnings.append("未提供 OpenAPI：已扫描 Controller 和接口；字段说明需要导入 OpenAPI 后扫描")
        else:
            schemas = (spec.get("components") or {}).get("schemas") or {}
            for schema_name, schema in schemas.items():
                if not isinstance(schema, dict):
                    continue
                properties = schema.get("properties") or {}
                candidates = schema_classes.get(schema_name, [])
                for field_name, field_spec in properties.items():
                    if not isinstance(field_spec, dict) or field_spec.get("description") or field_spec.get("title"):
                        continue
                    target = f"{schema_name}.{field_name}"
                    if len(candidates) != 1:
                        skipped.append({"target": target, "reason": "同名类映射不唯一" if candidates else "未找到对应 Java 类", "candidates": [c[0].relative for c in candidates]})
                        continue
                    jf, fqn, node = candidates[0]
                    found, seen = [], set()
                    while fqn not in seen:
                        seen.add(fqn)
                        for d in jf.declarations.values():
                            if d.class_name != fqn or d.kind not in {"field", "method"}:
                                continue
                            jackson = annotation_named(d.node, "JsonProperty", jf.data)
                            args = child(jackson, "arguments") if jackson else None
                            explicit = literal(attribute(jackson, "value", jf.data), jf.data)
                            if explicit is None and args and args.named_child_count == 1:
                                explicit = literal(args.named_children[0], jf.data)
                            names = {explicit} if explicit else {d.name}
                            if not explicit:
                                if d.kind == "method" and not d.parameter_names:
                                    prefix = "get" if d.name.startswith("get") else ("is" if d.name.startswith("is") else "")
                                    if prefix and len(d.name) > len(prefix):
                                        value = d.name[len(prefix):]
                                        names.add(value if len(value) > 1 and value[1].isupper() else value[0].lower() + value[1:])
                                if d.kind == "field" and d.return_type in {"boolean", "Boolean"} and d.name.startswith("is") and len(d.name) > 2:
                                    value = d.name[2:]
                                    names.add(value[0].lower() + value[1:])
                            if field_name in names:
                                found.append((jf, d))
                        if found:
                            break
                        superclass = child(node, "superclass")
                        if not superclass:
                            break
                        base_name = text(superclass, jf.data).removeprefix("extends").strip()
                        parent = classes.get(jf.resolve_class(base_name))
                        if not parent:
                            break
                        jf, node = parent
                        fqn = next((name for name, n in jf.classes if n == node), "")
                    fields = [pair for pair in found if pair[1].kind == "field"]
                    found = fields or found
                    if len(found) != 1:
                        skipped.append({"target": target, "reason": "字段/访问器无法唯一定位（record 组件暂不自动写入）"})
                        continue
                    jf, decl = found[0]
                    if decl.kind == "field" and sum(n.type == "variable_declarator" for n in decl.node.named_children) > 1:
                        skipped.append({"target": target, "reason": "同一声明包含多个字段，无法分别添加注解"})
                        continue
                    ann = annotation_named(decl.node, "Schema", jf.data, jf.imports, SWAGGER["schemaField"][0])
                    if not has_value(ann, "description", jf.data):
                        add(jf, decl, "schemaField", target, {"schema": schema_name, "field": field_name, "schemaType": field_spec.get("type", "")})

    if "javadoc" in modes:
        for jf in files.values():
            visited = set()
            for decl in jf.declarations.values():
                if decl.node.start_byte in visited or previous_doc(decl.node):
                    continue
                visited.add(decl.node.start_byte)
                mods = modifiers(decl.node)
                if decl.kind == "field" and ("static" in text(mods, jf.data).split() or sum(n.type == "variable_declarator" for n in decl.node.named_children) > 1):
                    continue
                add(jf, decl, "javadoc")
    # Several OpenAPI schemas can refer to the same declaration. One physical edit per kind.
    unique = {}
    for item in items:
        if item["id"] in unique:
            unique[item["id"]]["context"].setdefault("aliases", []).append(item["target"])
        else:
            unique[item["id"]] = item
    return {"format": "java-description-workbench", "version": 1, "taskId": uuid.uuid4().hex,
            "createdAt": now(), "modes": modes, "roots": roots,
            "includeSnippets": include_snippets, "scannedFiles": len(files),
            "items": list(unique.values()), "skipped": skipped, "warnings": warnings}

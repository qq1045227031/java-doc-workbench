"""An isolated, disposable demonstration; never operates on a selected user repository."""
from pathlib import Path
import uuid

from .changes import git
from .scanner import scan, now


def create_demo(store):
    root = store.directory.parent / "examples" / uuid.uuid4().hex
    source = root / "src" / "demo"
    source.mkdir(parents=True)
    (source / "PersonController.java").write_text('''package demo;

@RestController
public class PersonController {
    @GetMapping("/people/{id}")
    public Person findById(String id) {
        return new Person(id, "示例人员");
    }
}

@interface RestController {}
@interface GetMapping { String value(); }
''', encoding="utf-8")
    (source / "Person.java").write_text('''package demo;

public class Person {
    private String id;
    private String displayName;

    public Person(String id, String displayName) {
        this.id = id;
        this.displayName = displayName;
    }
}
''', encoding="utf-8")
    # Minimal annotation definitions make the example compile without Maven downloads.
    definitions = {
        "io/swagger/v3/oas/annotations/tags/Tag.java": 'package io.swagger.v3.oas.annotations.tags; public @interface Tag { String name() default ""; }',
        "io/swagger/v3/oas/annotations/Operation.java": 'package io.swagger.v3.oas.annotations; public @interface Operation { String summary() default ""; }',
        "io/swagger/v3/oas/annotations/media/Schema.java": 'package io.swagger.v3.oas.annotations.media; public @interface Schema { String description() default ""; }',
    }
    for relative, content in definitions.items():
        path = root / "stubs" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content + "\n", encoding="utf-8")
    git(root, "init")
    git(root, "add", "--", "src", "stubs")
    git(root, "-c", "user.name=Workbench Demo", "-c", "user.email=demo@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-m", "Isolated demonstration baseline")
    spec = {"openapi": "3.0.3", "components": {"schemas": {"Person": {"type": "object", "properties": {"id": {"type": "string"}, "displayName": {"type": "string"}}}}}}
    analysis = scan(str(root), ["src"], ["swagger", "javadoc"], spec)
    task = store.create(analysis, "示例 · 人员接口说明", str(root))
    rows = []
    for item in analysis["items"]:
        context = item["context"]
        suggestion = {"controllerTag": "人员管理", "operation": "根据标识查询人员", "schemaField": "人员标识" if context.get("field") == "id" else "显示名称"}.get(item["kind"])
        if suggestion is None:
            suggestion = "提供人员数据相关功能"
            for p in context["typeParameters"]:
                suggestion += f"\n@param <{p}> 类型参数"
            for p in context["parameters"]:
                suggestion += f"\n@param {p} { {'id':'人员标识','displayName':'显示名称'}.get(p,'输入参数') }"
            if context["returns"] and context["returns"] != "void":
                suggestion += "\n@return 查询结果"
            for exception in context["throws"]:
                suggestion += f"\n@throws {exception} 操作失败时抛出"
        rows.append({"id": item["id"], "suggestion": suggestion, "reason": "内置模拟结果，用于体验流程，未调用模型 API"})
    store.write(task, "suggestions.json", [{"id": "demo", "profile": {"provider": "demo", "baseUrl": "内置示例（无网络调用）", "model": "模拟建议 · 非真实模型"}, "createdAt": now(), "items": rows, "usage": [], "errors": []}])
    return {"task": task}

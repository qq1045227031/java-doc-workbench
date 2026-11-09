"""Review decisions shared by the browser and independently testable batch actions."""
from __future__ import annotations

from .models import validate_suggestion


def adopt_page(analysis, runs, review, item_ids, source_run="auto"):
    items = {item["id"]: item for item in analysis["items"]}
    if not isinstance(item_ids, list) or not item_ids or any(not isinstance(i, str) for i in item_ids):
        raise ValueError("当前页没有可采用的说明项")
    if len(set(item_ids)) != len(item_ids) or set(item_ids) - items.keys():
        raise ValueError("采用范围包含重复或未知 id")
    if source_run != "auto" and source_run not in {r["id"] for r in runs}:
        raise ValueError("所选模型结果不存在，请刷新任务")
    updated = dict(review)
    adopted, skipped, newly_adopted = [], [], 0
    for identifier in item_ids:
        item = items[identifier]
        row = dict(review.get(identifier, {}))
        value = row.get("text", "").strip()
        if not value:
            candidates = [dict(proposal, runId=run["id"]) for run in runs
                          if source_run == "auto" or run["id"] == source_run
                          for proposal in run["items"]
                          if proposal["id"] == identifier and proposal.get("suggestion", "").strip()]
            distinct = {p["suggestion"].strip() for p in candidates}
            if not distinct:
                skipped.append({"id": identifier, "target": item["target"], "reason": "没有可用建议或已填写文本"})
                continue
            if len(distinct) > 1:
                skipped.append({"id": identifier, "target": item["target"], "reason": "存在多个不同建议，请选择批量来源模型，或逐项采用"})
                continue
            value = candidates[0]["suggestion"].strip()
            row["runId"] = candidates[0]["runId"]
        try:
            value = validate_suggestion(item, value)
        except ValueError as exc:
            skipped.append({"id": identifier, "target": item["target"], "reason": str(exc)})
            continue
        newly_adopted += not bool(row.get("use"))
        updated[identifier] = {"text": value, "use": True, "runId": row.get("runId", "manual")}
        adopted.append(identifier)
    return {"review": updated, "adopted": len(adopted), "newlyAdopted": newly_adopted,
            "adoptedIds": adopted, "skipped": skipped}

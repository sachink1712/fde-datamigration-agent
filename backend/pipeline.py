"""Deterministic record builder: map columns -> run the Cleaner's tool plan -> resolve manager references ->
reconcile all source files by employee id -> apply human overrides. Pure function of the run document, so it can be
replayed after every human decision without calling an LLM."""
from __future__ import annotations

import os
from typing import Any

from .checks import intrinsic_issue
from .policies import classify_header
from .tools import DATE_FIELDS, Cell, Directory, clean_text, infer_dayfirst, run_tools

REGION = os.getenv("DEFAULT_PHONE_REGION", "IN")


def column_key(file: str, header: str) -> str:
    return f"{file}::{header}"


def split_full_name(raw: str, src: str) -> dict[str, Cell]:
    if not raw:
        return {}
    if "," in raw:
        last, _, first = raw.partition(",")
        first, last, conf, note = clean_text(first), clean_text(last), 0.95, "reordered 'LAST, First'"
    else:
        parts = raw.split(" ")
        first, last = parts[0], " ".join(parts[1:])
        conf, note = (0.95, "") if len(parts) == 2 else (0.85, "first word taken as given name") if len(parts) > 2 else (0.3, "only one name part")
    return {"first_name": Cell(first or None, raw, conf, note, source=src), "last_name": Cell(last or None, raw, conf, note, source=src)}


def sample_raw(run: dict, target_field: str, n: int = 4) -> list[str]:
    out: list[str] = []
    for c in run["classifications"]:
        if c["status"] != "accepted" or c.get("transform") == "ignore":
            continue
        if c["target_field"] == target_field or (c["transform"] == "split_full_name" and target_field == "last_name"):
            out += [str(r.get(c["source_header"], "")) for r in run["source_rows"] if r["_source"] == c["source_file"] and r.get(c["source_header"])][:n]
    return out[:n]


def _same(a: Any, b: Any) -> bool:
    if all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (a, b)):
        return abs(a - b) <= 0.001 * max(abs(a), abs(b), 1)      # 316667 x 12 vs 3,800,000 is rounding noise
    return str(a).casefold() == str(b).casefold()


def _final(v: Any) -> Any:
    return int(v) if isinstance(v, float) and v.is_integer() else round(v, 2) if isinstance(v, float) else v


def _drafts(run: dict, plan: dict[str, list[str]], enum: list[str]) -> list[dict]:
    drafts: list[dict] = []
    classes = run["classifications"]
    for file in dict.fromkeys(r["_source"] for r in run["source_rows"]):
        rows = [r for r in run["source_rows"] if r["_source"] == file]
        cols = [c for c in classes if c["source_file"] == file and c["status"] == "accepted" and c.get("target_field") and c["transform"] != "ignore"]
        others = [c["source_header"] for c in classes if c["source_file"] == file and c not in cols and classify_header(c["source_header"]) != "sensitive"]
        if not cols:
            continue
        date_raw = [str(r.get(c["source_header"], "")) for c in cols if c["target_field"] in DATE_FIELDS for r in rows]
        ctx = {"region": REGION, "enum": enum, "dayfirst": infer_dayfirst(date_raw)}
        transforms = {c["target_field"]: c["transform"] for c in cols}
        for row in rows:
            cells: dict[str, Cell] = {}
            src = row["_source_row"]
            for c in cols:
                raw = clean_text(str(row.get(c["source_header"], "") or ""))
                new = split_full_name(raw, src) if c["transform"] == "split_full_name" else {c["target_field"]: Cell(raw or None, raw, source=src)}
                for f, cell in new.items():
                    if f not in cells or cells[f].value is None:
                        cells[f] = cell
            for f, cell in list(cells.items()):
                cell = run_tools(f, cell, plan.get(f, []), ctx, "cell")
                if transforms.get(f) == "monthly_to_annual" and isinstance(cell.value, (int, float)):
                    cell = Cell(round(cell.value * 12, 2), cell.raw, cell.confidence, "; ".join(x for x in (cell.note, "monthly x 12") if x), derived=True, source=src)
                cells[f] = cell
            drafts.append({"source": src, "cells": cells, "extra": {h: clean_text(str(row.get(h, ""))) for h in others if row.get(h)}, "ctx": ctx})
    directory = Directory()
    for d in drafts:
        g = lambda f: d["cells"][f].value if f in d["cells"] else None  # noqa: E731
        directory.add(g("employee_id"), g("first_name"), g("last_name"))
    for d in drafts:
        if "manager_employee_id" in d["cells"]:
            d["cells"]["manager_employee_id"] = run_tools("manager_employee_id", d["cells"]["manager_employee_id"], plan.get("manager_employee_id", []), {**d["ctx"], "directory": directory}, "reference")
    return drafts


def build_records(run: dict, schema: dict) -> list[dict[str, Any]]:
    plan = {p["target_field"]: p["tools"] for p in run.get("cleaning_plan", [])}
    enum = schema["properties"]["employment_status"]["enum"]
    groups: dict[str, list[dict]] = {}
    for d in _drafts(run, plan, enum):
        idc = d["cells"].get("employee_id")
        key = str(idc.value).upper() if idc and idc.value else d["source"]
        groups.setdefault(key, []).append(d)
    records = []
    for members in groups.values():
        rec: dict[str, Any] = {"_source_row": "; ".join(m["source"] for m in members), "_sources": [m["source"] for m in members],
                               "_cells": {}, "_notes": [], "_conflicts": {}, "_extra": {k: v for m in members for k, v in m["extra"].items()}}
        for f in dict.fromkeys(f for m in members for f in m["cells"]):
            cands = [(m["source"], m["cells"][f]) for m in members if f in m["cells"] and m["cells"][f].value not in (None, "")]
            if not cands:
                continue
            clusters: list[dict] = []
            for src, cell in cands:
                for cl in clusters:
                    if _same(cl["cell"].value, cell.value):
                        cl["sources"].append(src)
                        if cl["cell"].derived and not cell.derived:
                            cl["cell"] = cell
                        break
                else:
                    clusters.append({"cell": cell, "sources": [src]})
            chosen = clusters[0]["cell"]
            if len(clusters) > 1:
                valid = [cl for cl in clusters if intrinsic_issue(f, cl["cell"].value) is None]
                if len(valid) == 1:
                    chosen = valid[0]["cell"]
                    bad = "; ".join(f"{cl['cell'].value} ({cl['sources'][0]})" for cl in clusters if cl not in valid)
                    rec["_notes"].append(f"{f}: used {chosen.value} ({valid[0]['sources'][0]}); other source value was invalid: {bad}")
                else:
                    chosen = (valid or clusters)[0]["cell"]
                    rec["_conflicts"][f] = [{"value": _final(cl["cell"].value), "source": cl["sources"][0]} for cl in clusters]
            rec[f], rec[f"_raw_{f}"] = _final(chosen.value), chosen.raw
            if chosen.confidence < 1 or chosen.note or chosen.flag:
                rec["_cells"][f] = chosen.to_dict() | {"candidates": chosen.candidates}
        records.append(rec)
    for rec in records:
        subject = str(rec.get("employee_id") or rec["_source_row"])
        for f, v in (run.get("overrides", {}).get(subject) or {}).items():
            rec.pop(f, None) if v is None else rec.__setitem__(f, v)
            rec["_cells"][f] = {"raw": rec.get(f"_raw_{f}", ""), "confidence": 1.0, "note": "human corrected", "flag": "", "source": "human"}
            rec["_conflicts"].pop(f, None)
            rec["_notes"].append(f"{f}: set by consultant")
    return records

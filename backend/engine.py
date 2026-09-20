"""Run lifecycle: ingest -> profile -> (agents) -> build/evaluate/settle -> human decisions -> push/rollback.
Everything here is deterministic and PII-safe in traces; the LLM agents live in agents.py and are orchestrated in graph.py."""
from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from . import checks
from .observability import Observability
from .pipeline import REGION, build_records, column_key, sample_raw
from .policies import CLEAN_MIN, REQUIRED_FIELDS, TARGET_FIELDS, TARGET_SCHEMA, authorize_target_write, classify_header
from .store import Store
from .tools import FORMAT_TOOL, TOOLS, Cell, clean_text, run_tools, safe_plan

OPEN, DECIDED = "open", {"approved", "corrected", "rejected"}
ENUM_STATUS = TARGET_SCHEMA["properties"]["employment_status"]["enum"]


class MigrationEngine:
    def __init__(self, store: Store, fixtures_dir: str = "data/fixtures") -> None:
        self.store, self.fixtures_dir, self.obs = store, Path(fixtures_dir), Observability()

    # ------------------------------------------------------------------ bookkeeping
    def load(self, run_id: str) -> dict[str, Any] | None:
        return self.store.get_run(run_id)

    def save(self, run: dict[str, Any]) -> None:
        self.store.save_run(run)

    def event(self, run: dict[str, Any], stage: str, message: str, **metrics: Any) -> None:
        run["events"].append({"at": datetime.now(UTC).isoformat(), "stage": stage, "message": message, "metrics": metrics})
        self.obs.emit(stage, run["id"], metrics)

    def create_run(self, source_files: list[dict[str, str]] | None = None) -> dict[str, Any]:
        run = {"id": str(uuid.uuid4()), "batch_id": str(uuid.uuid4()), "status": "created", "source_files": source_files or [], "source_headers": [],
               "source_rows": [], "profiles": [], "classifications": [], "cleaning_plan": [], "records": [], "escalations": [], "warnings": [], "llm_warnings": [],
               "events": [], "retry_log": [], "overrides": {}, "waivers": [], "excluded": [], "merges": [], "outcomes": [], "summary": {}, "unmapped_required": []}
        self.store.save_run(run)
        return run

    # ------------------------------------------------------------------ ingest + profile
    def _tables(self, files: list[dict[str, str]]) -> list[tuple[str, pd.DataFrame]]:
        paths = [(f["name"], Path(f["path"])) for f in files] if files else [(p.name, p) for p in sorted(self.fixtures_dir.glob("*.csv"))]
        out: list[tuple[str, pd.DataFrame]] = []
        for name, path in paths:
            if path.suffix.lower() == ".csv":
                try:
                    frames = {None: pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")}
                except UnicodeDecodeError:
                    frames = {None: pd.read_csv(path, dtype=str, keep_default_na=False, encoding="cp1252")}
            else:
                frames = pd.read_excel(path, sheet_name=None, dtype=str, keep_default_na=False)
            for sheet, df in frames.items():
                df.columns = [str(c).strip() for c in df.columns]
                df = df.loc[:, [c for c in df.columns if not (c.startswith("Unnamed:") and (df[c] == "").all())]]
                df = df[(df.apply(lambda col: col.astype(str).str.strip()) != "").any(axis=1)]
                label = name if sheet is None or len(frames) == 1 else f"{name}[{sheet}]"
                while any(label == x[0] for x in out):
                    label += "'"
                out.append((label, df))
        return out

    def ingest(self, run: dict[str, Any]) -> None:
        rows, headers, profiles = [], [], []
        for name, df in self._tables(run["source_files"]):
            file_rows = [{**{k: str(v) for k, v in rec.items()}, "_source": name, "_source_row": f"{name}:{i}"} for i, rec in zip(df.index + 2, df.to_dict("records"))]
            rows += file_rows
            for h in df.columns:
                headers.append(h)
                vals = list(dict.fromkeys(str(r[h]).strip() for r in file_rows if str(r[h]).strip()))
                sensitive = classify_header(h) == "sensitive"
                profiles.append({"key": column_key(name, h), "file": name, "header": h, "samples": ["<withheld>"] if sensitive else vals[:3],
                                 "non_empty": sum(1 for r in file_rows if str(r[h]).strip()), "rows": len(file_rows), "sensitive": sensitive})
        run.update(source_rows=rows, source_headers=headers, profiles=profiles, status="processing")
        self.event(run, "ingest", f"Read {len({p['file'] for p in profiles})} source file(s): {len(rows)} rows, {len(profiles)} columns", source_files=len({p["file"] for p in profiles}), source_rows=len(rows), columns=len(profiles))

    # ------------------------------------------------------------------ classification bookkeeping
    def merge_classifications(self, run: dict, items: list[dict], pending: set[str]) -> None:
        by_key = {p["key"]: p for p in run["profiles"]}
        current = {c["key"]: c for c in run["classifications"]}
        for it in items:
            key = column_key(it.get("source_file", ""), it.get("source_header", ""))
            if key in pending and key in by_key:
                current[key] = {**it, "key": key, "status": "pending"}
        for key in pending:        # guardrail: sensitive columns are never mapped, whatever the model said
            if by_key[key]["sensitive"]:
                current[key] = self._forced_ignore(by_key[key], "Sensitive column (bank / government ID / health): never migrated.")
        run["classifications"] = [current[p["key"]] for p in run["profiles"] if p["key"] in current]

    @staticmethod
    def _forced_ignore(p: dict, why: str) -> dict:
        return {"key": p["key"], "source_file": p["file"], "source_header": p["header"], "target_field": None, "transform": "ignore", "confidence": 1.0, "rationale": why, "alternatives": [], "status": "ignored"}

    def finalize_classification(self, run: dict, issues: dict[str, list[str]]) -> None:
        have = {c["key"] for c in run["classifications"]}
        for p in run["profiles"]:
            if p["key"] not in have:
                run["classifications"].append({**self._forced_ignore(p, "No classification was returned."), "confidence": 0.0, "status": "pending"})
        esc = []
        for c in run["classifications"]:
            k = c["key"]
            if k in issues and not (c["status"] == "ignored" and c["confidence"] == 1.0):
                c["status"] = "needs_review"
                cands = list(dict.fromkeys(t for t in [c.get("target_field"), *c.get("alternatives", [])] if t in TARGET_FIELDS)) or sorted(TARGET_FIELDS)
                proposal = f"may map to {c['target_field']}" if c.get("target_field") else "could not be classified"
                esc.append({"id": str(uuid.uuid4()), "key": f"col:{k}", "scope": "column", "type": "ambiguous_mapping", "field": c["source_header"], "source_file": c["source_file"], "column_key": k,
                            "status": OPEN, "reason": f"{c['source_header']} ({c['source_file']}) {proposal}, but: {' '.join(issues[k][:2])}", "candidates": cands,
                            "context": {"sample_rows": next((p["samples"] for p in run["profiles"] if p["key"] == k), []), "policy": "Pick the target field only if it is semantically correct; otherwise Reject to skip this column."}})
            elif c["target_field"] is None or c["transform"] == "ignore":
                c["status"] = "ignored"
            else:
                c["status"] = "accepted"
        run["escalations"] = [e for e in run["escalations"] if e["scope"] != "column"] + esc
        self.event(run, "classifier_validator", f"Classification settled: {sum(c['status']=='accepted' for c in run['classifications'])} mapped, {sum(c['status']=='ignored' for c in run['classifications'])} ignored, {len(esc)} sent to human review",
                   mapped=sum(c["status"] == "accepted" for c in run["classifications"]), review=len(esc))

    def accepted_targets(self, run: dict, file: str | None = None) -> set[str]:
        out: set[str] = set()
        for c in run["classifications"]:
            if c["status"] == "accepted" and c.get("target_field") and (file is None or c["source_file"] == file):
                out.add(c["target_field"])
                if c["transform"] == "split_full_name":
                    out.add("last_name")
        return out

    def plannable_fields(self, run: dict) -> list[dict]:
        return [{"target_field": f, "samples": sample_raw(run, f)} for f in sorted(self.accepted_targets(run))]

    # ------------------------------------------------------------------ build / evaluate / settle
    def ensure_plan(self, run: dict) -> None:
        planned = {p["target_field"] for p in run["cleaning_plan"]}
        for f in sorted(self.accepted_targets(run) - planned):
            run["cleaning_plan"].append({"target_field": f, "tools": safe_plan(f), "confidence": 0.9, "rationale": "Deterministic schema-driven plan."})

    def build(self, run: dict) -> None:
        run["records"] = build_records(run, TARGET_SCHEMA)
        fixes = Counter(f for r in run["records"] for f in FORMAT_TOOL if r.get(f) not in (None, "") and str(r[f]) != str(r.get(f"_raw_{f}", r[f])))
        self.event(run, "cleaner", f"Built {len(run['records'])} record(s) from {len(run['source_rows'])} source rows; applied the tool plan", records=len(run["records"]), **{f"fixed_{k}": v for k, v in sorted(fixes.items())})

    def evaluate(self, run: dict, validator: Any = None) -> list[dict]:
        plan = {p["target_field"]: p["tools"] for p in run["cleaning_plan"]}
        run["unmapped_required"] = sorted(REQUIRED_FIELDS - self.accepted_targets(run))
        if validator:
            findings = validator.review_records(run, run["records"], plan)
        else:
            findings = checks.validate_records(run["records"], plan, set(run["unmapped_required"]), set(run["waivers"]))
        return [f for f in findings if f["key"] not in set(run["waivers"])]

    def settle(self, run: dict, findings: list[dict], llm_warnings: list[dict] | None = None) -> None:
        if llm_warnings is not None:
            run["llm_warnings"] = llm_warnings
        errors = [f for f in findings if f["severity"] == "error"]
        cols = [e for e in run["escalations"] if e["scope"] == "column"]
        existing = {e["key"]: e for e in run["escalations"] if e["scope"] == "record"}
        groups: dict[str, list[dict]] = {}          # one card per (record, root cause)
        for f in errors:
            groups.setdefault(f["key"] if f["group"] == f["field"] else f"{f['type']}:{f['subject']}:{f['group']}", []).append(f)
        now: dict[str, list[dict]] = groups
        recs = []
        for k, fs in now.items():
            card = self._card(run, k, fs)
            if k in existing:
                e = existing[k]
                if e["status"] == "auto_resolved":
                    e["status"] = OPEN
                if e["status"] == OPEN:
                    e.update({key: v for key, v in card.items() if key not in ("id", "status")})
                recs.append(e)
            else:
                recs.append(card)
        for k, e in existing.items():
            if k not in now:
                if e["status"] == OPEN:
                    e["status"] = "auto_resolved"
                recs.append(e)
        run["escalations"] = self._sync_missing_required(run, cols) + recs
        run["warnings"] = run.get("llm_warnings", []) + checks.soft_flags(run["records"])
        blocked = self.blocked(run)
        rejected = {e["record_id"] for e in run["escalations"] if e["scope"] == "record" and e["status"] == "rejected"} | set(run["excluded"])
        for r in run["records"]:
            s = str(r.get("employee_id") or r["_source_row"])
            r["_status"] = "rejected" if s in rejected else "needs_review" if s in blocked else "approved"
        run["summary"] = {"source_rows": len(run["source_rows"]), "records": len(run["records"]), "approved": sum(r["_status"] == "approved" for r in run["records"]),
                          "needs_review": sum(r["_status"] == "needs_review" for r in run["records"]), "rejected": sum(r["_status"] == "rejected" for r in run["records"]),
                          "open_escalations": sum(e["status"] == OPEN for e in run["escalations"]), "merged": len(run.get("merges", [])), "warnings": len(run["warnings"]),
                          "auto_fixes": dict(Counter(f for r in run["records"] for f in FORMAT_TOOL if r.get(f) not in (None, "") and str(r[f]) != str(r.get(f"_raw_{f}", r[f]))))}

    ACTIONS = {"approved": "Apply suggested fix", "corrected": "Correct value", "rejected": "Reject record", "merged": "Merge", "keep_both": "Keep both"}

    def _card(self, run: dict, key: str, fs: list[dict]) -> dict:
        f0, subject = fs[0], fs[0]["subject"]
        rec = next((r for r in run["records"] if str(r.get("employee_id") or r["_source_row"]) == subject), {})
        fields = list(dict.fromkeys(f["field"] for f in fs))
        if fields == ["record"]:
            fields = [f for f in TARGET_SCHEMA["properties"] if rec.get(f) not in (None, "")]
        shown = [{"field": f, "source": str(rec.get(f"_raw_{f}") or ""), "cleaned": "" if rec.get(f) is None else rec.get(f)} for f in fields]
        sug = next((f["suggestion"] for f in fs if f["suggestion"]), None)
        if f0["type"] == "identity_collision":
            acts = ["merged", "keep_both", "rejected"]
        elif f0["type"] == "missing_required_value":
            acts = ["corrected", "rejected"]
        else:
            acts = ["approved", "corrected", "rejected"]
        actions = [{"action": a, "label": "Approve as-is" if a == "approved" and not (sug and (sug["changes"] or sug["action"])) else self.ACTIONS[a],
                    "default": a == acts[0]} for a in acts]
        return {"id": str(uuid.uuid4()), "key": key, "scope": "record", "record_id": subject, "type": f0["type"], "field": f0["field"], "fields": fields,
                "finding_keys": [f["key"] for f in fs], "status": OPEN, "reason": " ".join(dict.fromkeys(f["feedback"] for f in fs)), "candidates": f0["candidates"],
                "suggested_fix": sug, "actions": actions,
                "context": {"sample_rows": [{"employee_id": subject, "field": x["field"], "source_value": x["source"], "cleaned_value": x["cleaned"]} for x in shown],
                            "fields": shown, "related": f0["related"], "policy": checks.POLICY.get(f0["type"], ""), **f0["context"]}}

    def _sync_missing_required(self, run: dict, cols: list[dict]) -> list[dict]:
        accepted = self.accepted_targets(run)
        proposed = {c["target_field"] for c in run["classifications"] if c["status"] == "needs_review" and c.get("target_field")}
        wanted: dict[str, tuple[str, str | None]] = {f"missing:{f}": (f, None) for f in sorted(REQUIRED_FIELDS - accepted - proposed - {"employee_id"})}
        for file in dict.fromkeys(p["file"] for p in run["profiles"]):
            got = self.accepted_targets(run, file) | {c["target_field"] for c in run["classifications"] if c["source_file"] == file and c["status"] == "needs_review" and c.get("target_field")}
            if "employee_id" not in got:
                wanted[f"missing:employee_id:{file}"] = ("employee_id", file)
        out, have = [], set()
        for e in cols:
            if e["type"] == "missing_required_target":
                have.add(e["key"])
                if e["key"] not in wanted and e["status"] == OPEN:
                    e["status"] = "auto_resolved"
            out.append(e)
        for key, (f, file) in wanted.items():
            if key in have:
                continue
            cands = [c["key"] for c in run["classifications"] if c["status"] in {"ignored", "needs_review"} and (file is None or c["source_file"] == file) and classify_header(c["source_header"]) != "sensitive"]
            out.append({"id": str(uuid.uuid4()), "key": key, "scope": "column", "type": "missing_required_target", "field": f, "source_file": file or "", "status": OPEN,
                        "reason": f"No source column was confidently mapped to required field {f}" + (f" in {file}." if file else "."), "candidates": cands,
                        "context": {"policy": "Pick the source column that holds this field, or upload a corrected file. Nothing is guessed."}})
        return out

    def blocked(self, run: dict) -> set[str]:
        return {e["record_id"] for e in run["escalations"] if e["scope"] == "record" and e["status"] in {OPEN, "rejected"}} | set(run["excluded"])

    def rebuild(self, run: dict) -> None:
        """Deterministic replay used after every human decision (no LLM calls)."""
        self.ensure_plan(run)
        self.build(run)
        self.settle(run, self.evaluate(run))
        run["status"] = "ready_to_push"

    # ------------------------------------------------------------------ human decisions
    def _clean_override(self, run: dict, field: str, value: str) -> Any:
        if field == "employee_id":
            raise ValueError("employee_id cannot be edited; reject the record instead")
        if value.strip() == "":
            if field in REQUIRED_FIELDS:
                raise ValueError(f"{field} is required and cannot be blank")
            return None
        if field == "manager_employee_id":
            ids = {str(r["employee_id"]).upper(): str(r["employee_id"]) for r in run["records"] if r.get("employee_id")}
            if value.strip().upper() not in ids:
                raise ValueError(f"{value} is not an employee id in this migration")
            return ids[value.strip().upper()]
        tool = FORMAT_TOOL.get(field)
        cell = Cell(clean_text(value), value)
        cell = run_tools(field, cell, [tool] if tool and TOOLS[tool].stage == "cell" else [], {"region": REGION, "enum": ENUM_STATUS, "dayfirst": None}, "cell")
        if (msg := checks.intrinsic_issue(field, cell.value)) or cell.confidence < CLEAN_MIN:
            raise ValueError(f"'{value}' is not a valid {field}: {msg or cell.note}")
        return cell.value

    def resolve(self, run: dict, escalation_id: str, action: str, selected_value: str | None = None, field: str | None = None) -> None:
        item = next((e for e in run["escalations"] if e["id"] == escalation_id), None)
        if not item:
            raise LookupError(f"Escalation {escalation_id} not found")
        if item["scope"] == "column":
            if action in {"merged", "keep_both"}:
                raise ValueError("Merge / keep both only apply to duplicate-record cards")
            self._resolve_column(run, item, action, selected_value)
            detail = f"Consultant {action} a {item['type'].replace('_', ' ')} item"
        else:
            detail = self._resolve_record(run, item, action, selected_value, field)
        item["status"], item["resolution"] = action, selected_value
        self.event(run, "human_review", detail, action=action, scope=item["scope"], type=item["type"])
        self.rebuild(run)

    def _resolve_record(self, run: dict, item: dict, action: str, selected: str | None, field: str | None) -> str:
        subject, sug, keys = item["record_id"], item.get("suggested_fix"), item.get("finding_keys") or [item["key"]]
        label = item["type"].replace("_", " ")
        if action in {"merged", "keep_both"} and item["type"] != "identity_collision":
            raise ValueError("Merge / keep both only apply to duplicate-record cards")
        if action == "approved" and selected not in (None, "") and item.get("candidates"):      # a candidate picked in the UI is a correction
            action, field = "corrected", field or item["field"]
        if action == "approved" and sug and sug.get("action") == "merge":
            action = "merged"
        if action == "merged":
            s, d = sug["survivor"], sug["merged"]
            if selected:
                if selected not in (s, d):
                    raise ValueError(f"Survivor must be {s} or {d}")
                s, d = selected, (d if selected == s else s)
            run["merges"].append({"survivor": s, "merged": d, "at": datetime.now(UTC).isoformat()})
            run["overrides"].pop(d, None)
            return f"Consultant merged duplicate {d} into {s} (survivor: {'lower ID' if not selected else 'chosen by consultant'})"
        if action == "keep_both":
            run["waivers"] += keys
            ids = ", ".join(x["employee_id"] for x in (item["context"].get("related") or [{"employee_id": subject}]))
            return f"Consultant kept both records as separate employees ({ids}); duplicate check waived"
        if action == "approved":
            if sug and sug.get("changes"):
                for ch in sug["changes"]:
                    run["overrides"].setdefault(subject, {})[ch["field"]] = self._clean_override(run, ch["field"], ch["to"])
                return f"Consultant approved the suggested fix for {subject} ({label}: {', '.join(c['field'] for c in sug['changes'])})"
            run["waivers"] += keys
            return f"Consultant approved {subject} as-is ({label})"
        if action == "rejected":
            run["excluded"].append(subject)
            return f"Consultant rejected record {subject} ({label}); it will not be pushed"
        field = field or item["field"]
        if field not in (item.get("fields") or [item["field"]]):
            raise ValueError(f"{field} is not part of this card ({', '.join(item.get('fields') or [item['field']])})")
        if selected is None:
            raise ValueError("A corrected value is required")
        run["overrides"].setdefault(subject, {})[field] = self._clean_override(run, field, selected)
        return f"Consultant corrected {field} on {subject} ({label})"

    def _resolve_column(self, run: dict, item: dict, action: str, selected: str | None) -> None:
        if item["type"] == "missing_required_target":
            if action == "rejected":
                return
            col = next((c for c in run["classifications"] if c["key"] == selected), None)
            if not col:
                raise ValueError("Select one of the listed source columns")
            target = item["field"]
        else:
            col = next((c for c in run["classifications"] if c["key"] == item["column_key"]), None)
            if col is None:
                raise LookupError("Column not found")
            if action == "rejected":
                col.update(target_field=None, transform="ignore", confidence=1.0, status="ignored", rationale="Human rejected this mapping.")
                return
            target = selected or col.get("target_field")
            if target not in TARGET_FIELDS:
                raise ValueError(f"{target!r} is not a target schema field")
        transform = col["transform"] if (col["transform"] == "split_full_name" and target == "first_name") or (col["transform"] == "monthly_to_annual" and target == "annual_salary") else "direct"
        col.update(target_field=target, transform=transform, confidence=1.0, status="accepted", rationale="Human-reviewed mapping.")

    # ------------------------------------------------------------------ target
    def push(self, run: dict, retry: bool = False) -> None:
        """Push guard: a record is written only if it has no pending/rejected card AND still passes validation right now."""
        pending = self.blocked(run)
        failed = {f["subject"]: f["feedback"] for f in self.evaluate(run) if f["severity"] == "error"}
        outcomes = []
        for rec in run["records"]:
            eid = rec.get("employee_id")
            sid = str(eid or rec["_source_row"])
            if sid in pending or sid in failed:
                outcomes.append({"employee_id": eid or rec["_source_row"], "status": "blocked",
                                 "reason": "Awaiting or failed human review" if sid in pending else f"Failed validation: {failed[sid]}"})
                continue
            decision = authorize_target_write(rec, set() if eid else {str(rec["_source_row"])}, "retry" if retry else "upsert")
            if not decision.allowed:
                outcomes.append({"employee_id": eid or rec["_source_row"], "status": "blocked", "reason": decision.reason})
                continue
            try:
                self.store.upsert_target({k: v for k, v in rec.items() if not k.startswith("_") and v is not None}, run["batch_id"])   # `_*` = internal audit metadata
                outcomes.append({"employee_id": eid, "status": "success", "reason": ""})
                rec["_status"] = "pushed"
            except Exception as error:  # noqa: BLE001
                outcomes.append({"employee_id": eid, "status": "failed", "reason": f"Target API error: {type(error).__name__}"})
        run["outcomes"] = outcomes
        ok, failed = sum(o["status"] == "success" for o in outcomes), sum(o["status"] == "failed" for o in outcomes)
        run["status"] = "partial_failure" if failed else "completed_with_review" if any(o["status"] == "blocked" for o in outcomes) else "completed"
        self.event(run, "target_executor", f"{'Retried' if retry else 'Pushed'}: {ok} succeeded, {sum(o['status']=='blocked' for o in outcomes)} withheld for review, {failed} failed",
                   successes=ok, blocked=sum(o["status"] == "blocked" for o in outcomes), failed=failed)

    def rollback(self, run: dict) -> None:
        count = self.store.rollback_batch(run["batch_id"])
        for o in run["outcomes"]:
            if o["status"] == "success":
                o["status"] = "rolled_back"
        for r in run["records"]:
            if r.get("_status") == "pushed":
                r["_status"] = "approved"
        run["status"] = "rolled_back"
        self.event(run, "rollback", f"Rolled back batch: {count} target record(s) restored to their previous state", rolled_back=count)

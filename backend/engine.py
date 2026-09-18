from __future__ import annotations

import csv
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .observability import Observability
from .policies import ALIASES, COMPOSITE_SOURCE_ALIASES, REQUIRED_FIELDS, authorize_target_write, classify_header, normalize_header
from .store import Store


class MigrationEngine:
    def __init__(self, store: Store, fixtures_dir: str = "data/fixtures") -> None:
        self.store, self.fixtures_dir, self.obs = store, Path(fixtures_dir), Observability()

    def _event(self, run: dict[str, Any], stage: str, message: str, **metrics: Any) -> None:
        run["events"].append({"at": datetime.now(UTC).isoformat(), "stage": stage, "message": message, "metrics": metrics})
        self.obs.emit(stage, run["id"], metrics)

    def create_run(self, source_files: list[dict[str, str]] | None = None) -> dict[str, Any]:
        run = {"id": str(uuid.uuid4()), "status": "created", "batch_id": str(uuid.uuid4()), "events": [], "escalations": [], "records": [], "mapping": {}, "source_headers": [], "source_files": source_files or []}
        self.store.save_run(run)
        return run

    def ingest(self, run_id: str) -> dict[str, Any] | None:
        run = self.store.get_run(run_id)
        if not run:
            return None
        rows, headers = self._load(run.get("source_files"))
        # Source rows stay in application storage; graph/trace state holds only the run ID.
        run["source_headers"] = headers
        run["source_rows"] = rows
        run["status"] = "profiling"
        source_count = len(run["source_files"]) or len(list(self.fixtures_dir.glob("*.csv")))
        self._event(run, "ingest", "Ingested source exports", source_files=source_count, source_rows=len(rows))
        self.store.save_run(run)
        return run

    def profile(self, run_id: str) -> dict[str, Any] | None:
        run = self.store.get_run(run_id)
        if not run:
            return None
        headers = run.get("source_headers", [])
        self._event(run, "profile", "Profiled schema and masked value shapes", unique_headers=len(set(headers)), sensitive_headers=sum(classify_header(h) == "sensitive" for h in headers))
        self.store.save_run(run)
        return run

    def seed_mapping(self, run_id: str) -> dict[str, Any] | None:
        run = self.store.get_run(run_id)
        if not run:
            return None
        run["mapping"], run["escalations"] = self._map_headers(run.get("source_headers", []), run.get("source_rows", []))
        self._event(run, "mapping_seed", "Created conservative mapping candidates", mapped_fields=len(run["mapping"]), escalations=len(run["escalations"]))
        self.store.save_run(run)
        return run

    def apply_agent_proposal(self, run_id: str, proposals: list[dict[str, Any]], model: str, fallback: bool = False) -> dict[str, Any] | None:
        run = self.store.get_run(run_id)
        if not run:
            return None
        allowed_targets = set(ALIASES)
        headers = set(run.get("source_headers", []))
        accepted = 0
        for proposal in proposals:
            source, target = proposal.get("source_header"), proposal.get("target_field")
            confidence = float(proposal.get("confidence", 0))
            if source not in headers or target not in allowed_targets or classify_header(source) == "sensitive":
                continue
            if confidence >= 0.85:
                run["mapping"][source] = target
                accepted += 1
            elif not any(e.get("field") == source and e["status"] == "open" for e in run["escalations"]):
                run["escalations"].append({"id": str(uuid.uuid4()), "type": "low_confidence_mapping", "field": source, "candidates": [target], "status": "open", "reason": proposal.get("rationale", "AI confidence did not clear the autonomous threshold")})
        run["agent"] = {"model": model, "mode": "deterministic_fallback" if fallback else "gemini", "proposal_count": len(proposals)}
        self._event(run, "mapping_agent", "Generated structured mapping proposal", model=model, proposals=len(proposals), accepted=accepted, fallback=fallback)
        self.store.save_run(run)
        return run

    def transform_and_validate(self, run_id: str) -> dict[str, Any] | None:
        run = self.store.get_run(run_id)
        if not run:
            return None
        run["records"] = self._reconcile(run["mapping"], run.get("source_rows"))
        invalid = [record for record in run["records"] if REQUIRED_FIELDS - set(key for key, value in record.items() if value)]
        self._event(run, "transform", "Normalized and reconciled source rows", reconciled_records=len(run["records"]))
        self._event(run, "quality_evaluator", "Validated records against target schema", valid_records=len(run["records"]) - len(invalid), invalid_records=len(invalid))
        self.store.save_run(run)
        return run

    def route(self, run_id: str) -> dict[str, Any] | None:
        run = self.store.get_run(run_id)
        if not run:
            return None
        run["status"] = "needs_review" if any(e["status"] == "open" for e in run["escalations"]) else "ready_to_push"
        self._event(run, "policy_router", "Selected next graph route", route=run["status"])
        self.store.save_run(run)
        return run

    def _load(self, source_files: list[dict[str, str]] | None = None) -> tuple[list[dict[str, str]], list[str]]:
        rows, headers = [], []
        paths = [Path(item["path"]) for item in source_files] if source_files else sorted(self.fixtures_dir.glob("*.csv"))
        for path in paths:
            if path.suffix.lower() == ".csv":
                with path.open(newline="", encoding="utf-8-sig") as handle:
                    reader = csv.DictReader(handle)
                    headers.extend(reader.fieldnames or [])
                    rows.extend({**row, "_source": path.name} for row in reader)
            elif path.suffix.lower() in {".xlsx", ".xlsm"}:
                from openpyxl import load_workbook
                book = load_workbook(path, read_only=True, data_only=True)
                sheet = book.active
                values = sheet.iter_rows(values_only=True)
                header_row = next(values, ())
                fieldnames = [str(value).strip() if value is not None else "" for value in header_row]
                headers.extend(fieldnames)
                for raw_row in values:
                    rows.append({**{fieldnames[index]: "" if value is None else str(value) for index, value in enumerate(raw_row) if index < len(fieldnames) and fieldnames[index]}, "_source": path.name})
                book.close()
        return rows, headers

    def _map_headers(self, headers: list[str], source_rows: list[dict[str, str]] | None = None) -> tuple[dict[str, str], list[dict[str, Any]]]:
        mapping, escalations = {}, []
        for header in sorted(set(headers)):
            if header == "_source":
                continue
            if classify_header(header) == "sensitive":
                escalations.append({"id": str(uuid.uuid4()), "type": "sensitive_column", "field": header, "status": "open", "reason": "Unexpected sensitive source column was quarantined", "context": {"source_values": ["Sensitive values hidden by policy"], "policy": "Quarantine or explicitly reject this column; its values are never sent to Gemini."}})
                continue
            key = normalize_header(header)
            candidates = [target for target, aliases in ALIASES.items() if key in aliases]
            if key in COMPOSITE_SOURCE_ALIASES:
                mapping[header] = "__full_name__"
                continue
            if header == "Start":
                candidates = ["start_date", "job_title"]  # intentional demo ambiguity
            if len(candidates) == 1:
                mapping[header] = candidates[0]
            elif len(candidates) > 1:
                samples = self._source_preview(header, source_rows)
                escalations.append({"id": str(uuid.uuid4()), "type": "ambiguous_mapping", "field": header, "candidates": candidates, "status": "open", "reason": "Top mapping candidates have insufficient confidence margin", "context": {"affected_record_count": len([row for row in (source_rows or []) if row.get(header)]), "sample_rows": samples, "policy": "Select one target field. Applying it will map this source column for every affected record."}})
            else:
                escalations.append({"id": str(uuid.uuid4()), "type": "unmapped_column", "field": header, "status": "open", "reason": "No safe target field was identified for this source column", "context": {"affected_record_count": len([row for row in (source_rows or []) if row.get(header)]), "sample_rows": self._source_preview(header, source_rows), "policy": "Reject/quarantine this column, or extend the versioned target schema before mapping it."}})
        return mapping, escalations

    def _source_preview(self, header: str, source_rows: list[dict[str, str]] | None = None) -> list[dict[str, str]]:
        """Context shown to the assigned consultant; sensitive source fields are never previewed."""
        if classify_header(header) == "sensitive":
            return [{"record": "Sensitive field", "value": "Values hidden by policy"}]
        rows = source_rows if source_rows is not None else self._load()[0]
        identifier_headers = ("Employee ID", "Staff ID", "Emp ID", "ID", "employee_id")
        preview = []
        for index, row in enumerate(rows, start=2):
            if not row.get(header):
                continue
            record_id = next((str(row[key]) for key in identifier_headers if row.get(key)), f"Row {index}")
            preview.append({"record": record_id, "value": str(row[header])[:80]})
            if len(preview) == 3:
                break
        return preview

    @staticmethod
    def _normalise(record: dict[str, str]) -> dict[str, str]:
        output = {key: value.strip() for key, value in record.items() if value is not None}
        if output.get("email"):
            output["email"] = output["email"].lower()
        if output.get("employment_status"):
            output["employment_status"] = output["employment_status"].lower().replace(" ", "_")
        if output.get("__full_name__"):
            full_name = re.sub(r"\s+", " ", output.pop("__full_name__")).strip()
            romanized = re.search(r"\(([^()]+)\)", full_name)
            name_for_split = romanized.group(1).strip() if romanized else full_name
            pieces = name_for_split.split(" ")
            if len(pieces) >= 2:
                output.setdefault("first_name", pieces[0].title())
                output.setdefault("last_name", " ".join(pieces[1:]).title())
            else:
                output["full_name_unresolved"] = full_name
        for date_field in ("start_date", "date_of_birth", "end_date"):
            if not output.get(date_field):
                continue
            for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d %b %Y", "%d-%b-%Y"):
                try:
                    output[date_field] = datetime.strptime(output[date_field], fmt).date().isoformat()
                    break
                except ValueError:
                    pass
        return output

    def _reconcile(self, mapping: dict[str, str], source_rows: list[dict[str, str]] | None = None) -> list[dict[str, str]]:
        """Merge rows without allowing an empty alias to overwrite a known value."""
        rows = source_rows if source_rows is not None else self._load()[0]
        by_email: dict[str, dict[str, str]] = {}
        for source in rows:
            mapped: dict[str, str] = {}
            for header, target in mapping.items():
                value = source.get(header)
                if value:
                    mapped[target] = value
            record = self._normalise(mapped)
            email = record.get("email", "")
            if email and email in by_email:
                by_email[email].update({key: value for key, value in record.items() if value})
            else:
                by_email[email] = record
        return list(by_email.values())

    def start_demo(self) -> dict[str, Any]:
        """Compatibility helper for deterministic tests; HTTP uses the LangGraph workflow."""
        run = self.create_run()
        self.ingest(run["id"])
        self.profile(run["id"])
        self.seed_mapping(run["id"])
        self.transform_and_validate(run["id"])
        return self.route(run["id"]) or run

    def resolve(self, run_id: str, escalation_id: str, action: str, selected_value: str | None = None) -> dict[str, Any] | None:
        run = self.store.get_run(run_id)
        if not run:
            return None
        item = next((e for e in run["escalations"] if e["id"] == escalation_id), None)
        if not item:
            return None
        item["status"], item["resolution"] = action, selected_value
        if action == "approved" and item["type"] == "ambiguous_mapping" and selected_value:
            run["mapping"][item["field"]] = selected_value
            run["records"] = self._reconcile(run["mapping"], run.get("source_rows"))
        if all(e["status"] != "open" for e in run["escalations"]):
            run["status"] = "ready_to_push"
        self._event(run, "review", "Human resolved escalation", action=action, escalation_type=item["type"])
        self.store.save_run(run)
        return run

    def push(self, run_id: str, retry: bool = False) -> dict[str, Any] | None:
        run = self.store.get_run(run_id)
        if not run:
            return None
        unresolved = sum(e["status"] == "open" for e in run["escalations"])
        outcomes = []
        for record in run["records"]:
            decision = authorize_target_write(record, unresolved, "retry" if retry else "upsert")
            if not decision.allowed:
                outcomes.append({"employee_id": record.get("employee_id"), "status": "blocked", "reason": decision.reason})
                continue
            # Deterministic transient failure makes retry visible in the demo.
            if record.get("employee_id") == "EMP-004" and not retry:
                outcomes.append({"employee_id": "EMP-004", "status": "failed", "reason": "Mock target timed out"})
                continue
            self.store.upsert_target(record, run["batch_id"])
            outcomes.append({"employee_id": record["employee_id"], "status": "success"})
        run["outcomes"] = outcomes
        run["status"] = "completed" if all(o["status"] == "success" for o in outcomes) else "push_failed"
        self._event(run, "push", "Target push completed", successes=sum(o["status"] == "success" for o in outcomes), failures=sum(o["status"] == "failed" for o in outcomes))
        self.store.save_run(run)
        return run

    def rollback(self, run_id: str) -> dict[str, Any] | None:
        run = self.store.get_run(run_id)
        if not run:
            return None
        count = self.store.rollback_batch(run["batch_id"])
        run["status"] = "rolled_back"
        self._event(run, "rollback", "Rolled back target changes", rolled_back=count)
        self.store.save_run(run)
        return run

"""LangGraph generator-evaluator workflow.

  ingest -> classify (generator) <-> validate_classification (evaluator, bounded retries with comments)
         -> clean (generator)    <-> validate_cleaning       (evaluator, bounded retries with comments)
         -> route

Unresolved items after MAX_RETRIES become human escalations; every other record stays approved and can be pushed.
Human decisions, push and rollback are further operations on the same graph. Data lives in the store (escalations must
survive restarts); the graph state carries the loop control: attempts and evaluator feedback per generator.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Literal, TypedDict

from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph

from .agents import CleanerAgent, ColumnClassifierAgent, LLMProvider, StrictValidatorAgent
from .engine import MigrationEngine
from .policies import MAX_RETRIES

load_dotenv()


class MigrationState(TypedDict, total=False):
    run_id: str
    operation: Literal["start", "resolve", "push", "retry", "rollback"]
    decision: dict[str, Any]
    classifier_attempts: int
    classifier_feedback: dict[str, list[str]]     # column key  -> evaluator comments
    cleaner_attempts: int
    cleaner_feedback: dict[str, list[str]]        # target field -> evaluator comments


class MigrationGraph:
    def __init__(self, engine: MigrationEngine, llm: LLMProvider | None = None) -> None:
        self.engine = engine
        self.llm = llm or LLMProvider()
        self.classifier, self.cleaner, self.validator = ColumnClassifierAgent(self.llm), CleanerAgent(self.llm), StrictValidatorAgent(self.llm)
        self.graph = self._build()

    def _build(self):
        g = StateGraph(MigrationState)
        for name, fn in [("dispatch", self._dispatch), ("ingest", self._ingest), ("classify", self._classify), ("validate_classification", self._validate_classification),
                         ("clean", self._clean), ("validate_cleaning", self._validate_cleaning), ("route", self._route),
                         ("resolve", self._resolve), ("push", self._push), ("rollback", self._rollback)]:
            g.add_node(name, fn)
        g.add_edge(START, "dispatch")
        g.add_conditional_edges("dispatch", lambda s: s["operation"], {"start": "ingest", "resolve": "resolve", "push": "push", "retry": "push", "rollback": "rollback"})
        g.add_edge("ingest", "classify")
        g.add_edge("classify", "validate_classification")
        g.add_conditional_edges("validate_classification", lambda s: "classify" if s.get("classifier_feedback") else "clean", {"classify": "classify", "clean": "clean"})
        g.add_edge("clean", "validate_cleaning")
        g.add_conditional_edges("validate_cleaning", lambda s: "clean" if s.get("cleaner_feedback") else "route", {"clean": "clean", "route": "route"})
        for end in ("route", "resolve", "push", "rollback"):
            g.add_edge(end, END)
        return g.compile()

    # ------------------------------------------------------------------ nodes
    def _dispatch(self, s: MigrationState) -> MigrationState:
        return {"classifier_attempts": 0, "classifier_feedback": {}, "cleaner_attempts": 0, "cleaner_feedback": {}}

    def _ingest(self, s: MigrationState) -> MigrationState:
        run = self.engine.load(s["run_id"]); assert run
        self.engine.ingest(run)
        self.engine.save(run)
        return {}

    def _classify(self, s: MigrationState) -> MigrationState:
        run = self.engine.load(s["run_id"]); assert run
        attempt, feedback = s.get("classifier_attempts", 0) + 1, s.get("classifier_feedback", {})
        pending = set(feedback) if attempt > 1 else {p["key"] for p in run["profiles"]}
        previous = {c["key"]: c for c in run["classifications"]}
        items, error = self.classifier.generate([p for p in run["profiles"] if p["key"] in pending], previous, feedback if attempt > 1 else {})
        self.engine.merge_classifications(run, items, pending)
        run["agent"] = {"mode": "llm" if self.llm.available else "no-llm", "architecture": "classifier + cleaner generators; strict validator evaluator"}
        self.engine.event(run, "classifier", f"Classified {len(pending)} column(s) (attempt {attempt})" + (f" - {error}" if error else ""), attempt=attempt, columns=len(pending))
        self.engine.save(run)
        return {"classifier_attempts": attempt}

    def _validate_classification(self, s: MigrationState) -> MigrationState:
        run = self.engine.load(s["run_id"]); assert run
        issues, attempt = self.validator.review_classification(run), s["classifier_attempts"]
        if issues and attempt <= MAX_RETRIES and self.llm.available:
            comments = [f"{k.split('::', 1)[1]}: {' '.join(v)}" for k, v in issues.items()]
            run["retry_log"].append({"agent": "classifier", "attempt": attempt, "outcome": "rejected", "feedback": comments})
            self.engine.event(run, "classifier_validator", f"Rejected {len(issues)} column mapping(s); returning comments to the classifier", attempt=attempt, issues=len(issues))
            self.engine.save(run)
            return {"classifier_feedback": issues}
        if issues:
            run["retry_log"].append({"agent": "classifier", "attempt": attempt, "outcome": "escalated", "feedback": [f"{k.split('::', 1)[1]}: {' '.join(v)}" for k, v in issues.items()]})
        self.engine.finalize_classification(run, issues)
        self.engine.save(run)
        return {"classifier_feedback": {}}

    def _clean(self, s: MigrationState) -> MigrationState:
        run = self.engine.load(s["run_id"]); assert run
        attempt, feedback = s.get("cleaner_attempts", 0) + 1, s.get("cleaner_feedback", {})
        fields = self.engine.plannable_fields(run)
        pending = fields if attempt == 1 else [f for f in fields if f["target_field"] in feedback]
        plan, error = self.cleaner.generate(pending, feedback if attempt > 1 else {})
        merged = {p["target_field"]: p for p in run["cleaning_plan"]}
        merged.update({p["target_field"]: p for p in plan})
        run["cleaning_plan"] = list(merged.values())
        self.engine.ensure_plan(run)          # a field the model skipped still gets the deterministic plan
        self.engine.event(run, "cleaner", f"Planned cleaning tools for {len(pending)} field(s) (attempt {attempt})" + (f" - {error}; used deterministic plan" if error else ""), attempt=attempt, fields=len(pending))
        self.engine.build(run)
        self.engine.save(run)
        return {"cleaner_attempts": attempt}

    def _validate_cleaning(self, s: MigrationState) -> MigrationState:
        run = self.engine.load(s["run_id"]); assert run
        attempt = s["cleaner_attempts"]
        findings = self.engine.evaluate(run, self.validator)
        feedback: dict[str, list[str]] = defaultdict(list)
        for f in findings:
            if f["retryable"] and len(feedback[f["field"]]) < 4:
                feedback[f["field"]].append(f["feedback"])
        plan_issues = self.validator.review_plan(run["cleaning_plan"], [f["target_field"] for f in self.engine.plannable_fields(run)])
        for field, msgs in plan_issues.items():
            feedback[field] += msgs
        if feedback and attempt <= MAX_RETRIES and self.llm.available:
            run["retry_log"].append({"agent": "cleaner", "attempt": attempt, "outcome": "rejected", "feedback": [f"{k}: {' '.join(v)}" for k, v in feedback.items()]})
            self.engine.event(run, "cleaner_validator", f"Rejected cleaning for {len(feedback)} field(s); returning comments to the cleaner", attempt=attempt, issues=len(feedback))
            self.engine.save(run)
            return {"cleaner_feedback": dict(feedback)}
        warnings = [{"type": "cleaning_plan", "subject": k, "field": k, "message": " ".join(v)} for k, v in plan_issues.items()]
        if self.llm.available:
            warnings += self.validator.cleaning_critic(run["records"]) + self.validator.org_critic(run["records"])
        if feedback:
            run["retry_log"].append({"agent": "cleaner", "attempt": attempt, "outcome": "escalated", "feedback": [f"{k}: {' '.join(v)}" for k, v in feedback.items()]})
        self.engine.settle(run, findings, warnings)
        self.engine.event(run, "validator", f"Strict validator finished: {sum(f['severity']=='error' for f in findings)} item(s) need a human, {run['summary']['approved']} record(s) approved", rejected=sum(f["severity"] == "error" for f in findings), approved=run["summary"]["approved"])
        self.engine.save(run)
        return {"cleaner_feedback": {}}

    def _route(self, s: MigrationState) -> MigrationState:
        run = self.engine.load(s["run_id"]); assert run
        run["status"] = "ready_to_push"
        self.engine.event(run, "router", "Isolated only unresolved work for human review; everything else is approved", open_escalations=run["summary"]["open_escalations"])
        self.engine.save(run)
        return {}

    def _resolve(self, s: MigrationState) -> MigrationState:
        run = self.engine.load(s["run_id"]); assert run
        d = s["decision"]
        self.engine.resolve(run, d["escalation_id"], d["action"], d.get("selected_value"), d.get("field"))
        self.engine.save(run)
        return {}

    def _push(self, s: MigrationState) -> MigrationState:
        run = self.engine.load(s["run_id"]); assert run
        self.engine.push(run, retry=s["operation"] == "retry")
        self.engine.save(run)
        return {}

    def _rollback(self, s: MigrationState) -> MigrationState:
        run = self.engine.load(s["run_id"]); assert run
        self.engine.rollback(run)
        self.engine.save(run)
        return {}

    # ------------------------------------------------------------------ public API
    def start(self, source_files: list[dict[str, str]] | None = None) -> dict[str, Any]:
        run = self.engine.create_run(source_files)
        self.graph.invoke({"run_id": run["id"], "operation": "start"})
        return self.engine.load(run["id"]) or run

    def resume_review(self, run_id: str, decision: dict[str, Any]) -> dict[str, Any] | None:
        if not self.engine.load(run_id):
            return None
        self.graph.invoke({"run_id": run_id, "operation": "resolve", "decision": decision})
        return self.engine.load(run_id)

    def execute(self, run_id: str, operation: Literal["push", "retry", "rollback"]) -> dict[str, Any] | None:
        if not self.engine.load(run_id):
            return None
        self.graph.invoke({"run_id": run_id, "operation": operation})
        return self.engine.load(run_id)

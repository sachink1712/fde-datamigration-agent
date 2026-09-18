from backend.agent import MigrationGraph
from backend.engine import MigrationEngine
from backend.store import Store


def test_langgraph_pauses_for_review_then_resumes(monkeypatch, tmp_path):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("REQUIRE_GEMINI", "false")
    graph = MigrationGraph(MigrationEngine(Store(str(tmp_path / "agent.db")), "data/fixtures"))
    run = graph.start()
    assert run["status"] == "needs_review"
    assert run["agent"]["mode"] == "deterministic_fallback"

    for item in list(run["escalations"]):
        if item["status"] == "open":
            run = graph.resume_review(run["id"], {"escalation_id": item["id"], "action": "approved", "selected_value": "start_date" if item["type"] == "ambiguous_mapping" else None})

    assert run and run["status"] == "ready_to_push"

from backend.engine import MigrationEngine
from backend.store import Store


def engine(tmp_path):
    return MigrationEngine(Store(str(tmp_path / "migration.db")), "data/fixtures")


def test_demo_routes_only_real_ambiguities_to_review(tmp_path):
    run = engine(tmp_path).start_demo()
    assert run["status"] == "needs_review"
    assert any(item["type"] == "ambiguous_mapping" for item in run["escalations"])
    assert any(item["type"] == "sensitive_column" for item in run["escalations"])


def test_no_target_write_before_all_reviews_resolved(tmp_path):
    service = engine(tmp_path)
    run = service.start_demo()
    blocked = service.push(run["id"])
    assert all(item["status"] == "blocked" for item in blocked["outcomes"])


def test_resolved_run_pushes_then_retries_mock_failure(tmp_path):
    service = engine(tmp_path)
    run = service.start_demo()
    for item in run["escalations"]:
        service.resolve(run["id"], item["id"], "approved", "start_date" if item["type"] == "ambiguous_mapping" else None)
    first = service.push(run["id"])
    assert any(item["status"] == "failed" for item in first["outcomes"])
    retried = service.push(run["id"], retry=True)
    assert retried["status"] == "completed"

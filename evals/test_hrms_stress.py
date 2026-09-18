from backend.engine import MigrationEngine
from backend.store import Store


def test_full_name_is_safely_decomposed_and_hrms_fields_map(tmp_path):
    engine = MigrationEngine(Store(str(tmp_path / "stress.db")))
    run = engine.create_run([{"name": "stress.csv", "path": "data/employees_stress_test.csv", "size_bytes": "1"}])
    engine.ingest(run["id"])
    engine.seed_mapping(run["id"])
    engine.transform_and_validate(run["id"])
    current = engine.store.get_run(run["id"])
    priya = next(record for record in current["records"] if record.get("employee_id") == "E1001")
    assert priya["first_name"] == "Priya"
    assert priya["last_name"] == "Sharma"
    assert priya["date_of_birth"] == "1992-04-15"
    assert priya["manager_employee_id"] == "E1010"
    assert "Annual CTC" in [item["field"] for item in current["escalations"]]

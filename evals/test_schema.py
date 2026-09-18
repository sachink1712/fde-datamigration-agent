from backend.policies import ALIASES, REQUIRED_FIELDS, TARGET_SCHEMA


def test_target_schema_is_the_policy_source_of_truth():
    assert TARGET_SCHEMA["title"] == "Employee migration target schema"
    assert REQUIRED_FIELDS == set(TARGET_SCHEMA["required"])
    assert "employee_id" in ALIASES
    assert "start_date" in ALIASES

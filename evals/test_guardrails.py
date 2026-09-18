from backend.policies import authorize_target_write, safe_trace_value


def test_pii_is_redacted_in_observability_payloads():
    assert safe_trace_value("email", "ada@example.com").startswith("redacted:")
    assert "ada@example.com" not in safe_trace_value("email", "ada@example.com")


def test_write_policy_blocks_unresolved_or_invalid_records():
    record = {"employee_id": "EMP-1", "first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com", "start_date": "2023-01-01"}
    assert not authorize_target_write(record, unresolved_count=1).allowed
    assert not authorize_target_write({**record, "email": "bad"}, unresolved_count=0).allowed
    assert authorize_target_write(record, unresolved_count=0).allowed

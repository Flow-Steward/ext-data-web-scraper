"""Bounded URL-policy failures stay actionable through the service client."""

from fake_service import example, schema_errors


def test_sample_timeout_preserves_the_configuration_field(invoke, service):
    answer = example("error.validation_failed.json")
    message = "URL pattern url_include[0] (5d06171653cf) exceeded its execution budget"
    answer["error"].update(message=message, fields=[{
        "field": "url_include[0]", "code": "pattern_timeout", "message": message,
    }])
    assert not schema_errors("error.schema.json", answer)
    service.override("POST", r"^/v1/samples$", 422, answer)
    config = example("post_config_validate.response.json")["config"]
    response = invoke("fetch_samples", {"config": config})
    assert response["ok"] is False
    assert "url_include[0]" in response["error"]
    assert "execution budget" in response["error"]


def test_failed_url_policy_run_matches_the_bundled_contract():
    answer = example("get_run.response.json")
    answer.update(state="failed", outcome=None, stop_reason=None, result=None)
    answer["error"] = {
        "code": "pattern_timeout",
        "message": "URL pattern url_include[0] exceeded its execution budget",
        "at": answer["finished_at"],
    }
    assert not schema_errors("run_summary.schema.json", answer)

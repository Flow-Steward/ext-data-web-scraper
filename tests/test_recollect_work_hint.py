"""Re-collect hints describe the service's recovery work, including source cursors."""

import pytest

from fake_service import example, schema_errors
from test_pages_0_1_18 import query  # noqa: F401 -- page-query fixture

RUN = "run_01JCXA7Q3M5VZ8K2N4P6R8T0W1"
PARENT = "run_01PARENT000000000000000001"


@pytest.mark.parametrize(
    ("work", "description"),
    [
        ({"url_count": 0, "source_count": 2, "by_reason": {}}, "2 unfinished sitemap sources"),
        (
            {"url_count": 4, "by_reason": {"stopped": 4}},
            "4 missed or queued pages",
        ),
        (
            {"url_count": 4, "source_count": 1, "by_reason": {"stopped": 4}},
            "4 missed or queued pages and 1 unfinished sitemap source",
        ),
        (
            {"url_count": 1, "source_count": 2, "by_reason": {"fetch_failed": 1}},
            "1 missed page and 2 unfinished sitemap sources",
        ),
        ({"url_count": 3, "by_reason": {"blocked": 3}}, "3 missed pages"),
        ({"url_count": 0, "by_reason": {}}, "unfinished work"),
        ({"by_reason": {}}, "unfinished work"),
    ],
    ids=["sources", "queued", "mixed-queued", "mixed-failed", "legacy", "zero", "unknown"],
)
def test_hints_describe_recollect_work(invoke, query, service, work, description):  # noqa: F811
    run = example("post_runs.response.json")
    run["recollect_of"] = {"run_id": PARENT, "reasons": ["stopped", "blocked"], **work}
    service.override("POST", rf"^/v1/runs/{PARENT}/recollect$", 201, run)
    response = invoke("recollect_run", {"run_id": PARENT})
    assert response["ok"], response
    result = response["result"]
    assert description in result["message"]
    assert "0 missed pages" not in result["message"]
    assert result["url_count"] == work.get("url_count", 0)
    assert result["parent_run_id"] == PARENT and result["created"] is True
    assert "It has its own result" in result["message"]

    for action, output in (("show_run", "state"), ("get_run_summary", "message")):
        service.override("GET", rf"^/v1/runs/{RUN}$", 200, run)
        response = invoke(action, {"run_id": RUN})
        assert response["ok"], response
        assert description in response["result"][output]
        assert "0 missed pages" not in response["result"][output]

    service.override("GET", rf"^/v1/runs/{RUN}$", 200, run)
    response = query("run_report", {"run_id": RUN})
    assert response["ok"], response
    content = response["result"]["content"]
    assert description in content
    assert "0 missed pages" not in content
    assert f"p.run_id={PARENT}" in content


def test_source_only_recollect_response_matches_service_contract():
    run = example("post_runs.response.json")
    run["recollect_of"] = {
        "run_id": PARENT,
        "reasons": ["stopped"],
        "url_count": 0,
        "source_count": 1,
        "by_reason": {},
    }
    assert not schema_errors("run_summary.schema.json", run)

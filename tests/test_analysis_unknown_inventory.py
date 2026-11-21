"""Skipped or failed inspection is unknown, rather than an empty catalogue."""

import pytest

from fake_service import example
from test_config_and_runs import _config


@pytest.mark.parametrize("inspection", ["skipped", "limited"])
def test_analysis_preserves_unknown_sitemap_inventory(invoke, service, inspection):
    config = _config(invoke)
    answer = example("post_analyze.response.json")
    answer["sitemap"].update(
        found=None,
        url_count=None,
        matching_url_count=None,
        inspection=inspection,
        url_count_capped=inspection == "limited",
    )
    service.override("POST", r"^/v1/analyze$", 200, answer)
    response = invoke("analyze_site", {"config": config})
    assert response["ok"], response
    result = response["result"]
    assert result["sitemap_found"] is None
    assert result["sitemap_url_count"] is None
    assert result["analysis"]["sitemap"]["inspection"] == inspection

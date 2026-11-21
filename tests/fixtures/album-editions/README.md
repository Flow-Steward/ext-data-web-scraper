# Optional edition coverage acceptance

This invented variable album has seven CD, LP and DVD editions. Each edition has
its own description, tracklist, label and packaging. One value of each optional
field is genuinely absent on a different edition. Generic `data-*` attributes
survive sample distillation; no supplier selectors or product URLs are used.

`initial.mapping.json` passes required title/SKU/format but fills description,
label and packaging on 0/7 rows and tracklists on 2/7 rows. `reviewed.mapping.json`
fills every optional field on 6/7 rows.
`expected.rows.json` is the literal expected data for all seven reviewed rows,
including nulls for genuine absences. These files can also be used directly by
the service's mapping engine and contract validators.

Run from the extension checkout with both projects' Python dependencies installed:

```sh
SCRAPER_SERVICE_SOURCE=/absolute/path/to/web-scraper-service \
  python -m pytest -q tests/test_optional_mapping_acceptance.py
```

Without that environment variable these five cross-repository tests skip. The
ordinary extension tests retain their independent fake-service checks. This adds
no production dependency on the service source checkout.

The acceptance executes the shipped `impl_build_mapping` YAML with the repository's
workflow walker, real extension handlers, generated `samples.md` artifacts, local
HTTP request/response schema checks, and the service's actual `mapping_test`, sample
store and extraction engine. A deterministic author adapter replaces only the paid
model boundary: it reads the attachment, starts with the deficient mapping, and
repairs only fields named by real coverage feedback. It then either keeps the
reviewed mapping, fails, or regresses required title extraction.

Budgets 0/1/2 remain bounded; remaining 6/7 optional coverage warnings remain visible
and nonfatal. Author failure and required-field regression retain the latest tested
passing mapping, including its improved optional values. This verifies the shipped
workflow policy and extraction contract offline. It does not measure a live model's
authoring quality or execute the Flow Steward platform runtime.

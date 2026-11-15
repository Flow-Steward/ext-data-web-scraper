# Web Scraper for Flow Steward

Collect product and page data from websites into a CSV or JSON Lines file for
your Flow Steward workflows, through a scraper service you run yourself.

`flowsteward.web-scraper` scrapes sites through the separate **Web Scraper
service** (a Docker container built on Crawlee for Python) and hands your workflow
the records of each crawl as `result.jsonl` (one JSON record per line) and
`result.csv`. AI builds and repairs the extraction mapping in a visible workflow
step; politeness, proxies and an optional challenge solver are set per scraper
profile and run.

- Kind: `tool_provider`, subprocess entrypoint, host contract `extension_host_v2`
- Requires Flow Steward 0.9.18 or later (extension host 1.8.0)
- Catalog category: Data (also AI)
- Licence: Apache-2.0 (see `LICENSE`, `NOTICE`)

## What it contains

| Part | Where |
|---|---|
| Connection type `scraper_profile` (service URL + token, webhook URL + signing secret, defaults, proxy tiers 1–3, challenge solver) | `contracts/connection_types.yaml`, form in `ui/components/profile_connection_form.yaml` |
| Actions for every service call, each with a Contract V2 operation and a builder form | `ui/actions/actions.yaml`, `contracts/operation_manifest.yaml`, `contracts/step_ui_manifest.yaml`, handlers in `handlers/` |
| Composite action `build_mapping` (ADR 0065), its implementation workflow, the mapping agent and skill | `artifacts/workflows/impl_build_mapping.yaml`, `artifacts/agents/`, `artifacts/skills/` |
| Read-only workflows of the extension: Scrape a site, Scraper events, Service maintenance | `artifacts/workflows/` (generated from the templates by `.github/scripts/extension_workflows.py`) |
| Templates to import into a project (Workflows → Import YAML) | `templates/scrape_site.bundle.yaml`, `templates/scraper_events.bundle.yaml`, `templates/service_maintenance.bundle.yaml` (same content as the `.bundle.json` files) |
| Extension pages: Profiles (first), Service, Guide, Runs, Run detail, About (Service, Runs and Run detail use the profile chosen in the page's selector; Flow Steward 0.9.18+) | `ui/` |
| The service contract the actions call (copied; a test fails when it drifts) | `service_contract/` |

### Actions

| Action | Service call | Kind |
|---|---|---|
| `validate_config` | `GET /health`, `POST /v1/config/validate` | read |
| `analyze_site` | `POST /v1/analyze` | read |
| `fetch_samples` | `POST /v1/samples` | read |
| `test_mapping` | `POST /v1/mappings/test` | read |
| `get_mappings` / `activate_mapping` | `GET` / `PUT /v1/sites/{site}/mappings` | read / write |
| `export_mappings` / `import_mappings` | `GET /v1/sites` + mappings / `PUT` per template | read / write |
| `start_run` | `POST /v1/runs` with `Idempotency-Key: fs:<job>:<step>[:<loop>]` (also a declared external effect) | write |
| `stop_run` / `resume_run` | `POST /v1/runs/{id}/stop` / `resume` | write |
| `get_run_summary` | `GET /v1/runs/{id}` | read |
| `claim_event` / `mark_event_stage` / `complete_event` | `POST /v1/events/{id}/claim` / `stage` / `complete` | write |
| `get_mapping_request` / `fail_mapping_request` | `GET /v1/mapping-requests/{id}` / `POST …/fail` | read / write |
| `get_result` | `GET /v1/runs/{id}/result` + download → platform artifact `result.jsonl` (size, SHA-256 and record count checked first) | write |
| `build_mapping` | composite: agent → `test_mapping` → refine ≤ 2× | — |
| `test_connection`, `test_proxy`, `test_captcha_solver` | connection form checks | read |
| `list_runs`, `show_run`, `show_unextracted`, `result_download`, `resend_event` | run pages | read / write |

Every action answers with the contract's machine code mapped to a clear
message; no secret ever appears in a result, a log line or an error (every
answer is scrubbed against the profile's secrets).

## Set up and use

The guide is in the extension itself: page **Guide** (and the same text in
[`docs/setup-guide.md`](docs/setup-guide.md)) has the setup checklist, every run form
field with its default and when to set it, the steps of both workflows and a
troubleshooting list. The extension opens on **Profiles**, where the connection to
the scraper service is created; **About** has the short version.

## Use it responsibly

Scrape only sites whose terms allow it. The service respects robots.txt by
default (ignoring it needs an operator switch on the service), keeps a pause
between pages and slows down when a site struggles. It does not log in to sites,
and it cannot promise to get past CAPTCHAs or bot protection: the model-free
checkbox click and paid solvers are off unless the service operator allows them,
and a site that blocks the scraper ends the run with that reason.

## Limits worth knowing

- The extension timeout is one value for every action: 330 s by default, at
  most 3630 s, set by the operator with `FS_EXTENSION_OPERATION_TIMEOUT_SECONDS`.
- The result artifact may be 200 MiB by default (`FS_EXTENSION_ARTIFACT_MAX_BYTES`
  raises it); local file storage caps an upload at 100 MiB, so a larger result
  needs S3 artifact storage. The service stops a run before its result passes its
  own maximum (99 MiB by default).
- A service on a private network needs `FS_ALLOW_PRIVATE_REMOTE_URLS=1` on the
  platform.

## Develop

Tests run in a container, never on the host:

```bash
docker run --rm -v "$PWD":/ext -w /ext python:3.12-slim sh -c \
  "pip install -q -r requirements-dev.txt && FS_ALLOW_PRIVATE_REMOTE_URLS=1 python -m pytest -q tests"
```

The tests use `tests/fake_service.py`, a fake scraper service built from the
contract: it serves the contract's examples and validates every request and
response against `service_contract/schemas`. `tests/workflow_sim.py` walks the
bundled workflows offline against it.

Package with `python .github/scripts/package_extension.py` (writes
`dist/flowsteward.web-scraper-<version>.zip`). `docs/DEV-LOG.md` records the
decisions taken while building it.

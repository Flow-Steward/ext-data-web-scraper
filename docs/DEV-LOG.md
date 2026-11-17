# Development log

Newest last. Commands ran in a disposable `python:3.12-slim` (arm64) container
`ws-ext-dev` with this repository mounted at `/ext`, never on the host.

## M1 — skeleton, manifest, connection type, schemas, fake service, first actions

- Layout modelled on `ext-example-showcase` (handlers/, ui/, contracts/,
  artifacts/) and `ext-document-docling` (external service by URL + token,
  artifact output, packaging, CI). SDK wheel `flowsteward_extension_sdk-0.4.0`
  copied from the reference extensions (sha256 ffb5f376…af, equal to the
  attestation on `flow-steward/main`).
- The contract schemas live in `service_contract/schemas/`, not `schemas/`:
  the platform's YAML-only rule forbids `schemas/*.schema.json`
  (`FORBIDDEN_EXTENSION_CONTRACT_JSON_PATTERNS`). The contract examples are test
  fixtures in `tests/contract_examples/` and are not shipped.
- Connection type `scraper_profile` with flat fields (questions file Q1, Q2).
  Provider presets fill only protocol/host/port when they are empty:
  Oxylabs `pr.oxylabs.io:7777`, Bright Data `brd.superproxy.io:33335`,
  Decodo `gate.decodo.com:7000` (from the providers' public docs as I know
  them; not re-checked online, nothing was contacted). Storm Proxies has no
  global gateway, so it has no preset.
- The custom proxy list is one masked (`secret`) field; URLs are separated by
  spaces, commas or new lines, because a password input has one line.
- `tests/fake_service.py` serves the contract examples, keeps state for
  idempotent starts, mapping versions and claims, and validates every request
  body and every response against the vendored schemas.
- Contract and manifest generation: `contracts/operation_manifest.yaml`,
  `contracts/step_ui_manifest.yaml`, `ui/actions/actions.yaml` and the profile
  form were produced once by a throw-away script and are maintained by hand.

## M2 — all actions, artifact output, error mapping, idempotency

- Every service call of section 10.1 is an action with a Contract V2
  operation, a builder form and its error codes. Reads are `operation_kind:
  source` (safe for the host to retry), everything that changes service state
  or writes an artifact is `action`.
- Error mapping: every request-level code of `error.schema.json` maps to an
  extension code and a sentence (`handlers/errors.py`); field codes are turned
  into "field … is above the service limit (100000)" lines; unknown codes are
  read by HTTP status. Every response passes through a scrubber holding every
  secret of the profile, so even a service message that quoted a secret comes
  out as `[redacted]` (tested).
- Idempotency: `fs:<job id>:<step id>[:<loop position>]` from the SDK runtime
  context. The host field that carries a loop position is not documented; the
  extension reads `loop_position`, `item_index`, `for_each_index` or `loop.*`
  from the raw runtime context and, inside a For each without any of them, a
  hash of the item's `step_execution_id` (questions file Q5). Only a numeric
  position is sent as `platform.loop_position` (the contract's pattern).
- `get_result` downloads into a temporary file (resuming with `Range` and
  `If-Range` up to three times), checks byte count, SHA-256 and the number of
  lines against the descriptor, and only then streams the file into the
  artifact grant. A mismatch stores nothing. A result above the grant is
  refused before the download, naming `FS_EXTENSION_ARTIFACT_MAX_BYTES`.
- Claim answers "already claimed / handled / stale" are a normal result
  (`claimed: false`), so the template stops cleanly instead of failing.
- Run page actions (`list_runs`, `show_run`, `show_unextracted`,
  `result_download`, `resend_event`) return at most a dozen plain lines plus
  `details` (questions file Q3, Q4).

## M3 — build_mapping (composite action), agent, skill

- `build_mapping` is `handler: {mode: workflow, workflow_id: impl_build_mapping}`,
  workflow-visible, with a same-ID operation without `connection_type_ids`; the
  operation's inputs/outputs equal the workflow's Interface (tested).
- `impl_build_mapping`: `prepare` (set_variable) → `write_mapping` (agent
  `scraper_mapping_author`, skill `build_extraction_mapping`) → `test_candidate`
  (connector `extension.flowsteward.web-scraper`, `connection_binding_jsonb:
  {slot: profile}`, operation `test_mapping`) → `Passed?` → `done_passed`, or
  `Refinement left?` → rework edge back to the agent / `done_not_passed`.
  `test_mapping` counts the attempts and answers `can_refine`, because a
  set_variable step cannot add numbers. The rework edge has `rework_limit: 5`
  (the job config caps `max_refinements` at 5), so the platform's rework
  budget never pauses the loop.
- Strict structured output: the platform builds the agent's structured-output
  schema from the skill's `expected_outputs` rows (name + top-level type,
  `prompt_agent_artifacts._resolve_final_response_schema`), so the deep
  mapping schema cannot be enforced by the model call itself. The full schema
  is in the agent's system prompt and the skill's example; the extension
  checks the shape before calling the service, and the service's own
  validation errors come back as failures for the next refinement
  (questions file Q7).
- `tests/workflow_sim.py` walks the workflow offline (connector steps call the
  real handlers against the fake service, the agent is scripted) and proves:
  refine-then-pass (2 generations), give up after 1 + 2 generations with
  `passed: false`, configurable refinements, repair starting from the previous
  mapping.

## M4 — the two template bundles

- `templates/scrape_site.bundle.json` and `templates/scraper_events.bundle.json`
  are `platform.workflow_bundle.v2` with exactly the six top-level sections the
  importer accepts (`workflow_portability_import.py`), `enabled: false`, and
  `import_hints.default_enabled: false`.
- Connector steps name the extension by package id
  (`connector_id: extension.flowsteward.web-scraper`, override `extension_id` +
  `operation_id`, no install id). The importer reports such a step as an
  extension dependency (`reason: connector_install_id_missing`) and the rewrite
  fills `connector_install_id` with the install chosen at import
  (`workflow_portability_export.discover_dependencies`,
  `workflow_portability_rewrite._rewrite_step`).
- The connection of every step is the symbolic `connection_ref:
  scraper_profile`, listed under `dependencies.project_connections`; import maps
  it to a project connection of this extension. No connection id, install id or
  secret is in either bundle (tested).
- The `build_mapping` step is an action Invoke whose `action.install_id` and
  `workflow_id` (`extension.flowsteward-web-scraper.impl_build_mapping`) use the
  package id as a placeholder install, so the rewrite's prefix replacement
  turns both into the mapped install. This exact shape is a guess from the
  rewrite code; it needs the live import (questions file Q9).
- `Scrape a site`: run form (9 fields, only the first two required, no
  defaults), `Site settings` (exactly the 22 settings of 5.3 with empty values,
  described in the step and in a canvas note), Validate → URL list? →
  Analyze → Choose strategy → Get stored mappings → Fetch samples → Rebuild
  asked? → Stored mapping? → Test stored / Build mapping (AI) → Test mapping
  before activation → Activate (fail on conflict) → Start run. Canvas notes
  mark "before the start" and "review the mapping".
- `Scraper events`: Claim → Ours? → switch on the event type
  (`run_completed`, `mapping_required`, `problem`, `run_failed`, default) →
  … → Complete event. `run_completed` resumes on the recorded stages:
  completed → straight to Complete; processed → skip Get result and the
  processing; else Get result → tabular_feed read (working example) → Mark
  processed → Mark completed.
- A bug the template walk found: when `claim_event` did not get the event it
  returned fewer keys, and the platform fails a step whose output mapping
  names a missing key. Every action now returns every declared output on
  success (asserted for every call in the tests).
- The walker treats an absent optional run input (`state.inputs.urls`) as
  allowed. Whether the platform passes `null` or fails such an input mapping
  is a live check (questions file).

## M5 — UI pages, queries, row actions

- Pages: Overview (markdown + health panel fed by the query
  `extension_health`), Profiles (the `scraper_profile` connection form with
  Test service / Test proxy tiers / Test challenge solver), Runs and Run
  detail.
- Runs and Run detail are connection forms of `scraper_profile` whose buttons
  are actions, because a page query gets no connection (questions file Q3).
  Their `config_fields` is only `profile_name`, which the forms do not show,
  so pressing the platform's Save button there changes nothing; their
  `secret_fields` is empty.
- Resend asks for confirmation the way the service allows it: the first press
  without "Take over a live claim" is refused by the service and the message
  names the job that holds the claim (`/jobs/<job id>`); ticking the box and
  pressing again revokes it. "Expected delivery number" makes a double click
  harmless.
- Grids with row actions were not used: a grid row comes only from a query.

## M6 — docs, packaging, release checks, platform validation

- Docs: `README.md`, `docs/setup-guide.md` (the four steps with the exact
  signed-webhook settings of contract section 9 and the steps that must stay),
  `docs/tutorial-outline.md`, `NOTICE`, `LICENSE` (Apache-2.0, copied from a
  reference extension), `icon.png` (the Flow Steward mark of the reference
  extensions).
- Packaging and CI copied from `ext-document-docling` (`ci.yml`, `release.yml`,
  `package_extension.py`, `catalog_check.py` with its tests); the packager also
  leaves out `tests/contract_examples/` (they carry the contract's documented
  fake secrets). `.github/requirements-test.txt` adds `jsonschema` for CI.
  The catalog checker fetches a pinned file from GitHub; it was not run here.
- `release/checks.py` adapted: installed, implementation hidden, service
  reachable (a failure naming the missing inputs when the stand has no service),
  and NOT RUN entries with reasons.
- Platform tooling (no platform image was built or started): the existing
  local image `flow-steward-fs-audit:latest` (arm64) was run with a
  `git archive` export of `flow-steward/main` (abaac4b991) mounted read-only
  at `/app`:
  - `extensions validate --health --marketplace` (`validate_extensions`):
    manifest compile and validate ok, icon ok, health healthy, marketplace
    checks passed, UI pages checks passed, schema keywords executable. It first
    failed on select options with an empty value; fixed.
  - `composite_action_descriptor_issues` (release rules of ADR 0065): none.
  - `validate_workflow_definition` for `impl_build_mapping` and both templates:
    none. Canvas notes against the install-time note contract: none.
  - Bundle import parser `parse_bundle` and `_topology_issues`: both bundles
    parse, no topology issue. `discover_dependencies` finds exactly the
    extension `flowsteward.web-scraper` and the connection `scraper_profile`.
    `action_config_issues` for both `build_mapping` steps: none.
  - `extension_gate static` (compileall, ruff check, ruff format --check,
    mypy, bandit): ok after fixing 17 mypy findings, one bandit false positive
    and two unformatted files.
  - `extension_gate tests` refused to run outside the launcher's dedicated test
    runtime (not run).
- Not validated with platform code: a real import with dependency mapping and
  save-time checks against a database, the save-time action Invoke check
  (`action_invoke_issues` needs installed composites), the webhook, the
  artifact upload, a real agent run.

## Final test run

From commit 0722238, in the `ws-ext-dev` container (`python:3.12-slim`,
arm64, the service contract mounted read-only so the drift test runs):

    python -m pytest -p no:cacheprovider -q -rs tests .github/tests/test_catalog_check.py
    166 passed, 0 failed, 0 skipped in 74.7 s

## Fix round after the independent review (2026-11-13)

Review: `logs/web-scraper-2026-11-12/REVIEW-extension.md` in the platform
repository; acceptance notes A1-A5 from the curator's stand. Platform code read
only from `flow-steward/main` (abaac4b).

- Templates (B1-B4, M1): `connection_binding_jsonb: {connection_id}`; Build
  mapping's `workflow_id` is `extension.flowsteward.web-scraper.impl_build_mapping`
  (the importer replaces the `projection_bridge_id(<source install>, "")` prefix);
  `Start run` writes `state.scraper_run_id` and the workflow output is
  `scraper_run_id`; optional run inputs are read as `state.inputs.<x>|default:null`
  (the handlers read null as "not set"), the child's `test_candidate` uses
  `|default:0.9`, `[]`, `"auto"`, `2`.
- Error routes (M2): the platform has `error_policy: {on_error: route_to_step}`
  with an `error` edge (`workflow_definition_validation.py:284-330`,
  `step_hub_results.py:405-452`). Get result / the tabular example → stop_fail
  `Processing failed` (claim kept, recovery = Resend with take-over); Build mapping
  / Activate → `fail_mapping_request` → `complete_event` → stop_fail. No new cycle.
  m5: a conflicting repair closes its request; m6: resume after `processed` goes
  through Mark processed again.
- B6 (platform refuses `webhook_signature`): the guide's primary path is auth
  mode `hmac_sha256` with header `X-Scraper-Body-Signature` (prefix `sha256=`),
  which the service adds to every delivery; the timestamped mode is an appendix.
- `tests/workflow_sim.py` now follows the platform where it is strict (run start
  drops absent optional inputs, a connector input without a default that is not
  found fails the step, Invoke skips empty optional inputs, reserved roots,
  error routes). New tests run every step's mapping against required-only inputs.
- Bundles ship also as `templates/*.bundle.yaml` (`.github/scripts/bundle_yaml.py`;
  the importer's picker accepts .yaml/.yml only and parses with yaml.safe_load).
- Commands ran in the existing local image `flow-steward-fs-audit:latest`
  (arm64, Python 3.12.14, pytest 9.0.3, SDK 0.4.0, ruff, mypy, bandit), network
  off, repository at `/ext`, service contract read-only at `/contract`.

Platform validation (offline, `.github/scripts/platform_check.py`, platform
`core/` from `git archive flow-steward/main` mounted read-only at `/app`, the
extension as a clean copy of the tracked files):

- `validate_extensions(health, marketplace)`: passed (manifest, icon, health,
  marketplace, UI pages, schema keywords).
- `scan_extension_code` (the extension page's code review): no errors, no review
  items.
- `validate_workflow_definition`, `validate_workflow_state_selectors`,
  `validate_step_config_keys`, `_validate_step_state_write_targets`: none, for
  impl_build_mapping and both bundles before and after the import rewrite.
- `parse_bundle` (YAML and JSON), `_topology_issues`, `discover_dependencies`
  (one extension, one project connection), then `_import_payload` with the
  extension mapped to an install and `scraper_profile` to a connection: every
  connector step gets the install and `{connection_id}`, the Invoke's
  `workflow_id` equals `projection_bridge_id(install, "impl_build_mapping")`,
  `action_config_issues` none.
- `validate_extension_connector_step` with the release's operation manifest in
  place of the database: none (the old `connection_ref` gives
  `workflow.connection_required`).
- `connector_mapping._render_input_payload` with required inputs only: nothing
  unresolved.
- Control: the same script over the templates of 54cfc8e reports B1 (18 x
  connection_required), B2 (workflow_id mismatch), B3
  (`workflow.step_output_mapping_target_reserved`) and B4/M1 (11 unresolved).
- Not reached offline: `action_invoke_issues` and readiness against installed
  composites, connections and secrets in a database; the webhook trigger; a run.

Final test run (commit after the docs), in the container above:

    python -m pytest -p no:cacheprovider -q -rs tests .github/tests/test_catalog_check.py
    206 passed, 0 failed, 0 skipped in 87.5 s

Then `python .github/scripts/package_extension.py` rebuilt
`dist/flowsteward.web-scraper-0.1.0.zip` (0.2 MB zipped, 0.8 MB unpacked).

## 0.1.2 — stand findings (2026-11-13)

- Blocker: editing a saved profile failed schema validation. With
  `data.config_fields` declared, the platform's save (`connection-save.ts`
  `applyConnectionConfigValues`, 99-124) writes the raw form value of every
  listed path; for a hidden field (`visible_if` false) there is no field to type
  it, and the edit form holds "" for it, so "" reached the integer fields. The
  profile form no longer declares `config_fields`: the browser saves only the
  visible non-secret fields; a hidden field keeps its last stored value and a
  tier or solver set to "Not used" is ignored by the handlers. `tier` (the test
  button's select, now saved too) is an optional property of the config schema.
  `tests/connection_form_sim.py` ports the save logic (paths in its docstring);
  `tests/test_connection_form_save.py` runs create and edit with every tier and
  the solver hidden and shown, switches tiers back off, clears visible integers,
  and saves the two run-page forms over a profile. Before the change the edit
  cases failed exactly like the stand.
- A6/A7: approve each profile again after an extension update; the webhook URL
  is the address at which the service reaches the platform.
- A8: the bundle carries no step positions, so the builder lays steps out from
  x >= 0 (`builder/layout.ts`); the notes now stand in a column at x = -640.
- The service contract gained `examples/webhook_body_signature.vector.json`;
  copied to `tests/contract_examples/` with a test that checks both signatures
  the platform way.
- Version 0.1.2 (0.1.1 is installed on the stand).

Test run: 230 passed, 0 failed, 0 skipped (same container and command as above).
Offline platform check (`.github/scripts/platform_check.py`): `extensions validate`
passed for 0.1.2, code scan empty, every workflow check none.

## 0.1.3 — the mapping agent sees the samples and may answer (2026-11-13)

Recorded model requests on the stand showed two blockers in `write_mapping`.

- AI-1, samples never reached the model. The platform renders an agent step's
  inputs into the prompt bounded to 4000 characters in all and 800 per value
  (flow-steward/main `agents/prompt_builder.py` 318-379); the Structured Task
  Envelope repeats the child's inputs bounded to 900 (`:293-307`). Chosen
  delivery: a text file bound to the agent step (`override.agent.file_bindings`).
  `agents/agent_files.py:31-62,65-135` attaches every bound text file whole to
  the task message while all of them fit FS_AGENT_FILE_INLINE_MAX_CHARS
  (default 2,000,000, `agent_file_policy.py:127-138`), otherwise it pages them
  through a `read_agent_file` tool. The file must be `.md`/`text/markdown`, free
  of control characters (`agent_file_runtime.py:27,137-145`) and owned by the
  agent step's job (`:191-196`), so `impl_build_mapping` writes it itself:
  new operation `write_samples_file` (artifact grant `samples.md`), step
  `Samples for the agent` before the agent and again before every refinement
  (the rework edge now targets it). The file carries the task, the previous
  candidate with its failures, and every sample (URL, title, template hint,
  JSON-LD, meta, distilled HTML; HTML min(64 KiB, 384 KiB / n) per sample,
  JSON-LD and meta with shared budgets, whole file at most 1,000,000).
  The agent step no longer maps `samples`. Tools backed by extension actions
  (option a) were not chosen: the agent inside an action implementation
  resolved no runtime tools ("registry_only"), and a file needs no new
  platform wiring.
- AI-2, the structured-output schema had `artifacts: {properties: {}}`. An
  installed skill reaches the runtime as its bundle document
  (`lifecycle_descriptor_io.py:266`, `projected_artifact_rows.py:165-272`), and
  `SkillManifest.from_db_row` reads only `_outputs` and `output_schema`
  (`registry/skill_manifest.py:33-72`), so `expected_outputs` was lost and
  `step_output_contract._add_skill_fields` (306-366) declared nothing. The
  skill now carries `output_schema` (`mappings`: array of objects, required;
  `notes`: string).
- `tests/tools/render_agent_prompt.py` runs the platform's own code offline
  (connector resolver, AgentFileRuntime with a fake artifact plane,
  `prepare_agent_files`, `PromptBuilder`, `resolve_step_output_contract`,
  `build_phase_result_json_schema`) on four samples (`tests/page_fixtures.py`:
  three books.toscrape.com pages rebuilt from their markup, one WooCommerce
  product page with invented content). Result: samples.md 20,268 characters,
  appended whole (20,519 characters), every sample's URL, title, hint, JSON-LD,
  meta and HTML present; `## Step Inputs` 456 characters without samples;
  artifacts.mappings declared and required, an answer with mapping objects
  passes, one without is refused; the 0.1.2 skill gives `properties: {}`.
  The platform's security scanner did not run offline (it needs the database).
- Scrape a site: `Build mapping (AI)` routes errors to `Build mapping failed`
  with a message naming the usual causes.
- Stand items: A13 Show run prints 0 for counters a young run has not sent, and
  the profile-data line is no longer named `secrets` (the platform blanks such
  keys); A15 guide: Start test run suppresses `Start run`, a crawl needs Run
  workflow with the acknowledgement.
- Service contract 2026-11-13 synced (CHANGES.md): repairs pass the run's
  `max_refinements`; Show run shows `records_dropped_over_limit` and
  `tier_escalations` when present; warning `target_pages_guessed` has a text.

Tests: 241 passed. Offline platform check: passed for 0.1.3.

## 0.1.4 — events with more fields than the four inputs (2026-11-13)

The stand refused every delivery with `workflow.input.unknown` for
`occurred_at`, `delivery_generation`, ...: with `strict_inputs: true` the platform
refuses every undeclared JSON body key of a webhook
(`workflow_entrypoint_input_service.py:113-160`). `Scraper events` now declares
`strict_inputs: false` (read from `settings.callable`, `workflow_contract.py:528`;
the builder's Interface panel loads and saves the stored value,
`detail-workflow-settings-controller.ts:342,585`), so additive event fields need no
change and the guide no extra step; UI, API and manual starts are unchanged.
`tests/tools/check_webhook_inputs.py` runs the platform's `build_inputs` and the
run-start validation over every event contract example and a body with an
unknown field (blank and explicit mapping), and confirms `Scrape a site` stays
strict for its API/UI start.

## 0.1.5 — the mapping travels as JSON text (2026-11-13)

With a real OpenAI model every `write_mapping` returned mappings as empty objects.
The platform sends `with_structured_output(method=json_schema, strict=True)` for
the `openai` provider and first rewrites the schema
(`agents/common_runtime_structured_utils.py:11-73`): every object without
declared properties becomes `{properties: {}, additionalProperties: false}`, so
`items: {type: object}` admitted only `{}`. Anthropic and Google get the schema
unchanged (no strict flag). Chosen: the skill outputs `mappings_json` (string,
required) and `notes`; `test_mapping` takes `mappings_json`, reads a fenced or
plain JSON array (or one object), and turns text that is not JSON, not a list
or not objects into a `format` failure for the next refinement. A `Clear the
last answer` step empties `state.mappings_json` before every agent call. Skill
and agent instructions say the JSON array goes into `mappings_json` and that the
first answer must already contain it.
`tests/tools/render_agent_prompt.py` builds the schema with the platform's code
for openai/anthropic/google, checks the strict-mode rules, validates the two
contract mappings sent as text, shows the 0.1.3 schema admits only `{}`, and
builds the actual OpenAI request body with `langchain_openai` (response_format
json_schema, strict true, `mappings_json` string).
The result descriptor's `file_name` is now `<run id>.jsonl`: `<site>_<run id>.jsonl`
matched the scanner's `dotted_provider_token` (secret_detection.py:176-183).

## 0.1.6 — no Storm Proxies; guide and tutorial from the stand (2026-11-13)

- Owner decision: the Storm Proxies preset is removed (form options, connection
  schema enum, handlers, NOTICE). The service contract dropped it the same day
  (CHANGES.md #16); `service_contract/` is synced. An older copy of the contract
  that still lists it is tolerated by the tests (`REMOVED_PROVIDERS`).
- Setup guide: structured-output model (verified with OpenAI), editing `Site
  settings` (Literal value, Save parameters), and "Updating the extension"
  (what an update changes, re-importing templates, the pre-0.1.4 Interface fix).
- Tutorial outline: the order that worked on the stand and a troubleshooting
  list from the acceptance log.

## 0.1.7 — slow analysis and samples wait to the action's deadline (2026-11-13)

Since the service runs the protection route in analysis and samples, a protected
site took 113–190 s and the fixed 180 s wait failed `Fetch samples`. The slow calls
now wait for what is left of the action's own deadline (`handlers/budget.py`:
FS_EXTENSION_OPERATION_TIMEOUT_SECONDS clamped to 30..3600, default 300, minus 20 s,
minus 10 s for the answer: about 270 s by default); when less than 15 s is left or
the wait runs out the step fails with the retryable `service_timeout` naming the
variable. `get_result` uses the same budget module. The contract has no knob to
make `POST /v1/samples` stop early or to give it a time budget (question for the
service). Service contract synced (protection traces, `screenshot_not_found`);
the traces are not shown yet.

## 0.1.8 — debug mode on the run form (2026-11-13)

The owner wanted to choose debug or normal mode when starting a run; `debug`
was only a value of the `Site settings` step. `Scrape a site` now has the
optional run input **Debug mode** (boolean). `validate_config` takes `debug`
and sends it in the `run_inputs` layer, which the service merges before
`site_settings`, so the box overrides the step; without it the step's value
applies. Service contract synced again (`protection_trace.environment` from the
service's exit-IP environment round). Project copies of the template do not
update with the extension: import `scrape_site.bundle.yaml` again.
Tests: 262 passed (`flow-steward-fs-audit:latest`, network off, service
contract mounted for the drift check).


## 0.1.9 — protection in plain words, every setting on the run form, test runs (2026-11-13)

Findings A23, A25, A28, A29 (owner: every setting, not only two), A30 (extension half).

**Protection in plain words (A23).** `handlers/protection.py` turns each
`protection_trace` (`analysis.schema.json#/$defs/protection_trace`) into one
paragraph: URL without scheme, challenge (Cloudflare when a `cf_` cookie is
named), HTTP status, each route step and its outcome, Passed / Not passed, proxy
tier, exit country with timezone and locale (and the browser's own timezone when
it differs), browser from the page's user agent ("Firefox 158 on Windows"), the
title of a page left challenged, and the screenshot id with one sentence on
`GET /v1/screenshots/<id>`. The exit IP stays in the raw trace only.
- `Analyze site`: new outputs `protection_summary` and `protection_not_passed`
  (the raw `analysis` stays). `Fetch samples`: new output `protection_summary`.
- `Fetch samples` with no sample and a challenge not passed fails with the new
  code `protection_not_passed` (not retryable): "The site's protection check was
  not passed on <host>, so no sample page could be fetched. What to try: …" and
  the first three paragraphs. It reads the service's new `stopped` and the
  `protection_not_passed` warning, and falls back to the traces for today's
  service. The error's result carries the raw trace, `stopped` and the summary.
- A samples timeout after an analysis that met a challenge says the protection
  is the likely cause and repeats what the analysis saw: the template maps
  `state.protection_summary` / `state.protection_not_passed` into the new
  optional inputs `analysis_protection_summary` / `analysis_protection_not_passed`
  (`|default:null`; url_list mode has no analysis).
- Run detail → Show run: new line *Challenged pages* (first 5 of
  `protection.traces`, same words, screenshot sentence with the service URL).
  The platform's action-result panel shows only the first 12 plain values
  (`extension-action-result-summary.ts`, 0.9.16; `stored_profile_data` was the
  13th and never shown): `repairs` joined `templates`, `proxy` joined
  `protection`. Keys avoid `trace`/`path`/`request` (hidden) and
  `token`/`secret`/`cookie` (blanked).
- Service fields (A22): warning code `protection_not_passed` in `warnings[]` of
  `/v1/analyze` and `/v1/samples`, and `stopped = {reason, host, url,
  skipped_urls}` in the samples answer (`skipped_urls` may be 0: then no count is
  written). First coded against placeholders, then checked against the service's
  final contract (265c66a, CHANGES 20-24) and its example
  `post_samples.response.stopped.json`.
- `environment.preset_id` (A24, the browser identity kept per exit IP) is written
  as "fingerprint fp_…". The exit locale is now the country's main language
  (lv-LV, not ru-LV); the examples say so.

**Fields never filled (A26).** `test_mapping` reads `templates[].empty_fields`
and the service's `fields_never_filled` warnings (`mappings[i].fields` names the
template) and returns them in the same `warnings` output as the selector
warnings, each appended to `summary` as "Warning: …" (so `Build mapping (AI)` and
`Test mapping before activation` show it) and, on a failed candidate, added to
`failures` for the agent. The pass rule is unchanged. Without the service's
warning the extension words it from `empty_fields`.

Service contract re-synced (schemas, openapi.yaml, examples) from
web-scraper-service 265c66a; the drift test passes again. Service decisions noted
in the guide: pages listed in url_list mode are exempt from the product page
selector gate.

**Every setting is a run input (A29, owner).** The `Site settings` step is gone;
the run form has 31 optional fields (site URL, what to extract, maximum records,
product page selector, include/exclude URLs, discovery, debug first), none with a
`default` (Core 0.9.16 fixes an interface default and refuses a caller's value).
Each help text says what empty means. `validate_config` takes them all and sends
them in the `run_inputs` layer with the typed coercion of `settings.py`; the
`site_settings` input stays for copies imported before 0.1.9 (below the run
inputs). The template's entry is `Validate settings`.

**Test Run (A25).** A Test Run suppresses `Start run` (`test_mode_status:
suppressed`, outputs left unset). New `if` *Crawl started?* on
`state.scraper_run_id` (truthy on a missing value is false,
`control_flow_step_helpers._evaluate_operator_condition`) leads to the stop
*Test run: the mapping was built and tested; no crawl was started. Use Run
workflow with the acknowledgement to scrape.*; `scraper_run_id` is no longer a
required workflow output (`validate_outputs` skips a non-required one).
`workflow_sim` now leaves outputs of a suppressed result unset and checks the
required outputs at a success stop, as the platform does; a test shows the 0.1.8
shape failing there.

**Where results appear (A28).** The final message names the output
`scraper_run_id`, the separate `Scraper events` job, `Get result` and
`result.jsonl`, and Run detail (a stop message is literal text, so the run id
itself is in `run_page_hint`, which now says the same with the id). Setup guide:
"Where are my results".

**Product page selector guides the mapping (A30).** `validate_config` outputs
`target_page_selector`; `build_mapping`, `write_samples_file` and `test_mapping`
take it, and `get_mapping_request` returns it from the request's instructions
(repairs in `Scraper events`). The samples file and the skill tell the agent to
put it into every template's `match.selector`. Curator's decision: the service
itself treats a page without the selector as `not_target` even when a template
matches, so `test_mapping` does not fail a template whose selector does not carry
it (itself, or containing it; for a list `a, b`: one part). It returns the new
output `warnings` ("…match.selector does not include the product page selector
'…'; the service still skips pages without it."), appends them to `summary`, and
on a failed candidate adds them to `failures` for the agent's refinement. A
stored mapping is therefore not rebuilt when a selector is added.

Tests (`flow-steward-fs-audit:latest`, network off): 302 passed with the service
contract mounted (drift test included); 301 passed, 1 skipped (the drift test)
without it. Offline platform check against 0.9.16: `extensions validate: passed`,
`RESULT: OK`.

## 0.1.10 — reinstallable 0.1.9 (2026-11-13)

Version number only: the stand already had an earlier 0.1.9 build, and the
platform refuses to install the same version twice. Content = 0.1.9 at f21a1b5.

## 0.1.11 — browser OS and canvas noise; canvas state in the protection text (2026-11-13)

- Service contract re-synced from web-scraper-service f115e6c (CHANGES 25–29).
- `Scrape a site`: optional run inputs **Browser OS** (`browser_os`: windows,
  macos, linux, host) and **Canvas noise** (`canvas_noise`: auto, off, on) right
  after Browser; no `default` (empty = windows / auto, the help says so and when
  to try them). `validate_config` takes both, lower-cases them like the other
  enums and sends them in the `run_inputs` layer.
- Protection paragraph: "…, fingerprint fp_…, canvas noise." from
  `environment.canvas_readback` (`exact` / `noise` / `unavailable` → unreadable;
  WebGL named only when it differs), and "The host had passed earlier (click, on
  <page>) and was challenged again." from `passed_earlier`. *What to try* adds
  "a browser that reads its canvas back exactly (Browser OS linux or Canvas noise
  off)".
- Counts are singular or plural everywhere in the extension's texts
  (`common.count`): "so 1 more URL was not tried", "1 more challenged page",
  "1 record", "1 retry", "1 conflict", "1 sample page" ….
- Version 0.1.11 (0.1.10 is on the stand).

## 0.1.12 — the browser claims the machine's own OS by default (2026-11-13)

Owner's protection matrix on his Cloudflare site (acceptance row 64): Camoufox
with the host OS identity (`linux`) passed at the wait step; every Windows claim
failed after the click, with canvas noise and without it. The service's default
`browser_os` becomes `host` (service round in parallel; contract: default value
and docs only).
- Help of **Browser OS**: host is the default, protection checks usually accept
  it, a Windows or macOS claim on a Linux service is usually refused; "Empty:
  host". **Canvas noise**: auto covers readback only for a claimed OS other than
  the machine's; with the default the canvas reads back exactly.
- *What to try* names "the machine's own OS for the browser (leave Browser OS
  empty or host)" first when a trace shows a Windows or macOS claim
  (`environment.browser_os`, else the user agent), last otherwise; the canvas
  line is gone.
- Setup guide: run form text and a troubleshooting row "not passed while the
  browser claims Windows".
- A35: when `Fetch samples` finds no target page (and protection is not the
  cause), the error says what the service tried: every warning's message in order,
  then `explanation.summary` (counts and the next hint in plain words, service
  1b46904) and each `explanation.hints[]` message not already in the summary, as
  "Suggested: …", at most 12 lines, before the general advice. The whole
  `explanation` stays in the error result.
- A36: a run that produced no records (row 70: stopped after 18 pages) made `Get
  result` fail with `artifact_upload_missing`: the result is a 0-byte file and the
  platform refuses a step whose output grant received no bytes
  (`runtime_grants.finalize_uploads` needs a sha256 for every prepared output), so
  skipping the upload inside the step cannot succeed. Instead `get_run_summary`
  returns `record_count`, `has_records` and `no_records_message` ("The run produced
  no records from 18 pages loaded: it was stopped by hand. There is no result file;
  nothing was stored."; every contract `stop_reason` has words), and `Scraper
  events` has a new `if` *Any records?* after *Already processed?*: without records
  it skips `Get result` and `Read the result` and still marks the event processed
  and completed. `get_result` on an empty run fails with the new code
  `result_empty` (not retryable, no download) instead of uploading nothing; the old
  test that expected a 0-byte artifact is replaced.
- A37: Run detail's *errors* line lists errors, then warnings (such as the new
  `page_fetch_failed`, "page not loaded: <URL>: <reason>"), then debug lines.
- Service contract re-synced from web-scraper-service fa9071d (CHANGES 30–35:
  `browser_os` default `host`, 30 s wait, samples `explanation`, per-page status,
  `page_fetch_failed`); the A35 test fixture validates against it. "1 more URL
  was not tried" has been singular since 0.1.11 (`protection.stopped_text`).
- Version 0.1.12.

## 0.1.13 — the Guide in the extension; Profiles first (2026-11-13)

Documentation and first-page work only; no behaviour change.
- Pages: **Profiles** first (the landing page), then **Guide**, Runs, Run detail and
  **About** (the former Overview, now a short summary pointing to the Guide).
- **Guide** page (`ui/components/guide.yaml`, a `markdown` component) is generated
  from `docs/setup-guide.md` by `.github/scripts/build_guide_page.py` (`--check`).
  The setup guide is now written in what the page renders (0.9.16
  `extension-markdown.tsx`: headings, paragraphs, lists, bold, italic, code; no
  tables or HTML). It has how it works and where results are, the setup checklist,
  every run form field grouped as basics / set when you know better / advanced
  (default when empty and when to set it), the run buttons, every step of both
  workflows, troubleshooting and updating. `tests/test_guide.py` fails when the page
  differs from the guide, when the guide uses something the page cannot render, or
  when a run form field, a working step or a checklist item is missing.
- Every step of both templates has a one-line description (≤ 200 characters);
  canvas notes are short and point to the Guide. README points to the Guide.
- Version 0.1.13.

## 0.1.14 — product variants (2026-11-14)

ROUND2 §2/§3; service 2f63b48 (CHANGES 36–40); contract re-synced (mapping
`variants`, record `variant`, samples `variant_sources`/`microdata`, mapping test
variant figures, source kind `microdata`).
- Agent input: every sample now carries `variant_sources` (the ready source, count,
  option names, example, raw keys) and `microdata`, bounded per sample (12,000 /
  8,000 characters in state; 6,000 / 4,000 per sample and 100,000 / 40,000 over all
  samples in the samples file, so 50 samples stay under its 1,000,000 characters).
  The samples file shows them per sample ("copy one `source` into
  variants.sources as it is").
- Skill: structured data first (JSON-LD or microdata), the variants rules (copy the
  source, option fields by name, page-common values in `fields`, equal SKUs are not
  duplicates, Shopify minor units and currency), a WooCommerce example with a
  variants block (validated against the vendored mapping schema), and the input
  `variant_sample_ids`.
- `Fetch samples` outputs `variant_sample_ids` and `variant_summary`; the mapping
  request returns `variant_sample_ids`; both templates and `impl_build_mapping`
  pass them to the agent and the tests.
- `test_mapping`: reads `variant_count`, `samples_with_variants`,
  `variant_sources_used` and `variant_fields` per template ("lens_cover: 6 variants
  on 2 of 2 samples (embedded_json 2); price 6/6, markings 6/6" in the summary);
  warnings `variant_fill` (a variant field below the threshold) and
  `variants_not_found`; and a failure `variants_missing` when a template matched
  samples that carry variant data but has no variants block (otherwise the crawl
  would give one record where the page has several). The service's pass rule is
  unchanged otherwise. Structural checks of the variants block before the call.
- The result example step (`tabular_feed` read) lists no columns, so records with
  `variant` need no change there.
- Guide: "Products with variants" and two troubleshooting entries.
- Version 0.1.14.

## 0.1.15 — ROUND2 phase B: crawl scope, images, specifications, why it stopped (2026-11-14)

Service 46c7b49, 2f83def, ad40d94, 1fe4c14, 7f3560b (CHANGES 41–45); contract
re-synced from a `git archive 7f3560b` export.
- Run form after the basics: **Where to walk** (`crawl_include`), **Stay inside the
  start page's section** (`stay_in_section`), **Let the AI choose the fields**
  (`ai_choose_fields`) and **Fields hint** (`fields_hint`); typed like the others,
  sent in `run_inputs`. *Only these URLs* is now described as what to extract,
  *Never these URLs* as never fetch.
- `ai_choose_fields` / `fields_hint` flow from `validate_config` (and from a mapping
  request's instructions) through `build_mapping` / `impl_build_mapping` into the
  samples file and the agent prompt.
- Skill: images full size first (zoom link, data-large_image, data-zoom-image,
  data-src, `srcset_largest`; `regex_replace` only for a thumbnail; galleries as
  `multiple`, `join` for one string), specification tables (`spec_table` type and
  source, `additionalProperty`), AI-chosen fields; a wine example validated against
  the vendored mapping schema. Structural checks know the `spec_table` source.
- Run texts: `explanation` (sentence and advice), `completeness` (sentence, or the
  progress text while running) with `comparison.warning`, and a short `timeline`
  on Run detail (merged into the existing outcome / records / progress lines: the
  panel shows 12 values) and as new outputs of `Get run summary` (also in its
  message); stop reason `site_unavailable` in words. The problem codes
  `site_unavailable` / `sitemap_outdated` go the existing problem route. A
  `spec_table` value is an object: the extension reads records only by line count
  and shows sample values as JSON, so nothing breaks.
- Guide: crawl scope, images and specifications, why the run stopped and is it
  complete; run-form and troubleshooting entries.
- Version 0.1.15.

## 0.1.16 (2026-11-14): two small acceptance fixes

- A38: `Get run summary` no longer repeats the completeness sentence when the
  service's explanation already contains it; a comparison warning is kept.
- A39: the run form's two URL fields are renamed so they cannot be confused:
  `urls` → **Pages to scrape (url_list)**, `url_include` → **Extract only URLs
  matching (regex)** (its help says it is a filter, not a list of pages). A
  `url_list` run refused for missing `urls` while `url_include` holds addresses is
  told to move them. Guide and mapping hint use the new names.
- Tests: `tests/test_0_1_16.py`; 344 passed in `fs-batch-audit-fs-audit:latest`.

## 0.1.17 (2026-11-14): service storage and clean-up; results in the Scraper events job (A40)

Service 3b92b1d (CHANGES 46–49: `GET /v1/admin/storage`, `POST /v1/admin/cleanup`,
fetched-result rule, sweeper records). Contract re-synced from a `git archive 3b92b1d`
export (`storage.schema.json`, `cleanup.schema.json`, `openapi.yaml`, four examples).
The service's working tree already holds uncommitted changes of its next round, so
the drift test ran with `WEB_SCRAPER_CONTRACT_DIR` on that export, not on the tree.

- **Service storage** (`service_storage`, source): twelve plain lines (summary,
  disk, working files, result files, samples, screenshots, database, runs, automatic
  clean-up, last automatic and last manual clean-up, never removed), sizes as
  KB/MB/GB, times as `2026-11-14 09:59 UTC`; "Low on disk space" first under 10 % or
  2 GB free. The service's whole answer is the object output `details`.
- **Clean up service storage** (`clean_up_storage`, action): inputs per part (working
  files / result files / run records older than N days, samples and screenshots
  older than N hours, include results not fetched, compact the database) and
  `dry_run` (empty = on; no interface default). An empty part is not sent; nothing
  chosen is refused before a call. The answer says per part what was (would be)
  removed and the bytes, what compacting did or why it was skipped, what was kept,
  `freed` and `bytes_freed`. Page-only `clean_up_storage_now` applies after the tick
  *Yes, remove these files*.
- Errors through `errors.from_service_error`; an older service's `404 not_found`
  becomes `service_route_missing` "update the service image"; `validation_failed`
  keeps `config_invalid` with "refused the clean-up settings".
- **Service** page (after Profiles): a `scraper_profile` connection form with
  *Show storage*, *Clean up - dry run*, *Clean up now*, the age fields pre-filled
  with the service's default ages (7 / 14 / 30 days, 24 hours, compact on) and a
  help text. No page query with a connection (ADR 0067 needs 0.9.17).
- Template **Service maintenance** (`templates/service_maintenance.bundle.json` +
  YAML): Storage before → Clean up service storage (the ages from the service's own
  `retention`, results only when fetched, compact, `dry_run` from the optional run
  input *Dry run only*, else false) → Storage after → *Clean-up done*; outputs
  `storage_before`, `cleanup`, `freed`, `bytes_freed`, `storage_after`; trigger
  channels include `schedule`; canvas note on the weekly schedule.
- **A40.** Scraper events: `Get result` has `override.returned_files:
  [{result: artifact_handle, key: result_file}]` (0.9.16 derives a workflow's file
  outputs from that, `workflow_returned_files.derive_returned_files`; the same
  `result_file` row in `expected_artifacts` is then satisfied, not a second file);
  `expected_outputs` `run_summary` (Get run summary's `message`), `result` (the
  descriptor `state.result`) and `result_preview` (the tabular read's
  `preview_rows`, 10 rows, through the step's `output_mapping_jsonb`); all
  optional; `output_mode` `variables_and_artifacts`. `Scrape a site`'s final message
  and `run_page_hint` name the Scraper events job's Result panel and the Runs page.
  Run detail *Show run* has a line `result` (file, records, size, kept until, the
  job that stored it); the starting job moved into the `state` line to stay at 12.
- `tests/workflow_sim.py` applies a tabular step's output mapping, captures returned
  files and collects outputs as `collect_runtime_outputs` does.
- Guide: *Where are my results*, *Disk use and clean-up* (what is kept and for how
  long, the Service page, the weekly schedule: Schedules → New schedule → Weekly),
  the service's `docker run` with `--log-driver json-file --log-opt max-size=20m
  --log-opt max-file=5`, the maintenance steps, three troubleshooting entries.
- Offline platform check against a `git archive v0.9.16` export (the stand):
  `extensions validate` passed (manifest, icon, health, marketplace, UI pages);
  the three bundles parse, rewrite and validate; the new section 7 runs
  `WorkflowInterface.from_settings`, `captured_returned_files` and
  `collect_runtime_outputs` on the imported Scraper events: returned file
  `result_file` from `get_result.artifact_handle`, outputs `result`,
  `result_preview`, `run_summary`; `RESULT: OK`.
- Version 0.1.17.

## 0.1.18 (2026-11-14): CSV beside JSON Lines; collect the missed pages again (A45)

Service dc8ae93 + ef08989 (round 3 batch A, image rc-next9; CHANGES 50–53, README
4.10, 7.5). Contract re-synced from a `git archive ef08989` export (the service tree
again holds newer uncommitted work); the drift test ran against that export.

- **CSV.** A second step, not a second output of `Get result`: the platform fails a
  step when any output grant it prepared got no bytes (`runtime_grants.
  finalize_uploads`, `artifact_upload_missing`), and an older service writes no CSV.
  `get_result` keeps one output and returns `has_csv` (after the contract
  descriptor's fields); new operation `get_result_csv` (binding `result_csv_handle`,
  `result.csv`, `text/csv`, same size limit) downloads `csv.download_path`, checks
  size and SHA-256 (no line count: a quoted field may hold a line break), stores it.
  A service without `csv` → `result_csv_missing`. Both share `_store` (download,
  check, upload). A descriptor with `csv` and `fetched_at` and one without both work.
- Scraper events: Get result → *CSV available?* (`state.result.has_csv`) → **Get
  result CSV** (`returned_files: [{result: artifact_handle, key: result_csv}]`,
  error route to *Processing failed* like Get result) → Example: read the result;
  `expected_artifacts` lists `result_file` and `result_csv`. Run detail's `result`
  line names both files; *Result file* gives the CSV address too.
- **Re-collect (A45).** Action/operation `recollect_run` (*Collect missed pages
  again*, step name *Re-collect missed pages*): `POST /v1/runs/{id}/recollect` with
  the profile (with webhook), `platform` and optional `reasons`. As a step the key
  and ids are start_run's (`fs:<job>:<step>[:<item>]`); on the page (no job) job id
  `page-<hash>`, step `run_detail_recollect`, key `fs:page:<hash>` over profile,
  run, reasons and a ten-minute window, so a double press starts one run. The
  answer names the new run, pages by reason, `created`. `run_not_finished` and
  `nothing_to_recollect` (with the missed pages by reason) in plain words. Run
  detail: button and a *Missed pages to collect again* checkbox list (default the
  service's six reasons); *Show run* links `recollect_of` / `recollected_by` in the
  state line; a page-started run says so instead of a `/jobs/page-…` link;
  `get_run_summary`'s message names the parent. Not a declared external effect,
  like stop_run, resume_run and resend_event (the host's direct-action effect flow
  from a page was not verifiable offline); the Guide says a Test Run really starts it.
- A child's `run_completed` with `parent_run_id` passes Scraper events unchanged
  (strict inputs off; nothing reads it): tested with the walker.
- Guide: *The CSV file* (BOM, `;`, columns, lists, formula guard, JSON Lines for exact
  text), *Collect the missed pages again*, steps and troubleshooting.
- Pages read through the page's profile (ADR 0067); see below.
- Offline platform check passed against `git archive v0.9.16` and fs-batch-0917b
  HEAD d47cd70e (0.9.17): validate, import rewrite, connector checks; returned files
  `result_file` and `result_csv`.
- Version 0.1.18.

### 0.1.18 pages: no connection forms outside Profiles (owner, 2026-11-14)

The owner found the run pages confusing (connection forms with Save / Create
connection, typed run and event ids, a cursor box, Stop and Resume on finished runs).
Service, Runs and Run detail now declare `connection: {connection_type_ids:
[scraper_profile]}` and `refresh_seconds` (Runs and Run detail 30, Service 300); the
platform's selector beside the project picks the profile. `platform_min: 1.7.0` in
both manifests (the validator reads `runtime.compatibility` of extension.yaml).
The host supplies SDK 0.5.0 (`dev-wheels/` is not shipped); `handlers/pages.py`
reads the connection with `connection_from_payload`, falling back to the root
`target.connection` with an older SDK. The three old connection-form components are
gone; the only `connection_form` is on Profiles.

- Six connection queries, read only (`handlers/pages.py`): `runs_table` (grid),
  `runs_links` (filter links and run links: a grid cell cannot hold a link, ADR
  0067), `run_report` (markdown), `run_events` and `run_missed` (grids),
  `storage_report` (markdown). `p.run_id`, `p.state`, `p.site` are checked
  (run id shape, known state, host-like site) before use. Grids return a domain
  failure on a service error; markdown components show no load error in 0.9.17, so
  their answer is the sentence "This could not be loaded. …".
- Row buttons only where they apply (`visible_if` on row flags): Stop for queued or
  running; Resume for waiting_for_credentials (the service's only resumable state; a
  stopped run is final); Re-collect missed pages for a failed run or a partial
  completed one whose summary counts missed pages (the newest 20 such runs are read
  for the count); Send again on an event that is stale, not delivered with an error,
  delivered but not taken for 30 minutes, or claimed for 30 minutes (then it takes
  over the claim). The row carries the values the action needs; nothing is typed.
- Run detail sections: Status, Progress, Why it stopped / what to do, Results (file
  names, sizes, the Scraper events job link), Missed pages, Timeline; then Events and
  Missed pages tables. Without `p.run_id`: one sentence and a link to Runs.
- Service: the storage report and a `form` (ages, include unfetched, compact, the
  confirmation tick) with *Clean up - dry run* (primary) and *Clean up now*.
- The page-only actions `list_runs`, `show_run`, `show_unextracted`,
  `result_download`, `resend_event` stay as API actions (resend_event is the Events
  row button); no page shows them as forms. Event handling stays in workflow steps.
- Messages that named old buttons ('Show run', 'Resend event', 'Take over a live
  claim', 'Result file') now name the page sections.
- Core limits met: grid cells cannot link (markdown list instead), there is no
  rendered filter bar (filter links set `p.*` parameters), a query button of a form
  fills only its own form, and a markdown component does not show a load error.

Page by page (buttons and inputs):
- Profiles: unchanged (the connection form and its tests).
- Service: inputs working files / result files / run records ages (how much to
  remove), samples age, include unfetched results (risk choice), compact (choice),
  confirmation tick (guards Clean up now); buttons Clean up - dry run (always),
  Clean up now (always; refuses without the tick).
- Runs: no inputs; filter links; row buttons as above.
- Run detail: no inputs; Send again as above.
- Guide, About: no inputs.

Tests: `tests/test_pages_0_1_18.py` (the host's query envelope, every row flag per
state, links and filters, forged parameters, outages, report sections, events,
storage); `tests/test_ui.py` checks the only connection form, the page connections,
the row conditions and every page input. Offline platform check: `extensions
validate` passed against fs-batch-0917b HEAD (0.9.17).

## 0.1.19 (2026-11-14): page queries were refused on the stand

- Every page query declared `required_scopes: [read:projects]`, which the extension
  does not request, so the platform refused all of them ("requires scopes not granted
  to the extension: read:projects"): Runs, Run detail, Service and the Overview
  health stayed empty. Queries now declare `extension:invoke`, like the actions.
- `tests/test_0_1_19.py`: every action and query asks only for scopes in
  `extension.yaml` `required_scopes`.

## 0.1.20 (2026-11-14): service Batch B — links skipped, duplicates, one run per site, stalls

Service 5fa1be4 + 301145b (image rc-next10; CHANGES 54–59, README 4.11–4.14, 7.3).
Contract re-synced from a `git archive 301145b` export; the drift test ran against it.
Built on 0.1.19 (5fd8b62: page queries declare `extension:invoke`).

- **Runs in one call.** `GET /v1/runs` items carry `completeness` and `queued_reason`:
  the per-run reads for the Re-collect flag are gone (was 1 + up to 20 calls). Progress
  "M of N" (`completeness.progress.text` while active, else collected of found);
  *Why it stopped* adds "N pages missed"; Re-collect shows for a finished run with
  `not_collected > 0`; a queued run says "waiting: another run of this site is running"
  or "waiting for a free slot".
- **Run detail:** the queued reason in Status; in Progress "N pages skipped as
  duplicates" (`pages_by_class.duplicate`) and one line "Skipped links: 15 500
  account/cart/wishlist, 11 340 filters, …" (`counters.links_skipped`, largest first;
  `canonical` has the duplicates line instead).
- **Stop reason `stalled`** in words on the pages and in summaries; the `stalled`
  problem goes the existing problem route (Scraper events' problem branch reads the
  run summary and its explanation).
- **Queued at start:** `start_run`'s `run_page_hint` and `get_run_summary`'s message
  begin with the service's `queued_reason.message`.
- **Analyze site** has a new output `link_rules` (rules on, rules switched off, trap
  budget, in words); `Scrape a site` maps it to `state.link_rules`.
- **Run form:** `link_rules_off` (*Links not to skip*, an array input: Core has no
  multi-select run input, so the help lists the rule names; unknown names are refused
  before the service is asked) and `trap_pattern_budget` (*Trap budget (pages)*,
  empty = 100, 0 = off); typed in `settings.py`, sent in `run_inputs`.
- Guide: *Links the scraper skips* (each rule, when to switch one off: a shop reached
  only through filters → `facet_params`), the two run form fields, troubleshooting
  *the run waits: another run of this site is running*, *the run stopped: stalled*,
  *products behind filters*.
- Templates changed: **Scrape a site** (two run inputs, `link_rules` output mapping) —
  a new copy is needed for the run form fields. Scraper events and Service maintenance
  carry only the new version number.
- Version 0.1.20.

## 0.1.21 (2026-11-14): Flow Steward 0.9.18 — grid links and filters; connection slots

Core 0.9.18 (branch `claude/release-batch-0.9.18`, host contract 1.8.0; not released
yet). `runtime.compatibility.platform_min: 1.8.0` in both manifests.

- **Runs:** the site cell links to Run detail (`type: link`, `link: {page: run_detail,
  params: {run_id: "{row.run_id}"}}`, `row_fields: [run_id]`) and the whole row does
  too (`row_link`). Filters above the table: *State* (a select of the six run states)
  and *Site* (a text box: options from a query must come from an ordinary query
  without a connection, and the sites live in the service). The link list under the
  table and its query `runs_links` are gone. `p.state` / `p.site` are checked as
  before.
- **Run detail:** the Events table has a *Job* column (`link: {path_field:
  job_path}`, `/jobs/<id>`) to the Scraper events job that took each event. Links
  inside the report's text (the files' job, parent and child re-collect runs, Back to
  Runs) stay markdown: 0.9.18 opens them in place.
- **Connection slots (ADR 0068).** 0.9.18 fills slots only for a workflow an
  extension install projected (`installed_connection_slots`); an imported copy is the
  account's own workflow and is never filled, so a slot-bound step in a copy would
  fail with "explicit connection binding is required". Two variants therefore:
  - `templates/*.bundle.json|yaml` — the project copies, unchanged except the
    version; the import path stays as it was.
  - `artifacts/workflows/slots/{scrape_site,scraper_events,service_maintenance}.yaml`
    — generated by `.github/scripts/slot_workflows.py` (`--check`) from the copies, in
    the artifact format of `impl_build_mapping.yaml`: `settings.connection_slots:
    [{slot: scraper, extension_id: flowsteward.web-scraper, connection_type_ids:
    [scraper_profile], label: Scraper profile}]`, every connector step
    `connection_binding_jsonb: {slot: scraper}`, the build_mapping Invoke
    `connections: {profile: {slot: scraper}}`. Not listed in the descriptor yet:
    publishing would project them into every installing account now, before the
    hooks (A42) exist.
- Offline platform check against `git archive claude/release-batch-0.9.18` (96587f89):
  `extensions validate` passed (the grid link and filter contract runs in it; a
  control with an unknown link page fails with `Page 'no_such_page' is not a page of
  this extension`); new section 8 runs 0.9.18's `connection_slot_descriptor_issues`
  on a descriptor that would publish the three variants (none), and
  `validate_workflow_definition`, state selectors, step config keys, reserved
  targets and `undeclared_slot_issues` on each variant (none).
- Version 0.1.21. Templates: only the version number changed; existing copies keep
  working.

## 0.1.22 (2026-11-14): the extension's own read-only workflows (A42), slot and hooks

Core 0.9.18 candidate (`claude/release-batch-0.9.18`, tip 4995f7d7). Scope cut by the
owner mid-way: no `process_result` / `on_completed` hooks and no use-case recipes (one
shared Scraper events cannot route different purposes); `before_start`,
`review_mapping` and `on_error` are in.

- **Published workflows.** `artifacts/workflows/{scrape_a_site,scraper_events,
  service_maintenance}.yaml`, listed in the descriptor, generated from
  `templates/*.bundle.json` by `.github/scripts/extension_workflows.py` (`--check`;
  replaces 0.1.21's `slot_workflows.py` and `artifacts/workflows/slots/`). Each has
  `settings.connection_slots` (slot `scraper`, type `scraper_profile`, label
  "Scraper profile"), every connector step on `{slot: scraper}`,
  `settings.portability.allow_full_export: true`, `enabled: true`, notes pointing to
  the hooks. `build_mapping` is called as the extension's action (`action:
  {extension_id, action_id}`, no `workflow_id`, no install): 0.9.18
  `composite_action_runtime.plan_action_invoke` takes the install and the connection
  from the run's pinned slots (`jobs.meta.connection_slots`). `impl_build_mapping`
  stays the hidden implementation.
- **Hooks (ADR 0038, contract_version 1).**
  - `review_mapping` (Scrape a site): between *Stored mapping passes?* / *Mapping
    passed?* and *Test mapping before activation*; inputs `site`, `mappings`
    (mutable), `field_results`, `summary`; the retest and *Activate mapping* read
    `state.hooks.review_mapping.output.mappings`.
  - `before_start` (Scrape a site): after *Activate mapping*, before *Start run*;
    inputs `config` (mutable), `site`, `mapping_summary`; output-only `skip_run`
    (default false) → new *Skip the crawl?* / *Crawl skipped*; *Start run* reads
    `state.hooks.before_start.output.config`.
  - `on_error` (Scraper events): on the problem / run_failed branch between *Get run
    summary* and *Complete event*; inputs `event_type`, `run_id`, `state`,
    `terminal`, `message`, `explanation`, `error`, `errors`; nothing mutable.
  - A changed contract (schemas, mappings, mutability, outputs, stage) changes the
    hook's semantic digest, so 0.9.18 `workflow_hook_release` blocks an update that
    would break a project's binding.
- **Events trigger: a signed webhook on the linked read-only Scraper events.**
  0.9.18 `WorkflowWebhookCreationService.create` needs only that the workflow is
  linked to the project; it writes an entrypoint row and a project secret and does
  not patch the workflow (`trigger` and `settings` are protected patch fields,
  `workflow_portability_common.PROTECTED_WORKFLOW_PATCH_FIELDS`). A webhook start
  of a projected workflow pins its slots like any start (ADR 0068). The callback +
  `workflow_triggers` path would need an `ingress_event` action, a callback
  declaration with its own secret handling and a provider trigger row: more parts,
  and none of them tested live.
- **Copies:** `templates/` unchanged except the version; the import path stays.
- `tests/workflow_sim.py` runs a hook as 0.9.18 does for an empty one (inputs by
  presence, output-only defaults, `state.hooks.<id>.output`) and can simulate a
  project's hook steps. `tests/test_0_1_22.py`: publishing, generation, slot,
  export flag, action call, hook contracts and places, walks with empty and filled
  hooks (review changes what is tested; before_start changes the config or skips;
  on_error only on problems).
- Platform check against `git archive` of 4995f7d7: `extensions validate` passed
  with the three published workflows; section 8 now runs the install path's
  `connection_slot_descriptor_issues` on the real descriptor (`extensions validate`
  does not run it: a control with an undeclared slot passes validate and fails
  section 8), plus `invoke_config_issues` (hook declarations), definition, selector,
  step-config and readiness checks: all clean.
- No durable "wait for an external signal" step exists in 0.9.18: durable waits are
  timer, approval and child only (`core/application/jobs/workflow_wait_contract.py`);
  the `wait` step takes only a duration (`workflow_step_config_contracts.py:131`,
  `registry/builtin_step_kind_profiles.py`).
- Version 0.1.22.

## 0.1.23 (2026-11-14): Scrape a site waits for its own run (A55, A56); A54 in the extension

Core 0.9.18 candidates: `claude/wait-for-event` 56d0a073 (ADR 0071, "Wait for an event")
and `claude/run-titles` c823f60f (A56, `settings.run_title`). Neither changes the host
contract (still 1.8.0), so `platform_min` stays `1.8.0`. A 0.9.18 host without ADR 0071
refuses the Wait's `event` when the workflow is saved or installed (only a timer Wait is
known there); a host without run titles ignores `settings.run_title`. Service: A54 work
3867895/6ddeaab/7d98f20 (image rc-next11, CHANGES rows 62-68); contract re-synced from a
`git archive 7d98f20` export (openapi.yaml, six schemas; examples unchanged).

- **Scrape a site** (project copy and the extension's own workflow): after *Start run* a
  Wait for an event, then the result inside the same job.
  - Start run's step id is now `start_crawl`: the service keeps the starting step
    (`platform.step_id`), so Scraper events can tell a waiting starter from an older copy
    (`start_run`), Re-collect (`run_detail_recollect`) or a person's own step.
  - `wait_for_run`: `override.wait = {event: {source: flowsteward.web-scraper,
    event_types: [run_completed, run_failed], correlation_key: state.scraper_run_id},
    timeout_ms: 259200000, output_mapping: {event_type: state.run_event.event_type,
    payload: state.run_event.payload}}`, `error_policy: route_to_step`. Timeout: a fixed
    3 days. ADR 0071 takes `timeout_ms` as an integer only (no mapping), and the run form
    has no max-duration field (the service's own limit is 720 minutes of running, queued
    time not counted, at most 7 days), so 3 days covers a default run with queueing and
    repairs. A copy can change it; the read-only workflow cannot.
  - Success and timeout both go to `check_run` (*Get run summary* with the new input
    `require_completed: true`): a run that is not `completed` fails the step
    (`run_not_completed`) with the run's summary as the message, plus "It is still going
    on ..." when it is not final. So `run_failed` is a clear failed end with the summary,
    and a timeout on a run that did complete (a broken webhook) still gets its files.
  - Then *Any records?* -> *Get result* -> *CSV available?* -> *Get result CSV* -> *Read
    the result* -> *Result ready*; no records -> *No records*; Get result / CSV failure ->
    *Storing the result failed*; a failed preview still ends *Result ready*.
  - Interface: `output_mode: variables_and_artifacts`; outputs `scraper_run_id`,
    `run_page_hint`, `mapping_summary`, `run_summary`, `result`, `result_preview`;
    returned files `result_file` (Get result) and `result_csv` (Get result CSV), also as
    `expected_artifacts`. An Invoke parent maps them (`output_mapping`,
    `artifact_mapping`).
  - Run title (A56): `settings.run_title: "{{ inputs.run_name }} {{ inputs.site_url }}"`,
    new optional input `run_name` (*Run name*), read by no step.
- **Scraper events**: `claim_event` answers top-level `resume_waits` for a claimed
  `run_completed` / `run_failed`: `[{correlation_key: <run id>, event_type,
  payload: {run_id, state, outcome, stop_reason, record_count}}]` (the run is read once;
  if that read fails the wake still goes, with the event's outcome and no count). The
  handler leaves `_resume_waits` in its result and the router moves it to the top of the
  response. Any copy of Scraper events wakes waiting runs, old copies included.
  - New step *Stored by Scrape a site?* after *Get run summary*: `get_run_summary`'s new
    output `result_by_starter` (`platform.step_id == start_crawl`) -> straight to
    *Complete event*; otherwise the result is stored here as before.
  - Decision (pre-1.0, simplicity, but cheap to keep): runs nobody waits for (copies of
    Scrape a site before 0.1.23, Re-collect from Run detail, a person's own Start run)
    keep getting their files in Scraper events; a waiting starter's run is stored once,
    in the starter's job. Not covered: a waiting Scrape a site that was cancelled or
    failed after the start - its files stay in the service (Run detail says so).
- Run detail (report, Results section, result_download): a waiting starter's job is
  named as the holder of the files.
- **A54** (service rows 62-68):
  - The mapping skill (0.1.23) and the agent's system prompt: `wss:section` comments and
    selectors against `<path>`; variant data only from `variant_sources`; `price_on_page`
    (shown/range need a price source, on_request/missing are not failures); currency
    from the page, `default_currency` last; the row-67 description rule word for word;
    `mapping_gap` failures and a repair's `repair_reason`.
  - `test_mapping`: each `templates[].failures[]` entry becomes a failure of kind
    `mapping_gap` (code, the service's message, `what_to_change`) that goes back to the
    agent, sets `passed: false` even if the answer said true, and is listed as "Not
    passed: <template>: <message>" in the summary. Nothing is activated while one
    remains (Scrape a site, Scraper events' repair). Samples' `price_on_page`,
    `price_status`, `price_max`, `currency_from_default` are in `sample_results`, and the
    summary says "Prices on the samples: price shown 2, price on request 1; currency taken
    from the default on 1 sample."
  - `get_mapping_request`: `failures.repair_reason` puts `failures.gaps` in words.
  - samples.md: the section/path rule, and "Price on the page: ..." per sample;
    `_agent_sample` carries `price_on_page`.
  - Run detail, *Missed pages*: a column *What went wrong* (`last_error`, else
    `mapping_gaps` in words); `show_unextracted` adds the same.
  - Guide: *Prices in the result* (shown, range + `price_max`, on request, missing,
    `currency_from_default`; skip on_request/missing when comparing prices), CSV columns.
- Guide: *Where are my results* rewritten; new *Use Scrape a site from your own
  workflow* (how it works, the Invoke step with its mappings, the supplier and the
  competitor-price recipes, several schedules with different inputs on one workflow);
  troubleshooting for a long wait, `did not complete`, price status and mapping gaps.
- Tests: `tests/test_0_1_23.py` (41). The walker (`tests/workflow_sim.py`) runs an event
  Wait as 0.9.18 does: the key resolved once at entry, a wake of another key or type
  ignored, the event mapped to state, a timeout failing the step under its error policy.
  The full-cycle test walks Scrape a site to the Wait, walks Scraper events on that run's
  `run_completed`, hands the `resume_waits` of the real Claim event response to the Wait
  (with another run's event first, which is ignored) and checks the files and outputs.
  Also: run_failed, timeout with a completed and with a running run, no records, a Test
  Run (no Wait). Without a `wait` callback the walker delivers the run's own event at
  once (older tests). Control: with the router's `resume_waits` line removed, or the gap
  rule removed, 8 of the new tests fail.
- Platform check against `git archive claude/wait-for-event` (56d0a073): `extensions
  validate` passed; all earlier sections clean; section 7 now also for Scrape a site
  (returned files captured after Get result / Get result CSV, outputs collected);
  new section 9 runs `wait_event_config_issues` and `resolve_wait_event` on both
  variants, then a real `claim_event` response (extension against the fake service)
  through `normalize_extension_response`, `checked_wait_event`, `deliver_wait_events`
  (in-memory inbox: stored 1, source flowsteward.web-scraper; none for an action without
  them) and `event_wake_verdict` (own event accepted, another run's ignored). Controls
  against the same archive: no timeout, a bad source, a duration beside the event are
  refused. Run title against `git archive claude/run-titles` (c823f60f):
  `run_title_issues` none on both variants; renders "Supplier A weekly https://a.example/",
  without a name "https://a.example/", from a schedule "Supplier A · https://a.example/";
  control with a secret-like and an unknown input refused.
- Not verified: a live run on a stand (no stand access in this round); the two Core
  branches together (each checked on its own archive); that the connector step path of a
  real job keeps the top-level `resume_waits` up to `process_project_state_updates`
  (read in code: the subprocess output is kept whole as `invocation.response`).
- Version 0.1.23.

## 0.1.24 (2026-11-15): Invoke of Scrape a site (A68), a busy site is skipped (A66), cancel (A67), service rc-next12

Findings A60-A68 of the stand round on 0.1.23 (row 93 of the acceptance log); service
2d57b00 (image rc-next12, CHANGES rows 69-75). Contract re-synced from a `git archive
2d57b00 contract` export: `openapi.yaml`, six schemas (error, job_config, mapping_test,
record, run_summary, samples) and ten examples in `tests/contract_examples/`.
`platform_min` stays `1.8.0`: nothing here needs more than Flow Steward 0.9.18.

- **A68 (blocker):** Scrape a site's `settings.callable.trigger_channels` gain
  `parent_workflow` (template JSON, the uploaded YAML, the extension's own workflow), so
  an own workflow's Invoke of it is no longer refused with
  `invoke.child_not_parent_workflow_triggerable`. Guide, *Use Scrape a site from your
  own workflow*: from 0.1.24 on (extension's workflow or a new copy); an older copy needs
  a new import or `parent_workflow` added; mapping `skipped`/`skipped_reason`; an If on
  `skipped`; a cancel does not stop the crawl.
- **A66 (busy site, row 75):** `handlers/errors.py` maps `site_busy` to its own code
  with the sentence "Skipped: <site> is already being scraped (run <id>, since <time>).
  No new crawl was started; follow that run on Web Scraper > Runs." (`queued since …`
  for a queued run; no request id; details keep `run_id`, `state`, `started_at`,
  `created_at`, `site`). `start_run` has a new optional input `skipped_start_succeeds`:
  with it a 409 `site_busy` ends the step successfully (`skipped: true`,
  `skipped_reason`, `busy_run_id`, no run id); without it (older copies, a person's own
  Start run) the step fails with `site_busy` and the same sentence. Scrape a site passes
  `literal:true`, maps `skipped`/`skipped_reason` to state and goes `start_crawl` ->
  *Start skipped?* (`state.skipped`) -> *Skipped: site busy* (stop_success) or *Crawl
  started?* as before. New outputs `skipped`, `skipped_reason`. New run input **When the
  site is busy** (`when_site_busy`: skip / queue, empty = skip).
- **A67 (cancel while waiting):** Core 0.9.18 (fs-batch-0918 289205a8/f45d39f5) gives a
  workflow or an extension nothing to run on a cancel: ADR 0071 "Cancel is the ordinary
  job cancel: the job leaves `awaiting_event`" and a late event expires; the terminal
  path (`jobs/terminal_persistence.py`) only records cancel requests for Invoke children
  (`jobs/invoke_child_cancellation.py`); hooks (ADR 0038) are steps of a running job;
  automations get `job.completed`/`job.failed` and step/group/wait events only, no
  `job.cancelled`, and run notification actions only
  (`workflows/job_lifecycle_reactions.py`, `workflow_reaction_events.py`). So nothing
  calls Stop run: Run detail's *Status* names it for a queued/running run started by a
  waiting Scrape a site ("If that job (or the workflow that called it) was cancelled,
  this crawl still runs … Stop it with *Stop* on its row on the Runs page"), `show_run`
  too, the Run detail help and the Guide (*Cancelling does not stop the crawl*).
- **Politeness, page timeout (rows 73, 74):** run inputs **Politeness** (`politeness`:
  gentle / normal / fast; the help lists each preset) and **Page timeout (s)**
  (`page_timeout_seconds`, 10-300, empty = preset); the delay, concurrency and rate
  fields say they override it, the profile's defaults say so too (their texts and the
  connection schema's documented defaults follow the new service defaults 2-5 s, 2,
  30/min).
- **Run summary (rows 72-74):** `get_run_summary` returns `politeness` and the counters
  `soft_errors`, `soft_errors_by_reason`, `repairs_avoided`; its message, Run detail
  (Progress) and `show_run` (still 12 lines) say "Politeness: normal: 2–5 s, 2 at a
  time, 30/min, page timeout 60 s. Slowed down: shop (x2.0 since 09:15: …)." and "3
  unusable answers from the site (status 200 but not the page) retried (cut off 2, error
  or maintenance page 1); 1 mapping repair avoided …". Scrape a site maps
  `state.run.politeness`.
- **Prices (rows 69-71):** `price_mismatch` / `price_is_old_price` have plain texts
  (`TEST_CODE_TEXT`); a mapping gap line reads "<plain words> Details: <service
  message>", the failure for the agent carries `plain` and `what_to_change`; repair
  reasons and Missed pages know both codes. Samples: `_agent_sample` keeps
  `price_details`; samples.md shows "Price stated for programs: 24 (schema.org
  offers.price)" and "On sale: current price 24, old price 35 …". The skill and the
  agent prompt: machine-readable price source first ([itemprop=price] content, JSON-LD
  offers.price/lowPrice, product:price:amount, data-price), visible text as fallback;
  current price, never the struck-through one (row 70). Records' `regular_price` and
  `on_sale` come as the service writes them (result_preview, CSV columns); the extension
  defines no column list of its own, so the Guide (*Prices in the result*, *The CSV
  file*) names them.
- Tests: `tests/test_0_1_24.py` (41); older tests follow (run form list, outputs list,
  the generic error test leaves `site_busy` to its own test, the 0.1.23 version test
  moved here). Full run in `flow-steward-fs-audit:latest` (network off, contract export
  at `/contract`, `tests` and `.github/tests`): 581 passed, 0 failed, 0 skipped.
- Platform check against `git archive 289205a8` (the stand's Core): `extensions
  validate` passed, every earlier section clean; new section 10 runs Core's
  `validate_invoke_workflow_step` for an own workflow invoking Scrape a site (published
  and imported copy): none; control without `parent_workflow`:
  `invoke.child_not_parent_workflow_triggerable`, as on the stand.
- Not verified: a live run (no stand install in this round): the 409 path against
  rc-next12, the Skipped end in a real job, the Invoke on the stand, sale prices and
  politeness on Run detail with real data.
- Version 0.1.24.

## 0.1.25 (2026-11-15): public-release metadata; texts A70, A71

Preparing the first public release; nothing new in what the extension does.

- **Catalog metadata:** the root `description`, the descriptor's `identity.description`
  and `listing.summary` are one short line: Flow Steward's installed card and detail page
  show the root `description` (fs-batch-0918 `extension_runtime_manifest.py` compiles it,
  `query_presentation.py`/`query_list.py` serve it, `pages/extensions/index.tsx` renders
  it) and the marketplace view of an installed release `identity.description`
  (`query_marketplace_projection.py`). `listing.description` holds the detailed text the
  catalog shows (catalog `build_index.py`: `listing.description`, else the root one;
  `marketplace-entry.tsx` shows it split into paragraphs): capabilities, typical uses,
  setup, limits; no claim of logging in or of getting past CAPTCHAs. New `listing.setup_summary` (the catalog's Setup line) and
  `listing.categories: [data, ai]` (primary `data`; `ai` because `build_mapping` runs the
  mapping agent; the ids are from the catalog's `categories.json`). Tags reworded. The
  About page no longer says Scraper events stores the result (true before 0.1.23 only).
  README: Flow Steward 0.9.18 (it said 0.9.15), the three templates and the read-only
  workflows, a "Use it responsibly" section.
- **Icon:** `icon.png` is valid for the catalog (PNG, 128x128, RGBA with a transparent
  background, 4 915 bytes); it is the shared Flow Steward mark that Image Processing,
  File Transfer and Example Showcase also carry, not a mark of its own. Kept.
- **CI and release:** `.github/` follows the published `ext-ecommerce-shopify`: actions
  pinned by commit, `ubuntu-24.04`, pytest 9.0.3, CodeQL and Dependabot for the actions;
  `catalog_check.py` fails closed on every path: a failed or tampered download (it only
  warned and exited 0 before), a checker that does not load or lacks its functions,
  unreadable category JSON, no archive at all, an archive that is missing or misnamed,
  and a refused or crashed inspection all exit 1 (`.github/tests/test_catalog_check.py`).
- **A70 (texts):** a queued start's `run_page_hint` says "Scraper run … is queued" instead
  of "has started". While a run has not ended (queued, running, stopping, waiting for
  credentials), Run detail, `show_run` and `get_run_summary` leave out the comparison with
  the previous full run: the service compares and appends its warning to the explanation
  already while the run crawls (`app/jobs/manager.py`), which said "records fell from 20
  to 3" after 4 pages. After the end the comparison is shown as before.
- **A71 (text):** the service's `sample_count` warning is "found 0 of 4 target pages:
  <explanation.summary>", and the summary followed it again. The warning now keeps only
  its count when the rest is the summary (compared without the seconds, which the
  service measures once per copy); any other warning stays whole.
- `tests/test_manifest_and_contract.py` looks for the service contract next to this
  checkout (`../web-scraper-service/contract`) instead of a path on one machine; the
  drift test still skips without it.
- Templates and the extension's workflows carry 0.1.25 (no content change); the agent and
  the skill stay 0.1.24.
- Tests: `tests/test_0_1_25.py` (the 0.1.24 version test moved here).
- Version 0.1.25.

## 0.1.26 (2026-11-16): repository links and the published service image

Preparing the private repository `Flow-Steward/ext-data-web-scraper`; nothing new in what
the extension does.

- **Vendor links:** `extension.yaml` `vendor.support_url` / `website_url` and the
  descriptor's `vendor.support_url` point to `https://github.com/Flow-Steward/ext-data-web-scraper`
  (`/issues` for support) instead of `ext-web-scraper`.
- **Service image:** the setup guide names `ghcr.io/flow-steward/web-scraper-service:0.2.0`
  where it said the internal tags rc-next11/rc-next12 (price columns, sale prices, the
  0.1.24 note) and in setup step 1; the Guide page (`ui/components/guide.yaml`) regenerated
  with `.github/scripts/build_guide_page.py` (the generator reproduces the committed page
  from the 0.1.25 guide byte for byte, checked first).
- **Contract copy:** `tests/contract_examples/get_health.response.json` follows the
  service's example (`service.version` 0.2.0), so the drift test against the service
  contract stays green.
- **Release workflow in a private repository:** tag → CI (tests, ZIP, the pinned catalog
  checker, which reads only public `Flow-Steward/extensions` files and inspects the ZIP
  locally) → GitHub Release with the ZIP. The catalog announcement sends nothing without
  the `CATALOG_DISPATCH_TOKEN` secret: the step prints a notice and exits 0
  (`.github/tests/test_catalog_dispatch_workflow.py`
  `test_a_missing_dispatch_credential_uses_scheduled_catalog_without_calling_github`),
  so the release succeeds and the public catalog is not told. Not changed.
- Templates and the extension's workflows carry 0.1.26 (no content change).
- Tests: `tests/test_0_1_26.py` (the 0.1.25 version test moved here; links, versions of
  every template and workflow, the guide's image).
- Version 0.1.26.
- Not run at this commit: the test suite (`run_ws_tests.sh` in the fs-audit container) —
  Docker Desktop on the development Mac was down ("unable to start", 2.5 GB free on the
  host disk). The last run is 611 passed at 2cd5a4f (0.1.25); the first run on this tree
  is CI's (`.github/workflows/ci.yml`). The ZIP was packaged with
  `.github/scripts/package_extension.py` (standard library only).

## 0.1.27 (2026-11-16): the service is built with Docker Compose

The service repository `Flow-Steward/web-scraper-service` is public and publishes no image
any more; users build it with its `docker-compose.yml` (release 0.2.1).

- **Setup:** `listing.setup_summary` and setup step 1 of the guide: get
  `docker-compose.yml` and `.env.example` from https://github.com/Flow-Steward/web-scraper-service,
  fill `.env` (`WSS_API_TOKEN`, `WSS_SECRETS_KEY`, `FS_DOCKER_NETWORK`), `docker compose up -d`;
  the service joins the platform's network as `http://web-scraper:8080`. Every GHCR image
  name is gone (price columns, sale prices, the 0.1.24 and 0.1.26 notes, *Disk use and
  clean-up*, troubleshooting). Guide page regenerated with
  `.github/scripts/build_guide_page.py` (it reproduces the 0.1.26 page byte for byte).
- **Contract copy:** `service_contract/` and `tests/contract_examples/get_health.response.json`
  follow the public service contract 0.2.1 (descriptions without internal references;
  `service.version` 0.2.1). No schema structure changed.
- Templates and the extension's workflows carry 0.1.27 (no content change).
- Tests: `tests/test_0_1_27.py` (versions moved here from `test_0_1_26.py`; the Compose
  setup text, no image name). `pytest tests .github/tests` in `flow-steward-fs-audit:latest`,
  `--network none`, the repository read-only (`dist/` on a tmpfs) and
  `WEB_SCRAPER_CONTRACT_DIR` = the public 0.2.1 contract: 615 passed.
- Version 0.1.27.

## 0.1.28 (2026-11-17): refresh the development JSON Schema validator

- Update the CI and offline development requirement from jsonschema 4.23.0
  (unbounded in requirements-dev.txt) to 4.26.0. Existing Draft 2020-12 tests
  cover connection-form saves, schema defaults and the fake scraper-service
  contract. jsonschema remains a development dependency; runtime handlers and
  service contracts are unchanged, with no runtime wheel closure to update.
- Keep the extension manifest, descriptor, bundled workflow versions and template
  source versions synchronized at 0.1.28. Imported workflow behavior is unchanged.
- Record provenance for the existing Flow Steward wave icon in BRANDING.md;
  retain its bytes unchanged.
- Verification is performed by the repository's GitHub CI and release workflows.

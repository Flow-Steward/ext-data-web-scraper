# Tutorial outline — "Build a scraper on Flow Steward" (draft)

A draft structure for a public tutorial. Each part lists what the reader does,
what they see, and the screenshots to take on a stand.

## 0. What you will build (2 min read)

- One picture: run form → `Scrape a site` (validate, analyse, AI mapping,
  review, start) → scraper service crawls → signed webhook → `Scraper events`
  (result file → your processing).
- Why one result file: the user's steps run once, on the finished crawl, with a
  descriptor, never page by page.
- What stays declarative: the mapping is data (selectors, JSON-LD paths), AI
  never writes code, and every mapping is tested on sample pages before use.

## 1. Set up (follow the setup guide, steps 1–3), in the order that worked on the stand

1. Start the service (`WSS_API_TOKEN`, `WSS_SECRETS_KEY`); check `/health`.
2. Install the extension; on **Profiles** create the profile *No proxy*, typing
   the token; **Save**; **Approve use of secrets** on its card; **Test service**
   (version, both browsers, limits).
3. Import `Scraper events` (**Import YAML**, map the connection).
4. Add its webhook: **New webhook**, *Custom*, **Advanced settings** → Auth mode
   **HMAC SHA256**, header `X-Scraper-Body-Signature`, Idempotency mode
   **Require `Idempotency-Key`**; **Create webhook**; copy the one-time credential
   and the URL as the service reaches the platform into the profile; **Save**;
   approve again; enable the workflow.
5. Import `Scrape a site` and enable it.

- Screenshots: the profile form with masked secrets and the approval button; the
  webhook trigger form with Advanced settings open.

## 2. First trial run with `max_records: 10`

- Start `Scrape a site` with **Run workflow** after ticking the acknowledgement
  (a **Start test run** suppresses `Start run`: useful to try the mapping without
  crawling, but nothing is crawled). Inputs: site URL, a one-sentence
  description, `max_records: 10`.
- Walk through the job: `Validate settings` (merged configuration and its
  sources), `Analyze site` (sitemap, JavaScript, protection, chosen strategy),
  `Fetch samples`, `Build mapping (AI)` (open its child run: `Samples for the
  agent` = samples.md the model reads whole, the model's `mappings_json`, the
  test table, a refinement if one happened), `Activate mapping`, `Start run`.
- Run detail → **Show run**: discovery, pages, records, fill rate; the run ends
  with exactly `max_records` records.
- The `Scraper events` job the service started: `Claim event`, `Get result` (the
  stored `run_….jsonl` artifact and its descriptor), `Example: read the result`
  (the tabular_feed rows), `Mark processed`, `Mark completed`, `Complete event`.
  **Result file** on Run detail names this job.

## 3. Review the mapping before the crawl

- Add an **Approval** in the review place showing `state.field_results` and
  `state.samples`; connect *rejected* to a Stop step.
- Show the re-test: change a selector with a Set Variable, see `Test mapping
  before activation` catch a broken change.
- Explain versions: a mapping belongs to the site and template; a second run
  reuses it when it still passes; `rebuild_mapping: true` builds a new one.

## 4. Process the result

- Replace/extend `Example: read the result`: `tabular_feed transform` on the
  dataset (flatten `data.*`, round prices), then write to your destination
  (a dataset, a spreadsheet extension, an HTTP step).
- Upsert on `record_id`: it is stable across runs, so a repeated processing run
  creates no duplicates.
- Show what a resend does: Run detail → Resend (resume) skips the processing
  after `processed` was recorded; *from the beginning* repeats it.

## 5. Schedule it

- A schedule on `Scrape a site` stores its own inputs (site, description,
  limits and every other setting, since 0.1.9 all on the run form); one schedule
  per site.
- Mapping repairs during scheduled runs: a broken template raises
  `mapping_required`; `Scraper events` repairs it with the AI while the rest of
  the crawl goes on; a template that cannot be repaired ends the run *partial*.

## 6. Worked example A — competitor prices for repricing

- Inputs: competitor shop URL; *"Product pages: product name, price, currency,
  availability, EAN or article number"*; fields `title, price, currency,
  availability, part_number`; required `title, price, currency`;
  `discovery_mode: auto`; `url_include` limited to the product section.
- More run form fields: `structured_data_types: ["Product", "Offer"]`, polite delays,
  proxy tier 1 only.
- Processing: join on the article number with your catalogue; compute the price
  gap; send the list of articles whose gap exceeds a threshold to the repricing
  step (or an Approval first).
- Schedule nightly. Discuss *partial* outcomes and the unextracted list.

## 7. Worked example B — descriptions from a supplier by a list of URLs with keys

- Prepare a CSV `url;key` with the supplier's product URLs and your article
  numbers.
- Run with `discovery_mode: url_list`, the CSV as **URL file**, fields `title,
  description, image_urls, part_number`, `description` as HTML that keeps
  paragraphs, lists and the technical table.
- Every record carries `source_key` = your article number, so the result joins
  back to the catalogue without matching URLs.
- Processing: write `description` and `image_urls` into your PIM / shop
  extension by `source_key`; list the unextracted URLs (Run detail → Show
  unextracted URLs) for a manual check.

## 8. Protected sites (optional chapter)

- Proxy tiers: cheap tier first, escalation only after a block; sticky sessions.
- Checkbox click and solvers: off unless the service operator allows them and a
  run asks; the run page counts clicks and solves against their limits.
- Legal note: respect the site's terms; `robots.txt` is honoured by default.

## Troubleshooting seen on the acceptance stand

- `secret_configuration_unapproved` on every action: press **Approve use of
  secrets** on the profile after creating it, after every change and after an
  extension update.
- `secret_refs: missing required secret 'service_token'` when creating a
  profile: type the token; the stored-secret picker works only on a saved
  profile.
- Saving an edited profile refused with `expected type integer` for hidden proxy
  fields: fixed in 0.1.2; update the extension.
- `No target page could be sampled` on a site without schema.org data: give a
  hint - **Product page selector** (`target_page_selector`) or `url_include` on
  the run form.
- Deliveries refused with HTTP 400 `workflow.input.unknown`: a `Scraper events`
  copy from before 0.1.4; untick **Only accept listed inputs** in its Interface
  (setup guide, Updating the extension).
- `Build mapping (AI)` fails all attempts with a real model although the notes
  read well: an extension before 0.1.5 (the mapping must travel as JSON text in
  `mappings_json`); update and re-run.
- A protected site stops at `Fetch samples` with HTTP 403 and an interstitial
  challenge: analysis and samples only wait for a challenge; the checkbox click
  and solvers act only inside a crawl. Use a proxy tier or another entry URL.

## Appendix

- The run form reference (from the setup guide).
- Error messages and what to do.
- Limits: result size (99 MiB default; S3 for larger), extension timeout
  (`FS_EXTENSION_OPERATION_TIMEOUT_SECONDS`), artifact limit
  (`FS_EXTENSION_ARTIFACT_MAX_BYTES`), `tabular_feed` 1 GiB / 2 million rows.

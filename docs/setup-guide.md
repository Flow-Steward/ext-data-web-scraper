# Web Scraper guide

This guide is also the extension's **Guide** page. It is written in the Markdown the
page can show: headings, paragraphs, lists, bold, italic, `code` and code blocks, no
tables. `.github/scripts/build_guide_page.py` copies it into
`ui/components/guide.yaml`; a test fails when the two differ.

## How it works

Three parts work together:

- The **scraper service** (a separate container) loads pages, passes protection
  checks, extracts records and writes the result file.
- The **extension** gives your workflows actions that call the service, plus these
  pages.
- Two **workflows** you import from the extension's templates:
  - **Scrape a site** prepares a crawl, starts it and waits for it: settings →
    analyze the site → a few sample pages → the extraction mapping (written by AI,
    or the stored one) → test on the samples → activate → start the crawl → wait
    until it ends → store the result. Its job shows the files when the crawl ends.
  - **Scraper events** runs when the service calls back (a signed webhook): for a
    finished or failed crawl it wakes the Scrape a site job that waits for it; for a
    template that stopped extracting it repairs the mapping with AI.

A third, optional template, **Service maintenance**, cleans up the service's disk
once a week (see *Disk use and clean-up*).

**The results are in the `Scrape a site` job** (from 0.1.23): it waits for its own
crawl and shows the files when it ends; see *Where are my results*. To process them,
call Scrape a site from your own workflow (see *Use Scrape a site from your own
workflow*).

## Where are my results

1. `Scrape a site` starts the crawl, then its step **Wait for the run to end**
   waits: the job shows *Waiting for event* and holds no worker. Its output
   `scraper_run_id` is the run id.
2. The crawl runs in the service, for minutes or hours. Follow it on the page
   **Runs**: it lists the runs of the profile chosen at the top of the page; click
   one to open **Run detail**.
3. When the crawl ends, the service reports it to **Scraper events** (its webhook).
   Its step **Claim event** wakes exactly the Scrape a site job that started this run
   (the run id is the key).
4. Scrape a site goes on: **Get run summary**, **Get result** stores the records as
   the file `result.jsonl` (one JSON record per line), **Get result CSV** the same
   records as `result.csv` for Excel, **Read the result** the first rows.
5. The Scrape a site job's **Result** panel shows:
   - `result_file` — the JSON Lines file, to open or download;
   - `result_csv` — the same records as CSV (see *The CSV file*);
   - `result` — file name, records, size, SHA-256, outcome and records per template;
   - `result_preview` — the first 10 records;
   - `run_summary` — why the run stopped and how complete it is.
6. From the run: open it on **Runs**; Run detail, section *Results*, names the files
   and the records and links the job that holds them.

A run that produced no records has no file: Scrape a site ends *No records* and
`run_summary` says why. A failed run fails the Scrape a site job with the run's
summary as its message. The service keeps its own copy of the file for 14 days by
default; the copy in Flow Steward stays with the job.

Scrape a site waits at most **3 days**. After that it reads the run once more: a run
that has completed meanwhile (for example the webhook did not work) still gives its
files; a run that is still going on fails the job with *did not complete … It is
still going on*. The crawl itself goes on; follow it on Runs.

**Cancelling does not stop the crawl.** Flow Steward 0.9.18 tells an extension nothing
when a job is cancelled, so cancelling a waiting Scrape a site job — or the workflow
that called it — leaves the crawl running in the service. Press **Stop** on the run's
row on **Runs**; while such a run is going on, Run detail reminds you of it. A crawl
that ends after its job was cancelled keeps its files only in the service (Run detail,
*Results*).

**A site that is already being scraped.** When a run of the same site is still going
on, a new start is skipped by default: Scrape a site ends *Skipped: site busy* — a
successful end, not a failure — with the output `skipped` true and `skipped_reason`
"Skipped: shop.example is already being scraped (run …, since …)". Nothing new is
crawled and there are no files. **When the site is busy** `queue` waits for the other
run instead (see *The run form*).

Runs started another way — by a copy of Scrape a site imported before 0.1.23, by
**Re-collect missed pages** on Runs, or by your own **Start run** step — still get
their files in a **Scraper events** job, as before.

Jobs of Scraper events imported before 0.1.17 show an empty Result panel, and
before 0.1.18 no CSV: import the template again (see *Updating the extension*).

### The CSV file

The CSV holds the same records as `result.jsonl`, one row per record:

- UTF-8 with a byte order mark, so Excel reads accents and other alphabets right.
- The separator is `;` (Excel in most European settings splits on it, and a decimal
  comma never collides with it). Other tools need it named, for example pandas
  `sep=";"`.
- Columns: `record_id`, `source_url`, `requested_url`, `source_key`, `site`,
  `template_id`, `mapping_version`, `item_index`, `extracted_at`, then `variant.*`
  when the run has variants, then `price_status`, `price_max`, `regular_price`,
  `on_sale`, `currency_from_default` when the mapping has a price field, then
  `data.<field>` for every field.
- A list is joined with ` | `; a specification table or other object is JSON text;
  an empty value is an empty cell.
- **Formula guard:** a text that starts with `=`, `+`, `-` or `@` gets a leading `'`,
  so a spreadsheet shows it instead of running it as a formula. Numbers are never
  changed. When the exact text matters, read `result.jsonl`: it keeps every value as
  it was extracted.

A service older than 2026-11-14 writes no CSV; then **CSV available?** skips the
CSV step and only `result_file` is there.

With the service image `ghcr.io/flow-steward/web-scraper-service:0.2.0` (or later), a
run whose mapping has a price field also has the columns `price_status`, `price_max`,
`currency_from_default`, `regular_price` and `on_sale` after `extracted_at` (see
*Prices in the result*).

### Prices in the result

A record without a price is never left unexplained. Every record of a template that
has a price field carries `price_status` (in `result.jsonl` at the top of the record,
in the CSV as a column):

- `shown` — the page shows a price, and `data.price` holds it.
- `range` — the page shows a range such as "24,00 € – 54,00 €" (typical for a product
  with variants when the variants are not read): `data.price` holds the lowest value,
  `price_max` the highest.
- `on_request` — the page shows no price and says it is given on request ("price on
  request", "contact us for a price", "to find out the price…"). The empty price is
  right; nothing was missed.
- `missing` — no price and no sign why. Check the page; if it does show a price, the
  mapping misses it (see Troubleshooting).

`currency_from_default: true` means the page names no currency and the currency came
from the default the mapping set. A record without a price gets no default currency.

Steps that compare or set prices must skip `on_request` and `missing` records instead
of reading their empty price as 0. A variant record that has its own price is `shown`.

**A sale** (service image `ghcr.io/flow-steward/web-scraper-service:0.2.0` on).
`data.price` is always the **current** price, never the struck-through one. When the page or the variant shows a sale, the record also
has `regular_price` (the old price) and `on_sale: true`, at the top of the record and as
CSV columns:

```json
{"data": {"title": "Lamp", "price": 24.0, "currency": "EUR"}, "price_status": "shown",
 "regular_price": 35.0, "on_sale": true}
```

A record without a sale has neither key (an empty cell in the CSV). `result_preview`
shows them as they are in the file.

**Which price the mapping reads.** The mapping reads the price the page states for
programs first — `[itemprop=price]` `content`, JSON-LD `offers.price` (or `lowPrice`),
the `product:price:amount` meta, a `data-price` attribute — and the visible text only as
a fallback. The service also reads a price split into tags (`24<sup>99</sup>` is 24.99)
and decides an ambiguous `1.234` by the page's language. The mapping test fails a
mapping whose price differs from the stated one (*price_mismatch*) or reads the old
price on a sale page (*price_is_old_price*); the AI then corrects it.

## Setup checklist

Do these once per Flow Steward project, in this order.

1. **Run the scraper service** (the image `ghcr.io/flow-steward/web-scraper-service:0.2.0`,
   linux/amd64 or linux/arm64; about 2 GB of memory per running browser job). Environment:
   - `WSS_API_TOKEN` — required; a long random value. You type it into the profile.
   - `WSS_SECRETS_KEY` — required to start runs (32 bytes URL-safe base64, or a
     passphrase of 16+ characters); without it starts fail with
     `secrets_key_unavailable`.
   - `WSS_DATA_DIR` — the one volume (default `/data`); mount it.
   - `WSS_ALLOW_CHALLENGE_CLICK=1` allows the checkbox click on protected sites;
     `WSS_ALLOW_SOLVERS=1` allows paid solvers; `WSS_ALLOW_ROBOTS_OVERRIDE=1` allows
     ignoring robots.txt. All are off by default; use them only where you may.
   - Optional limits `WSS_LIMIT_<NAME>`, e.g. `WSS_LIMIT_MAX_PAGES_PER_JOB`.
   - Put it on the platform's Docker network with a container name, e.g.
     `web-scraper` (it listens on port 8080).
   - A service on a private address (Docker network, 10.x, 192.168.x) needs
     `FS_ALLOW_PRIVATE_REMOTE_URLS=1` in the platform's environment.
   - Check: `GET <service URL>/health` with `Authorization: Bearer <token>` answers
     200.
   - Without Compose, start it with the `docker run` command under *Disk use and
     clean-up*, and keep its log options.
2. **Install the extension** (Extensions → Install extension from file → the zip),
   enable it.
3. **Create a scraper profile** on the page **Profiles**:
   - **Service · URL** as the platform reaches it inside Docker, e.g.
     `http://web-scraper:8080`.
   - **Service · Token**: type the token (choosing a stored secret is refused when
     the profile is created).
   - Leave the webhook fields empty for now (step 6 fills them).
   - **Proxy tier 1–3** (optional): tier 1 is tried first. Protected sites need a
     **sticky** session, best a residential proxy. *Generic* takes
     `scheme://user:password@host:port`.
   - Save, then press **Approve use of secrets** on the profile's card, then **Test
     service**.
4. **Approve use of secrets again after every change** of the profile and after
   every extension update. Until then every action fails with
   `secret_configuration_unapproved`.
5. **Import both templates**: Workflows → Import YAML → Upload YAML, choose
   `scraper_events.bundle.yaml`, Run preflight, map the extension to your Web
   Scraper and the connection `scraper_profile` to your profile, Import workflow.
   Do the same for `scrape_site.bundle.yaml`. Both files are in the extension
   archive under `templates/`. Optional: `service_maintenance.bundle.yaml` for a
   weekly clean-up of the service's disk.
   With Flow Steward 0.9.18 you can use the extension's own read-only workflows
   instead of importing copies (see *Read-only workflows and hooks*).
6. **Create the webhook on Scraper events**: open the workflow's settings, tab
   **Webhook**, **New webhook**:
   - Who sends the requests?: **Custom**; input mapping blank.
   - Advanced settings → Auth mode **HMAC SHA256**, header name
     `X-Scraper-Body-Signature` (prefix `sha256=` is the default), content type
     `application/json`, Idempotency mode **Require `Idempotency-Key`**.
   - Create webhook; press **Copy credential** at once (it is shown once).
   - In the profile set **Events · Webhook URL** to the platform's **internal**
     address the service can reach, e.g.
     `http://compact-runtime:8080/webhook/workflows/<key>` (keep the path from
     **Copy public URL**), and **Events · Webhook signing secret** to the copied
     credential. Save and **Approve use of secrets** again.
   - In the workflow's **Interface**, keep **Only accept listed inputs** off (the
     service sends more fields than the four inputs).
   - Enable both workflows.
7. **Project model**: the mapping agent uses the project's default model route; it
   must support structured output (verified with OpenAI; Anthropic and Google also
   work).

## Read-only workflows and hooks

From 0.1.22 the extension brings its own **Scrape a site**, **Scraper events** and
**Service maintenance**. They belong to the extension: you cannot change their
steps, but you can read every step, export them and clone them. An update of the
extension updates them. Your own steps go into their **hooks**.

**Use them in a project:**

1. Open the workflow (Workflows; the extension's ones say *Web Scraper*), tab
   **Projects**, and link it to the project.
2. On its **Overview**, the **Connections card** says *This workflow needs: Scraper
   profile*. With one profile in the project it is chosen for you; with several,
   choose one. Until then the workflow is not ready and does not start.
3. For **Scraper events**, add the signed webhook as in setup step 6: the workflow's
   settings, tab **Webhook**, **New webhook** (Custom, HMAC SHA256, header
   `X-Scraper-Body-Signature`, Require `Idempotency-Key`), then put its internal URL
   and credential into the profile and **Approve use of secrets**. A webhook is the
   events trigger of the read-only workflow too; it needs no change of the workflow.
4. Run **Scrape a site** as before.

**Hooks** are places where your project adds its own steps. Open the workflow with
**View workflow (read-only)**, choose the project at the top, and use **Add step**
inside a hook. An empty hook changes nothing.

- **Review the mapping** (`review_mapping`, Scrape a site) — after the mapping was
  built or a stored one passed on the samples, before it is tested again and
  activated. It receives `site`, `mappings`, `field_results` and `summary`; your
  steps may change `mappings` (for example add a field, or ask a person in an
  approval step). The changed mappings must pass the test, or nothing is activated.
- **Before the crawl starts** (`before_start`, Scrape a site) — after the mapping is
  active, before the crawl starts. It receives `config`, `site` and
  `mapping_summary`; your steps may change `config` (for example lower
  `max_records` for a trial), or set `skip_run` to true to start no crawl.
- **On a problem or a failed run** (`on_error`, Scraper events) — when the service
  reports a problem or a failed run, after the run summary was read. It receives
  `event_type`, `run_id`, `state`, `terminal`, `message`, `explanation`, `error` and
  `errors`; for example notify someone.

Processing the result of a run (a supplier's products, a competitor's prices) is not
a hook: call Scrape a site from your own workflow and add your steps after it (see
*Use Scrape a site from your own workflow*).

**Your copies keep working.** Workflows you imported from `templates/` are your
project's own; they stay as they are and still work. To switch to the extension's
workflows: link them to the project, choose the profile on the Connections card,
create the webhook on the extension's Scraper events (and put its URL and credential
into the profile), move your own steps into the hooks, then disable your copies.

## Use Scrape a site from your own workflow

From 0.1.23 Scrape a site waits for its own crawl and returns the files. That makes it
a building block: each purpose gets a small workflow of your own that runs Scrape a
site and then works on the result. Scrape a site stays the extension's (an update
improves it); your workflows are yours and an update never touches them.

**How it works**

1. Your workflow starts: a schedule, or **Run workflow**.
2. An **Invoke workflow** step runs **Scrape a site** with this purpose's inputs. Your
   run waits for it. Neither run holds a worker while the crawl goes on, for hours if
   needed.
3. Scrape a site starts the crawl and waits. When the crawl ends, the service tells
   Scraper events, which wakes exactly this Scrape a site job. It stores the files and
   ends.
4. Your Invoke step receives the outputs and the files, and your own steps go on.

Needed once: Scraper events linked to the project with its webhook (setup step 6, or
*Read-only workflows and hooks*), in the same project as your workflow.

**The Invoke step**

- **Workflow**: Scrape a site — the Web Scraper extension's from 0.1.24 on, or a copy
  imported from 0.1.24 on. Before 0.1.24 Scrape a site did not accept a call from
  another workflow, and Flow Steward refused to enable the calling workflow with
  *invoke.child_not_parent_workflow_triggerable*. The extension's own workflow changes
  by itself with the update; a copy imported earlier needs a new import (or add
  `parent_workflow` to its trigger channels: workflow settings, *Callable*).
- **Inputs**: at least `site_url` and `extraction_instructions`; add any run form field
  this purpose needs (`max_records`, `discovery_mode`, `urls`, `fields`,
  `required_fields`, `run_name` …). A field you leave out uses the profile's default.
- **Output mapping**: `run_summary`, `result` (file details: `record_count`,
  `outcome`, `stop_reason`), `result_preview` (the first 10 rows), `scraper_run_id`,
  `skipped` and `skipped_reason` to your own variables, for example
  `state.scrape.summary` and `state.scrape.skipped`.
- **Returned files mapping**: `result_file` (JSON Lines) and `result_csv` (CSV) to your
  own variables, for example `state.supplier_jsonl` and `state.supplier_csv`. Your run
  then owns the files.
- A crawl that failed fails Scrape a site with the run's summary as its message, and
  then your Invoke step. Give the Invoke step an error route when your workflow should
  do something about it (notify someone, try again next week).
- A start while a run of the same site is still going on ends Scrape a site
  *Skipped* (with **When the site is busy** empty or `skip`): your Invoke step
  succeeds, `skipped` is true, `skipped_reason` says which run, and there are no files.
  Put an **If** on `state.scrape.skipped` right after the Invoke step, so your own
  steps do not read a missing file. Pass `when_site_busy: queue` when this purpose must
  wait for the other run instead.
- Cancelling your workflow cancels Scrape a site too, but not the crawl: stop it on
  **Runs** (see *Where are my results*).

In a definition the step looks like this (the builder fills in the workflow):

```yaml
step_id: scrape_supplier
step_kind: invoke_workflow
override:
  invoke_workflow:
    workflow_id: <Scrape a site, as the builder fills it in>
    input_mapping:
      site_url: state.inputs.site_url
      extraction_instructions: state.inputs.what_to_extract
      run_name: state.inputs.supplier_name
    output_mapping:
      run_summary: state.scrape.summary
      result: state.scrape.result
      skipped: state.scrape.skipped
      skipped_reason: state.scrape.skipped_reason
    artifact_mapping:
      result_file: state.supplier_jsonl
      result_csv: state.supplier_csv
```

**Recipe: a supplier's products, weekly**

1. Your workflow *Supplier import*, with inputs `site_url`, `what_to_extract` and
   `supplier_name`.
2. **Invoke** Scrape a site: the supplier's category page as `site_url`; *What to
   extract* such as "Product pages: name, SKU, price, availability, full description,
   all images in full size"; **Required fields** `title`, `sku`.
3. **Tabular feed** (read) on `state.supplier_jsonl`: the records as a dataset.
4. Your cleaning steps: names, company suffixes, HTML in texts, image addresses.
5. Compare with your shop (your shop extension's query or export) by SKU: new
   products, products gone, availability changed.
6. **Approval** with the list of new products.
7. Create the new products and update availability in the shop (your shop
   extension's steps).

**Recipe: competitors' prices, weekly**

1. Your workflow *Competitor prices*, with inputs `site_url`, `urls` (the competitor's
   product pages) and `competitor`.
2. **Invoke** Scrape a site in `url_list` mode: **Discovery** `url_list`, the pages in
   **Pages to scrape** (`urls`), *What to extract* "name, price, currency,
   availability". To keep your own SKU next to each page, use a URL file with a `key`
   column instead: the key comes back as `source_key` on each record.
3. Read the result; skip records whose `price_status` is `on_request` or `missing`
   (see *Prices in the result*).
4. Your rule, for example "competitor's price minus 1 €, never below my floor price".
5. **Approval** with the new prices before anything changes.
6. Update the prices in your shop.

**Several sites from one workflow.** A workflow can have many schedules, each with its
own **Name** and its own input values (the workflow's **Schedules** tab). Give *Supplier
import* one schedule per supplier — "Supplier A" on Monday 06:00 with A's category,
"Supplier B" on Tuesday with B's — instead of one workflow per supplier. Jobs then
read "Supplier A · …"; Scrape a site's own job is titled with the run name and the
site (`run_title`). When you only need the files, the schedules can sit on Scrape a
site itself, each with its own inputs.

## The pages

- **Profiles** — the only place to create and edit a scraper profile.
- **Service**, **Runs** and **Run detail** use the profile chosen in the selector at
  the top of the page, next to the project. With one profile it is chosen for you.
  These pages load by themselves; there is nothing to type.
- **Runs** — the newest runs, reloaded every half minute: site, state, progress,
  records, why it stopped, start and end. Filter by state or site above the table.
  Click a row (or its site) to open Run detail. A row shows a button only when it
  applies: **Stop** while the run is queued or running, **Resume** when the service
  waits for the profile's secrets, **Re-collect missed pages** when a finished run
  missed pages.
- **Run detail** — one run in sections: Status, Progress, Why it stopped / what to
  do, Results (the files and the job that holds them: the Scrape a site job that
  waited for the run, else a Scraper events job), Missed pages and Timeline; then the
  tables **Events** (with a link to the Scraper events job that took each event) and
  **Missed pages** (with *What went wrong*: for a page kept for a mapping repair, what
  the page shows that the mapping does not read). An event that failed or was never
  handled has a **Send again** button.
- **Service** — what the service keeps on its disk, and the clean-up (see *Disk use
  and clean-up*).

The pages need Flow Steward 0.9.18 or later (they read through the page's profile, and
their tables link and filter).

## Running Scrape a site

Open the workflow and run it. The dialog has two ways:

- Blue **Start test run** — runs everything up to the crawl (settings, analysis,
  samples, AI mapping, test, activation) and **does not crawl**. It ends with *Test
  run: the mapping was built and tested; no crawl was started.* Use it to try the
  instructions and the mapping.
- Grey **Run workflow**, after ticking the acknowledgement (*I understand this
  workflow run can create external changes…*) — the real run that starts the crawl.
  The **URL file** picker for `url_list` mode is in this Run workflow section.

Every setting is a field of the run form; nothing is edited inside the workflow. An
empty field uses the profile's default, then the service's. Webhooks and schedules
pass the same fields.

### The run form

**Basics — fill these:**

- **Site URL** (`site_url`) — the start page, e.g. `https://www.example-shop.test/`.
  In `url_list` mode the site's home page. Required.
- **What to extract** (`extraction_instructions`) — in words, for the AI: which
  pages and which data, e.g. *Product pages: name, price in EUR, article number,
  full description with the technical table, all images.* Required.
- **Run name** (`run_name`) — optional, e.g. *Supplier A weekly*. Only a title: Jobs
  shows the run as "Supplier A weekly https://…" (with Flow Steward 0.9.18 run
  titles); it is not sent to the scraper.
- **Maximum records** (`max_records`) — stop after this many records. Empty means
  **no limit: the whole site**. Use 3–10 for a first trial.
- **Product page selector (CSS)** (`target_page_selector`) — a class, id or
  attribute that is present **only on product pages**, e.g. `.product-detail`,
  `#product` or `[itemtype$='Product']`. Take a top-level one (the product page's
  main container), not something deep inside. Pages without it are loaded for their
  links but never extracted; the AI is told to use it in the mapping. Empty: pages
  are told apart by URL and structured data. Set it whenever the site has one — it
  is the best protection against a mapping for blog or category pages.
- **Debug mode** (`debug`) — screenshots of protection checks and a verbose job log.
  Empty: off. Switch on when a site does not let the scraper through.

**Set when you know better:**

- **Extract only URLs matching (regex)** (`url_include`) — *what to extract*: a JSON
  list of regular expressions matched against the full URL; only matching pages are
  extracted (other pages are still walked for links), e.g. `["/product/",
  "-p[0-9]+$"]`. A filter, not a list of pages: in `url_list` mode the pages go into
  **Pages to scrape (url_list)**. Empty: every URL.
- **Never these URLs (regex)** (`url_exclude`) — *never fetch*, e.g. `["/blog/",
  "/about", "/ru/"]`. Empty: none.
- **Where to walk (regex)** (`crawl_include`) — *where to walk*: links are followed
  only on pages whose URL matches, e.g. `["/lighting/"]`; the start page always.
  Product pages linked from there are still fetched. Empty: the whole site.
- **Stay inside the start page's section** (`stay_in_section`) — on: walk only the
  start page and everything below its path (its pagination included). Start from a
  category page and switch it on to take everything in that category and nothing
  else. Empty: off.
- **Let the AI choose the fields** (`ai_choose_fields`) — on: the AI adds the fields
  the sample pages really carry, beyond **Fields**; a specification table becomes one
  field. Empty: off.
- **Fields hint** (`fields_hint`) — for the AI-chosen fields: the kind of product and
  the data, e.g. *wine: vintage, type, bottle size, taste*. Empty: no hint.
- **Discovery** (`discovery_mode`) — how pages are found. Empty or `auto`: the
  analysis decides. `sitemap`: when the sitemap is complete and current. `crawl`:
  follow links from the Site URL — start from a category page to stay inside it.
  `sitemap_and_crawl`: both. `url_list`: exactly the given URLs, nothing else.
- **Pages to scrape (url_list)** (`urls`) — in `url_list` mode, a JSON list of the
  exact page URLs to scrape. `url_list` mode needs this list or a URL file.
- **URL file (url_list)** (`urls_file`) — in `url_list` mode, a CSV with a `url`
  column and an optional `key` column (copied into each record as `source_key`), or
  one URL per line.
- **Fields** (`fields`) — the field names as a JSON list, e.g. `["title", "price",
  "currency"]`. Empty: title, description, price, currency, part_number, condition.
- **Required fields** (`required_fields`) — fields a record must have; the mapping
  test fails until they are filled. Empty: title. Add every field you really need,
  otherwise a mapping that fills only the title passes.
- **Build a new mapping** (`rebuild_mapping`) — on: the AI writes a new mapping even
  when the stored one passes. Empty: off. Use it when the stored mapping is wrong
  (e.g. it matches the wrong pages).
- **Fetch mode** (`fetch_mode`) — `http` (fast, no JavaScript), `browser` (renders
  pages), `auto`. Empty: the profile's default, normally auto. Set `browser` when
  the site always needs JavaScript.
- **Allow the checkbox click** (`challenge_click`) — on: the scraper may click a
  protection checkbox (Cloudflare and similar). Needs a sticky proxy and
  `WSS_ALLOW_CHALLENGE_CLICK=1` on the service. Empty: the profile's default,
  normally off. Switch on for protected sites.
- **Proxy tiers** (`use_proxy_tiers`) — which of the profile's proxy tiers to use,
  e.g. `[1, 2]`. Empty: every tier the profile has.
- **Politeness** (`politeness`) — how gently the crawl treats the site:
  - `gentle` — a pause of 5–10 s between requests, 1 page at a time, 10 requests a
    minute, page timeout 120 s. For small or slow shops, and sites that block easily.
  - `normal` — 2–5 s, 2 at a time, 30 a minute, 60 s. Empty means normal.
  - `fast` — 0.5–1.5 s, 4 at a time, 120 a minute, 30 s. Only for sites that take it.

  **Shortest/Longest pause**, **Pages at once**, **Requests per minute** and **Page
  timeout** override the preset when you set them (on the run form or in the profile's
  defaults). The service keeps robots.txt's crawl-delay and slows down by itself when a
  host answers with errors or slowly (never faster than the preset); Run detail shows
  the effective politeness and the hosts it slowed down.
- **Page timeout (s)** (`page_timeout_seconds`) — how long one page may take to load,
  10–300 s; a slower page is retried, then counted as not loaded. Empty: from
  Politeness (normal: 60). Raise it to 120 or more for a slow site.
- **When the site is busy** (`when_site_busy`) — a run of the same site is still going
  on: `skip` (empty) ends this job *Skipped: site busy* without a new crawl — right for
  schedules, so a daily run never piles up behind a long one; `queue` stores this run
  and starts it when the other one ends.

**Advanced — leave empty unless you have a reason:**

- **Browser OS** (`browser_os`) — the OS the browser claims. Empty or `host`: the
  machine's own OS (Linux in the service image). Keep it: protection checks such as
  Cloudflare usually refuse a Windows or macOS claim from a Linux machine (tested:
  host passed, every Windows claim failed). `linux` is the same as host.
- **Canvas noise** (`canvas_noise`) — whether canvas and WebGL readback is covered
  with random data. Empty or `auto`: only when Browser OS claims another OS. With
  the default Browser OS the canvas reads back exactly.
- **Browser** (`browser`) — `camoufox` (default) or `chromium`.
- **Allow the challenge solver** (`use_solver`) — on: use the profile's paid solver
  (needs `WSS_ALLOW_SOLVERS=1`). Empty: off.
- **Sitemap URL** (`sitemap_url`) — when the sitemap is not in robots.txt or at
  `/sitemap.xml`.
- **Maximum pages** (`max_pages`) — pages loaded at most, listing pages included.
  Empty: 10000.
- **Only the start page** (`single_page`) — on: scrape only the Site URL.
- **Shortest pause (s)** and **Longest pause (s)** (`delay_min_seconds`,
  `delay_max_seconds`) — random pause between requests; override **Politeness**.
  Empty: the profile's default if it has one, else Politeness (normal: 2 and 5).
- **Pages at once** (`max_concurrency`) — overrides Politeness. Empty: the profile's
  default, else Politeness (normal: 2).
- **Requests per minute** (`max_requests_per_minute`) — overrides Politeness. Empty:
  the profile's default, else Politeness (normal: 30).
- **Extraction mode** (`extraction_mode`) — `structured_data` (JSON-LD only),
  `selectors` (CSS and meta only), `auto` (both). Empty: auto.
- **Structured data types** (`structured_data_types`) — schema.org types that mark a
  product page. Empty: Product, Offer, AggregateOffer.
- **Extra wait after load (ms)** (`wait_after_load_ms`) — empty: 0.
- **Wait for the selector (ms)** (`wait_for_selector_ms`) — how long a browser page
  waits for the product page selector. Empty: 10000.
- **Extra HTTP headers** (`headers`) — e.g. `{"Accept-Language": "de-DE"}`; no
  credentials or cookies.
- **Respect robots.txt** (`respect_robots`) — off ignores robots.txt (only if the
  service allows it). Empty: on.
- **Links not to skip** (`link_rules_off`) — a list of link rules to switch off for
  this run, e.g. `["facet_params"]`. Empty: every rule on. See *Links the scraper
  skips*.
- **Trap budget (pages)** (`trap_pattern_budget`) — a URL pattern stops being
  followed after this many pages in a row without a product page. 0: off. Empty:
  100.

## Links the scraper skips

When the crawl walks a site, it does not follow every link. These rules keep it out
of pages that never hold products and out of endless loops. Each skipped link is
counted; Run detail shows a short line such as *Skipped links: 15 500
account/cart/wishlist, 11 340 filters*.

- `scheme` — mail, phone, JavaScript, messenger and app links.
- `messenger_social` — messenger and share buttons that lead to other sites.
- `sensitive_path` — account, login, cart, checkout, wishlist, compare, search,
  review-form, newsletter and print pages.
- `action_params` — links that do something: add to cart, add to wishlist, compare,
  one-time form keys.
- `file_extension` — files: documents, archives, images, media, fonts, feeds.
- `nofollow` — links the page marks `rel="nofollow"`.
- `facet_params` — filter and sort variants of a listing (color, size, price, brand,
  sort, limit …).
- `trap_budget` — a URL pattern that gave no product page for many pages in a row
  (calendars, endless filter combinations). The number is **Trap budget (pages)**.
- `dynamic_params` — not a skip: a parameter with a new random value on most links
  is ignored when telling pages apart.
- `canonical` — another address of a page already collected: the page names its real
  address (`rel="canonical"`), so it is counted as a duplicate, not collected twice.

**Analyze site** says which rules are on (output `link_rules`).

**When to switch one off** (run form, **Links not to skip**):

- A shop whose products are reached only through its filters (no plain category
  pages): switch off `facet_params`.
- The products you want sit under a path the rules call an account or search page:
  switch off `sensitive_path`.
- A site whose canonical addresses are wrong (many products name one address) is
  detected by the service, which then stops trusting them; to stop it yourself, switch
  off `canonical`.
- A long listing that the trap budget cuts too early: set **Trap budget (pages)**
  higher, or 0 to switch it off.

## Products with variants

A product page with options (a format CD or vinyl, a sticker no / EU / USA, sizes)
gives **one record per variant**. Each record has the variant's own option values,
price, SKU, availability, image and weight; the page's common fields (title,
description, brand …) are repeated in every variant record. A page without variants
stays one record. Each variant record also carries `variant`: its position, the
number of variants, the source and the option values.

- **Where the data comes from:** the page's own code, never by clicking through the
  options. The service finds JSON in the page (the WooCommerce variation form, a
  Shopify product script), the shop's product JSON (Shopify `/products/<handle>.js`,
  the WooCommerce Store API) or structured data (JSON-LD or microdata offers).
  **Fetch samples** says what it found (output `variant_summary`, e.g. *2 of 4
  samples carry variants: embedded_json 3 variants (options Lens cover markings)*);
  the AI copies that source into the mapping; the mapping test shows *6 variants on
  2 of 2 samples* and how many variants each variant field filled.
- **The same SKU on several variants** is kept: some shops use one article number
  for all variants on purpose (e.g. the same part with different stickers). Records
  are never merged by SKU; each variant has its own record id.
- **Prices in minor units** (Shopify JSON: pence, cents) are converted; when the
  shop's JSON has no currency, the mapping sets it.
- **Protected sites:** data inside the page (embedded JSON, JSON-LD) is preferred —
  it needs no further request. The shop's product JSON is one light request per
  product through the session that passed the protection check.
- A mapping stored before 0.1.14 has no variants: on pages with variant data its
  test fails and the AI builds a new mapping by itself.

## Crawl scope

Three controls, each with one job:

- **What to extract** — **Extract only URLs matching** (`url_include`): which pages become
  records.
- **Where to walk** — **Where to walk** (`crawl_include`) or **Stay inside the start
  page's section** (`stay_in_section`): on which pages links are followed. A crawl
  started on a category no longer leaves it through the menu.
- **Never fetch** — **Never these URLs** (`url_exclude`): language versions, sort
  and filter parameters, blog.

Example: Site URL `https://shop.example/lighting/`, **Stay inside the start page's
section** on, **Never these URLs** `["/en/", "\\?sort="]`. The run summary counts
the links skipped because of the scope separately.

## Images and specifications

- **Images:** the AI takes the full-size image first — the zoom link, the large-image
  data attributes, the largest `srcset` entry — and rewrites a thumbnail address only
  when nothing else has the full size. All gallery images go into one list field
  (duplicates dropped); ask in **What to extract** for "all images" to get them.
- **Specification tables** (rows of *name: value*): one field of type `spec_table`;
  in the result it is an object, e.g. `{"Vintage": "2019", "Bottle size": "0.75 l"}`.
  Ask for it in **What to extract**, or switch on **Let the AI choose the fields**.
- **Let the AI choose the fields** keeps every field you list and adds what the
  sample pages really carry; **Fields hint** says what matters. Values that exist only
  in free text (taste notes inside a sentence) are not extracted by the mapping — add
  an AI step after the crawl in **Scraper events**, over the result file.

## Why did the run stop, and is it complete?

**Run detail** and the step **Get run summary** in **Scraper events** say
it in plain words:

- **Why it stopped** (`explanation`) — e.g. *the site stopped answering after
  repeated server errors* (stop reason `site_unavailable`: the service pauses with
  growing waits and then stops instead of loading a site that is down), with **what
  to do**.
- **Completeness** — target pages found, pages collected, records, and what was not
  collected by reason; while running *M of N* collected.
- **Comparison** — when records fell clearly against the previous full run of the
  site, a warning says whether the site has fewer products or the scraper missed
  found pages.
- **Timeline** — start, discovery decided, first block, proxy tier change, repairs,
  stop.
- A problem *sitemap outdated* means many sitemap URLs answered 404; with Discovery
  `auto` the run adds the link crawl by itself.
- **Unusable answers** — a server under load sometimes answers with an empty, cut-off
  or error page instead of the product. The service retries such a page like a server
  error and counts it ("3 unusable answers from the site … retried (cut off 2, error
  or maintenance page 1)"); a page that stays unusable is *not loaded*, never a
  half-empty record. *Mapping repairs avoided* counts repairs not started because the
  pages were fine when fetched again.
- **Politeness** — the effective pauses, pages at once, requests per minute and page
  timeout ("normal: 2–5 s, 2 at a time, 30/min, page timeout 60 s"), and any host the
  service slowed down because it struggled.

## The steps

### Scrape a site

- **Validate settings** — merges the run form with the profile; fails naming a field
  that is above a service limit or not valid.
- **URL list?** — `url_list` mode skips the analysis.
- **Analyze site** — robots.txt, sitemap, JavaScript, protection; output
  `protection_summary` describes any protection check. Fails when the service is
  unreachable or out of time.
- **Choose strategy** — takes the recommended discovery when Discovery is auto.
- **Get stored mappings** — reads the mapping already active for this site.
- **Fetch samples** — loads a few product pages. Fails with *protection check was not
  passed* or *No target page could be sampled* followed by what the service tried
  (see Troubleshooting).
- **Rebuild asked?**, **Stored mapping?**, **Test stored mapping**, **Stored mapping
  passes?** — a stored mapping that still passes on the samples is reused.
- **Build mapping (AI)** — the AI writes a mapping, it is tested on the samples and
  refined at most twice. Its failure ends in *Build mapping failed* (usually the
  model gave no structured answer).
- **Mapping passed?** — no passing mapping ends in *No passing mapping*.
- **Test mapping before activation** — tests again (also after your own review
  steps); *Changed mapping fails* when it no longer passes.
- **Activate mapping** — makes the mapping active; fails when someone changed it
  meanwhile.
- **Start run** — starts the crawl (suppressed in a test run). Its step id
  `start_crawl` tells Scraper events that this job waits for the run.
- **Start skipped?** — a run of this site was still going on and **When the site is
  busy** is `skip`: the job ends *Skipped: site busy* (a success) with `skipped_reason`.
- **Crawl started?** — goes on to the wait, or ends in *Test run: no crawl*.
- **Wait for the run to end** — waits for `run_completed` or `run_failed` of this run
  (Flow Steward *Wait for an event*), at most 3 days; no worker is held.
- **Get run summary** — how the run ended. A run that did not complete (failed, or
  still going on after 3 days) fails here with the run's summary as the message.
- **Any records?** — a run without records ends in *No records*.
- **Get result**, **CSV available?**, **Get result CSV** — store `result.jsonl` and
  `result.csv` as files of this job; a failure ends in *Storing the result failed*
  (the service still keeps the files).
- **Read the result** — the first 10 rows as `result_preview`; if it fails, the files
  are still returned.
- *Result ready* — the outputs and files are in the Result panel and go to a calling
  workflow.

### Scraper events

- **Claim event** — reserves the event for this job; *Handled elsewhere* when another
  job has it.
- **Event type** — run completed, mapping required, or a problem.
- **Completed before?**, **Processed before?** — a resent event skips what was done.
- **Get run summary** — the run's outcome, stop reason and record count.
- **Stored by Scrape a site?** — a run started by Scrape a site 0.1.23 or later is
  stored by that job (Claim event woke it); this job only marks the event handled.
- **Any records?** — a run without records skips the result and is still marked
  handled.
- **Get result** — stores `result.jsonl` as a file of this job. Its failure ends in
  *Processing failed* and the job keeps the claim (see Troubleshooting).
- **CSV available?**, **Get result CSV** — stores the same records as `result.csv`
  when the service writes one; a failure ends in *Processing failed* like Get result.
- **Example: read the result** — reads the records into a dataset; replace it with
  your own steps.
- **Mark processed**, **Mark completed**, **Complete event** — record progress so a
  resent event never processes twice. Keep them.
- **Get mapping request**, **Still open?**, **Build mapping (AI)**, **Repair
  passed?**, **Activate repaired mapping**, **Report mapping failure** — repair a
  template that stopped extracting during the crawl; a failure gives the template up
  for this run and the run ends partial.
- **Close request after conflict**, **Fail mapping request (error)**,
  **Complete event (error)** — the repair's error routes: the service is told, the
  event is handled, and the job shows the failure (*Repair failed*).

### Service maintenance

- **Storage before** — reads what the service keeps and its own automatic clean-up
  ages.
- **Clean up service storage** — removes finished runs' working files, result files,
  run records, samples and screenshots older than those ages, keeps every result
  Flow Steward has not fetched, and compacts the database. Run input **Dry run
  only** (`dry_run`) removes nothing.
- **Storage after** — reads the storage again.
- *Clean-up done* — the Result panel shows `storage_before`, `cleanup`, `freed`,
  `bytes_freed` and `storage_after`.

Add your own steps where the canvas notes say: before *Validate settings*, on the
path into *Test mapping before activation* (a review), and in Scraper events between
*Get result* and *Mark processed* (your processing).

## Collect the missed pages again

A run may end with pages it could not extract: the site timed out, blocked a page,
a required field was empty, or no template matched. The table *Missed pages* on Run
detail lists them with the reason. To try only those pages again, without a whole
new crawl:

1. Wait until the run is completed or failed.
2. On **Runs**, the run's row then shows **Re-collect missed pages** (only when the
   run missed pages). Press it.
3. The answer names the new run and how many pages by reason. It takes the pages
   that failed to load, were blocked, missed a required field, matched no template,
   or were stopped; pages that do not exist (404) are not tried again.

The new run uses the first run's settings in `url_list` mode, the site's mappings
active now (a repair made meanwhile counts) and the profile's current proxies and
solver. It has **its own result**: when it ends, a separate Scraper events job stores
it like any run, so the first run's file stays as it was. **Run detail** links both
ways: the new run says which run it collects again, the first run lists its
re-collect runs. A double press within ten minutes starts one run, not two.

In a workflow, the step **Re-collect missed pages** (`recollect_run`) does the same
with a run id; it can also take other reasons, such as `not_found`. A Test Run does not skip it: it really starts a
run.

## Disk use and clean-up

The service keeps everything on its one volume (`/data`). It removes old items by
itself; the ages are its settings `WSS_RETENTION_*`:

- **Working files** — the pages each run loaded, kept for re-extraction. Removed 7
  days after the run ends.
- **Result files** — removed 14 days after the run ends. Flow Steward's copy stays
  with the Scraper events job.
- **Run records** — the run, its records and events. Removed 30 days after the run
  ends; the run then leaves the Runs page.
- **Samples and screenshots** — pages fetched to build mappings, and screenshots of
  protection checks. Removed after 24 hours.
- **Database** — never removed. It grows and shrinks only when compacted.
- **Browser identities** — never removed.
- **The container log** — kept by Docker, not by the service. On a Linux server
  Docker's default log grows without end, so start the container with the log
  options below.

The service's `docker run` without Compose (from its README):

```
docker run -d --name web-scraper --restart unless-stopped \
  --shm-size 1g --memory 3g --stop-timeout 90 \
  --log-driver json-file --log-opt max-size=20m --log-opt max-file=5 \
  -e WSS_API_TOKEN -e WSS_SECRETS_KEY \
  -v web-scraper-data:/data -p 127.0.0.1:8080:8080 \
  web-scraper-service:server
```

`--log-opt max-size=20m --log-opt max-file=5` keeps at most 100 MB of log. A log
option takes effect only when the container is created again (`docker rm`, then
`docker run`).

**The Service page** shows by itself, for the profile chosen at the top: the size of each
part, runs by state, the automatic clean-up ages, free disk space and the last
clean-ups. To free space now:

1. Set the ages. The form starts with the service's own ages; lower ones remove more,
   0 removes every finished run's part, an empty field leaves that part.
2. Press **Clean up - dry run**. Nothing is removed; the answer says what would go
   and how much space it frees.
3. Tick **Yes, remove these files** and press **Clean up now**.

A clean-up never touches a run that is not finished. It keeps every result Flow
Steward has not fetched yet unless you tick **Include results Flow Steward has not
fetched**. **Compact the database** shrinks the database file only when no run is
active; otherwise the answer says it was skipped, and you can press again later.

**Every week, by itself.** Import `service_maintenance.bundle.yaml` (setup step 5),
map its profile and enable it. Then open the workflow, tab **Schedules**, **New
schedule**: Repeat **Weekly**, a day (for example Sunday) and a time when no crawl
runs, your timezone, **Enabled**, **Create schedule**. Each run uses the service's own
ages, keeps results not fetched yet, and compacts the database. A Test Run really
cleans up; run it with **Dry run only** first.

## Troubleshooting

- **The site's protection check was not passed** (Fetch samples) — read the
  paragraph under *What happened*: which steps ran, which were off.
  - Leave **Browser OS** empty (host); a browser claiming Windows or macOS is
    usually refused.
  - Switch on **Allow the checkbox click** (the service needs
    `WSS_ALLOW_CHALLENGE_CLICK=1`).
  - Use another proxy tier, best a residential proxy with sticky sessions.
  - Allow the solver if the service allows it, or run again later.
  - Switch on **Debug mode** for a screenshot; fetch it with
    `GET <service URL>/v1/screenshots/<id>` and the service token.
- **No target page could be sampled** — read what the service tried (the lines after
  the first sentence: discovery source, URL groups probed, pages loaded, the
  suggestion). Usually: set **Product page selector**, **Extract only URLs matching**, or
  **Discovery** `crawl` with a category page as Site URL.
- **Mapping fields are empty** (summary *Warning: template …: price, currency filled
  on 0 of 4 matched samples*) — the samples may not be product pages, or the sources
  are wrong. Set **Product page selector**, add the fields to **Required fields**, run
  again with **Build a new mapping**.
- **A stored mapping is for the wrong pages** (blog, about, category) — set **Product
  page selector** (and **Never these URLs**) and run with **Build a new mapping** on.
- **One record per product although it has variants** — look at **Fetch samples**,
  output `variant_summary`. Empty: the pages carry no variant data in their code
  (clicking through options is not supported). Not empty: run again with **Build a new
  mapping**; the mapping test fails a template without variants on such pages
  (*… carry variants … but the mapping has no variants block*).
- **A variant field is empty** (summary *Warning: template …: variant field markings
  filled on 2/6 variants*) — run again with **Build a new mapping**; the AI gets the
  warning with the samples' variant data.
- **The crawl leaves the category** — set **Stay inside the start page's section**
  (start on the category) or **Where to walk**; exclude languages and filters with
  **Never these URLs**.
- **Thumbnails instead of full-size images** — run again with **Build a new
  mapping**; mention "all images in full size" in **What to extract**.
- **The run stopped: site unavailable** — the site gave server errors or timeouts
  repeatedly; run again later, or with fewer **Pages at once** and longer pauses.
- **Fewer records than last time** (the comparison's warning) — read whether fewer
  target pages were found (the site changed) or found pages were not collected (see
  **Missed pages** on Run detail and the protection).
- **A run produced no records** — Scrape a site ends *No records* (an older run:
  Scraper events ends *Event handled*) without a file; `run_summary` gives the reason
  (stopped by hand, blocked, nothing matched). Run detail, table **Missed pages**,
  lists the pages and why.
- **Scrape a site keeps waiting** (*Waiting for event*) although the run ended on
  Runs — Scraper events did not take the event: check its webhook (*Scraper events
  never runs* below) and that Scraper events is linked to the same project. A
  resent event (Run detail, **Send again**) wakes it. After 3 days Scrape a site
  reads the run anyway and gets the files of a completed run.
- **Scrape a site ended "Skipped: site busy"** — a run of the same site was still going
  on (the reason names it). Expected for overlapping schedules; set **When the site is
  busy** to `queue` to wait for it instead. A copy of Scrape a site imported before
  0.1.24 fails here with *site_busy* and the same sentence: import it again.
- **The crawl goes on after the job was cancelled** — expected: a cancel does not
  reach the scraper. Press **Stop** on the run's row on **Runs**.
- **Pages time out on a slow site** (*not loaded*, timeouts in the errors) — set
  **Page timeout (s)** to 120 or more, or **Politeness** `gentle`.
- **The mapping test fails with *price_mismatch*** — the mapping reads another amount
  than the price the page states for programs (often a price split into tags, or a
  price of another product). **With *price_is_old_price*** — on a sale page it reads the
  struck-through old price. The AI gets the reason and corrects the mapping; if a
  stored mapping predates this, run with **Build a new mapping**.
- **Scrape a site failed: "The scraper run … did not complete"** — the run failed (the
  message is its summary) or was still going on after 3 days. The crawl is not
  stopped by this; see Run detail.
- **`price_status` is `missing` but the page shows a price**, or **one record per
  product although it has variants** — the mapping misses it. A new mapping test
  fails such a mapping (*price_not_extracted*, *variants_not_used*) and the AI gets
  the reason; during a crawl such pages are kept (Missed pages, *What went wrong*)
  and a repair runs. Run again with **Build a new mapping** for a stored mapping
  that predates this.
- **The price is empty with `price_status` `on_request`** — expected: the page asks
  you to contact the shop for a price.
- **A Test Run ended "Test run: … no crawl was started"** — expected; use grey **Run
  workflow** with the acknowledgement to crawl.
- **`secret_configuration_unapproved`** — press **Approve use of secrets** on the
  profile (after creating, after every change, after every update).
- **`network_blocked`** — the service is on a private address: set
  `FS_ALLOW_PRIVATE_REMOTE_URLS=1` on the platform.
- **`service_unauthorized`** — the profile's token is not the service's
  `WSS_API_TOKEN`.
- **Scraper events never runs** — the profile's webhook URL must be the platform's
  internal address; *Invalid HMAC signature* means the signing secret is not the
  webhook's credential (rotate it, paste, save, approve).
- **Processing failed** (Scraper events) — fix the cause, then open the run on
  **Runs**; on Run detail, table **Events**, press **Send again** on that event.
- **A run is `waiting_for_credentials`** — Run detail → **Resume run**.
- **The run waits: another run of this site is running** — the service runs one crawl
  per site at a time, so the site is not loaded twice as hard. The run starts by itself
  when the other one ends; stop one of them on **Runs** if you do not need both.
- **The run stopped: stalled** — nothing moved for 15 minutes (no page finished, no
  record written), and nothing again for another 15 minutes, so the service stopped
  it. *Why it stopped / what to do* on Run detail says what was stuck. Run it again;
  if it stalls again, check that the site answers and that the proxies work
  (Profiles, **Test proxy tiers**).
- **The crawl misses products behind filters** — switch off `facet_params` (see
  *Links the scraper skips*).
- **The service's disk is full** — Service page, then **Clean up -
  dry run** and **Clean up now** (see *Disk use and clean-up*). Check the container's
  log options too.
- **`service_route_missing` on the Service page** — the service is older than the
  storage routes; update its image.
- **The Scraper events job's Result panel is empty** — the workflow was imported
  before 0.1.17; import the template again.
- **No `result_csv` in the Result panel** — the service is older than 2026-11-14, or
  Scraper events was imported before 0.1.18; update the service or import the
  template again.
- **Pages were missed** (fetch failed, blocked, field missing) — Run detail, **Collect
  missed pages again** (see *Collect the missed pages again*).

## Updating the extension

Install the new zip over the old one and **Approve use of secrets** on every profile
again. Pages, actions and the AI step change at once. Imported workflows are your
project's copies and do not change: when the release notes name a template change,
import the template again as a new copy (for Scraper events also create its webhook
again and put the new URL and credential into the profile).

0.1.17 changes **Scraper events** (its Result panel shows the result file and the
first rows) and adds **Service maintenance**: import both. 0.1.18 changes
**Scraper events** again (the CSV beside the JSON Lines file): import it again. 0.1.18
needs Flow Steward 0.9.17 or later. 0.1.20 adds two run form fields (**Links not to
skip**, **Trap budget**) and the analysis's `link_rules`: import **Scrape a site**
again to get them. 0.1.21 needs Flow Steward 0.9.18 or later; the templates did not
change, so the copies you imported keep working. 0.1.22 brings the extension's own read-only
workflows with hooks; your copies keep working. 0.1.23 makes **Scrape a site** wait for
its run and return the files (needs Flow Steward 0.9.18 with *Wait for an event*); the
extension's own workflows change by themselves, and copies need a new import of **Scrape
a site** and **Scraper events**. An older copy of Scrape a site keeps working: its runs
get their files in Scraper events as before. 0.1.24 lets your own workflows call
**Scrape a site** with Invoke, adds **Politeness**, **Page timeout** and **When the
site is busy** to the run form, ends a start on a busy site *Skipped* instead of
failed, and shows sale prices (`regular_price`, `on_sale`); it needs the Web Scraper
service image `ghcr.io/flow-steward/web-scraper-service:0.2.0` for the new fields. The
extension's own workflows change by themselves; a copy of **Scrape a site** needs a new
import for these. 0.1.25 changes texts only: a
queued start says it is queued, Run detail compares a run with the previous one only
after it ends, and a failed *Fetch samples* gives the service's explanation once. The
templates did not change, so the copies you imported keep working. 0.1.26 changes links
and texts only: the support and website links point to the extension's repository, and
this guide names the published service image
`ghcr.io/flow-steward/web-scraper-service:0.2.0`. The templates did not change, so the
copies you imported keep working.

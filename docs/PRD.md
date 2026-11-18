# PRD: Trading Environment Workflow Tracker (WFT)

| | |
|---|---|
| **Status** | Draft v0.1 |
| **Date** | 2026-11-18 |
| **Owner** | James Deighton |
| **Maintainers** | Team of 3 (named in [§12](#12-ownership--support)) |
| **Release covered** | Release 1 (MVP) — TT only |

---

## 1. Summary

WFT is a desktop-hosted web tool that guides operations users through the setup and maintenance of the electronic trading environment (clients, accounts, users, limits) and tracks each request from creation to completion. It replaces today's paper-based tracking with a structured data-capture form and a checklist, keeps an append-only history of every action, and records how long each stage takes (including time spent waiting on third parties).

WFT is a **helper tool, not a system of record**. Trading systems (e.g. TT) and approval platforms (e.g. KRM22) remain authoritative for their own data.

## 2. Problem & goals

### Problem
- Setup and maintenance requests are tracked on paper. There is no shared view of what is in progress, who owns it, what it is waiting on, or how long things take.
- Steps differ by trading system and are easy to miss.
- There is no data on cycle times to set expectations with requesters.

### Goals
1. Guide users through each workflow with structured data capture and a step checklist, so steps are not missed.
2. Give the team a shared, live view of in-progress and completed requests.
3. Record timing for every step, with waiting time separated from working time and attributed to who/what is being waited on.
4. Provide a definition format that lets the maintainers add and change workflows and trading-system schemas over time without code changes to the engine.
5. Lay the foundation to automate individual steps via APIs (TT, KRM22) in later releases.

### Non-goals
- Being the system of record for clients, accounts, users or limits.
- Replacing the KRM22 approval workflow or its audit trail.
- Handling urgent / out-of-hours changes or unusual one-off setups (these stay on the existing manual process; WFT only lets users log them — see [§6.8](#68-off-process-records)).
- Integration with KYC / CRM platforms.

## 3. Users & roles

- Up to **10 users**, all in the operations team, each running their own instance of WFT.
- Expected volume: **< 30 requests per day** in total.
- Only these users create requests. Requests arriving by email/phone/form are keyed in by a user, with the original attached as evidence.

| Role | Who | Permissions |
|---|---|---|
| **Operator** | All users | Create, amend, hold, resume, cancel requests; claim and complete steps; mark steps *completed outside tool* (notes required); raise and resolve impact flags (including *not required*, notes required); create off-process records; export. |
| **Admin** | 1–2 maintainers | Everything an Operator can do, plus: publish workflow definitions, edit configuration (picklists, off-process reasons), add ad-hoc steps to an individual request. |

Identity is the Windows username of the person running the instance. Users, display names and the admin role are listed in `definitions/config/roles.yaml`, published to the share with the definitions; only published admins may publish (until an environment has any admins, anyone may, to bootstrap). Roles are a guardrail within a trusted team, not a security boundary; access to the shared drive (NTFS permissions) is the real control. Every action is recorded with the actor, so accountability comes from the audit trail.

## 4. Release 1 scope

### In scope
- Trading system: **TT** only.
- Workflows: **TT Limit Change** (via KRM22) and **TT Add User**.
- Off-process records.
- Full request lifecycle (Draft, Active, OnHold, Cancelling, Cancelled, Rejected, Completed) and impact flags.
- Read-only TT REST API usage: verification steps and picklists.
- KRM22 CSV generation for manual upload.
- Dashboard: My tasks, Open requests, Waiting-on board, Open impact flags, simple Completed-over-time chart.
- Desktop (Windows toast) notifications and in-app badge.
- Per-request record export (HTML/PDF); Excel export of request list and step timings.
- Prod and UAT environments.
- Workflow engine support for sub-workflows and `for_each` fan-out (exercised by a test workflow only).
- Manually-run validation scripts and a publish script for definitions.
- How-to documentation for maintainers.

### Out of scope for Release 1 (see [§13 Roadmap](#13-roadmap))
- Business parent/child workflows (e.g. client setup → accounts → limits) and rollback/compensation workflows.
- Trading systems other than TT; file-drop data imports.
- KRM22 API integration; any write operations to TT.
- ETA prediction and cycle-time analytics views.
- Business-hours calendars (all timing in UTC).
- CI pipeline (validation runs manually).
- Packaged distribution (pip package from Artifactory, `.exe`).
- Email notifications.
- Migrating in-flight paper requests (only new requests go into WFT from go-live).

## 5. Key concepts

| Term | Meaning |
|---|---|
| **Workflow definition** | A versioned YAML description of the steps for a type of request (e.g. `tt_limit_change` v1). |
| **Request** | One running instance of a workflow definition, pinned to the definition version it started on. |
| **Step** | A unit of work within a request (form, checklist, external request, verification, …). |
| **Sub-workflow** | A step that starts one or more child requests and waits for them. |
| **Event** | An immutable record of something that happened (request created, step completed, flag raised…). Current state is derived by replaying events. |
| **Impact flag** | A to-do raised when a lifecycle change (hold / amend / cancel) affects work already done or in progress outside WFT, e.g. "withdraw KRM22 request". |
| **Off-process record** | A log entry for a setup that was done outside WFT, with reason and notes. |
| **Wait reason** | What a step is waiting on: `client`, `internal`, `krm22`, `trading_system`, `other`. |

## 6. Functional requirements

### 6.1 Requests

- **FR-1.1** A user can create a request by choosing a workflow and completing its initial data-capture form. The request starts in **Draft** and becomes **Active** on submit.
- **FR-1.2** Each request has:
  - an internal unique ID (generated, collision-free, e.g. ULID);
  - a human-readable reference allocated on submit, per workflow type and year: `LIM-2026-0042`, `USR-2026-0017`. If allocation fails (e.g. lock timeout) the request is still saved and the readable reference is allocated later.
- **FR-1.3** Each request has an **owner** (defaults to the creator). Ownership can be reassigned by any Operator; reassignment is recorded.
- **FR-1.4** Any Operator can **claim** a ready step, or have one assigned to them. The owner remains accountable for the request overall.
- **FR-1.5** Requests can carry free-text notes and attachments at request and step level.
- **FR-1.6** A request can be linked to other requests (e.g. "follow-up of", "resubmission of") and to off-process records.

### 6.2 Request lifecycle

```
Draft ──submit──▶ Active ─────all steps done─────▶ Completed
                  │  ▲   │
             hold │  │   │ external rejection ──▶ Rejected  (or amend & resubmit)
                  ▼  │resume
                OnHold
Active / OnHold ──cancel──▶ Cancelling ──all impact flags resolved──▶ Cancelled
```

- **FR-2.1 Hold**: moves an Active request to OnHold with a mandatory reason. Step timers pause; hold duration is recorded separately and excluded from step timings. Holding a parent also holds its active child requests; each affected in-progress child raises an impact flag.
- **FR-2.2 Resume**: moves an OnHold request back to Active.
- **FR-2.3 Amend**: changes request input data while Active (or OnHold). See [§6.4](#64-amendments--impact-analysis).
- **FR-2.4 Cancel**: moves an Active or OnHold request to **Cancelling** with a mandatory reason. Impact flags are raised for affected steps and child requests. The request moves to **Cancelled** only when all its impact flags are resolved.
- **FR-2.5 Reject**: when an external approval is rejected, the user chooses *amend and resubmit* (loop back, history retained) or *close as Rejected*.
- **FR-2.6** Completed, Cancelled and Rejected are terminal. Changes after completion are made by creating a new **linked follow-up request**, not by reopening.
- **FR-2.7** A request cannot reach a terminal state while it has open impact flags.

### 6.3 Steps

- **FR-3.1 Step states**: `pending` (dependencies not met) → `ready` → `in_progress` ⇄ `waiting` (with wait reason) → `done` | `skipped` | `done_outside_tool`.
- **FR-3.2 Step types** (engine must support all in Release 1):

| Type | Behaviour |
|---|---|
| `form` | Data capture rendered from a JSON Schema; validated on submit. |
| `manual` | Checklist of items to tick off, with optional instructions (Markdown). |
| `external_request` | Raise a request in another platform; record its reference, optional link, generated artefacts (e.g. CSV); enters `waiting` with a wait reason until closed. |
| `outcome` | Record the result of an external process from a defined set of outcomes; each outcome routes to the next step or lifecycle action. |
| `verify` | Compare expected values against the trading system (via adapter); show differences; user accepts or raises an issue. |
| `decision` | Branch based on captured data. |
| `subworkflow` | Start one or more child requests (`for_each` over a list) and wait for all to finish (join). |
| `approval` | Generic internal sign-off (supported by engine; not used by R1 workflows). |

- **FR-3.3** Any step can be marked **completed outside tool** with mandatory notes.
- **FR-3.4** Any step can be put into `waiting` with a wait reason and optional note; resuming returns it to `in_progress`.
- **FR-3.5** Admins can add an **ad-hoc step** (manual checklist) to an individual request, with notes. This is the mechanism for one-off fixes; in-flight requests are otherwise never migrated to a new definition version.

### 6.4 Amendments & impact analysis

- **FR-4.1** Each step declares which input fields it consumes (`consumes: [account.*, limits.max_position]`).
- **FR-4.2** On amend, WFT computes the changed fields and:
  - steps not yet started simply use the new data;
  - completed or in-progress steps that consumed a changed field raise an impact flag ("needs re-check");
  - where a step defines it, the workflow is routed back to that step (e.g. KRM22 CSV step).
- **FR-4.3** Amendments are recorded with before/after values.
- **FR-4.4** What can be amended: the request's inputs (its initial capture form data), while Active or OnHold, validated like a submit. The user previews the changed fields, the steps that will be flagged and any steps that will be reopened before applying.
- **FR-4.5** Matching: a `consumes` pattern covers a changed field when either is a prefix of the other (`*` matches any part), so `limits.*` covers `limits.max` and a change to the whole `limits` group. Only steps already started, waiting or done are flagged; the flag uses `on_amend.flag` or "Needs re-check: <fields> changed".
- **FR-4.6** `on_amend.route_to` must name a step the declaring step depends on (checked by the validator). The target is reopened as ready and every step after it returns to pending, with ticks cleared and earlier form data kept as a draft.

### 6.5 Impact flags

- **FR-5.1** An impact flag records: the request, the affected step or child request, the trigger (hold / amend / cancel), a description of the required action, and who raised it and when.
- **FR-5.2** A flag is resolved as one of: **actioned** (notes; optional link to a follow-up request), **not required** (reason mandatory), or **deferred** (notes; remains visible on the Flags page but does not block terminal states). A deferred flag can later be resolved as actioned or not required. Resolutions sit outside the per-request sequence; if two people resolve the same flag at once, the first wins and the other sees a conflict.
- **FR-5.2a Which steps are flagged:** on **cancel**, every step that was started, waiting or done, except data-capture `form` steps without an `on_cancel`; on **hold**, only in-flight (started or waiting) steps that declare `on_hold`. Flag and compensation text may use expressions (e.g. `{{ inputs.account }}`).
- **FR-5.3** Step definitions may include an optional `compensation` block (a checklist or a workflow to start). In Release 1 no workflows define compensation workflows; flags display the step's compensation checklist text if present, otherwise a generic "manual rollback may be required".

### 6.6 Release 1 workflows

#### 6.6.1 TT Limit Change (`tt_limit_change`)

| # | Step | Type | Notes |
|---|---|---|---|
| 1 | Capture limit change | `form` | Account (picklist from TT API), limit type(s), current values (pre-filled from TT API where available), requested values, requester, justification, attachments. |
| 2 | Generate KRM22 CSV | `manual` (auto artefact) | WFT generates one CSV per request in the KRM22 upload format, validates it against the KRM22 column spec and attaches it to the request. |
| 3 | Raise KRM22 request | `external_request` | User uploads CSV to KRM22, records KRM22 reference. Step waits with reason `krm22`. |
| 4 | Record KRM22 outcome | `outcome` | Outcomes: **Approved**, **Approved with changes**, **Rejected**, **Withdrawn**. Captures *applied* limit values (defaults to requested; editable). |
| 5 | Verify in TT | `verify` | Read limits via TT API and compare with applied values. |

Outcome routing:
- **Approved** → step 5 → Completed.
- **Approved with changes** → step 5 (against the applied values) → Completed, with a "differs from request" marker.
- **Rejected** → user chooses *amend and resubmit* (amend → back to step 2) or *close as Rejected*.
- **Withdrawn** → only valid as part of a cancel.

Lifecycle effects:
- Cancel after step 3 is done/in progress → impact flag "withdraw request in KRM22".
- Amend after step 3 → impact flag "KRM22 request no longer matches"; route back to step 2.
- Hold while KRM22 request is pending → reminder banner that KRM22 may still approve; no flag.

#### 6.6.2 TT Add User (`tt_add_user`)

| # | Step | Type | Notes |
|---|---|---|---|
| 1 | Capture user details | `form` | Identity, email, company, accounts and access level (trade/view) per account, exchange/trader IDs per market, licences/apps required. Schema per TT data dictionary. |
| 2 | Create user in TT | `manual` | Checklist; captures TT user ID. |
| 3 | Grant account access | `manual` | Checklist generated from the captured account list. |
| 4 | Configure exchange IDs | `manual` | Checklist generated from captured markets. |
| 5 | Licences / app access | `manual` | Checklist (if handled by another team, step waits with reason `internal`). |
| 6 | Verify in TT | `verify` | Read user and account permissions via TT API; compare with captured data. |
| 7 | Send welcome email | `manual` | Checklist item; WFT does not send email. |

Lifecycle effects: cancel after step 2 → impact flag "disable/remove user in TT"; amend of accounts after step 3 → flag "re-check account access"; similar for steps 4–5.

> **Action:** the exact step list and fields are to be confirmed against the current paper form before the definition is written.

User-level limits are **not** part of this workflow in Release 1 (would require a sub-workflow).

### 6.7 Trading-system integration (TT)

- **FR-7.1** Each trading system has an **adapter** in the codebase providing: JSON Schemas for its entities (user, account, limits, …), checklist templates, read operations (lookups, verification) and — later — write operations.
- **FR-7.2** Release 1 uses the TT REST API **read-only** for:
  - picklists (accounts, existing users, exchanges), cached locally with a manual refresh and a configurable TTL;
  - pre-filling current values;
  - `verify` steps.
- **FR-7.3** TT API credentials are **per user**, held in the user's environment file (see [§7.5](#75-configuration--environments)), never on the share or in Git.
- **FR-7.4** If the TT API is unavailable, `verify` steps can be completed manually with notes (*completed outside tool*); picklists fall back to free text with a warning.
- **FR-7.4a How it appears in definitions:** a form field with `"x-picklist": "tt.accounts"` (or `tt.users`) offers suggestions from TT and still accepts typed values; a form step's `prefill: {operation, key, target}` adds a "Fill current values from TT" button; a `verify` step names `adapter` (e.g. `tt.account_limits`), `key` and `expected` (expressions). Checking records the result (match / mismatch / unavailable, with expected vs actual) on the step; the user then accepts (a reason is required if there are differences), records an issue (the step waits on the trading system), or completes it by hand with notes.
- **FR-7.4b Settings:** `WFT_TT_BASE_URL`, `WFT_TT_ENVIRONMENT`, `WFT_TT_APP_KEY`, `WFT_TT_APP_SECRET` (and optional `WFT_TT_APP_NAME`, `WFT_TT_COMPANY`, `WFT_TT_CACHE_MINUTES`) in the user's env file; `WFT_TT_BASE_URL=fake:` uses sample data for trials. The TT environment name is checked against the env profile. Picklists are cached in the user's profile, never on the share.
- **FR-7.5** The adapter interface must also allow, in later releases, a **file-drop** data source (manually exported files placed in `imports\<system>\inbox\`), with the export date shown against any comparison.

### 6.8 KRM22 CSV

- **FR-8.1** The KRM22 upload format is defined as a mapping file in the repo (column spec, types, validation rules), owned by the TT schema owner.
- **FR-8.2** One CSV per request. File name includes the WFT reference. The generated CSV is attached to the request and downloadable.
- **FR-8.3** Generation fails with a clear message if required fields are missing or invalid.
- **FR-8.4** The format lives in `definitions/mappings/krm22/tt_limit_upload.mapping.yaml`: the fixed first line `Header,IsvInstance,IsvTeType,IsvTeName`, the 22 column headers, and one `Data` row per product in the request's `products` list (exchange, family, type, product, action add/change/remove, max order quantity, max position). Adds and changes write `100`/`TRUE`; removals write `Not Set`. Files are UTF-8 without BOM, CRLF, quoted only where needed, named `<reference>_KRM22.csv`.
- **FR-8.5** Any step with `artefacts` offers "Generate"; the file is attached to that step (write-once) and the step can't be completed until a file has been generated since it was last (re)opened, so a resubmission or amendment forces a fresh CSV.
- **FR-8.6** Recording an `external_request` as raised (reference, optional link) completes it; the following `outcome` step then waits automatically with the request's wait reason (e.g. KRM22), so the wait shows on the Waiting-on board until the outcome is recorded. Outcomes: `next` continues (other outcomes' targets are skipped); `amend_and_resubmit` reopens the step named by the external request's `on_amend.route_to`; `close_rejected` ends the request as **Rejected** (open flags must be resolved first); `only_during: cancel` outcomes (e.g. *withdrawn*) are only offered while Cancelling and settle the external request's withdrawal flag; `capture` stores the values actually applied (defaulting to what was requested); `marker` is shown on the request. Outcomes with choices or a marker need a note.

### 6.9 Off-process records

- **FR-9.1** Users can create an **off-process record**: trading system, what was set up/changed, date performed, reason category, mandatory notes, optional attachments, optional link to a WFT request.
- **FR-9.2** Reason categories (admin-configurable): *Urgent / out of hours*, *Unusual structure not supported*, *Tool missing workflow or step*, *Tool or share unavailable*, *Trading-system issue*, *Other (notes required)*.
- **FR-9.3** Off-process records appear in search and exports. (Dashboard view by reason is a later release.)
- **FR-9.4** Records are stored like requests (an event in the recorder's journal, so they are shared and survive cache rebuilds) but are not workflow requests. Each gets a readable reference `OFF-YYYY-NNNN`, can have further notes and attachments, and can link to a request by reference; the linked request's page lists it and offers "Log an off-process setup for this request".
- **FR-9.5** Reason categories live in `definitions/config/off_process_reasons.yaml`, published with the definitions (admins). Without a published file the six seeded categories are used. Records keep the reason id, so ids must not be reused for a different meaning.

### 6.10 Dashboard & views

Release 1:
- **My tasks** — steps I own, have claimed, or can claim; requests I own with open flags.
- **Open requests** — all non-terminal requests with reference, workflow, owner, status, current step, age, time in current step; filter and sort.
- **Waiting-on board** — requests with waiting steps grouped by wait reason (client / internal / KRM22 / trading system / other), with time waiting.
- **Open impact flags** — all unresolved flags.
- **Completed over time** — simple count per day/week by workflow.
- **Request detail** — data, steps with state and timings, full event history, attachments, flags, links.

Later: cycle-time analytics per workflow/step, off-process by reason, ETA.

### 6.11 Notifications

- **FR-11.1** Windows desktop (toast) notifications from the user's running instance when: a step becomes ready on a request I own; a step is assigned to me; a request is reassigned to me; an impact flag is raised on my request; a request I own changes lifecycle state due to another user.
- **FR-11.2** Unread-count badge in the browser tab title.
- **FR-11.3** Notifications only fire while the user's instance is running; on startup the user sees what changed since they were last online.
- **FR-11.4** Only changes made by other people notify; automatic knock-ons (e.g. children cancelled with their parent) don't notify separately. More than five at once give one summary toast. Notices are kept in the user's local cache with read/unread; opening a request marks its notices read.
- **FR-11.5** `WFT_NOTIFICATIONS=windows` (default on Windows) shows native toasts via PowerShell's built-in toast API, with no extra packages; `browser` uses the browser's own notifications while a WFT tab is open (the user enables them on the Notifications page); `off` shows only the in-app count. The time the user was last online is kept in their profile, so deleting the cache doesn't replay old changes as new.

### 6.12 Exports

- **FR-12.1** **Request record**: per-request HTML (printable to PDF) showing all data, steps, every event with actor and UTC timestamp, notes, flags and attachment list.
- **FR-12.2** **Excel export** of the current filtered request list, and of step timings (one row per step instance: request, workflow, version, step, state transitions, work time, wait time by reason, hold time).

### 6.13 Attachments

- **FR-13.1** Any file type, max **25 MB** per file.
- **FR-13.2** Stored on the share under the request folder, write-once. Journal records file name, size, SHA-256, uploader and timestamp. Attachments can be added at any stage (including after completion) and optionally linked to a step; they are annotations, so they never conflict with other actions. The stored name is shortened and made file-system safe to keep UNC paths under Windows' limits; downloads use the original name.

### 6.14 Administration

- Publish workflow definitions (via script, see [§9](#9-maintenance--change-process)).
- Edit configuration: off-process reasons, wait reasons, picklist overrides.
- Add ad-hoc steps to a request.
- View instance status (which users' journals have been active recently, app versions in use).

## 7. Architecture & non-functional requirements

### 7.1 Overview

Each user runs a local Python backend that serves the web UI on localhost. All shared state lives as files on a shared drive. There is no shared server or database.

```
┌──────────────────── Browser (localhost, session token) ───────────────────┐
│ Dashboard │ My tasks │ Request detail / step forms │ Admin                │
└───────────────────────────────────▲────────────────────────────────────────┘
                           FastAPI (Jinja2 + HTMX)
┌───────────────────────────────────┴────────────────────────────────────────┐
│ Application services: create, submit, amend, hold, cancel, claim, complete │
├────────────────────────────────────────────────────────────────────────────┤
│ Workflow engine (pure Python, no I/O): definition + events → state        │
│ Step handlers: form │ manual │ external_request │ outcome │ verify │ …     │
│ Adapters: TT (schemas, checklists, read API) │ KRM22 (CSV mapping)        │
├────────────────────────────────────────────────────────────────────────────┤
│ Projections: local SQLite read model, timings, notifications              │
├────────────────────────────────────────────────────────────────────────────┤
│ Repository: journal writer/reader │ lock manager │ attachment store │ poller│
└────────────────────────────────────────────────────────────────────────────┘
                                   │ SMB
                       \\share\wft\{prod|uat}\
```

### 7.2 Technology

- Python (version per the approved software process; TBD), libraries from the approved Artifactory repository, pinned via `requirements.lock`.
- **FastAPI + Uvicorn** backend; **Jinja2 + HTMX** (+ Alpine.js) front end, with all JS/CSS assets vendored into the repo (no npm/Node).
- Forms rendered server-side from JSON Schema.
- Local **SQLite** as a disposable read model (rebuildable from journals at any time).
- A small charting library served locally for the dashboard.
- `pytest` for tests.

### 7.3 Storage & concurrency

**Layout**

```
\\share\wft\<env>\
  definitions\<version>\           # published workflow definitions, schemas, config
  journals\YYYY-MM\<username>.jsonl
  archive\YYYY\...                 # journals older than 12 months
  attachments\<request-id>\<first 16 hex of sha256>_<safe filename>   # write-once; full hash in the journal
  locks\                           # short-lived lock files (reference allocation)
  meta\references\<PREFIX>-<YYYY>.txt  # last readable reference number issued
  instance-locks\<username>.lock   # one running instance per user
  meta\min_app_version
  meta\environment                # "prod" or "uat"; must match the running env
  definitions\config\roles.yaml    # team: usernames, display names, admin role
```

**Rules**
- **NFR-3.1 Single writer per file.** Each user's instance appends only to its own monthly journal. An instance lock (`O_CREAT|O_EXCL` lock file with host, PID, start time; stale after a timeout with heartbeat) prevents the same user running two instances.
- **NFR-3.2 Events are JSON lines**, each with: event ID, request ID, per-request sequence number, the id of the event it was decided on top of (`prev`), event type, actor, host, app version, definition version, UTC timestamp, payload, schema version.
- **NFR-3.3 Readers poll** all journal files (size/mtime check) every 5–15 s and read only new bytes; a trailing partial line is ignored until complete. File-system watch notifications are not relied on.
- **NFR-3.4 State is derived by replay**, ordered per request by sequence number, not wall-clock time. Conflicting events (same parent sequence) are resolved deterministically (earliest UTC timestamp, then event ID); the losing action, and any later action built on it, is surfaced to its author as a conflict to retry. Dismissed conflicts are remembered in the user's profile so a cache rebuild doesn't resurface them.
- **NFR-3.5 Locks** are used only for readable-reference allocation and instance exclusivity. Lock files contain owner metadata and expire.
- **NFR-3.5a Annotations:** readable references, notes, owner changes and step claims/assignments are recorded outside the per-request sequence, so they never conflict with other users' actions. Owner changes and assignments: the latest wins. Claims are exclusive: of two simultaneous claims the earliest wins and the other claimant sees a conflict.
- **NFR-3.6 Share unavailable** → instance switches to **read-only mode** with a prominent banner, serving the last cached state; writes are blocked (no offline queueing). Users fall back to the manual process.
- **NFR-3.7** All storage access goes through a `Repository` interface so the backend can be swapped (e.g. for a database) later.
- **NFR-3.8 Retention:** all data kept indefinitely; journals older than 12 months moved to `archive\YYYY\` by an admin script and still read for history.

### 7.4 Security

- Bind to `127.0.0.1` only; port chosen at startup (configured preferred port, fallback to a free port).
- A random **session token** is generated per launch; the launcher opens the browser with it; it is set as a cookie and required on every request. Protects against other users on shared hosts (Citrix/VDI) reaching the port.
- Validate the `Host` header (DNS-rebinding protection) and use CSRF protection on state-changing requests.
- Identity = Windows username. Admin role determined by a list in published configuration.
- Secrets (TT API keys, later KRM22) only in per-user env files outside the repo; never logged, never written to the share.
- Data at rest is not encrypted (accepted).

### 7.5 Configuration & environments

- Startup: `wft.bat [--env prod|uat]` (default `prod`).
- Loads `%USERPROFILE%\.wft\<env>.env` containing: share root, TT API base URL, TT API key/secret, preferred port, (later) KRM22 settings.
- `.env.example` committed to the repo listing all keys.
- **Safety check:** the app refuses to start if the env file mixes environments (e.g. UAT profile pointing at a prod share or prod TT URL). Generic share/URL patterns per environment are in the public repo config (`config/environments.yaml`); the firm's real patterns (share paths, TT and KRM22 URLs) go in a local `environments.yaml` beside the user's `.env` files, merged over the defaults per environment and per key, and never committed. An invalid local file stops start-up with a message naming it. The share must also contain `meta\environment` naming its environment. Unknown env-file keys are rejected.
- UAT shows a persistent, highly visible banner and distinct colour scheme.

### 7.6 Deployment & updates

- Release 1: users run from a **Git clone** of a release tag.
  - `wft-update.bat`: fetch, check out latest release tag, `pip install -r requirements.lock` from Artifactory.
  - `wft.bat`: start the backend and open the browser.
- On startup the app compares its version with `meta\min_app_version` on the share; if older it refuses to write and tells the user to run `wft-update.bat`.
- Definitions are published to the share independently of app releases (a definition may declare a minimum app version).
- Later: package as a wheel in Artifactory (`pip install`), then possibly a signed `.exe`.

### 7.7 Performance & reliability

- Startup (cache warm) < 5 s; cold rebuild of local cache from one year of journals < 60 s.
- Other users' changes visible within 15 s.
- UI actions respond < 1 s excluding TT API calls.
- No data loss on crash: an event is acknowledged to the user only after it is appended and flushed to the share.
- Local cache is disposable; a "rebuild cache" action exists.

### 7.8 Timing

- All timestamps recorded in **UTC**. No business-hours calendar in Release 1; business-day analytics can be derived later from raw events.
- For each step instance the system can derive: time ready → started, working time, waiting time by reason, hold time, total elapsed.
- A ready step starts with an explicit **Start** or on the first work done on it (ticking an item, saving its form, completing it). A waiting step must be resumed before work continues. The capture form counts as being worked on from the request's creation until it is submitted.
- Request age runs from submission (creation, for a Draft) to completion; "time in step" is how long the current step has been in its current state.

### 7.9 Logging & diagnostics

- Local rotating log file in `%USERPROFILE%\.wft\logs\` (no secrets).
- An admin "diagnostics" view: app version, env, share reachability, last poll, journal sizes, TT API reachability.

## 8. Workflow definition model

Definitions are YAML files in the repo, validated against a JSON Schema for definitions. Illustrative example:

```yaml
id: tt_limit_change
version: 1
title: TT limit change
system: tt
reference_prefix: LIM
min_app_version: 1.0.0

inputs_schema: tt/limit_change.schema.json

steps:
  - id: capture
    type: form
    schema: tt/limit_change.schema.json

  - id: krm22_csv
    type: manual
    depends_on: [capture]
    consumes: [account, limits.*]
    artefacts:
      - generator: krm22.csv
        mapping: krm22/limit_upload.mapping.yaml
    checklist: tt/krm22_csv.md

  - id: krm22_raise
    type: external_request
    depends_on: [krm22_csv]
    consumes: [account, limits.*]
    platform: KRM22
    wait_reason: krm22
    on_amend: { route_to: krm22_csv, flag: "KRM22 request no longer matches – withdraw or update" }
    on_cancel: { flag: "Withdraw request in KRM22" }
    compensation:
      checklist: "Withdraw the KRM22 request {{ steps.krm22_raise.reference }}"

  - id: krm22_outcome
    type: outcome
    depends_on: [krm22_raise]
    outcomes:
      approved:              { next: verify }
      approved_with_changes: { next: verify, capture: applied_limits, marker: differs_from_request }
      rejected:              { choices: [amend_and_resubmit, close_rejected] }
      withdrawn:             { only_during: cancel }

  - id: verify
    type: verify
    depends_on: [krm22_outcome]
    adapter: tt.verify_limits
    expected: "{{ steps.krm22_outcome.applied_limits | default(inputs.limits) }}"
```

Sub-workflow syntax (supported by engine in R1, used in later releases):

```yaml
  - id: accounts
    type: subworkflow
    workflow: tt_add_account
    for_each: "{{ inputs.accounts }}"
    map_inputs: { client_id: "{{ inputs.client_id }}", account: "{{ item }}" }
    depends_on: [create_client]
```

Sub-workflows, decisions and approvals (engine support in Release 1; no business workflow uses them yet):
- **subworkflow:** when the step is reached, the person whose action reached it starts one child request per `for_each` item (or one, without `for_each`), using the latest published version of `workflow`, with inputs from `map_inputs` (`item` is the current element). Children whose inputs are complete are submitted straight away; others are left as Drafts. The step completes when every child is Completed; whoever completes the last child completes it, and the parent's owner catches up on the next poll if that was missed. A cancelled child leaves the step open (start a replacement, or mark it done outside WFT). Holding a parent holds its active children and flags in-progress ones; resuming it resumes the children it held; cancelling it cancels unfinished children and flags children that were worked on or completed.
- **decision:** settles itself when reached: the first branch whose `when` is true, else `default`, else someone chooses. Options not taken are skipped, as are steps that depend only on skipped steps. The validator's dry run tries every option.
- **approval:** Approve (optional notes) or Don't approve (reason required; recorded, and the step stays open). The person who submitted the request can't approve unless `allow_submitter: true`; `approver_role: admin` requires an admin.
- Automatic steps (starting children, joins, passing hold or cancel to children) never show conflict warnings.

Rules:
- Requests are pinned to the definition `id` + `version` they started on.
- Definitions reference schemas, checklists (Markdown) and mappings by relative path: schema refs under `schemas/`, checklist refs under `checklists/`, mapping refs under `mappings/`. A checklist may be an inline list or a Markdown file of `- item` lines.
- Every `form` step needs sample data for the validator's dry run: `samples/<id>.yaml` maps each form step id to sample form data. Form steps with no `depends_on` are the request's initial (capture) forms, filled in while it is a Draft; their data becomes the request's inputs and must match `inputs_schema` when one is set.
- Expressions are a restricted templating subset (no arbitrary code): `{{ path | filter }}` with paths rooted at `inputs`, `steps`, `item`, `request`, the filters `default`, `join`, `upper`, `lower`, `length`, and `==` / `!=` comparisons.
- The format is defined by `wft/schemas/workflow.schema.json`.
- Each version is published to the share as its own immutable folder, `definitions/workflows/<id>/<version>/`, containing the workflow, only the files it references, and a `manifest.json` (file hashes, publisher, git commit). Instances load every published version and pick up new ones while running.

## 9. Maintenance & change process

### 9.1 Repository layout (indicative)

```
wft/                     # application package
  engine/  repository/  web/  adapters/tt/  adapters/krm22/
definitions/
  workflows/             # <id>.yaml, the current version of each workflow
  samples/               # <id>.yaml sample inputs for the dry run
  schemas/tt/            # JSON Schemas
  checklists/tt/         # Markdown
  mappings/krm22/        # CSV mappings
  config/                # off-process reasons, wait reasons, admins, env patterns
scripts/
  validate.py            # schema, references, dependency cycles, expression check, dry-run
  publish_definitions.py # validate then publish to share (admins only)
  archive_journals.py
  new_workflow.py        # scaffold
docs/
  PRD.md
  howto/add-workflow.md
  howto/change-workflow.md
  howto/trading-system-schema.md
tests/
wft.bat  wft-update.bat  requirements.lock  .env.example
```

### 9.2 Process
1. Change made on a branch; pull request reviewed by another maintainer (GitHub).
2. Author runs `scripts/validate.py` (CI to be added later) and relevant tests.
3. Change tested end-to-end in **UAT** by a maintainer other than the author.
4. Merge, tag release.
5. App changes: users run `wft-update.bat`; bump `min_app_version` on the share if required.
6. Definition changes: admin runs `publish_definitions.py --env uat|prod`, which refuses to publish if validation fails.

### 9.3 Validation script checks
- Definition files conform to the definition schema.
- All referenced schemas, checklists, mappings, adapters and sub-workflows exist.
- No dependency cycles; every step reachable; every outcome routes somewhere valid.
- `consumes` fields exist in the input schema.
- Version bumped if a published definition changed.
- Dry-run: simulate each workflow with sample data through every outcome path.

### 9.4 Documentation
Maintained in `docs/` alongside the code, updated in the same PR as the change:
- How to add a new workflow.
- How to change an existing workflow (versioning rules, impact on in-flight requests).
- How to add or modify a trading-system schema (and KRM22 mapping).
- Operator user guide (later in R1).

### 9.5 Testing
- **Engine** (pure, high coverage): lifecycle transitions, step routing, impact-flag generation, amendment impact analysis, sub-workflow fan-out/join, deterministic replay, conflict resolution.
- **Repository**: against a temp directory simulating the share — multiple concurrent writers, partial trailing lines, lock expiry, read-only fallback.
- **Definitions**: validation and dry-run over every published definition.
- **UI**: manual testing in UAT for Release 1.

## 10. Data captured for future features

To enable later ETA and analytics without rework, Release 1 must record:
- every step state transition with UTC timestamp, actor and wait reason;
- hold start/end per request;
- workflow id + version and trading system per request;
- outcome per external/outcome step;
- whether a step was completed outside tool.

## 11. Success measures

No baseline of current paper cycle times will be collected. Success is judged on:
- Adoption: proportion of in-scope TT requests handled in WFT vs logged as off-process.
- Visibility: all in-progress in-scope requests visible on the dashboard.
- Completeness: no missed steps on WFT-handled requests.
- Timing data collected from go-live as the baseline for later ETA.

## 12. Ownership & support

- Built and maintained by a team of **3** (names TBD); code on GitHub.
- Each trading system schema and the KRM22 mapping has a **named owner** (TT owner TBD).
- WFT is **not operationally critical**: if it is unavailable, users revert to the existing manual process and log off-process records afterwards.

## 13. Roadmap

| Release | Candidate scope |
|---|---|
| R2 | TT client entity setup and add account, using sub-workflows (client → accounts → limits); compensation/rollback definitions where known. |
| R2/R3 | Cycle-time analytics view; off-process-by-reason view; ETA estimates (median/P80 per step on critical path, version-aware). |
| R3 | KRM22 REST API: create requests and poll status automatically. |
| R3+ | Additional trading systems (adapters, schemas); file-drop import data sources; reconciliation view. |
| Later | TT write operations; CI pipeline; packaged distribution (wheel → exe); business-hours calendars; email notifications; batched KRM22 uploads. |

## 14. Open items & assumptions

| # | Item | Owner |
|---|---|---|
| 1 | Confirm approved Python version. | TBD |
| 2 | Obtain KRM22 CSV upload format specification / sample file. | TT schema owner |
| 3 | Confirm the TT Add User step list and fields against the current paper form. | TBD |
| 4 | Build the TT data dictionary (user, account, limits) from existing API code and paper forms. | TT schema owner |
| 5 | Confirm per-user TT API keys are available for prod and UAT, and their read permissions. | TBD |
| 6 | Confirm share locations for prod and UAT and NTFS permissions for the 10 users. | TBD |
| 7 | ~~Confirm whether *deferred* impact flags block terminal states~~ Decided: they don't block, but stay visible on the Flags page (#9). | Team |
| 8 | Confirm whether users run on physical desktops or shared hosts (design handles both). | TBD |
| 9 | Name the team of 3, the admins, and the TT schema owner. | Product owner |

**Assumptions**
- All users have read/write access to the share and can run locally-installed Python with Artifactory access.
- Desktop clocks are reasonably synchronised (ordering does not depend on it; timings do).
- Volumes remain within ~10 users / < 30 requests per day; beyond that the storage approach should be revisited.

# Kanban Dispatcher Owner-Health Record Schema (v1)

> **Status:** AUTHORITATIVE for the singleton-owner health contract defined in
> `10-projects/hermes-migration/dedicated-dispatcher-cutover` §4.3 and §4.4.
> Consumed by the dedicated `dispatcher` Hermes gateway, the matching
> `notification`-only Sophie gateway, and the Watchdog owner/invariant check.
> `schema_version` is currently `1`. Evolution is **additive-only** within a
> version; a breaking change increments `schema_version` and updates both
> writers and readers in lockstep.
>
> **Reconciliation note:** The decomposition brief (task `t_7e93ca16`) named
> the example path `<root>/.health/<state>.json` (per-state files) and listed
> `absent` as a state. The design doc §4.3 (the authority) specifies
> `<root>/dispatcher-owners/<profile>-<pid>.json` (per-process files). This
> spec follows the design doc: a per-process path lets two live processes
> (e.g. a standby and an active) keep distinct records without clobbering,
> and it bounds the file set to "running processes," not "the universe of
> possible states." `absent` is a **reader-side synthesis** (no record
> exists) rather than a written state. The implementation card
> (`t_dc9b24d8`) MUST follow this spec, not the decomposition brief's
> exemplary wording.

This document is the formal interface between the **dispatcher health writer**
(`gateway/kanban_watchers.py`, dispatch-enabled gateways only) and the
**dispatcher health reader** (Watchdog dispatcher-owner check, the global
`hermes_cli/kanban.py` dispatcher-presence check, and any operator/operator-tool
that needs to answer "is the machine-wide dispatcher healthy, single, and
expected?"). The writer's first action is to read this file.

The record file is the **only** machine-readable artifact that signals
singleton-owner state. Cutover automation, Watchdog, and the absence-warning
path in `hermes kanban create` all read this file. Task bodies, the lock file,
the board databases, and the database `events` table are explicitly
**not** sources of ownership truth.

---

## 1. Goals and non-goals

**In scope / the contract this spec defines:**

- File location and naming under the global Kanban root.
- The bounded allow-list of fields a record may contain.
- The explicit deny-list that protects the credential boundary.
- The atomic-write protocol and the `absent` semantics.
- The freshness predicate used by readers to distinguish live from stale
  records.
- The wire-compatible JSON shape (types, units, enums).

**Out of scope / the contract this spec does NOT define:**

- The dispatcher lock implementation itself
  (`~/.hermes/kanban/.dispatcher.lock`). That is a separate primitive; this
  spec only consumes its observability surface (`lock_state`).
- The systemd unit configuration that bounds the dedicated service resource
  domain. Owned by `hermes-config` (`t_53e3b4dd`).
- The Watchdog owner/lock/heartbeat invariant and the per-board
  functional-progress check. Owned by `slideheroes/watchdog` (`t_2401599c`).
- The live cutover plan, the operator handoff, and the canary. Owner-only
  responsibilities from the design §6–§8.
- Worker capability contracts (assignee profile, Bitwarden sentinel, workspace
  pins). These are unchanged by the owner-record cutover; preserved by the
  implementation card (`t_dc9b24d8`).

---

## 2. File layout

All paths are anchored at the **global Kanban root**, which is the user-level
`~/.hermes/kanban/` directory (i.e. `${kanban_root}` resolves to the directory
that owns the lock file `~/.hermes/kanban/.dispatcher.lock` and the
`kanban.db` files). The record root is **not** the calling profile's
`HERMES_HOME`; it is the same machine-wide root for every profile.

```text
${kanban_root}/
├── .dispatcher.lock                 # machine-wide singleton lock (existing)
├── boards/                          # existing
│   └── <slug>/kanban.db
└── dispatcher-owners/               # NEW: per-process health records
    ├── dispatcher-12345.json        # committed, atomic, readable
    ├── dispatcher-12345.json.tmp    # in-flight; readers MUST ignore
    ├── sophie-67890.json            # only if `dispatch_in_gateway=true`
    └── ...
```

### 2.1 Record file naming

- Pattern: `<profile>-<pid>.json` (ASCII kebab-slug; no spaces, no slashes).
- `<profile>` is the Hermes profile name of the dispatch-enabled gateway
  process (e.g. `dispatcher`, `sophie`).
- `<pid>` is the OS process id of the publishing gateway process.
- The `.json` suffix is the committed, reader-visible file.
- The `.json.tmp` suffix is the in-flight writer staging file. Readers MUST
  skip `.tmp` files; writers MUST `os.replace` the temp file onto the final
  path before declaring the record published.

### 2.2 Atomic write protocol

The writer MUST:

1. Serialize the record to JSON with deterministic field ordering
   (`json.dumps(data, sort_keys=True)`), UTF-8, no BOM.
2. Open `<record_path>.tmp` for write, `os.fdopen(..., "w", encoding="utf-8")`.
3. `json.dump(data, fp, sort_keys=True)`; `fp.flush()`; `os.fsync(fp.fileno())`.
4. `os.replace(tmp_path, final_path)`. This is atomic on POSIX (`rename(2)`)
   and on Windows 3.12+ for files on the same volume.
5. On any exception, remove the `.tmp` file before propagating; never leave
   a stray `.tmp` record behind.

The contract:

- A concurrent reader either sees the file absent (no record yet) or sees
  the JSON of the **previous** successful publish. Readers never see a
  partial JSON document, a truncated file, or garbled bytes.
- A second writer who picks the same profile+pid slot MUST serialize against
  itself (OS-level `os.replace` is atomic per pairs of paths; the writer
  therefore MUST use unique `<pid>` slots or a per-process re-try loop).
- A writer that crashes mid-write leaves the previous committed record
  intact; the next tick re-publishes and `os.replace` replaces the stale
  record normally.

### 2.3 The `absent` case

A reader that finds no record files under `dispatcher-owners/` (or only `.tmp`
files) returns a synthetic `absent` record with the shape documented in
§3.7. This is **not** an error condition; it is the expected state before
the first gateway dispatch ever starts, and after a clean shutdown of the
last dispatch-enabled gateway.

---

## 3. Record schema

The record is a single JSON object. Top-level field allow-list (every other
field is rejected by the reader and is a writer bug):

| Field                          | Type           | Required | Meaning |
|--------------------------------|----------------|----------|---------|
| `schema_version`               | int            | yes      | Contract version. Literal `1` for this revision. |
| `profile`                      | string         | yes      | Hermes profile name of the publishing gateway (e.g. `dispatcher`). |
| `pid`                          | int            | yes      | OS process id of the publishing gateway. |
| `process_start_epoch`          | int (seconds)  | yes      | UNIX epoch when the publishing process started. `monotonic`-derived clocks are not allowed. |
| `state`                        | enum string    | yes      | One of `standby`, `active`, `error`, `stopping`, `stopped`. See §3.1. |
| `lock_state`                   | enum string    | yes      | One of `contended`, `held`, `unavailable`. See §3.2. |
| `last_tick_start_epoch`        | int (seconds)  | no       | UNIX epoch when the current tick began. Absent only on `standby` before the first tick. |
| `last_tick_completion_epoch`   | int (seconds)  | no       | UNIX epoch when the most recent tick finished. Absent only on `standby` before the first tick. |
| `last_tick_duration_ms`        | int (ms)       | no       | Wall-clock duration of the most recent tick in milliseconds (`>=0`). |
| `last_tick_outcome`            | enum string    | no       | One of `ok`, `error`, `skipped`. See §3.3. |
| `last_tick_board_count`        | int            | no       | Bounded count of boards the most recent tick included (`>=0`). |
| `last_tick_spawn_count`        | int            | no       | Bounded count of workers spawned by the most recent tick (`>=0`). |
| `last_tick_error_class`        | string         | no       | Redacted exception class name only (e.g. `PermissionError`). Never a message, never a traceback. See §3.4. |
| `board_slugs`                  | array<string>  | yes      | Bounded list of slugs the publisher knows about. See §3.5. |
| `board_count`                  | int            | yes      | `len(board_slugs)`. Redundant with the array length for readers that don't want to scan. |
| `aggregate_spawn_count`        | int            | yes      | Lifetime total of workers spawned by this process (`>=0`). |
| `configured_interval_seconds`  | int (seconds)  | yes      | The dispatch-interval configuration that this gateway is running under. |
| `build_version`                | string         | yes      | Installed Hermes build identifier (e.g. `0.27.0+dirty-hash`). |
| `freshness_token`              | int (seconds)  | yes      | UNIX epoch captured during `os.replace` of the current record. See §4. |

All time fields are integer-seconds **Unix epoch** (UTC). Sub-second precision
is **not** in scope for v1; readers MUST NOT rely on fractional epochs.

### 3.1 `state` enum

| Value      | Meaning |
|------------|---------|
| `standby`  | The process is a configured dispatcher but does not hold the singleton lock right now. It is retrying on a bounded cadence. |
| `active`   | The process holds the singleton lock and is dispatching. Only one process per machine may be in `active`. |
| `error`    | The process cannot dispatch. Either the lock is unavailable, a tick raised an unhandled exception, or atomic write failed. Fail-closed. |
| `stopping` | The process is performing a graceful shutdown. A `stopping` record is written before the lock is released. |
| `stopped`  | The process has released the lock and exited cleanly. `stopped` records are retained for the freshness window and then become stale. |

State transitions are monotonic with one exception: a `stopping` process that
crashes before finishing shutdown transitions to `error`. A `stopped` process
is never rerun — it is the terminal state for a single process lifecycle.

### 3.2 `lock_state` enum

| Value         | Meaning |
|---------------|---------|
| `contended`   | Another live process currently holds the singleton lock. The publisher is retrying. |
| `held`        | The publisher holds the singleton lock. Only the lock holder is permitted to publish `state=active`. |
| `unavailable` | Locking is not supported on this platform/build, or the lock file cannot be read/written. The publisher fails closed; `state` MUST be `error`. |

The invariant: `state=active` implies `lock_state=held`. The inverse is not
required: a process may hold the lock while in `state=standby` only during
handoff windows (e.g., the brief period between lock acquisition and the
first tick publish); however, the first record written after lock acquisition
**must** be `state=active`.

### 3.3 `last_tick_outcome` enum

| Value     | Meaning |
|-----------|---------|
| `ok`      | The tick completed without raising. `last_tick_error_class` MUST be absent. |
| `error`   | The tick raised an unhandled exception. `last_tick_error_class` MUST be present and named per §3.4. |
| `skipped` | The tick intentionally did no work (e.g. maintenance window, cap exhausted, no dispatchable cards). `last_tick_error_class` MUST be absent. |

### 3.4 `error_class` redaction

The `last_tick_error_class` field carries the **Python exception class name
only** (the type, not the message nor the traceback). Examples:

- `PermissionError`
- `OSError`
- `asyncio.TimeoutError`

The rule is: an exception class name is a stable public identifier and is
safe to publish. The exception's message, arguments, `__str__`, and
traceback frequently contain user-controlled input, file paths, task IDs,
and occasionally credentials — those are out of the deny-list (see §5).

Writers MUST NOT include any portion of the traceback, formatted message, or
exception args. If a class name itself contains user data (e.g. a custom
exception whose `__name__` is dynamic), the writer MUST redact the field
to a literal `"*redacted*"`.

### 3.5 `board_slugs`

The list of slugs is **bounded** to a configurable maximum (default `512`).
Slugs are textual identifiers; they are not credentials. The list is the
**discovered** board list from the most recent registry scan; it MAY be
empty during handoff windows. Archived boards are excluded.

### 3.6 Reserved future fields

The following v2 fields are explicitly reserved by this version and MUST NOT
appear in any v1 record:

- `host`, `pid_namespace`, `container_id`
- `worker_pids`, `worker_states`
- `subscription_count`, `delivery_obligation_count`

A reader that encounters any unknown field MUST ignore it (forward-compat),
and a reader that encounters one of these reserved fields MUST treat the
record as the v1 shape (the reserved field is not defined until v2 lands).

### 3.7 The `absent` synthetic record

A reader that finds no record reports an `absent` literal:

```json
{
  "schema_version": 1,
  "state": "absent",
  "lock_state": "unknown",
  "profile": null,
  "pid": null,
  "freshness_token": 0,
  "board_count": 0,
  "board_slugs": [],
  "configured_interval_seconds": 0,
  "build_version": "",
  "process_start_epoch": 0,
  "aggregate_spawn_count": 0
}
```

`absent` is not a `state` enum value stored on disk; it is a reader-side
synthesis indicating "no record files exist." Writers MUST NOT write a
record with `state: "absent"` to disk.

---

## 4. Freshness rule

This is the predicate Watchdog and the global presence check use to decide
whether a record is **live** or **stale**.

### 4.1 Definition

Let `now` be the current Unix epoch in seconds. Let `interval` be the
configured dispatch interval (default `60` seconds, sourced from
`configured_interval_seconds` or the dynamically-running value). Let
`record.completion_epoch` be `last_tick_completion_epoch` if present, else
`freshness_token` if present, else `process_start_epoch`.

A record is **fresh** iff:

```
record.completion_epoch is not None
and (now - record.completion_epoch) <= ttl(record)
```

The TTL is bound as:

```
ttl(record) = max(3 * record.configured_interval_seconds, 180)
```

with a lower bound of `180` seconds and no upper bound. The `180` second
floor covers the failure modes where the configured interval is very small
(e.g. tests) or where the publisher has not yet completed a tick.

### 4.2 `stale` field

A reader that is rendering a record for human or operator tooling MAY
derive and add a `stale: bool` field to the deserialized shape:

```python
def is_stale(record: dict, now: float, interval: float) -> bool:
    """Return True iff the record is past the freshness window."""
    completion = record.get("last_tick_completion_epoch")
    if completion is None:
        completion = record.get("freshness_token")
    if completion is None:
        completion = record.get("process_start_epoch", 0)
    ttl = max(3 * interval, 180)
    return (now - completion) > ttl
```

The `stale` field is **derived**, never written by the producer. The
on-disk format MUST NOT include a `stale` key.

### 4.3 Freshness token

`freshness_token` is the epoch captured **immediately before** `os.replace`
of the current record. It is the publisher's "I was alive at this moment"
attestation. Readers MAY use it as the freshness comparator when
`last_tick_completion_epoch` is absent (e.g. during the brief `standby`
window before the first tick), but `last_tick_completion_epoch` is
preferred when present.

```python
def freshness_token_for(record: dict) -> int:
    """The canonical 'when was this record committed' epoch."""
    return (
        record.get("last_tick_completion_epoch")
        or record.get("freshness_token")
        or record.get("process_start_epoch", 0)
    )
```

### 4.4 Testable freshness contract

The freshness predicate is implementable in five lines and is unit-testable
without any filesystem or process machinery:

- `freshness_completion_age_seconds(record, now) <= ttl(record)` ⇒ fresh.
- The `ttl` bound is `max(3 * interval, 180)`.
- The freshness comparator source is
  `last_tick_completion_epoch ?? freshness_token ?? process_start_epoch`.
- A record older than `ttl` is `stale`; readers MUST treat `stale` records
  as **not authoritative** for lock/active-count decisions.
- A fresh but `lock_state=unavailable` record is still fresh — but the
  publisher is in `error`, not `active`, so it does not count toward the
  active-owner total.

A test that asserts `(now - completion) <= ttl` ⇒ fresh is sufficient to
satisfy the testable freshness rule.

---

## 5. Allow-list and deny-list

The record is the credential boundary. The allow-list is the **complete**
set of fields a writer may set. The deny-list is the **complete** set of
values a writer MUST NOT include anywhere in the record (including future
fields).

### 5.1 Allow-list (writer's contract)

The following fields are the only top-level keys a writer may emit:

```
schema_version
profile
pid
process_start_epoch
state
lock_state
last_tick_start_epoch
last_tick_completion_epoch
last_tick_duration_ms
last_tick_outcome
last_tick_board_count
last_tick_spawn_count
last_tick_error_class
board_slugs
board_count
aggregate_spawn_count
configured_interval_seconds
build_version
freshness_token
```

Any other top-level key MUST be rejected by a reader. The reserved v2
fields in §3.6 are explicitly NOT in this list.

### 5.2 Deny-list (deny-by-value, never appears)

The writer MUST NOT include any of the following values, in any field, in
any encoding, in any format:

- **Prompt text.** Any user-facing instruction, system prompt, or context
  block. Includes task bodies, agent_card text, model inputs, tool
  outputs, and chat history excerpts.
- **Task bodies.** The `body`, `planning_body`, `result`, or `summary`
  fields of any Kanban task, including the current task, the parent task,
  any sibling task, and any task mentioned by id.
- **Channel IDs.** Any platform-specific identifier (Discord guild/channel,
  Telegram chat id, Slack workspace, email address, phone number, IRC
  channel, Matrix room id, webhook URL, etc.) — including the publisher's
  own adapter chat ids.
- **Credentials.** API keys, tokens, OAuth refresh tokens, bot tokens,
  session cookies, Bitwarden item ids, vault UUIDs, project sids, client
  secrets, machine-account names, signed JWTs, AWS access keys, and any
  password or passphrase. This is the literal deny-list; it does not
  enumerate values that *look* like credentials (that is a separate
  redaction pass).
- **Environment variable values.** Any value fetched from `os.environ`. The
  publisher's environment is its own credential surface; it MUST NOT be
  dumped.
- **Stack traces.** Python traceback strings and the path/message of any
  exception raised during dispatch. The exception class name is allowed
  (see §3.4); the rest is not.
- **User data.** Any value derived from a user message, a user-uploaded
  file, a user-entered form, a user-configured setting, or a user-owned
  record. The credential firebreak from §4.7 of the design doc is
  unconditional.

A reader that detects any deny-listed value in a record MUST treat the
record as a writer bug and surface a structured finding (and MUST NOT
re-emit the offending value).

### 5.3 Encoding

The deny-list applies to **all encodings**, including but not limited to:

- Plain string values.
- JSON-escaped string values (e.g. `"foo\"bar"`).
- Base64 payloads (anywhere in the record).
- Hex-encoded payloads.
- Concatenated fields where re-joining produces a deny-listed value.
- Sorted/normalized variants (e.g. lowercased, URL-encoded, percent-encoded).

If a string contains a substring that decodes to a deny-listed value, the
substring MUST be redacted to `"*redacted*"` before the record is written.
A regex pre-pass at write time is the minimum acceptable control.

### 5.4 Field-type constraints

Even within the allow-list, the writer MUST also enforce:

- `state` ∈ {standby, active, error, stopping, stopped}
- `lock_state` ∈ {contended, held, unavailable}
- `last_tick_outcome` ∈ {ok, error, skipped} (or absent)
- All int fields are integer-valued JSON numbers (no floats, no
  scientific notation).
- All string fields are bounded by `4096` characters; longer strings
  MUST be truncated to `4096` and a `*truncated*` marker appended.
- Arrays (currently `board_slugs`) are bounded by `512` elements; longer
  arrays MUST be truncated to the first `512` elements.

---

## 6. Reader contract

The reader is any code that consumes the on-disk record and draws a
machine-state conclusion. The two first-party readers are:

1. **Watchdog owner/invariant check** (`slideheroes/watchdog`).
2. **`hermes_cli/kanban.py::_check_dispatcher_presence`** — the global
   presence check that replaces the profile-local inference path.

A reader MUST:

1. Walk `${kanban_root}/dispatcher-owners/*.json` and ignore every
   `.tmp` file.
2. For each `.json`, parse JSON; on parse failure, treat the file as
   empty and continue.
3. Apply the freshness rule from §4. Drop stale records.
4. Apply the allow-list filter from §5.1. Unknown fields are ignored;
   reserved v2 fields are ignored.
5. Apply the deny-list sniff from §5.2. If a deny-listed value is found,
   surface a structured finding and exclude the record.
6. From the remaining records, compute:
   - `active_records`: those with `state=active` AND
     `lock_state=held` AND `fresh`.
   - `error_records`: those with `state=error`.
   - `standby_records`: those with `state=standby`.
   - `freshest_record`: the record with the greatest
     `freshness_token()` value (i.e. the most recently committed).
7. Return a result object with the above plus the synthetic `absent`
   shape from §3.7 when no records exist.

The reader MUST NOT:

- Cross-reference task bodies, the lock file contents, or the database
  `events` table. The record is the source of truth.
- Cache a decision for longer than the freshness TTL. Every read is
  evaluated against the current epoch.
- Emit deny-listed values in any log line, error message, or finding.
  All deny-listed values are redacted to `"*redacted*"` before being
  passed to a logging sink.

---

## 7. Writer protocol (summary)

The writer is the dispatch-enabled gateway process. The state machine:

```
            ┌─────────────┐
   start ──►│   standby   │◄───────────┐
            └──────┬──────┘            │
              lock │ acquire          │ backoff
                   ▼                  │
            ┌─────────────┐            │
            │   active    │────────────┤
            └──────┬──────┘            │
              SIGTERM / SIGINT         │
                   ▼                  │
            ┌─────────────┐            │
            │  stopping   │────────────┘
            └──────┬──────┘   (lock released → standby)
                   │
                   ▼
            ┌─────────────┐
            │   stopped   │  (terminal)
            └─────────────┘

            on any exception ──►│ error │ (fail-closed)
```

**Acquire path (lock available):**

1. Write `<record>.tmp` with `state=active`, `lock_state=held`.
2. `os.replace` to the final path.
3. Enter the tick loop.

**Acquire path (lock contended):**

1. Write `<record>.tmp` with `state=standby`, `lock_state=contended`.
2. `os.replace` to the final path.
3. Sleep on a bounded backoff (default `30s`, configurable via
   `STANDBY_BACKOFF_MS` with a safe default).
4. Retry. Bound by a configurable max-wait (`STANDBY_MAX_WAIT_SECONDS`,
   default `300`) so the process fails closed rather than hangs forever.

**Acquire path (lock unavailable):**

1. Write `<record>.tmp` with `state=error`, `lock_state=unavailable`.
2. `os.replace` to the final path.
3. Return immediately. **Do not proceed with dispatch.** This is the
   fail-closed branch.

**Tick loop:**

1. Before each tick: set `last_tick_start_epoch = now`.
2. Run the tick body.
3. After the tick: set `last_tick_completion_epoch = now`,
   `last_tick_duration_ms = max(0, now - last_tick_start_epoch) * 1000`,
   `last_tick_outcome = "ok" | "error" | "skipped"`,
   `last_tick_board_count = len(discovered_boards)`,
   `last_tick_spawn_count = spawned_count`.
4. `os.replace` to refresh the record.
5. Refresh at least once per dispatch interval. The
   `aggregate_spawn_count` field is monotonically increasing.

**Graceful shutdown:**

1. Receive `SIGTERM` / `SIGINT` / `asyncio.CancelledError`.
2. Write `<record>.tmp` with `state=stopping`, `lock_state=held`.
3. `os.replace` to the final path.
4. Release the lock.
5. Write `<record>.tmp` with `state=stopped`, `lock_state=contended`.
6. `os.replace` to the final path.
7. Exit.

**Crash path:**

1. The `finally` block (or the SIGTERM-equivalent handler) writes
   `<record>.tmp` with `state=error`, `lock_state=held` if the lock is
   still held, or `lock_state=unavailable` if the lock is already
   corrupted.
2. `last_tick_error_class` is set to the exception type name only.
3. `os.replace` and re-raise.

The tick loop MUST run on the parent's main loop. The tick loop MUST NOT
spawn a child process to perform the publish; the publish is in-process
and synchronous so the failure surface is well-defined.

---

## 8. Verification matrix

These are the testable claims of this spec. Each MUST be verifiable by
a deterministic unit test against a temp directory and a frozen clock.

| Claim | Verification |
|---|---|
| Schema version field is present and equal to `1`. | Load any committed record in tests; assert `record["schema_version"] == 1`. |
| Allow-list is complete. | Walk every key in the record; if any key is not in the §5.1 list, fail. |
| Deny-list is empty. | Serialize the record, run the redaction sniffer, assert no deny-listed values appear. |
| No prompt text in record. | Construct a record with a known prompt substring (e.g. `assert_prompt_in_record`); assert the sniffer rejects it. |
| No task bodies in record. | Same as above with a task body. |
| No channel IDs in record. | Same with a Discord channel id, a Telegram chat id, etc. |
| No credentials in record. | Same with a fake token, a fake API key, a fake vault UUID. |
| No env values in record. | Same with a value sourced from `os.environ`. |
| No stack traces in record. | Same with a literal `Traceback (most recent call last)` string. |
| `state` enum is respected. | Try `state="unknown"`; assert the writer rejects. |
| `lock_state` enum is respected. | Same. |
| `last_tick_outcome` consistency. | Try `outcome="error"` with `error_class` absent; assert rejection. |
| Atomic write — no partial JSON. | Reader thread polls the file during a writer holding a `time.sleep` between `flush` and `os.replace`; reader must never see partial JSON. |
| Freshness — fresh vs stale. | A record older than `ttl(interval)` is `stale`; a record newer is `fresh`. |
| TTL formula. | `ttl = max(3 * interval, 180)`. Configurable interval: `interval=10` → `ttl=180`. `interval=60` → `ttl=180`. `interval=120` → `ttl=360`. |
| `freshness_token` is the preferred comparator before the first tick. | A record with `last_tick_completion_epoch` absent but `freshness_token` present is fresh iff `(now - freshness_token) <= ttl`. |
| `absent` synthesis. | Empty directory → reader returns the §3.7 shape. |
| Active-count is consistent. | Reader counts `state=active AND lock_state=held AND fresh` records; if `>1`, surface a duplicate-owner finding. |
| Stale records are not authoritative. | Records past TTL are excluded from active-count and from the freshest-record decision. |
| Reserved v2 fields are ignored. | A record with a `host` field is parsed without error; the field is dropped. |
| Field truncation. | A `board_slugs` array of length `1024` is truncated to `512` at write time. |
| Field truncation. | A `last_tick_error_class` string of length `8192` is truncated to `4096` with a `*truncated*` marker. |

---

## 9. Versioning

| Version | Status | Notes |
|---------|--------|-------|
| `1`     | current | This document. Fields, enums, file layout, freshness rule. |
| `0`     | implicit pre-spec | The current `gateway/kanban_watchers.py` lock file alone. Reader returns `absent` until a v1 record is published. |

A v1 reader may exist alongside a v1 writer. A v2 writer is forbidden by
this revision; when v2 is written, the v1 reader must continue to work
on the v1 subset of v2 records (additive-only).

---

## 10. References

- `10-projects/hermes-migration/dedicated-dispatcher-cutover`, §4.3
  (single-owner state machine) and §4.4 (freshness predicates).
- `gateway/kanban_watchers.py` — the dispatch-enabled writer.
- `hermes_cli/kanban.py::_check_dispatcher_presence` — the global
  presence check that consumes the record.
- `slideheroes/watchdog` — owner/lock invariant and the dispatcher
  functional-progress check.
- `~/.hermes/kanban/.dispatcher.lock` — the singleton lock primitive
  this spec observes (`lock_state`).
- `t_dc9b24d8` — Hermes core implementation card.
- `t_53e3b4dd` — `hermes-config` / dedicated service / systemd card.
- `t_2401599c` — Watchdog owner/invariant and progress check.
- `t_bd818335` — initiative card and source-of-truth design narrative.

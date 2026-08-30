# Decision Log

Running log, updated as we build. Max 2 pages — trimmed for signal, not chronology.

## Key assumptions

- **Monday.com MCP transport**: the official server is the npm package
  `@mondaydotcomorg/monday-api-mcp`, run as a local stdio subprocess authenticated
  with a personal API token — not a hosted `mcp.monday.com` HTTP endpoint (no such
  endpoint exists publicly; verified against developer.monday.com docs). Both this
  dev session and the deployed app connect the same way: spawn the npm package via
  the MCP Python SDK's stdio client.
- **Board identification**: boards are looked up by exact name ("Work Orders",
  "Deals") at startup if no board ID env var is set, falling back to ID if given.
  Assumption: the user's Monday.com account has exactly one board matching each
  name. If ambiguous, the app should surface an explicit error rather than guess.

- **Cross-board join key**: neither board has an explicit foreign key linking a
  Deal to a Work Order. Work Orders has "Customer Name Code" (`WOCOMPANY_002`)
  and Deals has "Client Code" (`COMPANY089`) — same numbering scheme, different
  prefix. Assumption: these identify the same company when the numeric suffix
  matches (`WOCOMPANY_002` ↔ `COMPANY002`), and cross-board queries join on that
  normalized company id, not on a 1:1 deal-to-work-order mapping (one company can
  have many deals and many work orders). Secondarily, "BD/KAM Personnel code" on
  Work Orders and "Owner code" on Deals share the same `OWNER_NNN` namespace
  directly (no prefix mismatch) — usable for rep-level correlation. Both mappings
  live in `data_normalize.py` and are unit-tested; if real data ever fails to
  match on numeric suffix, that pair should be treated as unlinked rather than
  guessed at.
- **Item "Name" is not a business identifier on either board** — sample values
  are placeholder names (e.g. "Scooby-Doo", "Naruto"), not company or deal names.
  The real identifiers are Serial # / Customer Name Code (Work Orders) and
  Client Code (Deals). Normalization and insights code must never surface the
  "name" field as if it were a company name.

## Real messy-data findings (from live schema/sample pull, both boards)

- Numeric column values arrive as JSON **strings** (`"264398.08"`), not numbers,
  including for zero (`"0"`) and negative values (billing corrections go negative,
  e.g. `-82907.29608`) — cast explicitly, don't assume falsy-checks distinguish
  "0" from missing.
- Work Orders has **two columns with the identical title** "Quantities as per
  PO" — one `dropdown` type (`dropdown_mm6q5q2c`), one `text` type
  (`text_mm6q3y90`) — carrying duplicate values per row. Normalization picks one
  canonical source (the dropdown) and flags the text column as legacy/ignored,
  logged explicitly rather than silently averaging or double-counting.
- Status label typo found in production data: Work Orders' "Billing Status"
  column has both `"Billed"` and `"BIlled"` as distinct label values — exactly
  the casing inconsistency this project is meant to handle; normalization
  lower-cases and trims before matching.
- "Collection Date" is typed as a **text** column (not `date`) unlike every
  other date field on the board — our date parser can't rely on Monday's own
  column typing to know what's a date; it has to attempt parsing based on
  content, not column type alone.
- Sector taxonomies **don't agree across boards**: Work Orders' "Sector" values
  seen so far are Mining / Powerline / Renewables; Deals' "Sector/service" also
  includes "Tender", which is a deal-type, not a sector. Sector-based
  cross-board rollups need an explicit canonical sector map rather than an
  exact-string join.
- Both boards are sparsely populated per row — most non-required columns are
  `null` on any given item (e.g. "Expected Billing Month", "Actual Collection
  Month" were null across the entire first sample page). Aggregates must
  exclude nulls explicitly and the agent must say so in its answer.
- Confirmed scale via live pagination: 176 Work Orders items, 346 Deals items.

## Full-dataset validation (ran normalization against all 176 + 346 real rows, not just the sample)

- **Company-code join validated at scale**: 50 of 51 unique Work Orders company
  codes have at least one matching Deal by numeric suffix (WOCOMPANY_NNN ↔
  COMPANYNNN) — a 98% match rate, strong confirmation the join key assumption
  above is correct. The 1 orphan (a work order whose company has no matching
  deal record) should be surfaced as a disclosed exclusion in cross-board
  queries, not silently dropped.
- **"Deals" sector taxonomy is much messier than "Work Orders"**: Work Orders
  uses 6 sector values (Mining, Powerline, Renewables, Railways, Construction,
  Others); Deals uses 13, including "Tender" (a deal-sourcing channel, not an
  industry), "DSP", and — found twice — **"Sector/service"**, which is the
  column's own title typed in as a value, a genuine data-entry artifact.
  `is_valid_sector()` in `data_normalize.py` excludes that artifact from
  sector breakdowns with disclosure; "Tender" and "DSP" are kept as real,
  distinct categories (a judgment call, not a certainty — flagged here rather
  than silently decided). 8 Deals have no sector at all.
- **Deals' "Masked Deal value" is missing on 181 of 346 records (52%)** — any
  pipeline-value aggregate must disclose this exclusion prominently; it is not
  a rounding-error-sized gap.
- Billing Status typo ("BIlled") is confirmed to recur 3 times in the full
  data, alongside two labels not seen in the original sample page ("Not
  Billable", "Stuck") — both legitimate, no fix needed.
- "Collection Date" (the text-typed date column) is null on all 176 Work
  Orders rows — currently unexercised by real data, but `parse_date()` still
  handles it defensively since it's a text field with no format guarantee.

## Ground truth for BI logic (pulled live before writing insights.py)

- **2 of 346 Deals rows are corrupted template rows**, not real data: every
  status column's value is literally that column's own title ("Deal Status" ==
  "Deal Status", "Sector/service" == "Sector/service", "Product deal" ==
  "Product deal", ...) with every other field null — almost certainly a
  mangled header row from the CSV import. `is_template_artifact_item()` /
  `filter_out_artifact_items()` in `data_normalize.py` detect and exclude
  these generically (matching against the column titles fetched live via
  `get_board_schema`, not a hardcoded row shape), and every Deals aggregate
  discloses this exclusion.
- **Deal Status is the real win/loss field**: `Won` (165), `Dead` (127, i.e.
  lost), `Open` (49), `On Hold` (2), plus the 2 artifact rows above. "Won" is
  the definition of a closed-won deal used everywhere in this project.
- **Deal Stage** is a rich, mostly-lettered funnel (A. Lead Generated → B.
  Sales Qualified Leads → C. Demo Done → D. Feasibility → E.
  Proposal/Commercials Sent → F. Negotiations → G. Project Won → H. Work
  Order Received → I. POC → J. Invoice sent → K. Amount Accrued), with side/
  terminal states L. Project Lost, M. Projects On Hold, N./O. Not relevant.
  One value, "Project Completed", breaks the lettering convention (no prefix)
  — display code treats Deal Stage as a categorical breakdown (counts per
  stage) rather than relying on alphabetical sort for funnel order, since
  that would misplace "Project Completed".
- **Multi-value columns use inconsistent separators**: "Type of Work" joins
  multiple labels with commas ("Hydrology, Topography Survey: RGB"); "Product
  deal" joins with " + " ("Dock + DMO + Spectra"). `split_multi_label()`
  handles both. Assumption: an item with 2 work types counts toward both
  types in a breakdown (not split fractionally) — logged here since it's a
  judgment call, not a certainty.
- **WO Status (billed)**: null (74), Open (24), Closed (78) — "at-risk /
  overdue" work order status is not a literal column anywhere on the board;
  this project defines it operationally as: Probable End Date has passed AND
  Execution Status is not "Completed" / "Partial Completed" AND WO Status
  (billed) is not "Closed". This is an interpretation, not a monday.com field
  — flagged here and in `insights.py`.
- Heavy null rates to disclose wherever relevant: Deals' Closure Probability
  is null on 258/346 (75%); Work Orders' Invoice Status is null on 64/176
  (36%).

## Query understanding & "no static fallback data"

- `query_engine.py` and `insights.py` hardcode board **column titles**
  ("Sector", "Deal Status", etc.) and the confirmed meaning of "Won"/"Dead" as
  parsing/computation context. This is schema vocabulary, not data — it
  never changes across queries and doesn't vary per row. The project's "no
  cached/static fallback data" requirement is about *answers* (row values,
  aggregates): every single one of those still requires a live
  `get_board_items_page` call at the moment the question is asked (see
  `insights.load_board`, called fresh inside `agent.ask` on every turn — no
  result is ever reused across questions). Ambiguity about whether a
  *value* the LLM guessed at (e.g. a sector name) actually exists is
  resolved against that same live data, not against a hardcoded list — see
  `agent._filtered` / `NeedsClarification`.
- Filter matching (sector/status/owner) is case-insensitive substring
  matching (`data_normalize.label_key`), not a fuzzy-matching library — a
  deliberate scope cut for the time budget (see "what we'd do differently").
  When nothing matches, the agent asks one clarifying question listing the
  real values found on the live board, rather than guessing.

## Trade-offs

- **Hosting: Render (Docker) over Streamlit Community Cloud.** The MCP server
  requires Node.js to spawn via npx; Streamlit Community Cloud is Python-only
  (no arbitrary subprocess runtime guarantees). Render/Railway let us ship a
  Dockerfile with both Python and Node, which is a hard requirement here, not a
  nice-to-have. Trade-off: slightly more deploy setup than a one-click Streamlit
  push, in exchange for the integration actually working in production.
- **Streamlit as the UI framework** (not Flask+React): fastest path to a
  conversational, stateful chat UI within the 6-hour budget. `st.session_state`
  gives multi-turn context for free; a custom frontend would cost hours we don't
  have for equivalent polish.
- **Two LLM calls per question** (parse into a QueryPlan, then narrate the
  computed result) instead of one combined call: keeps the model from ever
  producing both "what to look up" and "the number" in a single step where a
  hallucinated number could slip through disguised as narration. Costs one
  extra round-trip in exchange for numbers that are always ours, never the
  model's guess.
- **Docker build and boot verified locally** before writing this off as
  deploy-ready: `docker build` succeeds, the container serves HTTP 200, and
  the missing-LLM-key error path renders correctly in-browser (clear message,
  no stack trace) — checked before assuming Render will just work.
- **Switched primary LLM provider from Anthropic to Groq mid-build**, when a
  paid Anthropic account wasn't available. Rather than rewrite three files
  around a new SDK shape, added `src/llm_provider.py` as a thin abstraction
  (`create_tool_call` / `create_text`) so `query_engine.py`, `agent.py`, and
  `leadership_update.py` never import a provider SDK directly — swapping
  providers (or going back to Anthropic later) is a `.env` change
  (`LLM_PROVIDER`), not a rewrite. This also surfaced a real, verified
  incompatibility worth recording: Groq validates tool-call arguments against
  the JSON schema **server-side** and rejects the whole call on a casing
  mismatch (the model emitted `"Work Orders"` where the schema said
  `"work_orders"`); Anthropic does not enforce this as strictly. Fixed by
  dropping strict `enum` constraints on the model-facing schema and validating/
  normalizing case-insensitively in `query_engine._normalize_enum` instead —
  a fix that also makes parsing more robust to whichever provider is active,
  not just Groq.

## Live end-to-end verification findings (after the provider swap)

Ran 6+ real founder questions through the full pipeline (parse → live Monday.com
fetch → compute → narrate), a real multi-turn exchange, a real ambiguous-filter
case, and 4 deliberately-triggered failures (bad Anthropic key, bad Monday.com
auth, a simulated MCP timeout via `tool_timeout=0.001`, and an off-topic
question) — all against the live boards, not fixtures. Two real bugs surfaced
and were fixed as a direct result, not left for later:

- **Dispatch bug**: "give me a breakdown of work orders by billing status"
  parsed to `shape=sector_breakdown, metric=work_order_count,
  group_by=billing_status` — a reasonable plan — but `agent._compute` checked
  `shape` before `group_by` and always ran a sector breakdown regardless,
  silently answering the wrong question. Fixed by making `group_by` the
  first, highest-priority dispatch signal (it's the most specific statement
  of what the user actually wants grouped by), with `_load_boards` updated to
  fetch the right board(s) for whichever `group_by` value is set.
- **Narration miscounting from a truncated sample**: the leadership update's
  "billing needs attention" section was only ever shown a 10-item sample (of
  15 real records) plus a correct total count, and the model presented a
  category breakdown of that sample ("10 Update Required, 5 Stuck") as if it
  were the full population — the true split was 12/3. The total was never
  wrong; the sub-breakdown was invented from an incomplete view. Fixed at the
  root: `compute_leadership_data` now computes the true full-population
  breakdown as its own field, and any list handed to a narrator is generically
  capped and flagged (`agent._trim_for_narration`) so the model is never shown
  enough of a list to be tempted into deriving a count from it.
- **Narration reliability is real but not perfect, and this is disclosed
  rather than hidden**: one live run had the narrator restate a correct
  68%/117/45 completion-rate result as a wrong "98%/119/none at-risk" — not
  reproducible on 10 immediate re-runs of the identical data at both
  `reasoning_effort=low` and `medium` (10/10 correct both times), so this
  reads as genuine, low-probability model variance rather than a deterministic
  bug in this codebase. This is a real, honest limitation of using a free/
  open-weight model (`openai/gpt-oss-120b` via Groq) for narration rather than
  a frontier model — mitigated, not eliminated, by `agent._looks_consistent`:
  a cheap tripwire that checks the narrated answer contains the key number(s)
  from the computed `interpretation` string, retries once on failure, and
  falls back to a guaranteed-correct (if less naturally phrased) templated
  answer if the retry also fails. The underlying computation was never wrong
  in any of this — only the LLM's restatement of it, rarely, was.

## Production crash: anyio cancel-scope violation on first real deployed use

The deployed app's very first real question failed with `Attempted to exit
cancel scope in a different task than it was entered in`. Root cause, found
by direct reproduction (not guessed): `MondayMCPClient.__aenter__` wrapped
`stack.enter_async_context(stdio_client(params))` in `asyncio.wait_for`.
`stdio_client` is an anyio-based context manager whose `__aenter__` opens a
task group meant to stay alive for the whole session (its reader/writer pump
tasks); `wait_for` runs its coroutine in a separate `ensure_future()` task, so
that task group's entry was bound to an ephemeral task, while the matching
exit (via `AsyncExitStack.aclose()` in `__aexit__`, called later from the
original task) ran in a different one — exactly the error text.

The first fix attempt (swap `asyncio.wait_for` for `anyio.fail_after`, same
placement) was **also wrong** and reproduced a second failure, this time a
deadlock: closing an outer `fail_after` scope while an inner one it contains
(stdio_client's task group, deliberately still open) remains open is illegal
in anyio regardless of whether anything actually timed out — this holds even
on the successful, non-timing-out path. Confirmed via a minimal reproduction
script before touching the real fix: wrapping the *entire* connect-use-close
lifecycle in one `fail_after` scope works; wrapping just the entry does not,
by either method. Final fix: no timeout wraps `stdio_client`'s own entry at
all (accept that a hang here is a narrow residual risk — subprocess spawn
either fails fast or succeeds fast in practice); `session.initialize()` and
each individual tool call (already the case) ARE safely wrapped in
`anyio.fail_after`, since neither opens a task group of its own that needs to
outlive the call. Verified via 3 sequential fresh `asyncio.run()` cycles (the
exact pattern Streamlit uses per rerun) and, more importantly, through the
actual Streamlit UI in a fresh browser session asking the exact question that
failed in production, plus follow-ups — all succeeded with no crash.

**Files changed for this fix**: `src/monday_mcp_client.py` (the above), and
`requirements.txt` (added `anyio` explicitly — it was already an indirect
dependency of `mcp`, but is now imported directly).

## Multi-turn context gap found while verifying the crash fix (partially fixed)

Replaying a 3-turn sequence during crash verification ("energy sector this
quarter" → clarification → "Renewables instead" → "Billed revenue") surfaced
a second, unrelated real bug: clarification exchanges discarded every plan
field except the clarifying question itself, and `agent.ask()` never updated
`state.last_plan` on a clarification turn — so a sector the model had already
correctly identified was silently lost by the next turn. Fixed at both
layers: `query_engine.parse_query` now keeps all recognized fields alongside
a `clarification_question` instead of blanking them, and `agent.ask()` stores
`state.last_plan` on both clarification paths, not just full successes.
Verified fixed for the case that matters most — a direct answer to the
agent's own clarifying question ("Renewables", asked which sector → next turn
correctly filters to Renewables). Not fully fixed: a terse reply following a
*complete* prior answer (not a question), like "Billed revenue" after a full
Renewables pipeline answer, doesn't reliably inherit the prior sector filter —
tried strengthening the merge instruction further, which caused a regression
elsewhere (the model started guessing at sectors instead of asking for
clarification) and still didn't fix this specific case, so the change was
reverted rather than kept. This is disclosed as a real, remaining limitation
rather than hidden — it's LLM instruction-following reliability on an
inherently ambiguous case (a two-word reply could reasonably be a fresh
question or a scoped follow-up), not a deterministic code defect.

## What we'd do differently with more time

- Track explicitly in `ConversationState` whether the last turn ended on an
  unanswered clarifying question (a bool, not left for the LLM to infer from
  a JSON summary each time) — a terse next message would then deterministically
  know "this is answering that question" instead of relying on the model to
  correctly judge it from context every single time, which is the open gap
  documented above.
- Replace substring-based filter matching with a real fuzzy-matching library
  (e.g. rapidfuzz) — today "renewable" matches "Renewables" via substring
  containment, but a genuine misspelling wouldn't, and would fall through to
  the clarifying-question path more often than necessary.
- "Pipeline movement" in the leadership update uses Created Date / Close Date
  as a proxy for activity because reconstructing true stage-by-stage
  transitions requires parsing `get_board_activity`'s change log in detail,
  which didn't fit the time budget — a real stage-change history would make
  that section meaningfully richer.
- Cache live-fetched board *schema* (column id/title map) for the lifetime of
  a Streamlit session rather than re-fetching it on every single question —
  it never changes mid-session, and skipping the re-fetch would cut latency
  without touching the "always live data" guarantee for actual answers.
- Add unit tests for `insights.py` against fixture data (today it's validated
  by running it against the real live boards during development, which is
  strong evidence but not a repeatable regression test in CI).
- A proper CI workflow (GitHub Actions) running `pytest` on every push —
  not set up here purely for time.
- Given a paid LLM budget, use a frontier model (Claude, GPT-4-class) for the
  narration step specifically, even if a cheaper/free model is fine for query
  parsing — narration is where the rare accuracy issue documented above shows
  up, and it's the one output the founder actually reads.

## How "leadership updates" was interpreted

Treated as a periodic (default trailing 30 days) executive brief with four
fixed, live-computed sections — Pipeline Movement, At-Risk/Overdue, Notable
Wins, Operational Highlights — rather than a free-form AI-written summary.
This was a deliberate choice: a founder-facing update should be reproducible
and auditable (the same period always yields the same numbers), not a
creative-writing exercise the LLM does over the data. The LLM is still used,
but only to *phrase* the four sections from numbers this app already
computed — same pattern as every other answer in this project. Two metric
definitions had to be invented since neither is a literal monday.com field:
"at-risk work order" (see above) and "stalled handoff" (a Won deal with no
matching work order ≥30 days after close — a possible dropped handoff from
sales to delivery). Both are stated in code comments in
`leadership_update.py` as well as here, per the requirement to make this
interpretation explicit in both places.

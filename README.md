# Skylark BI Agent

A conversational AI agent that answers founder-level business intelligence
questions by reading live data from two Monday.com boards — **Work Orders**
(project execution) and **Deals** (sales pipeline) — over the official
Monday.com MCP server. Every answer is computed from a fresh read at the
moment you ask; nothing is cached or hardcoded.

See [DECISION_LOG.md](DECISION_LOG.md) for the reasoning behind every
non-obvious choice (join keys, metric definitions, hosting, what "leadership
update" means here).

## Architecture

```
┌─────────────┐      chat / leadership-update click     ┌──────────────┐
│  Streamlit   │ ───────────────────────────────────────▶│   app.py     │
│  (browser)   │◀─────────────────────────────────────── │              │
└─────────────┘              rendered answer              └──────┬───────┘
                                                                   │
                                          ┌────────────────────────┼─────────────────────────┐
                                          ▼                        ▼                          ▼
                                  ┌───────────────┐        ┌───────────────┐         ┌─────────────────┐
                                  │ query_engine  │        │   insights    │         │ leadership_update│
                                  │ (NL -> plan,  │        │ (BI logic,    │         │ (periodic exec   │
                                  │  Claude API)  │        │  cross-board  │         │  brief, Claude   │
                                  └───────────────┘        │  joins)       │         │  for phrasing)   │
                                          │                └───────┬───────┘         └────────┬─────────┘
                                          │                        │                          │
                                          ▼                        ▼                          ▼
                                                    ┌───────────────────────────┐
                                                    │   monday_mcp_client.py    │
                                                    │   (MCP stdio client ->    │
                                                    │   npx monday-api-mcp)     │
                                                    └─────────────┬─────────────┘
                                                                  │ MCP protocol (stdio)
                                                                  ▼
                                                    ┌───────────────────────────┐
                                                    │  monday.com MCP server    │
                                                    │  (@mondaydotcomorg/       │
                                                    │   monday-api-mcp, npx)    │
                                                    └─────────────┬─────────────┘
                                                                  │ Monday.com GraphQL API
                                                                  ▼
                                                     ┌─────────────────────────┐
                                                     │  Work Orders / Deals    │
                                                     │  boards (your account)  │
                                                     └─────────────────────────┘
```

`data_normalize.py` sits underneath `insights.py` and `leadership_update.py`,
cleaning every raw value (nulls, inconsistent dates, casing typos, the
company-code join, malformed template rows) before any number is computed.
`agent.py` is the conversational orchestrator: it calls `query_engine` to
parse a question into a plan, `insights`/`leadership_update` to compute the
real answer from a live board read, then a second Claude call to phrase the
answer in natural language — the language model is only ever handed numbers
this app already computed, never allowed to invent one.

## Setup — Monday.com side (from scratch)

1. In Monday.com, import your two CSVs as separate boards named exactly
   **"Work Orders"** and **"Deals"** (Board menu → Import data → Excel/CSV).
   The app looks boards up by name at query time.
2. Get a personal API token: click your avatar (bottom-left) → **Developers**
   → **My Access Tokens** → copy it. This is `MONDAY_API_TOKEN`.
3. No further Monday.com-side configuration is needed — the app discovers
   both boards' schemas live via the MCP server, it isn't hardcoded here.

## Setup — local

Requirements: Python 3.11+, Node.js 20+ (the MCP server runs via `npx`).

```bash
git clone <this-repo>
cd skylark-bi-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`:
- `MONDAY_API_TOKEN` — from step 2 above.
- An LLM key, used for query understanding and phrasing answers. `LLM_PROVIDER`
  selects which: `groq` (default here — get a free key from
  [console.groq.com](https://console.groq.com), set `GROQ_API_KEY`) or
  `anthropic` (get a key from [console.anthropic.com](https://console.anthropic.com),
  set `ANTHROPIC_API_KEY`). Both are supported behind `src/llm_provider.py` —
  see DECISION_LOG.md for why.

## Running locally

```bash
streamlit run app.py
```

Open the URL Streamlit prints (default `http://localhost:8501`).

## Running the tests

```bash
python -m pytest tests/ -v
```

Every test in `tests/test_normalize.py` is anchored to a real data quirk
found in the actual Work Orders / Deals boards during development (a real
casing typo, real duplicate-titled columns, real malformed rows) — see
DECISION_LOG.md for how each was found.

## Deployment

**Render**, via the included `Dockerfile` and `render.yaml`. Why not
Streamlit Community Cloud: this app spawns the Monday.com MCP server as a
Node.js subprocess (`npx @mondaydotcomorg/monday-api-mcp`) on every board
read, and Streamlit Community Cloud's runtime is Python-only — there's no
reliable way to get Node.js into that environment. Render (or Railway) lets
you ship a Dockerfile with both runtimes, which this integration requires,
not just prefers.

To deploy:
1. Push this repo to GitHub (public, or shared with the reviewer).
2. In Render: **New → Blueprint**, point it at the repo — `render.yaml` is
   picked up automatically and provisions a Docker web service.
3. In the Render dashboard, set the secret environment variables
   (`MONDAY_API_TOKEN`, plus `GROQ_API_KEY` or `ANTHROPIC_API_KEY` matching
   `LLM_PROVIDER`) — `render.yaml` marks the LLM keys `sync: false` so they're
   never committed to the repo.
4. Deploy. Render builds the Dockerfile (installs Python deps + Node.js) and
   starts `streamlit run app.py` bound to Render's `$PORT`.

The deployed app authenticates to Monday.com and the LLM provider
independently at runtime from those dashboard secrets — it does not depend on
this local dev environment or any session-specific configuration.

## Project structure

```
skylark-bi-agent/
├── README.md
├── DECISION_LOG.md
├── requirements.txt
├── Dockerfile / .dockerignore / render.yaml
├── .env.example / .gitignore / .mcp.json
├── src/
│   ├── monday_mcp_client.py   # MCP stdio client + typed error handling
│   ├── data_normalize.py      # null/date/casing/join normalization (tested)
│   ├── query_engine.py        # NL question -> structured QueryPlan
│   ├── insights.py            # BI computations over live board data
│   ├── leadership_update.py   # periodic executive brief
│   └── agent.py                # conversational orchestrator, multi-turn state
├── app.py                      # Streamlit UI
└── tests/
    ├── test_normalize.py
    ├── test_query_engine.py
    └── test_insights.py
```

# PRD — Odysseus Stock Research Capability ("stock-research")

| | |
|---|---|
| Status | v2.0 |
| Date | 2026-06-11 |
| Owner | Edwin |
| Repo (planned) | `Projects/Ai/stock-research` (standalone; integrates with Odysseus via the capability contract in `docs/capabilities.md`) |
| Source verification | Every external API below was checked against live documentation on 2026-06-11. Anything unverifiable is listed under Known Gaps, not assumed. |

---

## 1. Background

Edwin invests through Stake (with a Stake Black subscription) by following news. He wants what professional investors actually have: a research process — systematic data collection, fundamental analysis, factor signals, event monitoring, and sentiment tracking — run automatically by AI, producing a report **every day** plus **on-demand** deep dives, so that buy/sell/hold decisions are made on evidence rather than headlines.

This PRD specifies that capability end to end: the complete verified data layer (free/open-source first), the storage model, the indexing/search/retrieval algorithms, the analytical signal engine, and the report and integration contracts. The reader is assumed to be new to trading; the system itself encodes the expertise.

A note on realism: no data pipeline beats the market by itself. What professionals have that retail investors lack is *process*: complete information, point-in-time discipline, written theses, and risk awareness. That is what this system delivers. Reports are decision support with provenance — never personalized financial advice (see `docs/capabilities.md` § Financial analysis).

## 2. Goals

- **G1 — Complete data picture.** For every ticker in the portfolio and watchlist (≤50 names), ingest fundamentals, filings, prices, insider activity, institutional holdings, short interest, news, social sentiment, macro context, and sector-specific data (patents, trials, government contracts) from verified free sources.
- **G2 — Daily brief + on-demand research.** A scheduled daily report (what changed, alerts, signal moves) and an on-demand mode for deep dives on any ticker, both through Odysseus.
- **G3 — Ask-anything corpus.** All ingested text and numbers are queryable by the Odysseus agent ("compare AMD and NVDA gross margins over 8 quarters", "what changed in NVDA's risk factors?").
- **G4 — $0 mandatory cost.** Paid sources only as optional, clearly-marked upgrades.
- **G5 — Beginner-aware output.** Every report explains its terms (inline glossary), states what a signal means and what would invalidate it.
- **G6 — Honest engineering.** Point-in-time data discipline (no lookahead), idempotent ingestion, graceful degradation when fragile sources fail, full provenance in every report.

### Non-goals

- Trade execution or order routing (Stake stays manual).
- Intraday/real-time signals; cadence is daily and weekly.
- Predicting prices. The system ranks, flags, and explains; it does not promise returns.
- Non-US listings in v1 (the design must not preclude ASX later).

## 3. Operating modes

| Mode | Trigger | Scope | Latency target |
|---|---|---|---|
| **Daily brief** | Odysseus scheduler (pre-market, e.g. 07:00 AEST ≈ US after-close) | All tickers: incremental ingest + signal refresh + report | ≤ 20 min |
| **Weekly deep run** | Scheduler (weekend) | Adds 13F refresh, patents, trends, full factor recompute, portfolio risk review | ≤ 45 min |
| **On-demand deep dive** | Agent/REST: `run` with `tickers=XYZ, mode=deep` | One or few tickers: full ingest + 5y history + full report section | ≤ 5 min/ticker |
| **Ad-hoc Q&A** | Odysseus agent queries the corpus API/MCP directly | Retrieval only, no ingest | seconds |

## 4. The research framework the system encodes

Professional equity research is a mosaic. Each subsystem below maps to a standard institutional practice:

1. **Fundamental analysis** (what the business earns and owes) → SEC XBRL facts, ratio analysis, quality scores.
2. **Factor investing** (what historically rewards holders: value, quality, momentum) → the signal engine's factor z-scores, the same families used by quant funds (Fama-French style).
3. **Event-driven monitoring** (8-Ks, earnings, auditor changes, guidance) → filing watchers + classifiers.
4. **Flow analysis** (what informed parties do: insiders, institutions, shorts) → Form 4, 13F, FINRA short interest.
5. **Sentiment & narrative** (what the crowd believes, and when it shifts) → news tone, Reddit/StockTwits deltas.
6. **Macro overlay** (the tide all boats float on) → rates, inflation, yield curve, recession-odds markets.
7. **Risk management** (the part amateurs skip) → concentration, correlation, drawdown stats, invalidation conditions per thesis.

## 5. Architecture

```text
+----------------------------------------------------------------------+
| stock-research worker  (FastAPI; implements Odysseus /v1/runs)       |
|                                                                      |
|  INGESTION LAYER                                                     |
|   one fetcher module per source; independent failure; all writes     |
|   idempotent upserts keyed on natural keys                           |
|        |                        |                                    |
|        v                        v                                    |
|  STRUCTURED STORE          TEXT CORPUS                               |
|  SQLite + Parquet          ChromaDB (vectors) + BM25 index           |
|  (facts, prices, flows)    (filing sections, news, PRs)              |
|        |                        |                                    |
|        v                        v                                    |
|  SIGNAL ENGINE             RETRIEVAL SERVICE                         |
|  deterministic, versioned  hybrid search + RRF + rerank              |
|  factor scores, events     (REST + MCP for the Odysseus agent)       |
|        \                       /                                     |
|         v                     v                                      |
|        SYNTHESIS (local LLM via Ollama)                              |
|        numbers come from the store, never from the model             |
|                |                                                     |
|                v                                                     |
|        Markdown report -> result contract -> Odysseus Documents      |
+----------------------------------------------------------------------+
```

Design decisions:

1. **OpenBB Platform as the structured-data access layer.** `pip install openbb` (AGPLv3 — acceptable: personal use, not redistributed as a service). One Python interface to 100+ providers; provider chosen per call. v1 uses only keyless/free providers (`yfinance`, `sec`, `cboe`, `federal_reserve`, `finra`). A paid provider later (FMP, Polygon) is a config change. OpenBB also ships a REST server (`openbb-api`) and an MCP server — a second, ad-hoc data path for the agent.
2. **SEC EDGAR is ground truth.** Yahoo is convenience; XBRL from `data.sec.gov` is authoritative. Disagreements are flagged, EDGAR wins.
3. **Two stores, by data nature.** Numbers live in SQLite/Parquet and are answered by SQL. Prose lives in the vector+keyword corpus and is answered by retrieval. Numeric questions never go through embeddings — this single rule eliminates the main source of RAG hallucination in finance.
4. **Local LLM via Ollama** for synthesis (consistent with the rest of Odysseus; private; free). The semantic-retrieval approach is informed by the hybrid-search design proven in `D:\Projects\Ai\DualSourceRag` (vector + BM25 + RRF + cross-encoder), but stock-research implements its own finance-specific pipeline — no code dependency on that repo.

## 6. Data layer — verified source catalog

Reliability classes: **A** official & stable · **B** official but registration/file-based · **C** unofficial/fragile (must degrade gracefully, never block a run).

### 6.1 Prices & market structure

| # | Data | Access (verified) | Auth | Limits | Cost | Class |
|---|---|---|---|---|---|---|
| 1 | EOD/intraday OHLCV, splits, dividends, options chains, analyst snapshot | `yfinance` (via OpenBB or direct) | none | unofficial; self-throttle ≤2 req/s, cache | $0 | C |
| 2 | EOD OHLCV backup | Stooq `https://stooq.com/q/d/l/?s={sym}.us&i=d` (CSV) | none | be polite | $0 | C |
| 3 | Delayed quotes, options, index data | Cboe via OpenBB `provider="cboe"` | none | low volume only | $0 | B |
| 4 | Daily short-sale volume per symbol | FINRA Query API `POST https://api.finra.org/data/group/otcMarket/name/regShoDaily` | free credential ([developer.finra.org](https://developer.finra.org)) | per-credential throttle | $0 | A |
| 5 | Short interest positions (days-to-cover, % float inputs) | FINRA `.../name/EquityShortInterest` | same | same; data twice-monthly | $0 | A |

### 6.2 Fundamentals & filings — SEC EDGAR

All EDGAR endpoints: **no key**, mandatory `User-Agent: "name email"`, hard cap **10 req/s**; bulk files for backfill.

| # | Data | Endpoint |
|---|---|---|
| 6 | All XBRL facts per company (every reported line item, every period) | `https://data.sec.gov/api/xbrl/companyfacts/CIK{10d}.json`; backfill: bulk `companyfacts.zip` (nightly refreshed) |
| 7 | One concept across companies/periods | `https://data.sec.gov/api/xbrl/companyconcept/CIK{10d}/us-gaap/{Tag}.json` |
| 8 | Filing index per company (form, accession, date) | `https://data.sec.gov/submissions/CIK{10d}.json` — poll daily |
| 9 | Filing documents (10-K/Q, 8-K, DEF 14A full text) | `https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/…` → parse → corpus |
| 10 | Full-text search, all filings since 2001 | `https://efts.sec.gov/LATEST/search-index?q=…&forms=…&dateRange=…` |
| 11 | Insider trades — Forms 3/4/5 (XML) | via #8 + Archives; parse to rows (insider, role, code, shares, price) |
| 12 | Institutional holdings — 13F-HR (XML) | via #8; quarterly |
| 13 | Ticker↔CIK map | `https://www.sec.gov/files/company_tickers.json` (weekly refresh) |

### 6.3 Macro & rates

| # | Data | Access | Auth | Limits | Cost | Class |
|---|---|---|---|---|---|---|
| 14 | FRED: 800k+ series (rates, CPI, GDP, yield curve, NFCI) | `https://api.stlouisfed.org/fred/series/observations` | free key | 120 req/min | $0 | A |
| 15 | BLS: employment, wages, CPI detail | `https://api.bls.gov/publicAPI/v2/timeseries/data/` | free key | 500 req/day | $0 | A |
| 16 | Treasury: yields, auctions, debt | `https://api.fiscaldata.treasury.gov/services/api/fiscal_service/…` | none | generous | $0 | A |

### 6.4 News & sentiment

| # | Data | Access | Auth | Limits | Cost | Class |
|---|---|---|---|---|---|---|
| 17 | Company news per ticker (1y history on free tier) | Finnhub `GET /api/v1/company-news` | free key | 60 calls/min | $0 | B |
| 18 | Global news + tone scores | GDELT 2.0 DOC API `https://api.gdeltproject.org/api/v2/doc/doc` | none | be polite | $0 | B |
| 19 | Press releases | GlobeNewswire/BusinessWire RSS per company | none | n/a | $0 | B |
| 20 | Reddit (r/stocks, r/investing, r/wallstreetbets, r/ValueInvesting, ticker subs) | Reddit OAuth API (PRAW) — same approach as painminer | free app creds | 100 QPM | $0 | B |
| 21 | StockTwits symbol streams | `https://api.stocktwits.com/api/2/streams/symbol/{SYM}.json` — public/unauthenticated; **new developer registration currently closed**, so treat as bonus | none | undocumented | $0 | C |
| 22 | Google search interest | `pytrends` (unofficial) | none | fragile; weekly | $0 | C |

### 6.5 Specialized & event data

| # | Data | Access | Auth | Limits | Cost | Class |
|---|---|---|---|---|---|---|
| 23 | US patents (moat/R&D tracking) | PatentsView PatentSearch `https://search.patentsview.org/api/v1/patent/` | free key | 45 req/min | $0 | A |
| 24 | Clinical trials (biotech names) | ClinicalTrials.gov v2 `https://clinicaltrials.gov/api/v2/studies` | none | generous | $0 | A |
| 25 | FDA approvals/labels | openFDA `https://api.fda.gov/drug/…` | optional free key | 240 req/min keyed | $0 | A |
| 26 | Prediction markets (recession/rate/event odds) | Polymarket Gamma `https://gamma-api.polymarket.com/markets` (no key); Kalshi `https://api.elections.kalshi.com/trade-api/v2/…` public read endpoints (no auth, ~30 req/s) | none | generous | $0 | B |
| 27 | Federal contract awards to holdings | USAspending `https://api.usaspending.gov/api/v2/…` | none | generous | $0 | A |
| 28 | Congress stock trades | Senate eFD / House Clerk disclosure scrape, or community mirrors | none | fragile | $0 | C |

### 6.6 Portfolio sync (Stake)

Stake exposes **no public developer API**. Stake Black's in-app extras (full market depth, course of sales, analyst ratings) are human-eyes-only and are not pipeline inputs — keep the subscription for execution and verification, not data.

| Option | Method | Status |
|---|---|---|
| Preferred | **SnapTrade** free tier — verified: 1 connected user, up to 5 brokerage connections, read-only, for personal use; Stake AUS is a supported integration | $0 |
| Fallback A | `stake-python` (unofficial client) | Class C |
| Fallback B | Hand-maintained `portfolio.yaml` (+ CSV export from Stake statements) | always works |

`portfolio.yaml`: `{ticker, qty, avg_cost, currency, opened_date, thesis}` · `watchlist.yaml`: `{ticker, thesis, added_date}`. The YAML file is always authoritative; sync only proposes updates.

### 6.7 Optional paid upgrades (NOT required)

| Provider | What it adds | Cost |
|---|---|---|
| FMP Starter | Earnings-call transcripts, analyst-estimate history, pre-parsed insider/13F | ~US$25–30/mo |
| Polygon Starter | Institutional-grade price history if yfinance fragility hurts | US$29/mo |
| Valyu (pay-per-query) | Hosted semantic SEC/web search instead of maintaining our own | per query |

### 6.8 Known gaps in the all-free stack (stated, not assumed away)

1. **Earnings-call transcripts** — no reliable free API exists. Mitigation: 8-K earnings exhibits + press releases carry the numbers and guidance; transcripts are the first paid upgrade if wanted. (Public transcript sites prohibit scraping; excluded.)
2. **Analyst-estimate history** — free sources give current snapshots only. We store our own daily snapshots and compute revisions from them (correct going forward; no history before day one).
3. **Real-time prices** — everything is EOD/delayed, by design.
4. **Class C sources** (yfinance, Stooq, StockTwits, pytrends, congress scrape) may break at any time; a run must still complete, with per-source `warnings` in the result.

## 7. Storage design

### 7.1 Structured store (SQLite + Parquet)

SQLite `data/stock_research.db`:

```text
tickers(ticker PK, cik, name, sector, industry, added_at)
xbrl_facts(cik, tag, unit, period_end, fy, fp, value, accession, filed_at)
            PK(cik, tag, unit, period_end, accession)
filings(accession PK, cik, form, filed_at, period, url, parsed_ok)
insider_tx(accession, insider_cik, name, role, code, shares, price, value, tx_date)
inst_holdings(fund_cik, ticker_cusip, period, shares, value)   -- from 13F
short_interest(ticker, settlement_date, si_shares, avg_daily_vol, days_to_cover)
short_volume(ticker, date, short_vol, total_vol)
estimates_snapshot(ticker, snap_date, eps_fy1, eps_fy2, rev_fy1, n_analysts, mean_target)
news(id PK, ticker, source, published_at, title, url, tone)
signals(ticker, run_date, signal_name, value, pctile_5y, confidence, algo_version)
runs(run_id PK, started_at, mode, status, sources_ok, sources_failed, warnings_json)
```

Parquet sidecar `data/prices/{ticker}.parquet`: `date, open, high, low, close, adj_close, volume` — columnar, fast for factor math, trivially rebuildable.

Rules:

- **Idempotent upserts** on natural keys (accession, settlement_date, …). Amendments (10-K/A) overwrite the original's rows — the record corrects, never duplicates.
- **Point-in-time discipline**: every fact stores `filed_at`/`snap_date` so any computation can be replayed "as known on date X" — the discipline that prevents lookahead bias, and what separates honest backtests from fantasy.

### 7.2 Text corpus (ChromaDB + BM25)

- One Chroma collection, metadata-namespaced per ticker plus a `macro` namespace.
- Documents: filing **sections** (not whole filings), news articles, press releases.
- Chunk IDs are deterministic: `sha1(accession|item|seq)` — re-ingestion upserts, amendments replace.
- A parallel BM25 index (`rank_bm25`, pickled per namespace, rebuilt incrementally on ingest) for exact-term matching.

## 8. Indexing, search & retrieval — algorithms

### 8.1 Document processing (indexing)

**Filing parsers, per form type.** A 10-K is parsed into its real items (1 Business, 1A Risk Factors, 3 Legal, 7 MD&A, 7A Market Risk, 8 Financial Statements…), a 10-Q into its quarterly items, an 8-K into its numbered event items (2.02 results, 4.02 non-reliance, 5.02 officer changes…). Implementation: strip inline-XBRL tags → normalize whitespace → locate item headings with per-form regex sets hardened against the usual variants (bold spans, nbsp padding, numbered/unnumbered) → fall back to "whole document, item=unknown, warn" when detection confidence is low. Golden-file tests pin parser behavior on a fixture set of real filings.

**Chunking.** Header-aware, size-bounded: split each section on its internal sub-headings, then pack to **300–800 tokens with 60-token overlap**, never crossing a section boundary. Each chunk carries `{ticker, cik, form, accession, filing_date, period, item, title, url}`.

**Embeddings.** Local, via sentence-transformers: default `BAAI/bge-base-en-v1.5` (strong, small, runs on CPU); evaluate `nomic-embed-text` (Ollama-native) and a finance-tuned variant in P2 on a 50-question eval set built from real portfolio questions. Cosine similarity, HNSW index (Chroma default).

### 8.2 Query pipeline (search & retrieval)

```text
question
  -> 1. QUERY UNDERSTANDING   extract {tickers, form types, date range, item}
  -> 2. ROUTE                 numeric? -> SQL on structured store
                              textual? -> corpus retrieval
  -> 3. SCOPED HYBRID SEARCH  metadata filters first, then:
         vector top-k (k=20)  +  BM25 top-k (k=20)
  -> 4. FUSION                Reciprocal Rank Fusion:
         RRF(d) = Σ_r 1/(60 + rank_r(d))
  -> 5. RERANK                cross-encoder (BAAI/bge-reranker-base) on top 20
  -> 6. ASSEMBLE              dedupe by (accession,item); top 5-8 chunks
                              + their metadata + EDGAR/source links
  -> 7. GENERATE              local LLM; citations mandatory; numbers only
                              from SQL results included in context
```

Step 1 is cheap and deterministic where possible: ticker/alias dictionary from #13, form-type and item keyword maps ("risk factors"→Item 1A, "MD&A"→Item 7), date phrases via `dateparser`; a small local LLM call only when regex extraction is ambiguous. Step 2's routing rule: if the question asks for a quantity, ratio, or comparison of figures, answer from SQL and let the LLM only select and phrase. Scoped filtering before vector search (step 3) is what keeps retrieval precise when fifty companies all have "risk factors" — the same principle Valyu's SEC index uses.

### 8.3 Diff analysis (what changed)

For "what changed in the latest 10-K": align sections by item across consecutive filings, sentence-split, compute embedding-similarity alignment, and surface sentences with no close match in the prior filing (new language) plus removed ones. New risk-factor language is one of the highest-signal, lowest-noise disclosures a filer makes; this is a first-class feature, not an LLM afterthought.

## 9. Signal & strategy engine

Deterministic, unit-tested, versioned (`algo_version` stamped in every row and report). Each signal outputs `{value, 5y-percentile vs own history, cross-sectional z-score vs portfolio+watchlist universe, confidence, data_timestamp, provider}`. The LLM interprets signals; it never computes them.

### 9.1 Valuation (value factor)

P/E (ttm), EV/EBITDA, P/FCF, P/S, earnings yield — each vs the stock's own 5-year distribution (percentile) and vs universe (z-score). Inputs: XBRL facts (#6) + prices (#1). A "cheap vs itself, cheap vs peers" two-axis read, the standard value screen.

### 9.2 Quality

- **Piotroski F-Score (0–9)**: one point each for ROA>0, CFO>0, ΔROA>0, CFO>net income (accruals check), Δleverage<0, Δcurrent ratio>0, no new shares issued, Δgross margin>0, Δasset turnover>0. Classic balance-sheet-health screen, fully computable from XBRL.
- **Altman Z-Score**: `1.2·WC/TA + 1.4·RE/TA + 3.3·EBIT/TA + 0.6·MktCap/TL + 1.0·Sales/TA` (distress < 1.8; safe > 3.0).
- Margin trends (gross/operating, 8-quarter slope), ROIC, net-debt/EBITDA, share-count trend (dilution flag), accruals ratio.

### 9.3 Momentum & technicals

12-1 month momentum (12m return excluding the most recent month — the academically standard formulation), 3/6m relative strength vs SPY, price vs 50/200-day SMA (and crossovers), 14-day RSI, realized volatility (20/60d annualized), max drawdown 1y. Computed with pandas from Parquet; no TA library dependency needed beyond `pandas`/`numpy`.

### 9.4 Flow (informed-party behavior)

- **Insider cluster rule**: ≥3 distinct insiders buying on the open market (code "P") within 30 days = strong flag; CEO/CFO purchases weighted 2×; routine 10b5-1 sales discounted. Cluster buying is one of the best-documented insider signals.
- 13F: holder-count delta and aggregate share delta QoQ; new positions/exits among tracked funds.
- Short interest: % of float, days-to-cover, and their deltas; daily short-volume ratio trend (#4) as the higher-frequency proxy between FINRA settlement dates.

### 9.5 Sentiment & narrative

Per ticker: Reddit mention volume and net sentiment (VADER baseline; upgrade to a local finance-tuned classifier in P4), each as a z-score vs the ticker's trailing 90-day distribution — *changes* matter, levels don't. News volume + GDELT tone delta. Divergence flag: price up + sentiment down (or vice versa) for >2 weeks.

### 9.6 Events

8-K item classifier (rule-based on item numbers — they are self-describing): 2.02 earnings, 4.02 non-reliance (**always an alert**), 5.02 executive departure, 1.01 material agreements, 7.01/8.01 other disclosures. Plus: new 10-K risk-factor diff (§8.3), auditor change, dividend change, going-concern phrase via full-text search (#10).

### 9.7 Composite & portfolio risk

- Per-ticker composite: equal-weight average of the four factor-family z-scores (value, quality, momentum, flow), sentiment shown alongside but not averaged in (it's a timing overlay, not a quality measure). No black-box ML in v1 — every score must be explainable in one sentence.
- Portfolio: weight concentration (HHI), sector exposure vs S&P sector weights, pairwise 90-day return correlation matrix, portfolio beta vs SPY, aggregate drawdown stats, "single-thesis risk" callout when >N% of value shares one driver.
- Every flagged idea carries an **invalidation condition** ("thesis breaks if gross margin < X% next quarter") — written into the report, the habit that most distinguishes professional research.

### 9.8 What is deliberately excluded

Price-prediction models, intraday signals, options-flow "unusual activity" hype, and any ML model that cannot explain its output. They are excluded for cause: they are where retail quant projects fail and where data costs explode.

## 10. Reports

### 10.1 Daily brief (scheduled)

1. **Header dashboard** — table: ticker, price, 1d/1w change, composite score, score delta, alert flags.
2. **Alerts** — events firing since last run (8-K 4.02, insider cluster, short-interest spike, sentiment divergence, signal threshold crossings), each with one-paragraph explanation + source link.
3. **What changed** — per ticker with activity only: new filings (1-line LLM summaries with links), notable news, sentiment moves.
4. **Macro snapshot** — yield curve, CPI/rates trajectory, recession odds (Polymarket/Kalshi), one-paragraph read.
5. **Watchlist** — entry-point commentary: valuation percentile vs 5y, momentum state.
6. **Glossary footnotes** — every term (F-score, days-to-cover, …) footnoted with a one-line plain-English explanation. Beginner mode is the default.
7. **Provenance appendix** — per data class: provider, timestamp, staleness warnings; algorithm version; failed sources.

### 10.2 On-demand deep dive (per ticker)

Business summary (from Item 1) → 5y financial history tables and trend commentary → full factor card → flow history → filing timeline with diffs → news/sentiment narrative → bull/bear case (LLM, each point cited) → invalidation conditions → provenance.

### 10.3 Result contract

```json
{
  "summary": "12 holdings, 6 watchlist. 2 alerts: NVDA insider cluster buy; SMCI 8-K item 4.02.",
  "report": {"title": "Daily Brief — 2026-06-12", "format": "markdown", "content": "..."},
  "metrics": {"tickers": 18, "filings_ingested": 7, "sources_ok": 15, "sources_failed": 1},
  "warnings": ["stocktwits: HTTP 403 — sentiment partial"]
}
```

## 11. Odysseus integration

Registry entry (`data/capabilities.yaml`):

```yaml
- id: stock-research
  name: Stock Research
  description: Daily brief and on-demand deep dives over portfolio and watchlist.
  admin_only: true
  timeout_seconds: 2700
  import_report: true
  transport:
    type: http
    base_url: http://stock-research-capability:8080
    token_env: STOCK_RESEARCH_CAPABILITY_TOKEN
  inputs:
    mode: {type: string, default: daily, choices: [daily, weekly, deep]}
    tickers: {type: string, description: "Comma list for deep mode; default = portfolio + watchlist"}
    include_sentiment: {type: boolean, default: true}
```

- Worker implements `POST /v1/runs`, `GET /v1/runs/{id}`, `GET /v1/runs/{id}/log`, `DELETE /v1/runs/{id}` (Bearer auth) per `docs/capabilities.md`; reports import into Documents.
- Daily/weekly scheduling via Odysseus scheduled tasks; on-demand via the agent's `manage_capabilities run`.
- The retrieval service (§8.2) is additionally exposed as REST + MCP so the agent answers ad-hoc questions without a full run.
- Docker: own image (own dependency set: chroma, sentence-transformers, openbb), Compose service `stock-research-capability`, no published host port, token in `.env` — same isolation model as the pain-miner overlay.

## 12. Run budget (50 tickers)

| Step | Calls | Constraint | Time |
|---|---|---|---|
| EDGAR submissions poll | 50 | 10 req/s cap | <1 min |
| New filing fetch+parse | ~5–20/day typical | same | 1–3 min |
| yfinance prices | 50 | self-throttle 2 req/s | ~1 min |
| Finnhub news | 50 | 60/min | ~1 min |
| FRED/BLS/Treasury | ~30 series | 120/min, 500/day | <1 min |
| FINRA short data | 2 dataset queries | credential throttle | <1 min |
| Reddit | ~10 subreddit sweeps | 100 QPM | 2–4 min |
| Embedding + indexing | new chunks only | local CPU/GPU | 2–5 min |
| Signals + report + LLM | local | — | 3–6 min |

Daily ≈ 10–20 min; weekly (adds 13F, patents @45/min, trends) ≈ 30–45 min. All within `timeout_seconds: 2700`. Every limit above has ≥5× headroom at 50 tickers; the design scales to ~200 tickers before any limit binds (Reddit first).

## 13. Costs

| Item | Cost |
|---|---|
| All mandatory data sources | **$0** |
| SnapTrade holdings sync (free tier) | $0 |
| LLM + embeddings (local Ollama / sentence-transformers) | $0 (hardware already owned) |
| Stake Black (kept for execution UX, not data) | already paid |
| Optional: FMP Starter (transcripts, estimate history) | ~US$25–30/mo if ever wanted |

## 14. Phases & acceptance criteria

- **P1 — Structured ingestion** (EDGAR XBRL+filings index, yfinance+Stooq, FRED, FINRA). AC: any S&P 500 ticker → 5y fundamentals, filing index, prices in store; re-run produces zero duplicate rows; EDGAR throttle respected under test.
- **P2 — Corpus & retrieval** (parsers, chunker, embeddings, hybrid search, rerank, diff). AC: 50-question eval ≥80% correct-citation rate; "risk factors that changed" correct on 5 test tickers; numeric questions routed to SQL 100% of the time.
- **P3 — Signals & daily report.** AC: full daily brief for the real portfolio with provenance appendix and glossary; one Class-C source deliberately broken → run succeeds with warning; F-score/Z-score verified against hand-computed fixtures.
- **P4 — Sentiment, events, holdings sync.** AC: Reddit/news z-scores appear; 8-K alert fires within one daily cycle of filing; SnapTrade (or fallback) round-trips holdings.
- **P5 — Odysseus integration.** AC: registry live; daily schedule runs unattended for a week; deep-dive on-demand ≤5 min; agent answers the §2 example questions via MCP.

## 15. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| yfinance breakage | price/estimate gaps | Stooq fallback; OpenBB provider swap is one line; warn-and-continue |
| EDGAR parser edge cases (old/odd filers) | bad chunks | per-form parsers + golden-file tests; skip-and-warn |
| StockTwits/pytrends disappear | minor sentiment loss | Class C by design; Reddit is primary |
| OpenBB AGPLv3 | license obligations | personal use, not redistributed as a hosted service |
| SnapTrade↔Stake quirks | sync fails | YAML portfolio always authoritative |
| LLM hallucinating figures | wrong decisions | numbers only from SQL; mandatory citations; report links every claim |
| Over-trust by a new investor | financial harm | beginner glossary, confidence fields, invalidation conditions, explicit "decision support, not advice" banner on every report |

## 16. Open questions

1. Daily report delivery: Documents import only, or also an Odysseus notification with the alert summary?
2. Watchlist ingest depth: full pipeline or lite (skip 13F/patents) to cut runtime?
3. Embedding model final choice: decide in P2 via the eval set (bge-base vs nomic-embed vs finance-tuned).
4. Alert severity thresholds (e.g., short-interest delta that warrants an alert): tune during P3 with real data.

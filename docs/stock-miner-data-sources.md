# Stock Miner — capability plan & data source map

A new Odysseus capability (working name: `stock-miner`) for analysing held and watched US stocks, built the same way as `pain-miner`: a sibling repo at `Projects/Ai/stock-miner`, run as an isolated HTTP worker, emitting the standard result contract with a markdown report imported into Documents.

Constraints: US equities only, data budget ≤ ~US$50/month.

## How this fits Odysseus

Follows `docs/capabilities.md` exactly:

```text
Projects/Ai/
  odysseus/
  pain-miner/
  stock-miner/    <- new repo
```

One stable CLI command does the full job and prints the result contract as its last stdout line:

```text
stockminer run --portfolio portfolio.yaml --watchlist watchlist.yaml --since 7d
```

Registry entry (in `data/capabilities.yaml`):

```yaml
- id: stock-miner
  name: Stock Miner
  description: Weekly analysis of held and watched US stocks.
  admin_only: true
  timeout_seconds: 1800
  import_report: true
  transport:
    type: http
    base_url: http://stock-miner-capability:8080
    token_env: STOCK_MINER_CAPABILITY_TOKEN
  inputs:
    since:
      type: string
      default: 7d
    include_news:
      type: boolean
      default: true
```

Per the "Financial analysis" section of `docs/capabilities.md`, every report must carry provenance: data timestamps and providers, algorithm version, signal strength/confidence, position sizing context, invalidation conditions, and warnings for stale or missing data. Decision support, not financial advice.

## Pipeline (mirrors pain-miner's fetch → cluster → digest)

1. **Ingest** — pull per-ticker data from the sources below into a local store (SQLite/parquet). Each fetcher independent; partial failure still produces a report.
2. **Signals** — deterministic scoring in code, not LLM: valuation vs history and sector (P/E, EV/EBITDA, FCF yield), momentum/trend, earnings revision direction, insider buying/selling balance, short-interest change, sentiment delta vs prior week, filing red-flags (new risk factors, going-concern language, auditor changes).
3. **LLM synthesis** — per-ticker brief citing the underlying numbers, plus portfolio rollup (concentration, sector exposure, correlated risks) and watchlist entry-point commentary.
4. **Report** — one markdown document: portfolio summary → per-holding sections → watchlist section → data provenance appendix.

## Data source map

### Tier 1 — free, official, build on these first

| Data | Source | Notes |
|---|---|---|
| SEC filings (10-K/Q, 8-K full text) | [EDGAR full-text search API](https://efts.sec.gov/LATEST/search-index?q=) + `data.sec.gov` | Free, no key. ~10 req/s limit, set a User-Agent. This is the same raw source Valyu indexes. |
| Financial statements (XBRL, structured) | `data.sec.gov/api/xbrl/companyfacts` | Every line item, every period, free. Best-quality fundamentals anywhere. |
| Insider trades (Form 4) | EDGAR | Free; parse the XML form index. |
| Institutional holdings (13F) | EDGAR | Free; quarterly. |
| Macro (rates, CPI, GDP, yield curve) | [FRED API](https://fred.stlouisfed.org/docs/api/fred/) | Free key, 800k+ series. |
| Employment/wages | BLS API | Free. |
| Short interest | FINRA (free file, twice monthly) | |
| Patents (for tech/biotech theses) | [USPTO PatentsView API](https://patentsview.org/apis/api-endpoints) | Free. Same source as Valyu's patent dataset. |
| Clinical trials (biotech holdings) | ClinicalTrials.gov API v2 | Free. |
| Prediction markets | Polymarket + Kalshi public APIs | Free; macro/event probabilities. |
| Prices/quotes (fallback) | `yfinance` | Free but unofficial — fine as backup, don't make it the backbone. |

### Tier 2 — one paid provider (the ~$25–30 slot)

**Recommended: [Financial Modeling Prep](https://site.financialmodelingprep.com/pricing-plans) Starter (~US$25–30/mo).** One key covers: EOD + delayed intraday prices, 30y fundamentals + precomputed ratios, analyst estimates & price targets, **earnings call transcripts**, earnings calendar, insider trades (pre-parsed), stock news, ETF/fund holdings. Transcripts and pre-parsed convenience are the things that are painful to get free.

Alternatives in the same slot:
- [EODHD](https://eodhd.com/) €19.99 EOD All-World — better if you later add ASX/global; fundamentals cost extra (€59.99 tier).
- [Polygon.io](https://polygon.io/pricing) Starter $29 — best-in-class price data, weaker fundamentals. Choose only if you later want minute bars/options.
- Tiingo $10 — cheap solid EOD + news, thinner fundamentals.

### Tier 3 — sentiment & alternative data (free, reuses painminer infra)

| Data | Source | Notes |
|---|---|---|
| Reddit sentiment | Existing painminer Reddit pipeline, pointed at r/stocks, r/investing, r/wallstreetbets, r/ValueInvesting, ticker subs | Biggest reuse win — mention volume + sentiment delta per ticker. |
| Social finance chatter | StockTwits public API | Free, per-symbol streams with bull/bear tags. |
| News firehose | Finnhub (free tier), GDELT (free), RSS (PRNewswire/BusinessWire per ticker) | |
| Search interest | Google Trends via `pytrends` | Free, unofficial. |
| Congress trading | Senate/House financial disclosures (free, scrape) or QuiverQuant API (paid, later) | |

### Optional — semantic search layer

Instead of rebuilding Valyu's hard part (section-level semantic search over filings), you can call [Valyu's API](https://docs.valyu.ai/concepts/data-coverage) pay-per-query for just `valyu/valyu-sec-filings` questions ("new risk factors in X's latest 10-K"), keeping everything else on the free/paid stack above. Or build a lighter local version: pull the specific filing sections from EDGAR and embed into the existing Odysseus chroma store.

## Where Stake Black fits

Stake has **no public developer API**, so Stake Black's data (market depth, course of sales, analyst ratings) is human-eyes-only — it cannot feed the pipeline. Its value here:

1. **Holdings sync** — via [SnapTrade's Stake integration](https://snaptrade.com/brokerage-integrations/stake-aus-api) (free dev tier) or the unofficial [stake-python](https://github.com/stabacco/stake-python) client, so `portfolio.yaml` updates itself. Fallback: manual CSV export.
2. **Execution & verification** — you act on reports and sanity-check live depth in the app.

Keep Stake Black for trading; it neither replaces nor contributes to the data layer.

## Recommended stack (total ≈ US$25–30/mo)

- EDGAR + FRED + FINRA + PatentsView + Polymarket/Kalshi: **$0**
- FMP Starter: **~$25–30**
- Reddit/StockTwits/news sentiment via painminer-style fetchers: **$0**
- Stake holdings via SnapTrade/CSV: **$0**

## Build order

1. Repo scaffold + result contract + worker Dockerfile (copy pain-miner pattern).
2. Fetchers: FMP (prices, fundamentals, estimates, transcripts) → EDGAR (filings, Form 4, 13F) → FRED.
3. Deterministic signal layer + tests.
4. Sentiment fetchers (port from painminer).
5. LLM synthesis + report renderer with provenance appendix.
6. Registry entry, smoke test, then schedule weekly.

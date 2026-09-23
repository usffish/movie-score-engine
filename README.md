# Movie Score Scraper

A Python CLI tool that aggregates film scores from OMDb, Metacritic, and Letterboxd, applies min-max normalisation, and produces a review-count-weighted composite ranking — all written back to Excel without touching the original file.

![Python](https://img.shields.io/badge/Python-3776AB?style=flat-square&logo=python&logoColor=white)
![BeautifulSoup](https://img.shields.io/badge/BeautifulSoup4-3776AB?style=flat-square&logo=python&logoColor=white)
![openpyxl](https://img.shields.io/badge/openpyxl-3776AB?style=flat-square&logo=python&logoColor=white)
![pytest](https://img.shields.io/badge/pytest-0A9EDC?style=flat-square&logo=pytest&logoColor=white)
![Hypothesis](https://img.shields.io/badge/Hypothesis-3776AB?style=flat-square&logo=python&logoColor=white)

---

## What it does

For every title in a personal Movies.xlsx watchlist, the tool:

1. Fetches **Metascore** and **IMDB rating** from the [OMDb API](http://www.omdbapi.com/)
2. Scrapes **critic review count** and **Metascore** from Metacritic:
   - For 4+ reviews: Uses published Metascore
   - For 1-3 reviews: Averages individual critic scores from the reviews page
   - For 0 reviews: Returns `None` (no default value)
3. Scrapes **average community rating** from Letterboxd
4. Checks all three sources matched the **same film** by release year, and records that year in the `Year` column (blank when they disagree)
5. Normalises all three scores column-wide using min-max scaling
6. Computes a **review-count-weighted composite score** grounded in Bayesian statistics
7. Writes results to Movies_updated.xlsx, leaving the original file untouched

---

## Highlights

- **Three-pass pipeline** — fetch → normalise → composite. Normalisation is intentionally separated from the fetch loop because min-max scaling requires the full column to be known before any single value can be computed.
- **Review-count-weighted composite** — Metacritic's contribution to the composite scales with its critic review count. A score backed by 80 reviews carries more weight than the same score backed by 4.
- **Dynamic denominator** — missing scores are dropped from both numerator and denominator rather than substituted with zeros, preserving the relative weighting of whichever sources are available.
- **Resilient scraping** — all HTTP fetches retry up to 3 times with exponential back-off behind a thread-safe per-domain rate limiter. Per-movie failures are logged and skipped; the rest of the batch continues.
- **Cloudflare-safe Metacritic requests** — Metacritic sits behind Cloudflare, which fingerprints the TLS handshake and serves older Python/OpenSSL builds a 403 "Just a moment…" challenge even with browser headers. Metacritic requests go through [`curl_cffi`](https://github.com/lexiforest/curl_cffi) impersonating Chrome, so it works regardless of the local Python build.
- **Year disambiguation** — an optional `Year` column steers all three sources to the right film when several share a title (e.g. *Parasite* 2019 vs. 1982). Left blank, it's filled with the year the sources matched, or left blank when they disagree; `--manual` asks for it.
- **Data safety** — existing cell values are never overwritten by a missing result, and they still count in the composite when a source returns nothing this run. The input workbook is never modified.
- **Accurate stability tracking** — `StableWeeks` correctly resets when the composite score shifts by more than ±0.05; the previous value is snapshotted before any writes so the comparison is always against the real old score.
- **Smart scheduling** — `--smart-update` reads `StableWeeks` to skip movies whose scores haven't changed, reducing network requests on repeat runs. A movie stable for N weeks is not re-fetched for N weeks — except movies with a blank or hand-typed score, which are re-checked every run so a source that adds the film later is picked up. (It reads the *input* workbook — see [Usage](#usage) for running it on the previous output.)
- **Manual entry that doesn't repeat itself** — `--manual` only asks about scores that are blank in the workbook, so anything you've typed in (or that was scraped before) is never asked again. Typed scores are tagged in a `Manual` column and replaced automatically once a source has the film. Ctrl-C stops the prompting without discarding anything already entered.
- **AI-powered slug resolution** — optional Gemini integration looks up hard-to-find movie pages with Google Search as a last resort, only for sources that couldn't find the film at all, and never asks the AI for scores. Every answer is verified (title + year) before use, and answers are cached between runs.

---

## Project Structure

```
.
├── update_scores.py          # Thin orchestrator: fetch_all, update_workbook, CLI
├── scoring.py                # Data models (RawScores, NormalisedScores), scoring math, year resolution
├── excel.py                  # Workbook I/O, header management, Year column, stability tracking
├── manual.py                 # Interactive prompts for unknown years and missing scores
├── requirements.txt
├── .env.example              # Template for .env (API keys)
├── Movies.xlsx               # Input watchlist (user-provided, not committed)
├── Movies_updated.xlsx       # Generated output (not committed)
├── .gemini_cache.json        # Cached Gemini answers (generated, not committed)
├── scraper/
│   ├── http.py               # Shared HTTP retry util, RateLimiter, slugify(), title/year matching
│   ├── omdb_client.py        # OMDb API client — Metascore + IMDB rating + release year
│   ├── metacritic_scraper.py # Scrapes critic review count + Metascore (curl_cffi, Cloudflare-safe)
│   ├── letterboxd_scraper.py # Scrapes average community rating
│   └── gemini_resolver.py    # Gemini + Google Search slug/ID lookup, answer cache (optional)
└── tests/
    ├── test_omdb_client.py
    ├── test_metacritic_scraper.py
    ├── test_letterboxd_scraper.py
    ├── test_orchestrator.py
    ├── test_normalisation_properties.py  # Property: normalise_column bounds
    ├── test_omdb_properties.py           # Property: OMDb parsing round-trip
    ├── test_composite_properties.py      # Property: formula correctness + safety
    ├── test_scraper_properties.py        # Property: review count, rating range, back-off
    ├── test_orchestrator_properties.py   # Property: input unchanged, output columns
    ├── test_manual.py                    # Manual prompts: progress counter, Ctrl-C handling
    ├── test_manual_workbook.py           # Blank-only prompts, Manual column, smart-update re-checks, Table1
    ├── test_year_disambiguation.py       # Year matching, auto-fill, and manual year prompts
    ├── test_gemini_validation.py         # Gemini IDs/slugs rejected unless title + year match
    └── test_gemini_resolver.py           # Prompt, cache, model fallback, OMDb search, targeted retries
```

---

## Composite score — theory

### The problem: can you trust a 100% rating?

Consider three sellers offering the same product at the same price:

| Seller | Rating | Reviews |
|--------|--------|---------|
| A | 100% positive | 10 |
| B | 96% positive | 50 |
| C | 93% positive | 200 |

Most people instinctively distrust the 100% rating — it comes from so few reviews that it feels fragile. But how do you make that intuition *quantitative*?

This is the central question in [3Blue1Brown's series on Bayesian statistics](https://www.youtube.com/watch?v=8idr1WZ1A7Q), and it is the theoretical foundation for how this project weights the Metacritic score.

### Laplace's rule of succession

When you observe p positive reviews out of n total, your best estimate of the true underlying success rate is not p/n but:

```
(p + 1) / (n + 2)
```

You pretend there were two extra reviews — one positive, one negative — before seeing any data. This is **Laplace's rule of succession** (18th century). It encodes a Bayesian prior of genuine uncertainty: the more real data accumulates, the less those two phantom reviews matter.

Applied to the sellers above:

| Seller | Adjusted estimate | |
|--------|------------------|-|
| A | (10 + 1) / (10 + 2) = **91.7%** | |
| B | (48 + 1) / (50 + 2) = **94.2%** | ← best choice |
| C | (186 + 1) / (200 + 2) = **92.6%** | |

The seller with the highest raw percentage is not the best choice. The one with the most *evidence* behind a strong rating wins.

### Applying this to Metacritic

A Metascore is a weighted average of critic reviews. A score of 85 from 6 critics and a score of 85 from 80 critics are not equally trustworthy.

This project applies the same logic: rather than giving Metacritic a fixed weight, its contribution to the composite is **scaled by its critic review count**. A Metascore backed by 80 reviews carries 80× the influence of a single fixed-weight term; one backed by 4 reviews carries only 4×.

### Step 1 — Min-max normalisation

The three sources use incompatible scales (0–100, 0–10, 0–5). Before combining them, each column is rescaled to [0, 1]:

```
st.X[i] = (X[i] − min(X)) / (max(X) − min(X))
```

min and max are computed across the entire batch after all fetches complete — not per-movie. This is why the pipeline separates fetching from normalisation. The best movie in each column maps to 1.0, the worst to 0.0, and everything else falls proportionally in between.

### Step 2 — Review-count-weighted composite

```
TRUE = ((st.Metacritic × Reviews) + st.Letterboxd + Global_Max + Global_Min + st.IMDB)
       / (Reviews + 4)
```

| Term | Weight | Rationale |
|------|--------|-----------|
| st.Metacritic × Reviews | Reviews | Metacritic's influence scales with critical coverage |
| st.Letterboxd | 1 | Fixed unit weight |
| st.IMDB | 1 | Fixed unit weight |
| Global_Max | 1 | Batch anchor — highest normalised value across all columns |
| Global_Min | 1 | Batch anchor — lowest normalised value across all columns |

The denominator is Reviews + 4 (4 fixed-weight terms plus the variable Metacritic weight).

### Dynamic denominator

Missing scores are dropped from both numerator and denominator, not substituted with zeros.

| Condition | Effect on denominator |
|-----------|----------------------|
| Reviews == 0 | Base denominator is 4 (Metacritic term dropped) |
| st.Letterboxd is None | − 1 |
| st.IMDB is None | − 1 |
| Global anchors unavailable | − 2 |
| All terms missing | Return None |

---

## Setup

**Requirements:** Python 3.10+ (the `google-genai` library used for the optional Gemini lookup needs 3.10), a free [OMDb API key](https://www.omdbapi.com/apikey.aspx)

```bash
# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\Activate.ps1     # Windows PowerShell

# Install dependencies
pip install -r requirements.txt
```

### Environment variables

Copy `.env.example` to `.env` and fill in your API keys:

```bash
cp .env.example .env
```

```env
# Required — OMDb API key (get one free at https://www.omdbapi.com/apikey.aspx)
OMDB_API_KEY=your_key_here

# Optional — Gemini API key for AI-powered slug resolution
GEMINI_API_KEY=your_gemini_key_here
```

The application loads `.env` automatically. You can also set these via environment variables or pass them as CLI flags (`--api-key`, `--gemini-key`).

**Optional:** Set `GEMINI_API_KEY` to enable AI-powered slug resolution for movies a source can't find at all. This uses Gemini with Google Search to find the correct URL slug for Metacritic, Letterboxd, or the IMDb ID for OMDb — but never asks the AI for scores. See [AI-Powered Slug Resolution](#ai-powered-slug-resolution).

---

## Usage

```bash
# Update all movies
python update_scores.py

# Process a single movie
python update_scores.py --movie "Boogie Nights"

# Test with a random sample of 10
python update_scores.py --limit 10

# Process all movies in random order
python update_scores.py --random

# Use a custom input file
python update_scores.py --input my_list.xlsx

# Skip recently-stable movies (see note below)
python update_scores.py --input Movies_updated.xlsx --output Movies_updated.xlsx --smart-update

# Ask for unknown release years and missing values
python update_scores.py --manual

# Adjust request delay (default 1.0s between sources)
python update_scores.py --delay 2.0

# Enable AI-powered slug resolution for hard-to-find movies
python update_scores.py --gemini-key $GEMINI_API_KEY
```

On Windows, run these with the virtual environment's Python — `.venv\Scripts\python update_scores.py …` — or activate it first. A bare `python` may open the Microsoft Store instead.

**`--smart-update` and the output file:** smart-update decides what to skip from the `StableWeeks`, `LastUpdated` and `Manual` columns of the *input* workbook. Results are written to a separate output file and the input is never changed, so running `--smart-update` on `Movies.xlsx` every time never skips anything. To build up stability history, run each time on the previous output, as in the example above (or copy `Movies_updated.xlsx` over `Movies.xlsx` between runs).

Movies with a blank score, or a score you typed in (listed in `Manual`), are never skipped: they're re-checked every run, so when Metacritic, Letterboxd or OMDb adds the film later, the real score is picked up. Re-checking is silent — with `--manual` you're still only asked about cells that are blank.

### All CLI options

| Flag | Default | Description |
|------|---------|-------------|
| --input PATH | Movies.xlsx | Path to the input workbook |
| --output PATH | \<stem\>_updated.xlsx | Path for the output workbook |
| --api-key KEY | — | OMDb API key (overrides OMDB_API_KEY env var) |
| --limit N | — | Pick N movies at random |
| --movie TITLE | — | Process a single movie by exact title |
| --delay SECS | 1.0 | Seconds between requests to each source |
| --verbose | off | Enable debug-level logging |
| --smart-update | off | Skip recently-stable movies |
| --manual | off | Prompt for unknown release years, then for scores blank in the workbook (Ctrl-C saves and stops) |
| --gemini-key KEY | — | Gemini API key for AI slug resolution (overrides GEMINI_API_KEY env var) |
| --random | off | Process movies in random order |
| --no-rate-limit | off | Disable the adaptive per-domain rate limiter (use the fixed `--delay` only) |

---

## Input format

Place your watchlist in Movies.xlsx in the project root. The workbook must have a column named **Movies** with one title per row. All other columns are optional — the script adds any missing output columns automatically. The input is never modified unless you point `--output` at it.

An optional **Year** column (release year) disambiguates films that share a title. When present, the year is sent to OMDb, and Metacritic and Letterboxd try the year-suffixed slug first (e.g. `/movie/buddy-2026/`, `/film/parasite-2019/`) and skip any page whose release year is more than 2 years off. Without it, `Parasite` resolves to the 1982 film on Letterboxd and `Buddy` to the 2019 film on Metacritic.

The 2-year tolerance exists because sources date the same film differently — a festival premiere and the theatrical release can be a year or two apart. *Without Blood* is 2024 on IMDb and Letterboxd (premiere) but 2026 on Metacritic (US release); either year works. OMDb only knows a film's first release year, so if a search with your year finds nothing, it retries without the year and accepts a match within 2 years.

Leave Year blank and the script fills it in for you (see **Output columns**) — check it to confirm the right film was found.

---

## Output columns

| Column | Description |
|--------|-------------|
| Year | Release year of the film the scores came from. A year you entered is kept as-is. Otherwise it's filled when Metacritic, Letterboxd and OMDb agree within 2 years (OMDb's year is used); left **blank** when they matched different films — the run log lists what each source found |
| Metacritic | Metascore (0–100) — Metacritic scrape, falls back to OMDb |
| st.Metacritic | Min-max normalised Metascore (0.0–1.0) |
| Reviews | Critic review count from Metacritic |
| Letterboxd | Average community rating (0.0–5.0) |
| st.Letterboxd | Min-max normalised Letterboxd rating (0.0–1.0) |
| IMDB | IMDB rating (0.0–10.0) from OMDb |
| st.IMDB | Min-max normalised IMDB rating (0.0–1.0) |
| TRUE | Weighted composite score (0.0–1.0, rounded to 2 dp) |
| LastUpdated | ISO date of last successful fetch (YYYY-MM-DD) |
| StableWeeks | Consecutive weeks the composite stayed within ±0.05 |
| Manual | Scores in this row you typed in with `--manual` (e.g. `IMDB, Letterboxd`). Cleared per score once a source supplies it |

If a source returns nothing for a score this run, the value already in the cell is kept and used in the `st.*` and `TRUE` calculations. To clear a wrong value, delete the cell (and its entry in `Manual`).

If the workbook has an Excel table named `Table1`, it's extended to cover any column the script adds (`Year`, `Manual`), so sorting the table in Excel keeps each row together.

---

## Manual entry

`--manual` first asks for the release year of any movie whose year is unknown (blank Year, and the sources matched different films), showing what each source found. The movie is re-fetched with the year you enter before any score prompts:

```
  Buddy [1/2 · 1 left]
    found: Metacritic 2019 · Letterboxd 1997 · OMDb 2026
    Release year: 2026
```

It then prompts for scores that are **blank in the workbook** and couldn't be fetched this run. Each prompt shows where you are in the queue:

```
  ── Manual entry for: Nirvana the Band the Show the Movie ── [3/12 · 9 left]
  (Press Enter to skip a field and leave it unchanged, Ctrl-C to stop and save)

  Metascore (0-100): 74
  IMDB rating (0.0-10.0):
  Critic review count (0+): 12
```

- **Only blank cells are asked about.** A score already in the workbook — typed in on an earlier run, or scraped before — isn't asked again, even if the source still doesn't have it. So once you've filled a movie in, later runs stay quiet about it.
- **Critic review count is only asked alongside a Metascore you type in.** A movie with no Metacritic page has no count to know, so it isn't asked on its own.
- **Typed scores are tagged** in the `Manual` column. They count in the composite like any other score, and smart-update keeps re-checking the movie. When a source adds the film, its score replaces yours and the tag is removed.
- **Movies that failed to fetch entirely** are asked about only for the fields their row is missing.

The count covers movies with at least one blank score plus failed movies with blanks — complete movies are never prompted for.

Press Enter to skip a field; the cell stays blank and you'll be asked again next run. **Ctrl-C (or end of input) stops the prompting without losing work** — every entry already made is written to the output workbook, including the fields typed for the movie you were on when you interrupted.

---

## AI-Powered Slug Resolution

When a movie title doesn't match the URL slug conventions of Metacritic, Letterboxd, or OMDb, the scrapers will fail. The optional `GeminiResolver` uses the Gemini API to find the correct page as a last-resort fallback.

**How it works:**
1. Local slug heuristics are tried first (lowercase, hyphens, remove articles, year suffix, etc.)
2. If that fails, each source's own search is tried. For OMDb that's its search endpoint (`?s=`), which returns real titles with years and IMDb IDs; a result is used only if the title matches and the year is within 2 years (or, with no year, it's the only film by that title)
3. Only when both fail does GeminiResolver kick in, and **only for the sources that couldn't find the film at all**. A film that was found but has no score yet — a new release with no IMDB rating on OMDb, say — isn't sent to Gemini, since a better ID can't fix missing data
4. Gemini is told the release year (from the Year column, or the year the other sources agree on) and looks the identifier up with **Google Search grounding** rather than recalling it. In testing, without grounding every model invented a wrong IMDb ID for *Without Blood*; with grounding all of them returned the right one
5. The AI **never provides scores** — only URL identifiers
6. **Every answer is verified** before its scores are used: the page Gemini points to must have the same title (ignoring case, punctuation and a leading "Prefix:", since IMDb lists *Oasis: Don't Look Back In Anger* as *Don't Look Back in Anger*) and a release year within 2 years of the one you entered or the other sources agree on. Anything else is discarded with a `Gemini: rejected …` warning and the field stays blank for `--manual`
7. **Answers are cached** in `.gemini_cache.json` next to your workbook (git-ignored), keyed by title and year, for 30 days. Repeat runs reuse them instead of calling the API, and an answer that failed verification is remembered so it isn't tried again. Delete the file to start fresh

**Models:** `gemini-3-flash-preview` first, then `gemini-3.1-flash-lite-preview`, then `gemini-2.5-flash-lite`. A model that's rate limited, unavailable to your key, or doesn't support search grounding is skipped for the rest of the run; a request that times out (90 s) or comes back empty is retried on the next model. In a test of six hard titles, `gemini-3-flash-preview` confirmed four (20–60 s each), `3.1-flash-lite` two (~3 s) and `2.5-flash-lite` none — and none of them returned a wrong film.

**Cost:** search-grounded requests may be billed separately from ordinary Gemini requests once past any free allowance — check the current Gemini API pricing for your plan. The steps above keep calls to a handful per run.

**Example:**
```python
from scraper import GeminiResolver

resolver = GeminiResolver(api_key="your_gemini_key", cache_path=".gemini_cache.json")
ids = resolver.resolve_all_ids("Without Blood", year=2024, want=("imdb_id",))
# Returns: {"metacritic_slug": None, "letterboxd_slug": None, "imdb_id": "tt18398986"}
```

To enable, set `GEMINI_API_KEY` or pass `--gemini-key` on the CLI. To turn it off, remove the key.

---

## Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| `Metacritic: HTTP 403` on every movie, `Reviews` always 0 | Cloudflare is challenging your Python's TLS fingerprint. Make sure `curl_cffi` is installed (`pip install -r requirements.txt`); upgrading to a current Python also helps |
| A movie's scores look wrong, or its `Year` is blank | The sources matched different films with the same title. Check the run log's `Year: sources matched different films` line, then enter the right release year in the `Year` column (or answer the year prompt in `--manual`) |
| The run ends with `PermissionError` when saving | The output workbook is open in Excel, which locks it on Windows. Close it and re-run |
| `GeminiResolver: … API key not valid` | `GEMINI_API_KEY` in `.env` is still the `your_gemini_key_here` placeholder or is wrong. Set a real key, or remove the line to turn Gemini off |
| A score you typed in is wrong, or you want to be asked again | Delete the cell (and its entry in the `Manual` column) in the workbook you feed back in; the next `--manual` run asks for it |
| A Gemini answer looks wrong or stale | Delete `.gemini_cache.json` to clear cached answers (they also expire after 30 days) |
| IMDB rating differs slightly from imdb.com | OMDb's copy of IMDb ratings lags a few days, most noticeably for new releases still gaining votes. The film is right; the number catches up |

---

## Running the tests

```bash
# Full test suite
python -m pytest

# Verbose output
python -m pytest -v

# Property-based tests only
python -m pytest tests/test_normalisation_properties.py tests/test_composite_properties.py tests/test_scraper_properties.py tests/test_omdb_properties.py tests/test_orchestrator_properties.py
```

All network calls (OMDb, Metacritic, Letterboxd, Gemini) are mocked, so the suite runs offline and needs no API keys.

---

## Author

**Ismail Jhaveri** — [LinkedIn](https://www.linkedin.com/in/ismail-jhaveri-2021/) · [ismailj@usf.edu](mailto:ismailj@usf.edu)

# Movie Score Scraper

A command-line tool that fetches critic and audience scores for a personal movie list and writes the results to a new Excel workbook.

For each movie in `Movies.xlsx` it retrieves:
- **Metascore** and **IMDB rating** from the [OMDb API](http://www.omdbapi.com/)
- **Critic review count** scraped from Metacritic
- **Average community rating** scraped from Letterboxd

It then applies min-max normalisation across the full dataset and computes a weighted composite score, writing everything to `Movies_updated.xlsx` without touching the original file.

---

## Requirements

- Python 3.10+
- A free OMDb API key — register at [omdbapi.com](https://www.omdbapi.com/apikey.aspx)

---

## Setup

```bash
# 1. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt
```

---

## Configuration

The OMDb API key can be provided in two ways (the CLI flag takes precedence):

```bash
# Option A — environment variable (recommended)
export OMDB_API_KEY=your_key_here

# Option B — CLI flag
python update_scores.py --api-key your_key_here
```

---

## Input file

Place your movie list in `Movies.xlsx` in the project root. The file must have a sheet with a column named **`Movies`** containing one title per row. All other columns are optional — the script will add any missing output columns automatically.

---

## Usage

```bash
# Update all movies (reads Movies.xlsx, writes Movies_updated.xlsx)
python update_scores.py

# Specify a custom input file
python update_scores.py --input my_list.xlsx

# Specify a custom output path
python update_scores.py --output results.xlsx

# Process only the first 10 rows (useful for testing)
python update_scores.py --limit 10

# Process a single movie by exact title
python update_scores.py --movie "Boogie Nights"

# Slow down requests (seconds between each source, default 1.0)
python update_scores.py --delay 2.0

# Enable debug logging
python update_scores.py --verbose
```

### All options

| Flag | Default | Description |
|---|---|---|
| `--input` | `Movies.xlsx` | Path to the input workbook |
| `--output` | `<input_stem>_updated.xlsx` | Path for the output workbook |
| `--api-key` | — | OMDb API key (overrides `OMDB_API_KEY` env var) |
| `--limit N` | — | Process only the first N movies |
| `--movie TITLE` | — | Process a single movie by exact title |
| `--delay SECS` | `1.0` | Seconds to wait between requests |
| `--verbose` | off | Enable debug-level logging |

---

## Output columns

The output workbook contains the original data plus these columns:

| Column | Description |
|---|---|
| `Metacritic` | Metascore (0–100) from OMDb |
| `st.Metacritic` | Min-max normalised Metascore (0.0–1.0) |
| `Reviews` | Critic review count from Metacritic |
| `Letterboxd` | Average community rating (0.0–5.0) |
| `st.Letterboxd` | Min-max normalised Letterboxd rating (0.0–1.0) |
| `IMDB` | IMDB rating (0.0–10.0) from OMDb |
| `st.IMDB` | Min-max normalised IMDB rating (0.0–1.0) |
| `TRUE` | Weighted composite score (rounded to 2 dp) |

The composite formula is:

```
TRUE = ((st.Metacritic × Reviews) + st.Letterboxd + Global_Max + Global_Min + st.IMDB)
       / (Reviews + 4)
```

Missing scores are dropped from both numerator and denominator rather than substituted with zeros.

---

## Running the tests

```bash
# Run the full test suite
python -m pytest

# Run a specific test file
python -m pytest tests/test_omdb_client.py

# Run with verbose output
python -m pytest -v

# Run only property-based tests
python -m pytest tests/test_normalisation_properties.py tests/test_composite_properties.py tests/test_scraper_properties.py tests/test_omdb_properties.py tests/test_orchestrator_properties.py
```

---

## Project structure

```
.
├── update_scores.py          # Orchestrator and CLI entry point
├── requirements.txt
├── Movies.xlsx               # Your input file (not included)
├── Movies_updated.xlsx       # Generated output (not committed)
├── scraper/
│   ├── omdb_client.py        # OMDb API client (Metascore + IMDB rating)
│   ├── metacritic_scraper.py # Scrapes critic review count
│   └── letterboxd_scraper.py # Scrapes average community rating
└── tests/
    ├── test_omdb_client.py
    ├── test_metacritic_scraper.py
    ├── test_letterboxd_scraper.py
    ├── test_orchestrator.py
    ├── test_normalisation_properties.py
    ├── test_omdb_properties.py
    ├── test_composite_properties.py
    ├── test_scraper_properties.py
    └── test_orchestrator_properties.py
```

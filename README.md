# NRL Try Predictor

A prediction site for who's going to score tries in NRL (rugby league) matches. It combines
historical match data with live team lists to produce a try-scoring probability for every
player named to play, plus a win probability for each team.

FastAPI backend + React/Vite frontend, backed by a Postgres (Supabase) database of historical
match data scraped from the public NRL website API.

## Features

- Try-scoring probability for every player in a named team list, ranked or grouped by position
- Win probability and predicted scoreline for each match
- "Best Edge" — picks where the model's probability beats the best bookmaker price
- Head-to-head history — try scorers from the last two meetings between the two teams
- Accuracy tracking — how the model's predictions have actually performed over time
- Team and player stat pages, searchable across the full scraped history

## Tech stack

- **Backend**: Python / FastAPI, Postgres (Supabase), `httpx` for the live NRL API client
- **Frontend**: React + Vite
- **Deployment**: Render (`render.yaml`), local dev via Docker Compose

## Getting started

### Backend (from `backend/`)

```
pip install -r requirements.txt
uvicorn main:app --reload --port 8000        # dev server
python scrape_job.py                         # one-shot: full historical scrape if DB empty, else incremental sync
```

### Frontend (from `frontend/`)

```
npm install
npm run dev        # Vite dev server; proxies /api -> $VITE_PROXY_TARGET (default http://localhost:8000)
```

### Full stack with Docker Compose (from repo root)

```
docker compose up --build
```

Backend on `:8000`, frontend (nginx) on `:3000`. `ODDS_API_KEY` and `DATABASE_URL` are both
optional — the app degrades gracefully without them (no bookmaker odds / no historical DB).

## Project structure

```
backend/    FastAPI app, NRL scraper, prediction model, Postgres access
frontend/   React/Vite single-page app
docs/       Architecture decision records and other reference docs
```

## Documentation

- [`AGENTS.md`](AGENTS.md) — architecture, data flow, caching layers, and other details for
  anyone (human or AI agent) working on this codebase
- [`docs/adr/`](docs/adr) — architecture decision records explaining *why* certain non-obvious
  choices were made
- [`docs/model-factors.md`](docs/model-factors.md) — the full list of factors the prediction
  model weighs

## Disclaimer

This site is for informational and entertainment purposes only. Predictions are statistical
estimates, not betting advice. Please gamble responsibly.

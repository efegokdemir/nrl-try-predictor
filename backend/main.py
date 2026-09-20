import os
import re
import json
import time
import logging
import asyncio
import threading
from contextlib import asynccontextmanager

from starlette.middleware.base import BaseHTTPMiddleware
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from nrl_client import (
    fetch_round, fetch_match_detail, fetch_ladder,
    parse_fixtures, parse_team_list, parse_team_stats, parse_scoring, parse_ladder,
    TOTAL_ROUNDS, SEASON,
)
from model import (
    generate_predictions, predict_win_probability,
    generate_multi_suggestion, find_value_picks, generate_team_summary,
    invalidate_cache,
)
from scraper import sync_current_season
from database import (
    init_db, get_total_match_count, get_total_try_count,
    get_player_game_log,
    upsert_prediction, get_accuracy_stats, get_unrecorded_completed_matches,
    search_players, search_teams, get_all_teams, get_all_players,
    get_team_roster, get_team_recent_results, get_team_season_matches, get_season_rounds,
    get_season_round_matches,
    get_team_attack_defence, get_home_away_win_rate,
    get_team_tries_conceded_by_edge, get_h2h_recent_tries,
    prefetch_round_data,
    get_player_headshot, update_player_headshots,
    save_cache_entry, load_all_cache_entries,
    prune_old_logs,
    get_edge_pick, save_edge_pick, get_betting_summary, get_prediction_seasons,
)
from odds_client import (
    add_implied_odds_to_players,
    fetch_bookmaker_odds,
    lookup_bookmaker_odds,
    has_api_key as has_odds_api_key,
    compute_best_edge_picks,
)
import log_handler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Gate the two writing background loops (_scrape_sync, _prediction_sync) behind
# an explicit opt-in, defaulting OFF: without this, running the backend
# locally against a production DATABASE_URL writes to production from a
# laptop the moment the process starts. Production sets this true in
# render.yaml. _warm_cache() is read-only and is never gated by this.
ENABLE_SYNC_LOOPS = os.environ.get("ENABLE_SYNC_LOOPS", "false").strip().lower() in ("true", "1", "yes")

# The model version actually being served, and so the one the accuracy pages
# report on by default. The predictions table still holds model_version 1 and 2
# rows as history (see docs/adr/0001-single-model-version.md), and aggregating
# across all three triple-counted every round V1/V2 also ran in -- round 1 read
# 11/24 instead of 3/8. Named rather than inlined because ADR-0006 puts a
# challenger (4) alongside the champion in 2027, at which point the served
# version is a thing that changes.
CHAMPION_MODEL_VERSION = 3


def _backfill_player_headshots(*player_lists):
    """Given one or more lists of player dicts (from parse_team_list), push
    any non-empty headshot URLs into the players table for rows that don't
    yet have one. Used as a lazy backfill path so headshots seen via live
    /api/match views get persisted for later searches."""
    name_to_url: dict[str, str] = {}
    for players in player_lists:
        if not players:
            continue
        for p in players:
            name = p.get("name")
            head = p.get("headshot")
            if name and head:
                name_to_url[name] = head
    if name_to_url:
        try:
            update_player_headshots(name_to_url)
        except Exception as e:
            logger.warning(f"Failed to backfill player headshots: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Bind the port immediately and run DB init in the background.

    init_db() used to run synchronously here, before the app could bind its
    port — if the DB was unreachable at boot (a paused Supabase project, a
    stale DATABASE_URL, a transient network blip), the whole process would
    crash before Render's health check could ever pass, so the entire site
    went down and every redeploy failed the same way until the DB issue was
    fixed AND a fresh deploy succeeded. Now DB init retries in the
    background: the app comes up and serves /api/health right away
    regardless of DB state, DB-dependent endpoints return clear errors
    (surfaced by the frontend's fetchJson timeout/error UI) until it
    connects, and the rest of startup (cache warm, prediction backfill)
    proceeds automatically once it does.
    """
    tasks = [asyncio.create_task(_startup_sequence())]
    yield
    for t in tasks:
        t.cancel()


async def _startup_sequence():
    """Retry DB init until it succeeds, then run the rest of startup."""
    while True:
        try:
            await asyncio.to_thread(init_db)
            break
        except Exception as e:
            logger.error(f"DB init failed, will retry in 30s: {e}")
            await asyncio.sleep(30)

    # Mirror logs into Supabase (app_logs table) now that the table exists,
    # so operational visibility survives beyond Render's log retention.
    log_handler.attach(source="backend")
    try:
        prune_old_logs()
    except Exception as e:
        logger.warning(f"Failed to prune old app_logs rows: {e}")

    existing = get_total_match_count()
    logger.info(f"Starting up. DB has {existing} matches.")

    # Restore previously-warmed cache from DB so requests can be served
    # immediately, even before the background warmup re-runs.
    _restore_cache_from_db()

    if ENABLE_SYNC_LOOPS:
        logger.info("ENABLE_SYNC_LOOPS is set — starting scrape sync and prediction sync loops.")
        asyncio.create_task(_scrape_sync())
        asyncio.create_task(_prediction_sync())
    else:
        logger.warning(
            "ENABLE_SYNC_LOOPS is not set — scrape sync and prediction sync loops are "
            "DISABLED, so the DB will not receive new data or accuracy backfill from this "
            "process. Set ENABLE_SYNC_LOOPS=true to enable them (production always does)."
        )
    asyncio.create_task(_warm_cache())


def _cache_key_str(round_number: int) -> str:
    return f"round:{round_number}"


def _match_cache_key_str(match_url: str) -> str:
    """cache_store key for a persisted match detail.

    Carries the model version because the stored payload is model output, not
    raw data. Changing the model must not keep serving predictions the current
    model never made -- a version bump simply stops matching, so the old rows
    are ignored and recomputed rather than silently trusted.
    """
    return f"match:v{CHAMPION_MODEL_VERSION}:{match_url}"


# Finals weeks don't carry a "round-N" segment in their NRL match_url — they're
# "finals-week-N" instead (e.g. "/draw/nrl-premiership/2026/finals-week-1/...").
# Map that onto the same 1..TOTAL_ROUNDS numbering /api/rounds/{n} uses, so
# code that needs "which round is this match in" (prediction history cutoffs,
# match-to-match nav) works in finals weeks too, not just the regular season.
# Must stay in sync with TOTAL_ROUNDS (nrl_client.py) — see AGENTS.md.
_ROUND_URL_RE = re.compile(r'/round-(\d+)/')
_FINALS_WEEK_URL_RE = re.compile(r'/finals-week-(\d+)/')
REGULAR_SEASON_ROUNDS = TOTAL_ROUNDS - 4


def _round_number_from_url(match_url: str) -> int | None:
    m = _ROUND_URL_RE.search(match_url)
    if m:
        return int(m.group(1))
    m = _FINALS_WEEK_URL_RE.search(match_url)
    if m:
        return REGULAR_SEASON_ROUNDS + int(m.group(1))
    return None


def _restore_cache_from_db():
    """Load every persisted round response into the in-memory cache at startup."""
    try:
        entries = load_all_cache_entries()
    except Exception as e:
        logger.warning(f"Could not load persisted cache from DB: {e}")
        return
    if not entries:
        logger.info("No persisted cache entries found.")
        return

    match_prefix = f"match:v{CHAMPION_MODEL_VERSION}:"
    rounds = 0
    matches = 0
    for entry in entries:
        key = entry["key"]
        try:
            payload = json.loads(entry["payload"])
        except Exception:
            continue
        if key.startswith("round:"):
            try:
                rnd = int(key.split(":", 1)[1])
            except Exception:
                continue
            with _round_cache_lock:
                _round_cache[rnd] = (payload, entry["refreshed_at"])
            rounds += 1
        elif key.startswith(match_prefix):
            # Only persisted completed matches land here, so the restored
            # entry is flagged completed -- which is what gives it the long
            # TTL rather than the 2-minute live one.
            url = key[len(match_prefix):]
            if not url:
                continue
            with _match_cache_lock:
                _match_cache[url] = (payload, entry["refreshed_at"], True)
            matches += 1
        # A match key carrying a different model version is left alone: it is
        # another version's output, and will be overwritten under its own key
        # when that match is next computed.
    logger.info(f"Restored {rounds} round and {matches} match cache entries from DB.")


def _likely_current_round():
    """Highest round already known to have fixtures, taken from the cache
    restored at startup. Only used to decide what to warm first -- if it is
    absent or wrong, the normal walk still warms everything, just later."""
    with _round_cache_lock:
        candidates = [
            rnd for rnd, (resp, _) in _round_cache.items()
            if resp.get("matches") and not resp.get("draw_not_released")
        ]
    return max(candidates) if candidates else None


async def _warm_cache():
    """Pre-load the cache for all rounds that have data so every page loads instantly."""
    try:
        await asyncio.sleep(2)

        # Warm the round people actually land on before backfilling the rest.
        # The full walk below takes minutes -- a fetch and a pause per round,
        # then the match details last of all -- so warming in round order left
        # the most-visited page cold for the longest after every deploy. That
        # window was wide enough to measure: a current-round match read 7.7s
        # nine minutes after a deploy, then 0.2s once the walk caught up.
        #
        # The round number comes from the cache restored a moment ago rather
        # than from walking to find it, since walking is the slow thing being
        # avoided. On a first-ever boot there is nothing restored and this is
        # simply skipped.
        priority = _likely_current_round()
        priority_result = None
        if priority:
            try:
                priority_result = await _refresh_round_cache(priority)
                if priority_result and priority_result.get("matches"):
                    logger.info(f"Priority-warmed round {priority} ahead of the full walk.")
                    if ENABLE_SYNC_LOOPS:
                        await _warm_round_matches(priority, priority_result)
                else:
                    priority_result = None
            except Exception as e:
                logger.warning(f"Priority warm of round {priority} failed: {e}")
                priority_result = None

        logger.info("Warming cache for all rounds...")

        latest = None
        for r in range(1, TOTAL_ROUNDS + 1):
            try:
                # Already done above, and re-running it would repeat the
                # per-fixture prediction pass for no benefit.
                result = priority_result if r == priority and priority_result else await _refresh_round_cache(r)
                if result is None:
                    logger.info(f"Round {r} unavailable, stopping warmup.")
                    break
                if not result.get("matches"):
                    logger.info(f"Round {r} has no fixtures, stopping warmup.")
                    break
                latest = (r, result)
                logger.info(f"Warmed round {r}.")
            except Exception as e:
                logger.warning(f"Failed to warm round {r}: {e}")
                continue
            # Small delay between rounds to avoid overwhelming DB on free tier
            await asyncio.sleep(0.5)

        logger.info("Cache warmup complete.")

        # The round pages are warm now, but /api/match is not, and the first
        # view of any given match pays the whole model pipeline against a cold
        # query cache -- measured at ~5s against production. Warming the latest
        # round's matches moves that cost off the first visitor.
        #
        # Gated, unlike the rest of _warm_cache, because _compute_match_detail
        # WRITES: it records predictions for completed matches and captures
        # edge picks. Ungated, a laptop pointed at a production DATABASE_URL
        # would write to production at startup, which is exactly what
        # ENABLE_SYNC_LOOPS exists to prevent.
        # Only if the walk found a later round than the priority guess --
        # otherwise its matches are already warm from above.
        if latest and ENABLE_SYNC_LOOPS and latest[0] != priority:
            await _warm_round_matches(*latest)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.warning(f"Cache warmup failed (non-critical): {e}")


async def _warm_round_matches(round_number, round_response):
    """Pre-compute /api/match for one round's fixtures, so their first visitor
    gets a cache hit. Deliberately serial with a delay between matches: this is
    background work competing with real requests for the same free-tier DB and
    the same NRL endpoints, and there is no deadline on it."""
    urls = [m.get("match_url") for m in round_response.get("matches", []) if m.get("match_url")]
    if not urls:
        return
    logger.info(f"Warming {len(urls)} match detail(s) for round {round_number}...")
    warmed = 0
    for url in urls:
        try:
            await _refresh_match_cache(url)
            warmed += 1
        except HTTPException as e:
            # 403 simply means team lists are not published yet -- expected for
            # an upcoming round, and not worth a warning.
            if e.status_code != 403:
                logger.warning(f"Failed to warm match {url}: {e.detail}")
        except Exception as e:
            logger.warning(f"Failed to warm match {url}: {e}")
        await asyncio.sleep(0.5)
    logger.info(f"Warmed {warmed}/{len(urls)} match details for round {round_number}.")


PREDICTION_SYNC_INTERVAL = 600  # 10 minutes
SCRAPE_SYNC_INTERVAL = 1800  # 30 minutes — matches the old external cron's cadence


async def _record_prediction_for_match(match_url: str, model_version: int = CHAMPION_MODEL_VERSION):
    """Fetch a completed match from the NRL API, run predictions, and record accuracy."""

    raw = await fetch_match_detail(match_url)
    if raw is None:
        return False

    match_state = raw.get("matchState", "")
    if match_state not in ("FullTime", "PostMatch"):
        return False

    home_players = parse_team_list(raw, "homeTeam")
    away_players = parse_team_list(raw, "awayTeam")
    if not home_players and not away_players:
        return False
    await asyncio.to_thread(_backfill_player_headshots, home_players, away_players)

    season_m = re.search(r'/(\d{4})/', match_url)
    before_season = int(season_m.group(1)) if season_m else None
    before_round = _round_number_from_url(match_url)
    if not before_season or not before_round:
        return False

    stats = parse_team_stats(raw)
    home_team = raw.get("homeTeam", {})
    away_team = raw.get("awayTeam", {})
    home_nickname = home_team.get("nickName", "Home")
    away_nickname = away_team.get("nickName", "Away")
    match_venue = raw.get("venue", "")
    match_weather = raw.get("weather", "")
    match_ground = raw.get("groundConditions", "")

    predictions = generate_predictions(
        home_players, away_players,
        stats.get("home", {}), stats.get("away", {}),
        home_team_name=home_nickname, away_team_name=away_nickname,
        before_season=before_season, before_round=before_round,
        weather=match_weather, ground_conditions=match_ground,
    )

    win_prediction = predict_win_probability(
        home_nickname, away_nickname,
        stats.get("home", {}), stats.get("away", {}),
        before_season=before_season, before_round=before_round,
        venue=match_venue, weather=match_weather, ground_conditions=match_ground,
    )

    multi = generate_multi_suggestion(
        predictions["home"], predictions["away"],
        home_nickname, away_nickname,
    )

    # Top-3 picks per side. Only the names are needed: the match page section
    # that displayed these was replaced by "Last Time These Teams Met" (commit
    # 112f401), leaving /api/accuracy -- which reads top3_hits and the
    # top3_*_json columns built below -- as the only consumer.
    top3_home = [{"name": p["name"]} for p in predictions["home"][:3]]
    top3_away = [{"name": p["name"]} for p in predictions["away"][:3]]

    scoring = parse_scoring(raw)
    if not scoring:
        return False

    home_actual = set(scoring["home_try_scorers"])
    away_actual = set(scoring["away_try_scorers"])
    for pick in top3_home:
        pick["scored"] = pick["name"] in home_actual
    for pick in top3_away:
        pick["scored"] = pick["name"] in away_actual

    all_actual = home_actual | away_actual
    multi_hits = sum(1 for p in multi["picks"] if p["name"] in all_actual)
    for p in multi["picks"]:
        p["scored"] = p["name"] in all_actual

    actual_winner = None
    win_correct = None
    if scoring["home_score"] is not None and scoring["away_score"] is not None:
        if scoring["home_score"] > scoring["away_score"]:
            actual_winner = home_nickname
        elif scoring["away_score"] > scoring["home_score"]:
            actual_winner = away_nickname
        else:
            actual_winner = "Draw"
        win_correct = 1 if win_prediction["predicted_winner"] == actual_winner else 0

    t3h_json = json.dumps([{"name": p["name"], "scored": p.get("scored")} for p in top3_home])
    t3a_json = json.dumps([{"name": p["name"], "scored": p.get("scored")} for p in top3_away])
    t3_hits = sum(1 for p in top3_home if p.get("scored")) + sum(1 for p in top3_away if p.get("scored"))
    m_json = json.dumps([{"name": p["name"], "team": p["team"], "scored": p.get("scored")} for p in multi["picks"]])

    upsert_prediction(
        match_url=match_url, season=before_season, round_number=before_round,
        model_version=model_version, home_team=home_nickname, away_team=away_nickname,
        predicted_winner=win_prediction["predicted_winner"],
        home_win_prob=win_prediction["home_win_prob"],
        predicted_home_score=win_prediction["predicted_home_score"],
        predicted_away_score=win_prediction["predicted_away_score"],
        actual_winner=actual_winner,
        actual_home_score=scoring.get("home_score"),
        actual_away_score=scoring.get("away_score"),
        win_correct=win_correct,
        top3_home_json=t3h_json, top3_away_json=t3a_json, top3_hits=t3_hits,
        multi_json=m_json, multi_hits=multi_hits,
        multi_all_scored=1 if multi_hits == len(multi["picks"]) else 0,
    )
    return True


async def _backfill_predictions():
    """Record predictions for all completed matches that haven't been recorded yet."""
    unrecorded = get_unrecorded_completed_matches(model_version=CHAMPION_MODEL_VERSION)
    if not unrecorded:
        return
    logger.info(f"Backfilling {len(unrecorded)} prediction(s)...")
    recorded = 0
    for match in unrecorded:
        try:
            ok = await _record_prediction_for_match(match["match_url"])
            if ok:
                recorded += 1
            await asyncio.sleep(0.5)  # rate limit NRL API
        except Exception as e:
            logger.warning(f"Failed to record prediction for {match['match_url']}: {e}")
    if recorded:
        invalidate_cache()
    logger.info(f"Backfilled {recorded}/{len(unrecorded)} predictions.")


async def _prediction_sync():
    """Background task: backfill on startup, then check for new completions periodically."""
    # Wait for scraper to finish populating the matches table
    await asyncio.sleep(30)
    while True:
        try:
            # Wait until scraper has data
            if get_total_match_count() > 0:
                break
        except Exception:
            pass
        await asyncio.sleep(10)

    # Initial backfill
    try:
        await _backfill_predictions()
    except Exception as e:
        logger.error(f"Prediction backfill error: {e}")

    # Periodic check for newly completed matches
    while True:
        await asyncio.sleep(PREDICTION_SYNC_INTERVAL)
        try:
            await _backfill_predictions()
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"Prediction sync error: {e}")


async def _scrape_sync():
    """Background task: periodically sync the current season's completed
    matches into the DB. Replaces relying on an external GitHub Actions
    cron — that schedule trigger proved unreliable (subject to queueing
    delays under GitHub's load, and GitHub auto-disables a schedule after
    60 days of repo inactivity with no way for it to self-recover; see
    docs/adr for the incident this came from). Runs in this same
    always-on process instead, so scraping now depends only on this
    service's own uptime (already monitored via the external health-check
    ping), not on GitHub's scheduler.
    """
    while True:
        try:
            new_matches = await sync_current_season()
            if new_matches:
                invalidate_cache()
                logger.info(f"Scrape sync added {new_matches} new matches — cache invalidated.")
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"Scrape sync error: {e}")
        await asyncio.sleep(SCRAPE_SYNC_INTERVAL)


app = FastAPI(title="NRL Try Predictor", version="3.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Without this, an unhandled exception skips CORSMiddleware entirely —
    Starlette's default 500 response carries no Access-Control-Allow-Origin
    header, so the browser reports a misleading CORS error instead of the
    real 500. Registering a handler keeps the response inside the normal
    middleware chain (CORS headers get attached) and logs the real cause
    (mirrored to Supabase's app_logs table via log_handler)."""
    logger.error(f"Unhandled exception on {request.method} {request.url.path}: {exc}", exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


class CacheControlMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == "/api/health":
            response.headers["Cache-Control"] = "no-cache"
        elif path.startswith("/api/match"):
            # Match detail: short cache for upcoming, long for completed
            response.headers["Cache-Control"] = "public, max-age=120, stale-while-revalidate=300"
        elif path.startswith("/api/rounds/"):
            # Round page: 2 min cache, serve stale while revalidating
            response.headers["Cache-Control"] = "public, max-age=120, stale-while-revalidate=300"
        elif path.startswith("/api/rounds"):
            # Rounds list: names can change during finals (an undrawn
            # round's placeholder label gets replaced once NRL publishes
            # the real draw), so match the same 2-min window as an
            # individual round page rather than the old 10-min cache.
            response.headers["Cache-Control"] = "public, max-age=120, stale-while-revalidate=300"
        elif path.startswith("/api/accuracy"):
            response.headers["Cache-Control"] = "public, max-age=300"
        elif path.startswith("/api/team") or path.startswith("/api/player"):
            response.headers["Cache-Control"] = "public, max-age=300"
        return response


app.add_middleware(CacheControlMiddleware)


@app.api_route("/api/health", methods=["GET", "HEAD"])
async def health_check():
    """Lightweight health check — keeps Render from sleeping.

    HEAD as well as GET, because uptime monitors default to HEAD and FastAPI,
    unlike plain Starlette, does not add it to a GET route automatically. The
    UptimeRobot monitor this endpoint exists for was getting 405 and reporting
    the service down. The request still woke Render either way -- a 405 comes
    from the app, so the app had to be running to send it -- which is the
    failure mode worth knowing about: the ping was doing its job while the
    dashboard said otherwise, so a real outage would have looked identical.
    """
    return {"status": "ok"}


@app.get("/api/status")
async def get_status():
    """Return DB status - how much historical data is loaded."""
    return {
        "matches": get_total_match_count(),
        "tries": get_total_try_count(),
    }


async def _is_round_drawn(round_number: int):
    """Cheaply determine whether a round's draw has actually been published
    yet (vs. NRL echoing the last real round — see _is_undrawn_echo),
    without running the full prediction pipeline that _refresh_round_cache
    does — that pipeline is too expensive to run for every round on every
    home-page load. A cached draw_not_released verdict is trusted
    unconditionally (cheap to have gotten right, and re-checking it costs a
    live fetch for no benefit). A cached "drawn" verdict is only trusted
    while fresh (same TTL get_round uses) and re-verified once stale —
    otherwise a round that was wrongly cached as drawn before this
    undrawn-detection existed (or simply by a persisted cache_store entry
    surviving a restart, refreshed_at intact) would stay wrong forever,
    since a cache hit here never used to expire. On a genuine miss, fetches
    just the raw fixtures to check, and caches the draw_not_released
    placeholder when a round turns out undrawn (cheap, and matches what
    _refresh_round_cache would store). A round that turns out drawn but
    wasn't cached is reported here without being cached — the full version
    is computed the normal way, whenever it's actually visited.
    A round that has been confirmed drawn and has finished being played
    is recorded in _round_drawn_verified and trusted from then on: neither
    half of that verdict can change, so re-verifying it against NRL is
    pure cost. Without this, /api/rounds re-fetched every past round of
    the season on every call whose _round_cache entry had aged past the
    30-minute completed-round TTL -- ~29 sequential NRL fetches, ~11s.
    Returns (is_drawn, round_title)."""
    with _round_cache_lock:
        verified = _round_drawn_verified.get(round_number, _MISSING)
    if verified is not _MISSING:
        return (True, verified)

    with _round_cache_lock:
        cached = _round_cache.get(round_number)
    if cached:
        resp, cached_at = cached
        if resp.get("draw_not_released"):
            return (False, None)
        has_live = any(
            (m.get("match_state") or "").lower() not in ("fulltime", "postmatch")
            for m in resp.get("matches", [])
        )
        ttl = _ROUND_CACHE_TTL_LIVE if has_live else _ROUND_CACHE_TTL_COMPLETED
        if time.time() - cached_at < ttl:
            return (True, resp.get("name"))
        # Stale — fall through and re-verify with a fresh fetch below.

    raw = await fetch_round(round_number)
    if raw is None:
        return (False, None)
    fixtures, byes, round_title = parse_fixtures(raw)
    if await _is_undrawn_echo(fixtures, round_number):
        response = {
            "round": round_number, "name": f"Round {round_number}",
            "matches": [], "byes": [], "draw_not_released": True,
        }
        with _round_cache_lock:
            _round_cache[round_number] = (response, time.time())
        return (False, None)

    # Genuinely drawn (echo check above says so). If it has also finished
    # being played, both halves of the verdict are now immutable, so record
    # it and stop re-checking it against NRL. Only stamped on this path --
    # never from a _round_cache hit -- because only here has the round been
    # affirmatively echo-checked; trusting a restored cache_store entry
    # would make permanent exactly the stale-verdict bug the TTL guards.
    all_played = bool(fixtures) and all(
        (f.get("match_state") or "").lower() in ("fulltime", "postmatch")
        for f in fixtures
    )
    if all_played:
        with _round_cache_lock:
            _round_drawn_verified[round_number] = round_title
    return (True, round_title)


_ROUND_VERDICT_WARM_CONCURRENCY = 6


async def _warm_round_verdicts():
    """Resolve the drawn/undrawn verdict for every round of the current season
    concurrently. get_rounds has to walk rounds in order (it stops at the
    first undrawn one), but on a cold cache that walk made ~29 NRL fetches
    strictly one after another -- the ~11s cold /api/rounds. Resolving them
    concurrently first turns that into roughly one round-trip and leaves the
    loop below hitting cache only, with its stop-at-first-undrawn semantics
    untouched. Verdicts already cached or verified return immediately, so
    this costs nothing on the warm path. Failures are swallowed per-round:
    the sequential walk re-runs the same call and handles the error there.
    """
    sem = asyncio.Semaphore(_ROUND_VERDICT_WARM_CONCURRENCY)

    async def resolve(i):
        async with sem:
            try:
                await _is_round_drawn(i)
            except Exception as e:
                logger.warning(f"Round verdict warm failed for round {i}: {e}")

    await asyncio.gather(*(resolve(i) for i in range(1, TOTAL_ROUNDS + 1)))


@app.get("/api/rounds")
async def get_rounds(season: int = SEASON):
    """List rounds for the round-selector. For the current season, stops as
    soon as we reach a round NRL hasn't actually drawn yet — finals weeks in
    particular don't appear on NRL's own site until the prior week's results
    decide who's playing, so they shouldn't show up as a clickable box here
    either, see _is_round_drawn / _is_undrawn_echo. Past seasons are fully
    played out already, so they're listed straight from the DB instead."""
    if season != SEASON:
        return {
            str(r["round_number"]): {"name": r["round_title"] or f"Round {r['round_number']}"}
            for r in get_season_rounds(season)
        }

    await _warm_round_verdicts()

    result = {}
    for i in range(1, TOTAL_ROUNDS + 1):
        drawn, title = await _is_round_drawn(i)
        if not drawn:
            break
        result[str(i)] = {"name": title or f"Round {i}"}
    return result


def _build_odds_comparison(home_odds_str, away_odds_str, home_model_prob, away_model_prob):
    """Calculate implied probabilities from decimal odds and compare to model.
    Shared by the round-fixture pass and the match-detail response so both
    report edges on identical arithmetic."""
    if not home_odds_str or not away_odds_str:
        return None
    try:
        home_dec = float(home_odds_str)
        away_dec = float(away_odds_str)
        if home_dec <= 0 or away_dec <= 0:
            return None
        home_implied = 1.0 / home_dec
        away_implied = 1.0 / away_dec
        return {
            "home_decimal": home_dec,
            "away_decimal": away_dec,
            "home_implied_prob": round(home_implied, 4),
            "away_implied_prob": round(away_implied, 4),
            "home_model_prob": round(home_model_prob, 4),
            "away_model_prob": round(away_model_prob, 4),
            "home_value": home_model_prob > home_implied,
            "away_value": away_model_prob > away_implied,
            "home_edge": round(home_model_prob - home_implied, 4),
            "away_edge": round(away_model_prob - away_implied, 4),
        }
    except (ValueError, ZeroDivisionError):
        return None


def _predict_single_fixture(f, round_number):
    """Predict win probability for a single fixture (runs in its own thread)."""
    home = f.get("home_team", "")
    away = f.get("away_team", "")
    if not home or not away:
        return f
    wp = predict_win_probability(home, away, {}, {},
                                 before_season=SEASON, before_round=round_number,
                                 venue=f.get("venue", ""))
    f["predicted_winner"] = wp["predicted_winner"]
    f["home_win_prob"] = wp["home_win_prob"]
    f["away_win_prob"] = wp["away_win_prob"]
    f["predicted_home_score"] = wp["predicted_home_score"]
    f["predicted_away_score"] = wp["predicted_away_score"]
    odds_comparison = _build_odds_comparison(
        f.get("home_odds", ""), f.get("away_odds", ""),
        wp["home_win_prob"], wp["away_win_prob"],
    )
    if odds_comparison:
        f["odds_comparison"] = odds_comparison
    state = (f.get("match_state") or "").lower()
    if state in ("fulltime", "postmatch") and f.get("home_score") is not None and f.get("away_score") is not None:
        if f["home_score"] > f["away_score"]:
            f["actual_winner"] = home
        elif f["away_score"] > f["home_score"]:
            f["actual_winner"] = away
        else:
            f["actual_winner"] = "Draw"
        f["prediction_correct"] = f["predicted_winner"] == f["actual_winner"]
    return f


def _enrich_fixtures(fixtures, round_number):
    """Add win predictions to all fixtures. Pre-fetches all team data in ONE query."""
    # Collect all teams and matchups (with venues for prefetch)
    team_names = set()
    matchups = []
    for f in fixtures:
        home = f.get("home_team", "")
        away = f.get("away_team", "")
        venue = f.get("venue", "")
        if home and away:
            team_names.add(home)
            team_names.add(away)
            matchups.append((home, away, venue))

    # Pre-fetch all team data in a single bulk query (1 query instead of ~40)
    if team_names:
        prefetch_round_data(
            list(team_names), matchups, last_n_games=10,
            before_season=SEASON, before_round=round_number,
        )

    # Now run predictions — all DB calls will hit the cache
    for f in fixtures:
        _predict_single_fixture(f, round_number)

    # Strip fields the frontend doesn't use
    for f in fixtures:
        f.pop("home_odds", None)
        f.pop("away_odds", None)
        f.pop("team_lists_announced", None)

    return fixtures


# --- Round response cache ---
_round_cache = {}       # key: round_number -> (response_dict, timestamp)
_round_cache_lock = threading.Lock()

# Rounds confirmed genuinely drawn (not an undrawn echo) AND fully played
# out: round_number -> round_title. Both halves of that verdict are
# immutable, so unlike _round_cache these entries never expire -- see
# _is_round_drawn for why that distinction matters to /api/rounds.
_round_drawn_verified = {}
_MISSING = object()  # sentinel: a verified round title may itself be None
_round_refreshing = set()  # round numbers currently being refreshed in background

# Match-detail response cache: match_url -> (response, timestamp, is_completed).
# In-memory only, deliberately NOT persisted to cache_store the way _round_cache
# is: a restart here is almost always a deploy, and a deploy is exactly when a
# recomputed prediction may legitimately differ, so carrying completed-match
# responses across one would serve predictions the current model never made.
_match_cache = {}
_match_cache_lock = threading.Lock()
_match_refreshing = set()  # match_urls currently being refreshed in background
_MATCH_CACHE_TTL_LIVE = 120         # 2 min while a match is upcoming or in progress
_MATCH_CACHE_TTL_COMPLETED = 21600  # 6h once full-time. The detail is immutable, so
                                    # this is bounded only to self-heal after a model
                                    # change, not for correctness.
_ROUND_CACHE_TTL_LIVE = 120      # 2 min for rounds with upcoming/live matches
_ROUND_CACHE_TTL_COMPLETED = 1800  # 30 min for fully completed rounds


async def _is_undrawn_echo(fixtures, round_number):
    """NRL's API doesn't error for a round beyond what's currently drawn —
    it just echoes back the latest available round's fixtures (same
    match_urls). Detect that by comparing against the previous round's
    fixtures, so we don't show finals weeks as if they were real, distinct
    rounds before NRL has actually published their draw. Prefers the
    previous round's cache, but fetches it fresh when uncached rather than
    assuming this round is real — an uncached previous round (cold cache,
    a fresh deploy, or simply never having been visited) used to let an
    echoed finals round slip through as if it were genuinely drawn."""
    if round_number <= 1:
        return False
    with _round_cache_lock:
        prev = _round_cache.get(round_number - 1)
    if prev:
        prev_urls = {m.get("match_url") for m in prev[0].get("matches", []) if m.get("match_url")}
    else:
        prev_raw = await fetch_round(round_number - 1)
        if prev_raw is None:
            return False  # couldn't verify — assume real rather than hide it
        prev_fixtures, _, _ = parse_fixtures(prev_raw)
        prev_urls = {m.get("match_url") for m in prev_fixtures if m.get("match_url")}
    new_urls = {f.get("match_url") for f in fixtures if f.get("match_url")}
    return bool(new_urls) and new_urls == prev_urls


async def _refresh_round_cache(round_number):
    """Fetch and compute a round response, store in cache."""
    cache_key = round_number
    try:
        raw = await fetch_round(round_number)
        if raw is None:
            return None
        fixtures, byes, round_title = parse_fixtures(raw)
        if await _is_undrawn_echo(fixtures, round_number):
            # Not a real round yet — NRL is just echoing the last drawn
            # round. Cache an empty result (the frontend already shows
            # "No match data available for this round yet" for that) rather
            # than confusingly duplicating another round's matches, and
            # skip the prediction work entirely since it'd be thrown away.
            response = {
                "round": round_number,
                "name": f"Round {round_number}",
                "matches": [],
                "byes": [],
                "draw_not_released": True,
            }
            refreshed_at = time.time()
            with _round_cache_lock:
                _round_cache[cache_key] = (response, refreshed_at)
            # Deliberately not persisted to cache_store: this is a transient
            # placeholder that should be re-derived (and likely replaced by
            # real data) on every restart, not carried forward as if stale
            # data were meaningful.
            return response
        fixtures = await asyncio.to_thread(_enrich_fixtures, fixtures, round_number)
        response = {
            "round": round_number,
            "name": round_title or f"Round {round_number}",
            "matches": fixtures,
            "byes": byes,
        }
        refreshed_at = time.time()
        with _round_cache_lock:
            _round_cache[cache_key] = (response, refreshed_at)
        # Persist to DB so the cache survives restarts
        try:
            await asyncio.to_thread(
                save_cache_entry,
                _cache_key_str(round_number),
                json.dumps(response, default=str),
                refreshed_at,
            )
        except Exception as e:
            logger.warning(f"Failed to persist round {round_number} cache to DB: {e}")
        return response
    finally:
        _round_refreshing.discard(cache_key)


async def _get_current_season_team_matches(team_name: str) -> list:
    """A team's full current-season schedule (played + upcoming), built
    round-by-round from the same cache/refresh path /api/rounds/{n} uses,
    so the cards it produces look identical. Prefers the cache; computes
    (with predictions) whatever rounds aren't cached yet — unlike the
    passive home-page load, this is a deliberate, user-initiated lookup,
    so it's fine to accept the one-time cost of warming missing rounds."""
    matches = []
    for i in range(1, TOTAL_ROUNDS + 1):
        with _round_cache_lock:
            cached = _round_cache.get(i)
        resp = cached[0] if cached else await _refresh_round_cache(i)
        if not resp or resp.get("draw_not_released"):
            continue
        for m in resp.get("matches", []):
            if m.get("home_team") == team_name or m.get("away_team") == team_name:
                matches.append({**m, "round_number": i, "round_name": resp.get("name")})
    return matches


@app.get("/api/rounds/{round_number}")
async def get_round(round_number: int, version: int = 3, season: int = SEASON):
    # `version` is accepted for backward compatibility with old links but
    # ignored — the app only runs the V3 model now.
    if round_number < 1 or round_number > TOTAL_ROUNDS:
        raise HTTPException(status_code=404, detail="Invalid round number")

    if season != SEASON:
        # Past seasons are fully played out — no live fetch or predictions,
        # just the completed matches straight from the DB (same shape
        # get_team_schedule already uses for a team's past-season games).
        rows = get_season_round_matches(season, round_number)
        if not rows:
            raise HTTPException(status_code=404, detail="Round not found for this season")
        return {
            "round": round_number,
            "name": rows[0]["round_title"] or f"Round {round_number}",
            "matches": [
                {
                    "match_id": r["match_url"],
                    "match_url": r["match_url"],
                    "match_state": r["match_state"],
                    "home_team": r["home_team"],
                    "away_team": r["away_team"],
                    "home_score": r["home_score"],
                    "away_score": r["away_score"],
                    "venue": r["venue"],
                    "venue_city": r["venue_city"],
                    "kickoff": r["kickoff"],
                    "home_theme_key": _TEAM_THEME_MAP.get(r["home_team"], "nrl"),
                    "away_theme_key": _TEAM_THEME_MAP.get(r["away_team"], "nrl"),
                }
                for r in rows
            ],
            "byes": [],
        }

    cache_key = round_number
    now = time.time()

    # Check response cache
    with _round_cache_lock:
        cached = _round_cache.get(cache_key)

    if cached:
        resp, ts = cached
        # A not-yet-drawn round (empty matches) is exactly as pending as a
        # live one — recheck it often so the real draw shows up promptly
        # once NRL publishes it, rather than sitting on the 30-min TTL.
        has_live = resp.get("draw_not_released") or any(
            (m.get("match_state") or "").lower() not in ("fulltime", "postmatch")
            for m in resp.get("matches", [])
        )
        ttl = _ROUND_CACHE_TTL_LIVE if has_live else _ROUND_CACHE_TTL_COMPLETED
        if now - ts < ttl:
            return resp
        # Stale — return immediately but refresh in background
        if cache_key not in _round_refreshing:
            _round_refreshing.add(cache_key)
            asyncio.create_task(_refresh_round_cache(round_number))
        return resp

    # No cache at all — must compute synchronously
    response = await _refresh_round_cache(round_number)
    if response is None:
        raise HTTPException(status_code=502, detail="Could not fetch round data from NRL")
    return response


@app.get("/api/player")
async def get_player(name: str):
    """Get a player's full game history."""
    if not name:
        raise HTTPException(status_code=400, detail="Player name is required")
    games = get_player_game_log(name)
    if not games:
        raise HTTPException(status_code=404, detail="No history found for this player")

    total_tries = sum(g["try_count"] for g in games)
    total_games = len(games)
    wins = sum(1 for g in games if g["won"])
    teams = list(dict.fromkeys(g["team"] for g in games))
    positions = list(dict.fromkeys(g["position"] for g in games if g["position"]))

    # Group by season
    seasons = {}
    for g in games:
        s = g["season"]
        if s not in seasons:
            seasons[s] = {"games": 0, "tries": 0}
        seasons[s]["games"] += 1
        seasons[s]["tries"] += g["try_count"]

    return {
        "name": name,
        "headshot": get_player_headshot(name),
        "teams": teams,
        "positions": positions,
        "total_games": total_games,
        "total_tries": total_tries,
        "try_rate": round(total_tries / total_games, 3) if total_games > 0 else 0,
        "wins": wins,
        "win_rate": round(wins / total_games, 3) if total_games > 0 else 0,
        "seasons_summary": seasons,
        "games": games,
    }


def _compute_match_detail(url, raw, home_players, away_players,
                          bookmaker_data, is_completed, match_state):
    """Heavy sync computation for match detail — runs in a thread."""

    season_match = re.search(r'/(\d{4})/', url)
    before_season = int(season_match.group(1)) if season_match else None
    before_round = _round_number_from_url(url)

    stats = parse_team_stats(raw)
    home_team = raw.get("homeTeam", {})
    away_team = raw.get("awayTeam", {})
    home_nickname = home_team.get("nickName", "Home")
    away_nickname = away_team.get("nickName", "Away")
    match_venue = raw.get("venue", "")
    match_weather = raw.get("weather", "")
    match_ground = raw.get("groundConditions", "")

    # Pre-fetch all team data in one bulk query (venue stats, attack/defence, h2h)
    prefetch_round_data(
        [home_nickname, away_nickname],
        [(home_nickname, away_nickname, match_venue)],
        last_n_games=10,
        before_season=before_season,
        before_round=before_round,
    )

    predictions = generate_predictions(
        home_players, away_players,
        stats.get("home", {}), stats.get("away", {}),
        home_team_name=home_nickname,
        away_team_name=away_nickname,
        before_season=before_season,
        before_round=before_round,
        weather=match_weather,
        ground_conditions=match_ground,
    )

    add_implied_odds_to_players(predictions["home"])
    add_implied_odds_to_players(predictions["away"])

    if bookmaker_data:
        for side, team_name in [("home", home_nickname), ("away", away_nickname)]:
            opp_name = away_nickname if side == "home" else home_nickname
            for p in predictions[side]:
                bk_list = lookup_bookmaker_odds(bookmaker_data, team_name, opp_name, p["name"])
                if bk_list:
                    p["bookmaker_odds"] = bk_list

        # Capture the match's top betting-edge picks once, pre-kickoff, for
        # the Dashboard's profit/loss simulation. Never recomputed after —
        # see edge_picks table comment in database.py::init_db().
        if not is_completed and before_season and before_round and not get_edge_pick(url):
            picks = compute_best_edge_picks(
                predictions["home"], predictions["away"],
                home_nickname, away_nickname,
                before_season, before_round, url,
            )
            for rank, pick in enumerate(picks, start=1):
                save_edge_pick(pick_rank=rank, **pick)

    win_prediction = predict_win_probability(
        home_nickname, away_nickname,
        stats.get("home", {}), stats.get("away", {}),
        before_season=before_season,
        before_round=before_round,
        venue=match_venue,
        weather=match_weather,
        ground_conditions=match_ground,
    )

    multi = generate_multi_suggestion(
        predictions["home"], predictions["away"],
        home_nickname, away_nickname,
    )

    home_summary = generate_team_summary(home_nickname,
                                          before_season=before_season, before_round=before_round)
    away_summary = generate_team_summary(away_nickname,
                                          before_season=before_season, before_round=before_round)

    value_picks_home = find_value_picks(predictions["home"], away_nickname, home_nickname,
                                         before_season=before_season, before_round=before_round)
    value_picks_away = find_value_picks(predictions["away"], home_nickname, away_nickname,
                                         before_season=before_season, before_round=before_round)

    h2h_recent_tries = get_h2h_recent_tries(home_nickname, away_nickname,
                                             before_season=before_season, before_round=before_round)

    # Top-3 picks per side. Only the names are needed: the match page section
    # that displayed these -- with an expandable "Why this pick" panel built
    # by a since-removed _build_pick_factors -- was replaced by "Last Time
    # These Teams Met" (commit 112f401), leaving the prediction row written
    # below as the only consumer. Kept in sync with the same block in
    # _record_prediction_for_match, per AGENTS.md.
    top3_home = [{"name": p["name"]} for p in predictions["home"][:3]]
    top3_away = [{"name": p["name"]} for p in predictions["away"][:3]]

    scoring = parse_scoring(raw) if is_completed else None

    if scoring:
        home_actual = set(scoring["home_try_scorers"])
        away_actual = set(scoring["away_try_scorers"])
        for pick in top3_home:
            pick["scored"] = pick["name"] in home_actual
        for pick in top3_away:
            pick["scored"] = pick["name"] in away_actual
        all_actual = home_actual | away_actual
        multi_hits = sum(1 for p in multi["picks"] if p["name"] in all_actual)
        multi["hits"] = multi_hits
        multi["all_scored"] = multi_hits == len(multi["picks"])
        for p in multi["picks"]:
            p["scored"] = p["name"] in all_actual
        for vp in value_picks_home:
            vp["scored"] = vp["name"] in home_actual
        for vp in value_picks_away:
            vp["scored"] = vp["name"] in away_actual
        if scoring["home_score"] is not None and scoring["away_score"] is not None:
            actual_winner = home_nickname if scoring["home_score"] > scoring["away_score"] else away_nickname
            if scoring["home_score"] == scoring["away_score"]:
                actual_winner = "Draw"
            win_prediction["actual_winner"] = actual_winner
            win_prediction["correct"] = win_prediction["predicted_winner"] == actual_winner

    if is_completed and scoring and before_season and before_round:
        try:
            t3h_json = json.dumps([{"name": p["name"], "scored": p.get("scored")} for p in top3_home])
            t3a_json = json.dumps([{"name": p["name"], "scored": p.get("scored")} for p in top3_away])
            t3_hits = sum(1 for p in top3_home if p.get("scored")) + sum(1 for p in top3_away if p.get("scored"))
            m_json = json.dumps([{"name": p["name"], "team": p["team"], "scored": p.get("scored")} for p in multi["picks"]])
            upsert_prediction(
                match_url=url, season=before_season, round_number=before_round,
                model_version=CHAMPION_MODEL_VERSION, home_team=home_nickname, away_team=away_nickname,
                predicted_winner=win_prediction["predicted_winner"],
                home_win_prob=win_prediction["home_win_prob"],
                predicted_home_score=win_prediction["predicted_home_score"],
                predicted_away_score=win_prediction["predicted_away_score"],
                actual_winner=win_prediction.get("actual_winner"),
                actual_home_score=scoring.get("home_score"),
                actual_away_score=scoring.get("away_score"),
                win_correct=1 if win_prediction.get("correct") else 0,
                top3_home_json=t3h_json, top3_away_json=t3a_json, top3_hits=t3_hits,
                multi_json=m_json, multi_hits=multi.get("hits", 0),
                multi_all_scored=1 if multi.get("all_scored") else 0,
            )
        except Exception as e:
            logger.warning(f"Failed to record prediction: {e}")

    home_theme = home_team.get("theme", {})
    away_theme = away_team.get("theme", {})

    # Strip internal-only fields from player predictions before sending
    _player_strip_keys = ("try_probability", "is_interchange")
    for side in ("home", "away"):
        for p in predictions.get(side, []):
            for k in _player_strip_keys:
                p.pop(k, None)

    return {
        "is_completed": is_completed,
        "home_team": home_team.get("name", home_nickname),
        "away_team": away_team.get("name", away_nickname),
        "home_nickname": home_nickname,
        "away_nickname": away_nickname,
        "home_colour": _theme_to_colour(home_theme),
        "away_colour": _theme_to_colour(away_theme),
        "home_theme_key": home_theme.get("key", "") if isinstance(home_theme, dict) else "",
        "away_theme_key": away_theme.get("key", "") if isinstance(away_theme, dict) else "",
        "home_position": home_team.get("teamPosition", ""),
        "away_position": away_team.get("teamPosition", ""),
        "odds_comparison": _build_odds_comparison(
            home_team.get("odds", ""), away_team.get("odds", ""),
            win_prediction["home_win_prob"], win_prediction["away_win_prob"],
        ),
        "venue": raw.get("venue", ""),
        "venue_city": raw.get("venueCity", ""),
        "weather": raw.get("weather", ""),
        "ground_conditions": raw.get("groundConditions", ""),
        "home_stats": stats.get("home", {}),
        "away_stats": stats.get("away", {}),
        "predictions": predictions,
        "scoring": scoring,
        "win_prediction": win_prediction,
        "multi": multi,
        "value_picks_home": value_picks_home,
        "value_picks_away": value_picks_away,
        "h2h_recent_tries": h2h_recent_tries,
        "home_summary": home_summary,
        "away_summary": away_summary,
        "db_status": {
            "matches": get_total_match_count(),
            "tries": get_total_try_count(),
        },
    }


async def _refresh_match_cache(url: str):
    """Fetch, compute and cache one match's detail. Raises the same
    HTTPExceptions the route always did, so the synchronous miss path keeps
    its 502/403 semantics; the background stale-refresh path wraps this."""
    try:
        raw = await fetch_match_detail(url)
        if raw is None:
            raise HTTPException(status_code=502, detail="Could not fetch match data from NRL")

        home_players = parse_team_list(raw, "homeTeam")
        away_players = parse_team_list(raw, "awayTeam")

        if not home_players and not away_players:
            raise HTTPException(
                status_code=403,
                detail="Team lists have not been announced for this match yet"
            )
        # Backfill headshots in a thread so DB writes don't block the event loop
        asyncio.create_task(asyncio.to_thread(_backfill_player_headshots, home_players, away_players))

        # Fetch bookmaker odds (async) before running sync computation
        match_state = raw.get("matchState", "")
        is_completed = match_state in ("FullTime", "PostMatch")
        bookmaker_data = {}
        if has_odds_api_key() and not is_completed:
            try:
                bookmaker_data = await fetch_bookmaker_odds()
            except Exception as e:
                logger.warning(f"Failed to fetch bookmaker odds: {e}")

        # Run all heavy DB/model computation in a thread
        result = await asyncio.to_thread(
            _compute_match_detail, url, raw, home_players, away_players,
            bookmaker_data, is_completed, match_state
        )
        refreshed_at = time.time()
        with _match_cache_lock:
            _match_cache[url] = (result, refreshed_at, is_completed)

        # A finished match's detail is frozen: the model runs under
        # before_season/before_round filters, so it only ever sees data up to
        # that round. Same inputs, same code, same answer -- so persist it and
        # stop paying the ~5s recompute once per process. Live and upcoming
        # matches are deliberately not persisted: their team lists, odds and
        # score are still moving, and the 2-minute TTL exists to catch that.
        if is_completed:
            try:
                await asyncio.to_thread(
                    save_cache_entry,
                    _match_cache_key_str(url),
                    json.dumps(result, default=str),
                    refreshed_at,
                )
            except Exception as e:
                logger.warning(f"Failed to persist match cache for {url}: {e}")
        return result
    finally:
        _match_refreshing.discard(url)


async def _refresh_match_cache_bg(url: str):
    """Background stale-refresh. Never raises — the caller already returned
    the stale response to the user, so a failure here must not surface."""
    try:
        await _refresh_match_cache(url)
    except Exception as e:
        logger.warning(f"Background match refresh failed for {url}: {e}")


@app.get("/api/match")
async def get_match_by_url(url: str, version: int = 3):
    # `version` is accepted for backward compatibility with old links but
    # ignored — the app only runs the V3 model now.
    if not url.startswith("/draw/"):
        raise HTTPException(status_code=400, detail="Invalid match URL path")

    # Response cache, same shape as _round_cache. This endpoint used to
    # recompute the whole model on every view -- ~7s for a match's first
    # view and ~2s for every repeat, measured against production -- even for
    # a match finished weeks ago whose detail can no longer change. Checked
    # before fetch_match_detail so a hit costs no NRL round-trip either.
    now = time.time()
    with _match_cache_lock:
        cached = _match_cache.get(url)

    if cached:
        resp, ts, was_completed = cached
        ttl = _MATCH_CACHE_TTL_COMPLETED if was_completed else _MATCH_CACHE_TTL_LIVE
        if now - ts < ttl:
            return resp
        # Stale — serve immediately, refresh behind it.
        if url not in _match_refreshing:
            _match_refreshing.add(url)
            asyncio.create_task(_refresh_match_cache_bg(url))
        return resp

    return await _refresh_match_cache(url)


# Theme key -> primary club colour, used for the badge fallback swatch.
# Mirrors frontend/src/components/MatchCard.jsx's TEAM_COLOURS.
_TEAM_COLOURS = {
    "broncos": "#6D2735", "raiders": "#56B947", "bulldogs": "#005DB5",
    "sharks": "#00A5DB", "titans": "#E8B825", "sea-eagles": "#6D2735",
    "storm": "#552D6D", "knights": "#005DB5", "cowboys": "#002B5C",
    "eels": "#005DB5", "panthers": "#2A2A2A", "rabbitohs": "#003B2F",
    "dragons": "#E2231A", "roosters": "#003B7B", "warriors": "#636466",
    "wests-tigers": "#F47920", "dolphins": "#C8102E",
}


def _theme_to_colour(theme: dict) -> str:
    if not isinstance(theme, dict):
        return "#333333"
    return _TEAM_COLOURS.get(theme.get("key", ""), "#333333")


def _selectable_seasons():
    """Seasons the year selector should offer. Always includes the current
    one even before it has any settled predictions -- otherwise the selector
    would drop the season you are actually in every January, leaving no way
    back to it from a prior year."""
    return sorted(set(get_prediction_seasons()) | {SEASON}, reverse=True)


@app.get("/api/accuracy")
async def get_accuracy(model_version: int = CHAMPION_MODEL_VERSION, season: int = SEASON):
    """Get prediction accuracy stats for the served model, this season, by
    default. Both filters matter. Without the model one, the retired V1/V2 rows
    triple-count the rounds they ran in. Without the season one, by_round groups
    on round_number alone, so next season round 1 would be silently averaged in
    with this season round 1. Explicit ?model_version= / ?season= still reach
    the history, and the by_model breakdown spans every version regardless of
    the model filter, so nothing is hidden by defaulting these."""
    stats = get_accuracy_stats(model_version=model_version, season=season)
    # Per-round betting P/L rides along so the by-round chart can show a P/L
    # column without a second request. It is model-agnostic -- edge_picks has
    # no model_version -- so unlike the accuracy figures it does not narrow
    # when ?model_version= is set.
    betting_by_round = get_betting_summary(season=season)["by_round"]
    # New dict: stats is a cached object shared with every other caller, so
    # it must not be mutated here.
    return {
        **stats,
        "season": season,
        "seasons": _selectable_seasons(),
        "betting_by_round": betting_by_round,
    }


@app.get("/api/dashboard")
async def get_dashboard(season: int = SEASON):
    """Overview data for the home dashboard: accuracy stats plus the
    betting-edge profit/loss simulation. Both are scoped to the current
    season -- neither is a career total, and pooling seasons would blend a
    finished season results into this one indefinitely."""
    return {
        "accuracy": get_accuracy_stats(model_version=CHAMPION_MODEL_VERSION, season=season),
        "betting": get_betting_summary(season=season),
        "season": season,
        "seasons": _selectable_seasons(),
    }


_TEAM_THEME_MAP = {
    "Broncos": "broncos", "Raiders": "raiders", "Bulldogs": "bulldogs",
    "Sharks": "sharks", "Titans": "titans", "Sea Eagles": "sea-eagles",
    "Storm": "storm", "Knights": "knights", "Cowboys": "cowboys",
    "Eels": "eels", "Panthers": "panthers", "Rabbitohs": "rabbitohs",
    "Dragons": "dragons", "Roosters": "roosters", "Warriors": "warriors",
    "Wests Tigers": "wests-tigers", "Dolphins": "dolphins",
}


@app.get("/api/search")
async def search(q: str, limit: int = 15):
    """Search for players and teams."""
    if not q or len(q) < 2:
        return {"players": [], "teams": []}
    players = search_players(q, limit=limit)
    team_names = search_teams(q)
    teams = [
        {
            "name": name,
            "theme_key": _TEAM_THEME_MAP.get(name, "nrl"),
            "colour": _theme_to_colour({"key": _TEAM_THEME_MAP.get(name, "nrl")}),
        }
        for name in team_names
    ]
    return {"players": players, "teams": teams}


@app.get("/api/team")
async def get_team(name: str, season: int = SEASON):
    """Get comprehensive team stats."""
    if not name:
        raise HTTPException(status_code=400, detail="Team name is required")

    # Check team exists
    all_teams = get_all_teams()
    if name not in all_teams:
        raise HTTPException(status_code=404, detail="Team not found")

    form = get_team_attack_defence(name, last_n_games=10)
    ha = get_home_away_win_rate(name)
    edge_vuln = get_team_tries_conceded_by_edge(name, last_n_games=15)
    roster = get_team_roster(name, season=season)
    recent = get_team_recent_results(name, last_n=10)
    summary = generate_team_summary(name)

    # Form string (W/L/D for last 10)
    form_str = [r["result"] for r in recent]

    theme_key = _TEAM_THEME_MAP.get(name, "nrl")

    return {
        "name": name,
        "theme_key": theme_key,
        "colour": _theme_to_colour({"key": theme_key}),
        "season": season,
        "stats": {
            "avg_scored": round(form["avg_scored"], 1),
            "avg_conceded": round(form["avg_conceded"], 1),
            "wins": form["wins"],
            "played": form["played"],
            "home_win_rate": round(ha["home_win_rate"], 3),
            "away_win_rate": round(ha["away_win_rate"], 3),
            "home_played": ha["home_played"],
            "away_played": ha["away_played"],
        },
        "edge_vulnerability": {k: dict(v) for k, v in edge_vuln.items()} if edge_vuln else {},
        "summary": summary,
        "form": form_str,
        "recent_results": recent,
        "roster": roster,
    }


@app.get("/api/team-schedule")
async def get_team_schedule(name: str, season: int = SEASON):
    """A team's games for a season, shaped for the match-card grid used on
    the round tab — live round-by-round data (with predictions, including
    upcoming games) for the current season, completed historical matches
    from the DB for past seasons."""
    if not name:
        raise HTTPException(status_code=400, detail="Team name is required")

    if season == SEASON:
        matches = await _get_current_season_team_matches(name)
    else:
        matches = [
            {
                "match_id": r["match_url"],
                "match_url": r["match_url"],
                "round_number": r["round_number"],
                "round_name": r["round_title"] or f"Round {r['round_number']}",
                "match_state": r["match_state"],
                "home_team": r["home_team"],
                "away_team": r["away_team"],
                "home_score": r["home_score"],
                "away_score": r["away_score"],
                "venue": r["venue"],
                "venue_city": r["venue_city"],
                "kickoff": r["kickoff"],
                "home_theme_key": _TEAM_THEME_MAP.get(r["home_team"], "nrl"),
                "away_theme_key": _TEAM_THEME_MAP.get(r["away_team"], "nrl"),
            }
            for r in get_team_season_matches(name, season)
        ]

    return {"team": name, "season": season, "matches": matches}


@app.get("/api/ladder")
async def get_ladder(season: int = SEASON):
    """Competition ladder for a season, straight from NRL's own ladder API
    (regular season only — finals don't change comp points, same as NRL's
    own ladder page)."""
    raw = await fetch_ladder(season)
    if raw is None:
        raise HTTPException(status_code=502, detail="Could not fetch ladder data from NRL")
    return {"season": season, "ladder": parse_ladder(raw)}


@app.get("/api/teams")
async def list_teams():
    """List all teams, with badge theme + colour for the Team Stats index page."""
    return [
        {
            "name": name,
            "theme_key": _TEAM_THEME_MAP.get(name, "nrl"),
            "colour": _theme_to_colour({"key": _TEAM_THEME_MAP.get(name, "nrl")}),
        }
        for name in get_all_teams()
    ]


@app.get("/api/players")
async def list_players(season: int = None):
    """List every player. Pass `season` to scope players/position/totals to
    that season only; omitted, totals are career-wide."""
    return get_all_players(season=season)

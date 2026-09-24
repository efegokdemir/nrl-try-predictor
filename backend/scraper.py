"""
Historical NRL data scraper.
Fetches match data from 2020-2026, including team lists and try scorers.
Stores everything in Postgres (see database.py).
"""

import asyncio
import httpx
import json
import logging

from field_side import JERSEY_FIELD_SIDE
from database import (
    init_db, get_db, is_round_scraped, mark_round_scraped,
    insert_match, bulk_insert_match_data, get_total_match_count, match_exists, update_match_conditions,
    repair_scrape_progress,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://www.nrl.com"
COMPETITION_ID = 111
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/html, */*",
}

# NRL fronts /draw/data with an OpenID Connect "silent SSO" check: a request
# with no session gets a redirect that dead-ends without a real login, but
# that same redirect response sets an anonymous `nrl_sso_probed` cookie, and
# retrying the same URL with it present succeeds directly (see nrl_client.py
# for the full explanation). follow_redirects is off on both clients below
# so we see the redirect and can retry, instead of auto-following into the
# dead end.
_REDIRECT_STATUS_CODES = (301, 302, 303, 307, 308)


async def _get_json(client: httpx.AsyncClient, url: str, error_label: str):
    """GET a URL from nrl.com and return its parsed JSON, or None on any
    failure — retries once on NRL's SSO probe redirect."""
    resp = await client.get(url)
    if resp.status_code in _REDIRECT_STATUS_CODES:
        resp = await client.get(url)
    if resp.status_code != 200:
        logger.warning(f"Failed to fetch {error_label}: {resp.status_code}")
        return None
    try:
        return resp.json()
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"Invalid JSON fetching {error_label}: {e}")
        return None


# Season config: (year, total_rounds_including_finals)
SEASONS = [
    (2020, 24),  # 20 rounds + 4 finals
    (2021, 29),  # 25 rounds + 4 finals
    (2022, 29),  # 25 rounds + 4 finals
    (2023, 31),  # 27 rounds + 4 finals
    (2024, 31),  # 27 rounds + 4 finals
    (2025, 31),  # 27 rounds + 4 finals
    (2026, 31),  # current season - 27 rounds + 4 finals weeks
]


async def scrape_all():
    """Main entry point - scrape all historical data."""
    init_db()

    existing = get_total_match_count()
    logger.info(f"Database has {existing} completed matches. Starting scrape...")

    async with httpx.AsyncClient(headers=HEADERS, timeout=20.0, follow_redirects=False) as client:
        for season, total_rounds in SEASONS:
            for rnd in range(1, total_rounds + 1):
                if is_round_scraped(season, rnd):
                    continue

                logger.info(f"Scraping {season} round {rnd}...")
                try:
                    await scrape_round(client, season, rnd)
                    mark_round_scraped(season, rnd)
                except Exception as e:
                    logger.error(f"Error scraping {season} R{rnd}: {e}")

                # Rate limiting - be respectful
                await asyncio.sleep(0.2)

    final = get_total_match_count()
    logger.info(f"Scrape complete. Database now has {final} completed matches.")


async def scrape_round(client: httpx.AsyncClient, season: int, round_number: int):
    """Scrape all matches in a round."""
    url = f"{BASE_URL}/draw/data?competition={COMPETITION_ID}&season={season}&round={round_number}"
    data = await _get_json(client, url, f"{season} R{round_number}")
    if data is None:
        return
    fixtures = data.get("fixtures", [])

    for match in fixtures:
        if not isinstance(match, dict):
            continue

        match_state = match.get("matchState", "")
        if match_state not in ("FullTime", "PostMatch"):
            continue  # Only store completed matches

        home = match.get("homeTeam", {})
        away = match.get("awayTeam", {})
        clock = match.get("clock", {})
        match_url = match.get("matchCentreUrl", "")

        if not match_url:
            continue

        round_title = match.get("roundTitle", f"Round {round_number}")
        home_team = home.get("nickName", "Unknown")
        away_team = away.get("nickName", "Unknown")
        home_score = home.get("score")
        away_score = away.get("score")
        venue = match.get("venue", "")
        venue_city = match.get("venueCity", "")
        kickoff = clock.get("kickOffTimeLong", "") if isinstance(clock, dict) else ""

        match_id = insert_match(
            season, round_number, round_title, match_url, match_state,
            home_team, away_team, home_score, away_score, venue, kickoff,
            venue_city=venue_city
        )

        if not match_id:
            continue

        # Fetch match detail for team lists and try scorers
        try:
            await scrape_match_detail(client, match_id, match_url, home_team, away_team)
            await asyncio.sleep(0.3)  # Rate limit between match details
        except Exception as e:
            logger.error(f"Error fetching detail for {match_url}: {e}")


def _determine_field_side(player_name, jersey_number, side, players_data,
                          interchanges_data, try_game_seconds):
    """
    Determine the field side for a try scorer.
    For starters (1-13): use their jersey number directly.
    For bench (14+): find which starter they replaced via interchange data,
    and use that starter's jersey number to infer field side.
    """
    if 1 <= jersey_number <= 13:
        return JERSEY_FIELD_SIDE.get(jersey_number, "middle")

    # Bench player (14+) — find the most recent interchange where they came on
    # before or around the try time, to determine whose spot they filled
    relevant_subs = [
        ic for ic in interchanges_data
        if ic["side"] == side
        and ic["player_on"] == player_name
        and ic["game_seconds"] <= try_game_seconds
    ]

    if relevant_subs:
        # Use the most recent substitution before the try
        relevant_subs.sort(key=lambda x: x["game_seconds"], reverse=True)
        replaced_name = relevant_subs[0]["player_off"]
        replaced_jersey = relevant_subs[0].get("jersey_off", 0)

        # If we know the replaced player's jersey, use it
        if 1 <= replaced_jersey <= 13:
            return JERSEY_FIELD_SIDE.get(replaced_jersey, "middle")

        # Fall back: look up the replaced player's jersey in the team list
        for p in players_data:
            if p["name"] == replaced_name and p["side"] == side:
                j = p["jersey_number"]
                if 1 <= j <= 13:
                    return JERSEY_FIELD_SIDE.get(j, "middle")
                break

    # Can't determine — fall back to middle
    return "middle"


def _parse_minute_to_seconds(minute_str: str) -> int:
    """Convert a minute string like \"27'\" to approximate game seconds."""
    try:
        mins = int(minute_str.replace("'", "").strip())
        return mins * 60
    except (ValueError, AttributeError):
        return 0


async def scrape_match_detail(client: httpx.AsyncClient, match_id: int,
                               match_url: str, home_team: str, away_team: str):
    """Fetch and store team lists, try scorers, and interchanges for a single match."""
    detail_url = f"{BASE_URL}{match_url.rstrip('/')}/data"
    data = await _get_json(client, detail_url, f"match detail {detail_url}")
    if data is None:
        return
    players_data = []
    tries_data = []
    interchanges_data = []

    # Build player ID → name/jersey lookup from both teams
    player_id_map = {}  # {playerId: {"name": ..., "jersey": ..., "side": ...}}

    for side, team_key, team_name in [("home", "homeTeam", home_team), ("away", "awayTeam", away_team)]:
        team = data.get(team_key, {})

        # Parse players
        player_list = team.get("players", [])
        for p in player_list:
            if not isinstance(p, dict):
                continue
            first = p.get("firstName", "")
            last = p.get("lastName", "")
            name = f"{first} {last}".strip()
            if not name:
                continue

            jersey = p.get("number", 0)
            player_id = p.get("playerId", 0)

            if player_id:
                player_id_map[player_id] = {
                    "name": name,
                    "jersey": jersey,
                    "side": side,
                    "team": team_name,
                }

            # Clean up NRL's headshot URL (mirror nrl_client.parse_team_list)
            head_img = p.get("headImage", "") or ""
            if head_img:
                proxy_marker = "remote.axd?"
                idx = head_img.find(proxy_marker)
                if idx >= 0:
                    head_img = head_img[idx + len(proxy_marker):]
                elif not head_img.startswith("http"):
                    head_img = f"{BASE_URL}{head_img}"
                center_idx = head_img.find("?center=")
                if center_idx >= 0:
                    head_img = head_img[:center_idx]

            players_data.append({
                "team": team_name,
                "side": side,
                "name": name,
                "jersey_number": jersey,
                "position": p.get("position", ""),
                "is_interchange": not p.get("isOnField", True),
                "headshot": head_img,
            })

        # Parse tries (from scoring summaries)
        scoring = team.get("scoring", {})
        tries = scoring.get("tries", {})
        summaries = tries.get("summaries", [])
        for s in summaries:
            if not isinstance(s, str):
                continue
            parts = s.rsplit(" ", 1)
            player_name = parts[0].strip() if len(parts) == 2 else s.strip()
            minute = parts[1].strip() if len(parts) == 2 else ""

            tries_data.append({
                "team": team_name,
                "side": side,
                "player_name": player_name,
                "minute": minute,
            })

    # Parse timeline for interchange events
    timeline = data.get("timeline", [])

    for event in timeline:
        if not isinstance(event, dict):
            continue
        etype = event.get("type", "")

        if etype == "Interchange":
            on_id = event.get("playerId", 0)
            off_id = event.get("offPlayerId", 0)
            game_secs = event.get("gameSeconds", 0)

            on_info = player_id_map.get(on_id, {})
            off_info = player_id_map.get(off_id, {})

            if on_info and off_info:
                interchanges_data.append({
                    "side": on_info.get("side", ""),
                    "player_on": on_info["name"],
                    "player_off": off_info["name"],
                    "jersey_on": on_info.get("jersey", 0),
                    "jersey_off": off_info.get("jersey", 0),
                    "game_seconds": game_secs,
                })

    # Now determine field_side for each try
    for t in tries_data:
        # Find the try scorer's jersey number from the player list
        scorer_jersey = 0
        for p in players_data:
            if p["name"] == t["player_name"] and p["side"] == t["side"]:
                scorer_jersey = p["jersey_number"]
                break

        try_seconds = _parse_minute_to_seconds(t["minute"])
        t["field_side"] = _determine_field_side(
            t["player_name"], scorer_jersey, t["side"],
            players_data, interchanges_data, try_seconds
        )

    # Update weather/ground conditions from match detail data
    weather = data.get("weather", "")
    ground_conditions = data.get("groundConditions", "")
    if weather or ground_conditions:
        update_match_conditions(match_id, weather or "", ground_conditions or "")

    if players_data or tries_data:
        bulk_insert_match_data(match_id, players_data, tries_data, interchanges_data)


CURRENT_SEASON = 2026
CURRENT_SEASON_ROUNDS = 31  # 27 regular-season rounds + 4 finals weeks


async def _scrape_single_match(client, season, round_number, match):
    """Scrape and store a single completed match."""
    home = match.get("homeTeam", {})
    away = match.get("awayTeam", {})
    clock = match.get("clock", {})
    match_url = match.get("matchCentreUrl", "")
    match_state = match.get("matchState", "")
    round_title = match.get("roundTitle", f"Round {round_number}")
    home_team = home.get("nickName", "Unknown")
    away_team = away.get("nickName", "Unknown")
    home_score = home.get("score")
    away_score = away.get("score")
    venue = match.get("venue", "")
    venue_city = match.get("venueCity", "")
    kickoff = clock.get("kickOffTimeLong", "") if isinstance(clock, dict) else ""

    match_id = insert_match(
        season, round_number, round_title, match_url, match_state,
        home_team, away_team, home_score, away_score, venue, kickoff,
        venue_city=venue_city
    )
    if not match_id:
        return

    logger.info(f"New match: {home_team} vs {away_team} (R{round_number})")
    try:
        await scrape_match_detail(client, match_id, match_url, home_team, away_team)
    except Exception as e:
        logger.error(f"Error fetching detail for {match_url}: {e}")


async def sync_current_season():
    """
    Incrementally sync the current season.
    Only processes newly completed matches that don't already exist in the DB.
    Skips fully-scraped rounds entirely. Stops at the first round NRL hasn't
    actually drawn yet.
    """
    init_db()
    logger.info("Starting current season sync...")

    repaired = repair_scrape_progress(CURRENT_SEASON)
    if repaired:
        logger.warning(f"Re-checking rounds {repaired} — previously marked scraped with no matches recorded.")

    new_matches = 0
    prev_urls = None  # fixture match_urls from the last round we actually fetched

    async with httpx.AsyncClient(headers=HEADERS, timeout=20.0, follow_redirects=False) as client:
        for rnd in range(1, CURRENT_SEASON_ROUNDS + 1):
            # Skip rounds already fully scraped — they won't change
            if is_round_scraped(CURRENT_SEASON, rnd):
                continue

            url = f"{BASE_URL}/draw/data?competition={COMPETITION_ID}&season={CURRENT_SEASON}&round={rnd}"
            try:
                data = await _get_json(client, url, f"{CURRENT_SEASON} R{rnd}")
                if data is None:
                    continue
                fixtures = data.get("fixtures", [])
                if not fixtures:
                    continue

                # NRL doesn't error for a round beyond what's currently
                # drawn — it echoes back the latest real round's fixtures
                # (same match_urls), which would otherwise look like a
                # genuine, fully-completed round and get permanently marked
                # scraped despite having nothing real to insert (see
                # repair_scrape_progress). If we don't have the previous
                # round's fixtures in hand (e.g. it was already marked
                # scraped and skipped above), fetch it fresh to compare.
                fixture_urls = {f.get("matchCentreUrl") for f in fixtures
                                 if isinstance(f, dict) and f.get("matchCentreUrl")}
                compare_urls = prev_urls
                if compare_urls is None and rnd > 1:
                    prev_data = await _get_json(
                        client,
                        f"{BASE_URL}/draw/data?competition={COMPETITION_ID}&season={CURRENT_SEASON}&round={rnd - 1}",
                        f"{CURRENT_SEASON} R{rnd - 1} (echo-check)",
                    )
                    if prev_data is not None:
                        compare_urls = {f.get("matchCentreUrl") for f in prev_data.get("fixtures", [])
                                         if isinstance(f, dict) and f.get("matchCentreUrl")}
                if compare_urls and fixture_urls and fixture_urls == compare_urls:
                    logger.info(f"Round {rnd}: fixtures are an echo of the previous round — not drawn yet, stopping.")
                    break
                prev_urls = fixture_urls

                completed = [f for f in fixtures if isinstance(f, dict)
                             and f.get("matchState") in ("FullTime", "PostMatch")]
                if not completed:
                    logger.info(f"Round {rnd}: no completed matches yet, stopping.")
                    break

                # Only scrape matches whose match_url is NOT already in the DB
                for match in completed:
                    match_url = match.get("matchCentreUrl", "")
                    if not match_url or match_exists(match_url):
                        continue
                    # New completed match — scrape it
                    await _scrape_single_match(client, CURRENT_SEASON, rnd, match)
                    new_matches += 1
                    await asyncio.sleep(0.3)

                # If every match in the round is completed, mark it so we never check again
                all_done = all(
                    f.get("matchState") in ("FullTime", "PostMatch")
                    for f in fixtures if isinstance(f, dict)
                )
                if all_done:
                    mark_round_scraped(CURRENT_SEASON, rnd)
                    logger.info(f"Round {rnd}: all matches done, marked as fully scraped.")

            except Exception as e:
                logger.error(f"Error syncing {CURRENT_SEASON} R{rnd}: {e}")

    count = get_total_match_count()
    logger.info(f"Sync complete. {new_matches} new matches added. DB total: {count}.")
    return new_matches


def run_scraper():
    """Synchronous wrapper to run the async scraper."""
    asyncio.run(scrape_all())


def run_sync_current_season():
    """Synchronous wrapper to run the async current season sync."""
    asyncio.run(sync_current_season())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_scraper()

"""
Player try scorer odds utilities.

Converts model try-scoring probabilities into equivalent decimal odds
so users can compare against bookmakers (e.g. bet365) side-by-side.

Optionally integrates with The Odds API (https://the-odds-api.com)
for automated bookmaker odds when ODDS_API_KEY is set.
"""

import os
import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
BASE_URL = "https://api.the-odds-api.com/v4"
SPORT = "rugbyleague_nrl"
MARKET_KEY = "player_try_scorer_anytime"

# Cache: {"data": {...}, "fetched_at": timestamp}
_cache = {}
CACHE_TTL = 300  # 5 minutes


def has_api_key() -> bool:
    return bool(ODDS_API_KEY)


def prob_to_decimal_odds(probability: float) -> Optional[float]:
    """Convert a probability (0-1) to decimal odds. Returns None if prob <= 0."""
    if probability <= 0:
        return None
    return round(1.0 / probability, 2)


def add_implied_odds_to_players(players: list) -> list:
    """
    Add model-implied decimal odds to each player prediction.
    Modifies players in-place and returns them.
    """
    for p in players:
        pct = p.get("try_percentage", 0)
        prob = pct / 100.0
        p["model_odds"] = prob_to_decimal_odds(prob) if prob > 0 else None
    return players


async def fetch_bookmaker_odds() -> dict:
    """
    Fetch anytime try scorer odds from The Odds API (if key configured).
    Uses per-event endpoint which supports player prop markets.
    Returns all bookmakers' odds per player:
    {
        normalised_matchup_key: {
            normalised_player_name: [
                {"bookmaker": "SportsBet", "decimal": 1.87},
                {"bookmaker": "TAB", "decimal": 1.80},
                ...
            ]
        }
    }
    """
    if not ODDS_API_KEY:
        return {}

    cache_key = "_ats"
    if cache_key in _cache and time.time() - _cache[cache_key]["fetched_at"] < CACHE_TTL:
        return _cache[cache_key]["data"]

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            events_resp = await client.get(
                f"{BASE_URL}/sports/{SPORT}/events",
                params={"apiKey": ODDS_API_KEY},
            )
            if events_resp.status_code != 200:
                logger.warning(f"Odds API events request failed: {events_resp.status_code}")
                return {}

            events = events_resp.json()
            result = {}

            for event in events:
                event_id = event.get("id")
                if not event_id:
                    continue

                odds_resp = await client.get(
                    f"{BASE_URL}/sports/{SPORT}/events/{event_id}/odds",
                    params={
                        "apiKey": ODDS_API_KEY,
                        "regions": "au",
                        "markets": MARKET_KEY,
                        "oddsFormat": "decimal",
                    },
                )
                if odds_resp.status_code != 200:
                    continue

                event_data = odds_resp.json()
                home = event_data.get("home_team", "")
                away = event_data.get("away_team", "")
                key = f"{_normalise(home)} v {_normalise(away)}"
                players = {}  # {norm_name: [{"bookmaker": ..., "decimal": ...}, ...]}

                for bm in event_data.get("bookmakers", []):
                    bm_name = bm.get("title", "")
                    for market in bm.get("markets", []):
                        if market.get("key") != MARKET_KEY:
                            continue
                        for outcome in market.get("outcomes", []):
                            name = outcome.get("description", "") or outcome.get("name", "")
                            price = outcome.get("price", 0)
                            if name and price and price > 1:
                                norm = _normalise(name)
                                if norm not in players:
                                    players[norm] = []
                                players[norm].append({
                                    "bookmaker": bm_name,
                                    "decimal": price,
                                })

                if players:
                    result[key] = players

            _cache[cache_key] = {"data": result, "fetched_at": time.time()}
            logger.info(f"Fetched bookmaker odds for {len(result)} events, {sum(len(v) for v in result.values())} players")
            return result

    except Exception as e:
        logger.error(f"Odds API error: {e}")
        return {}


def compute_best_edge_picks(home_players: list, away_players: list, home_team: str, away_team: str,
                             season: int, round_number: int, match_url: str, limit: int = 3) -> list:
    """
    Find the top `limit` positive betting-edge try-scorer picks across both
    teams for a match: model probability vs. the best bookmaker price on
    offer, ranked by edge descending. Returns [] if no player has a positive
    edge (or no bookmaker odds were attached at all). Distinct from
    `find_value_picks` in model.py, which has nothing to do with bookmaker
    odds.
    """
    candidates = []
    for players, team in [(home_players, home_team), (away_players, away_team)]:
        for p in players:
            bk_list = p.get("bookmaker_odds")
            if not bk_list:
                continue
            best_bk = max(bk_list, key=lambda b: b["decimal"])
            implied = 1.0 / best_bk["decimal"]
            model_prob = p.get("try_percentage", 0) / 100.0
            edge = model_prob - implied
            if edge > 0:
                candidates.append({
                    "match_url": match_url,
                    "season": season,
                    "round_number": round_number,
                    "home_team": home_team,
                    "away_team": away_team,
                    "player_name": p["name"],
                    "player_team": team,
                    "model_probability": round(model_prob, 4),
                    "bookmaker_decimal_odds": best_bk["decimal"],
                    "bookmaker_name": best_bk["bookmaker"],
                    "implied_probability": round(implied, 4),
                    "edge": round(edge, 4),
                })

    candidates.sort(key=lambda c: c["edge"], reverse=True)
    return candidates[:limit]


def _normalise(name: str) -> str:
    return " ".join(name.lower().strip().split())


def _first_names_compatible(a: str, b: str) -> bool:
    """Whether two given names could plausibly belong to the same person.

    Bookmakers are inconsistent about the first name -- "J. Smith",
    "Josh Smith", "Joshua Smith" all show up -- so requiring an exact match
    loses real odds. Matching on the first letter alone is the opposite
    problem: it makes every J-something with the same surname look like the
    same player.
    """
    a, b = a.rstrip("."), b.rstrip(".")
    if a == b:
        return True
    if len(a) == 1 or len(b) == 1:
        return a[:1] == b[:1]          # an initial against a full name
    return a.startswith(b) or b.startswith(a)   # "matt" / "matthew"


def lookup_bookmaker_odds(
    bookmaker_data: dict,
    home_team: str,
    away_team: str,
    player_name: str,
) -> Optional[list]:
    """
    Look up a player's bookmaker odds from fetched data.
    Returns list of {"bookmaker": str, "decimal": float} or None.

    An exact name match wins outright. Failing that, a surname plus a
    compatible given name is accepted, but only when it picks out exactly
    one player: `players` covers both teams in the match, and the NRL has
    no shortage of shared surnames, so a loose rule can hand a player their
    namesake's -- or an opponent's -- price. Nothing downstream would catch
    it. The wrong price goes onto the match page and into
    compute_best_edge_picks, which feeds the dashboard's profit/loss
    simulation, where it would read as a real result. When the name is
    ambiguous, no odds is the correct answer.
    """
    if not bookmaker_data:
        return None

    norm_player = _normalise(player_name)
    norm_home = _normalise(home_team)
    norm_away = _normalise(away_team)
    parts = norm_player.split()

    for bk_key, players in bookmaker_data.items():
        # Match if both team nicknames appear in the bookmaker key
        if norm_home not in bk_key or norm_away not in bk_key:
            continue
        if norm_player in players:
            return players[norm_player]
        if len(parts) < 2:
            continue

        candidates = []
        for pname, pdata in players.items():
            pp = pname.split()
            if len(pp) >= 2 and pp[-1] == parts[-1] and _first_names_compatible(pp[0], parts[0]):
                candidates.append((pname, pdata))

        if len(candidates) == 1:
            return candidates[0][1]
        if len(candidates) > 1:
            logger.warning(
                f"Ambiguous bookmaker odds for {player_name!r} in {bk_key!r}: "
                f"{[n for n, _ in candidates]} all match. Skipping rather than "
                f"risk attaching another player's price."
            )
    return None

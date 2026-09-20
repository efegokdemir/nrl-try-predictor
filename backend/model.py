"""
NRL Prediction Model - powered by historical data (2020-2026).

Factors used for try and win probability:
    - Position base rates, career + recency-weighted (last-5/last-10) player try factor
    - Team attack/defence form, edge vulnerability (position-specific sensitivity)
    - Venue-specific home advantage, weather/ground conditions
    - Margin-of-victory weighted form (big wins count more than scrappy wins)
    - Rest days / short turnaround penalty, bye-week freshness boost
    - Season progression weighting (early-season results discounted)
    - Opponent-quality adjusted try rates (tries vs strong defences worth more)
    - Interchange timing (bench minutes based on actual interchange data)
    - Probability calibration from historical prediction accuracy
"""

import numpy as np
import logging
from field_side import JERSEY_FIELD_SIDE
from database import (
    get_position_try_rates,
    get_players_try_histories_batch,
    get_team_attack_defence,
    get_h2h_record,
    get_home_away_win_rate,
    get_team_tries_conceded_by_position,
    get_team_tries_conceded_by_edge,
    get_players_recent_form_batch,
    get_venue_stats,
    clear_query_cache,
    get_team_margin_weighted_form,
    get_team_rest_days,
    get_team_had_bye,
    get_bench_minutes_batch,
    get_calibration_data,
    get_quality_adjusted_tries_batch,
)

logger = logging.getLogger(__name__)

# Fallback rates if DB is empty or position not found
FALLBACK_POSITION_RATES = {
    "Fullback": 0.28,
    "Winger": 0.35,
    "Centre": 0.22,
    "Five-Eighth": 0.15,
    "Halfback": 0.12,
    "Prop": 0.06,
    "Hooker": 0.10,
    "2nd Row": 0.10,
    "Lock": 0.08,
    "Interchange": 0.05,
}

LEAGUE_AVG_PPG = 22.0
HOME_ADVANTAGE_WIN = 0.08  # +8% win probability at home
HOME_ADVANTAGE_TRY = 1.06  # 6% more likely to score tries at home

# Position-specific sensitivity to edge vulnerability.
# Weights derived from 2026 rounds 1-8 try distribution per edge:
#   Left edge:  Winger 31%, Five-Eighth 23%, Centre 23%, 2nd Row 22%
#   Right edge: Winger 43%, Centre 27%, 2nd Row 15%, Halfback 11%
# Each weight is proportional to the position's share of edge tries,
# scaled so wingers (highest share) = 0.30.
POSITION_EDGE_SENSITIVITY = {
    "Winger": 0.30,       # 37% avg edge share — baseline
    "Centre": 0.20,       # 25% avg edge share
    "Five-Eighth": 0.19,  # 23% of left edge tries
    "Second Row": 0.15,   # 19% avg edge share
    "2nd Row": 0.15,
    "Halfback": 0.09,     # 11% of right edge tries
    "Fullback": 0.28,     # 95% of fullback-category tries
    "Prop": 0.03,         # ~5% middle share, rarely on edges
    "Hooker": 0.04,       # ~14% middle share
    "Lock": 0.04,         # ~14% middle share
    "Interchange": 0.05,  # ~30% middle share, varies by replaced position
}
_DEFAULT_EDGE_SENSITIVITY = 0.05

# Weather keywords that indicate wet/adverse conditions
_WET_KEYWORDS = {"rain", "rainy", "showers", "shower", "wet", "drizzle", "storm", "stormy", "thunderstorm"}
_HEAVY_GROUND = {"heavy", "soft", "wet", "muddy"}


def _get_weather_factor(weather: str, ground_conditions: str, position: str) -> float:
    """Calculate a multiplier for try probability based on weather/ground."""
    if not weather and not ground_conditions:
        return 1.0

    w_lower = (weather or "").lower()
    g_lower = (ground_conditions or "").lower()

    is_wet = any(kw in w_lower for kw in _WET_KEYWORDS)
    is_heavy = any(kw in g_lower for kw in _HEAVY_GROUND)

    if not is_wet and not is_heavy:
        return 1.0

    # Backs affected more than forwards in wet weather
    pos_lower = (position or "").lower()
    is_back = any(p in pos_lower for p in ("wing", "centre", "center", "full", "five", "half"))

    if is_wet and is_heavy:
        return 0.88 if is_back else 0.94
    elif is_wet:
        return 0.92 if is_back else 0.97
    else:  # heavy ground only
        return 0.94 if is_back else 0.97


# Cache for position rates (refreshed per request batch)
_position_rates_cache = None


def _get_position_rates() -> dict:
    """Get position try rates from DB, with fallback."""
    global _position_rates_cache
    if _position_rates_cache is not None:
        return _position_rates_cache

    db_rates = get_position_try_rates()
    if db_rates:
        _position_rates_cache = db_rates
        return db_rates
    return {}


def _match_position(position_name: str, rates: dict) -> float:
    """Find the best matching position rate."""
    if not position_name:
        return FALLBACK_POSITION_RATES.get("Interchange", 0.05)

    # Exact match
    if position_name in rates:
        return rates[position_name]["rate"]

    # Fuzzy match against DB positions
    pos_lower = position_name.lower()
    for db_pos, info in rates.items():
        if db_pos.lower() == pos_lower:
            return info["rate"]

    # Fallback mapping
    for key, fallback_rate in FALLBACK_POSITION_RATES.items():
        if key.lower() in pos_lower or pos_lower in key.lower():
            return fallback_rate

    return FALLBACK_POSITION_RATES.get("Interchange", 0.05)


def _get_player_try_factor_from_history(history: list, position: str) -> float:
    """
    Calculate a player-specific try factor from pre-fetched historical data.
    Blends recent 5-game form (60%) with career rate (40%).
    Returns a multiplier (1.0 = average for position).
    """
    if not history or len(history) < 3:
        return 1.0  # Not enough data, assume average

    rates = _get_position_rates()
    expected_rate = _match_position(position, rates)
    if expected_rate <= 0:
        return 1.0

    total_games = len(history)
    total_tries = sum(h["tries_scored"] for h in history)
    career_rate = total_tries / total_games if total_games > 0 else 0

    # Recent form (last 5 games) — history is ordered oldest-first
    recent = history[-5:]
    recent_games = len(recent)
    recent_tries = sum(h["tries_scored"] for h in recent)
    recent_rate = recent_tries / recent_games if recent_games > 0 else 0

    if recent_games >= 3:
        blended_rate = recent_rate * 0.6 + career_rate * 0.4
    else:
        blended_rate = career_rate

    factor = blended_rate / expected_rate
    # Clamp between 0.4 and 2.5 to avoid extreme outliers
    return max(0.4, min(factor, 2.5))


def _predict_try_with_history(
    player: dict,
    history: list,
    team_name: str,
    opp_name: str,
    is_home: bool,
    team_attack: dict,
    opp_defence: dict,
    opp_edge_vulnerability: dict = None,
    weather: str = "",
    ground_conditions: str = "",
    v3_context: dict = None,
) -> float:
    """Predict try probability using pre-fetched player history.
    v3_context (optional): dict with keys bench_minutes, calibration, rest_days,
    bye_week, round_number, quality_adj_rates, margin_form — populated whenever
    the caller knows the match's season/round; situational adjustments below are
    skipped gracefully when it's unavailable."""
    position = player.get("position", "Interchange")
    is_bench = player.get("is_interchange", False) or player.get("number", 0) >= 14
    jersey = player.get("number", 0)
    name = player.get("name", "")

    rates = _get_position_rates()
    base_rate = _match_position(position, rates)

    player_factor = _get_player_try_factor_from_history(history, position)

    # Blend in opponent-quality adjusted try rate
    if v3_context:
        qa_rate = v3_context.get("quality_adj_rates", {}).get(name, -1.0)
        if qa_rate >= 0:
            expected_rate = _match_position(position, rates)
            if expected_rate > 0:
                qa_factor = qa_rate / expected_rate
                qa_factor = max(0.5, min(qa_factor, 2.0))
                # Blend: 70% standard player factor, 30% quality-adjusted
                player_factor = player_factor * 0.7 + qa_factor * 0.3

    rate = base_rate * player_factor

    team_avg_scored_10 = team_attack.get("avg_scored", LEAGUE_AVG_PPG)
    team_avg_scored_5 = team_attack.get("avg_scored_recent", team_avg_scored_10)
    team_avg_scored = team_avg_scored_5 * 0.6 + team_avg_scored_10 * 0.4
    attack_factor = team_avg_scored / LEAGUE_AVG_PPG
    attack_factor = max(0.6, min(attack_factor, 1.8))
    rate *= attack_factor

    opp_avg_conceded_10 = opp_defence.get("avg_conceded", LEAGUE_AVG_PPG)
    opp_avg_conceded_5 = opp_defence.get("avg_conceded_recent", opp_avg_conceded_10)
    opp_avg_conceded = opp_avg_conceded_5 * 0.6 + opp_avg_conceded_10 * 0.4
    defence_factor = opp_avg_conceded / LEAGUE_AVG_PPG
    defence_factor = max(0.6, min(defence_factor, 1.8))
    rate *= defence_factor

    # Edge vulnerability — position-specific sensitivity so wingers get the
    # full factor, halves/centres/2nd rowers get proportionally less
    if opp_edge_vulnerability and 1 <= jersey <= 13:
        player_edge = JERSEY_FIELD_SIDE.get(jersey, "middle")
        edge_info = opp_edge_vulnerability.get(player_edge, {})
        edge_vuln = edge_info.get("vulnerability", 1.0)
        if edge_vuln != 1.0:
            edge_weight = POSITION_EDGE_SENSITIVITY.get(position, _DEFAULT_EDGE_SENSITIVITY)
            edge_factor = 1.0 + (edge_vuln - 1.0) * edge_weight
            edge_factor = max(0.85, min(edge_factor, 1.20))
            rate *= edge_factor

    # Home advantage
    if is_home:
        rate *= HOME_ADVANTAGE_TRY

    # Weather
    weather_factor = _get_weather_factor(weather, ground_conditions, position)
    rate *= weather_factor

    # --- Situational adjustments (need before_season/before_round context) ---
    if v3_context:
        # Rest days: short turnaround penalty
        rest = v3_context.get("rest_days", -1)
        if rest >= 0:
            if rest <= 5:
                rate *= 0.93  # Short turnaround — fatigue
            elif rest >= 9:
                rate *= 1.03  # Extra rest — freshness

        # Bye-week boost
        if v3_context.get("bye_week", False):
            rate *= 1.05

        # Season progression: discount early-season predictions
        rnd = v3_context.get("round_number", 0)
        if 1 <= rnd <= 4:
            rate *= 0.92  # Less data, less reliable form
        elif 5 <= rnd <= 8:
            rate *= 0.97

        # Margin-weighted form adjustment
        mwf = v3_context.get("margin_form", {})
        quality = mwf.get("quality_score", 0.5)
        # Quality score is 0-1 (sigmoid of margins). 0.5 = average.
        # Adjust rate: teams winning big → slightly more try-friendly
        margin_factor = 0.9 + quality * 0.2  # range 0.9-1.1
        rate *= margin_factor

    # Bench minute reduction
    if is_bench and position in ("Interchange", "", "Reserve"):
        if v3_context:
            # Use actual interchange timing data
            bench_mins = v3_context.get("bench_minutes", {}).get(name, 30.0)
            # Scale by proportion of game played (80 mins = full game)
            minute_fraction = bench_mins / 80.0
            rate *= max(0.15, min(minute_fraction, 0.70))
        else:
            rate *= 0.40

    # Deterministic noise for variety
    seed = sum(ord(c) for c in name) % 1000
    rng = np.random.RandomState(seed)
    noise = rng.normal(1.0, 0.04)
    rate *= max(noise, 0.6)

    # Calibration correction
    if v3_context:
        calibration = v3_context.get("calibration", {})
        if calibration:
            bucket = min(int(rate * 10), 5)
            cal = calibration.get(bucket, {})
            ratio = cal.get("ratio", 1.0)
            if cal.get("count", 0) >= 10:
                # Blend toward calibrated value (don't over-correct)
                rate *= (0.7 + 0.3 * ratio)

    return round(min(max(rate, 0.01), 0.60), 3)


def predict_win_probability(
    home_team: str,
    away_team: str,
    home_stats: dict,
    away_stats: dict,
    before_season=None,
    before_round=None,
    venue: str = "",
    weather: str = "",
    ground_conditions: str = "",
) -> dict:
    """
    Predict win probability for each team.

    Factors (recency-weighted — last 5 games carry more weight):
    1. Last 5 games form (20%) 2. Last 10 games form (10%)
    3. Points diff last 5 (15%) 4. Margin-weighted quality score (20%)
    5. H2H (10%) 6. Venue (15%) 7. Season (10%)
    Plus rest days, bye-week, and early-season adjustments layered on top.
    """
    # Recent form from DB
    home_form = get_team_attack_defence(home_team, last_n_games=10,
                                         before_season=before_season, before_round=before_round)
    away_form = get_team_attack_defence(away_team, last_n_games=10,
                                         before_season=before_season, before_round=before_round)

    # H2H
    h2h = get_h2h_record(home_team, away_team, before_season=before_season, before_round=before_round)

    # Home/away rates
    home_ha = get_home_away_win_rate(home_team, before_season=before_season, before_round=before_round)
    away_ha = get_home_away_win_rate(away_team, before_season=before_season, before_round=before_round)

    # Last-10 win rate
    home_recent10_wr = home_form["wins"] / home_form["played"] if home_form["played"] > 0 else 0.5
    away_recent10_wr = away_form["wins"] / away_form["played"] if away_form["played"] > 0 else 0.5

    # H2H
    if h2h["played"] > 0:
        home_h2h = h2h["team_a_wins"] / h2h["played"]
        away_h2h = h2h["team_b_wins"] / h2h["played"]
    else:
        home_h2h = 0.5
        away_h2h = 0.5

    # Venue factors
    home_venue_factor = home_ha["home_win_rate"]
    away_venue_factor = away_ha["away_win_rate"]

    # Season stats
    home_season_wr = home_stats.get("wins", 0) / home_stats.get("played", 1) if home_stats.get("played", 0) > 0 else 0.5
    away_season_wr = away_stats.get("wins", 0) / away_stats.get("played", 1) if away_stats.get("played", 0) > 0 else 0.5

    recent_n = min(5, home_form["played"])
    home_recent5_wr = home_form["wins_recent"] / recent_n if recent_n > 0 else 0.5
    recent_n = min(5, away_form["played"])
    away_recent5_wr = away_form["wins_recent"] / recent_n if recent_n > 0 else 0.5

    home_ppg_diff = (home_form["avg_scored_recent"] - home_form["avg_conceded_recent"]) / LEAGUE_AVG_PPG
    away_ppg_diff = (away_form["avg_scored_recent"] - away_form["avg_conceded_recent"]) / LEAGUE_AVG_PPG
    home_diff_factor = max(0.1, min(0.5 + home_ppg_diff * 0.15, 0.9))
    away_diff_factor = max(0.1, min(0.5 + away_ppg_diff * 0.15, 0.9))

    # Margin-weighted quality score (sigmoid of win margins)
    home_mwf = get_team_margin_weighted_form(home_team, last_n_games=10,
                                              before_season=before_season, before_round=before_round)
    away_mwf = get_team_margin_weighted_form(away_team, last_n_games=10,
                                              before_season=before_season, before_round=before_round)

    home_raw = (
        home_recent5_wr * 0.20 +
        home_recent10_wr * 0.10 +
        home_diff_factor * 0.15 +
        home_mwf["quality_score"] * 0.20 +
        home_h2h * 0.10 +
        home_venue_factor * 0.15 +
        home_season_wr * 0.10
    )
    away_raw = (
        away_recent5_wr * 0.20 +
        away_recent10_wr * 0.10 +
        away_diff_factor * 0.15 +
        away_mwf["quality_score"] * 0.20 +
        away_h2h * 0.10 +
        away_venue_factor * 0.15 +
        away_season_wr * 0.10
    )

    # Rest days adjustment
    if before_season and before_round:
        home_rest = get_team_rest_days(home_team, before_season, before_round)
        away_rest = get_team_rest_days(away_team, before_season, before_round)
        if home_rest >= 0 and away_rest >= 0:
            rest_diff = home_rest - away_rest
            # +1 day rest advantage ≈ +1% win probability
            home_raw += rest_diff * 0.01
        elif home_rest >= 0:
            if home_rest <= 5:
                home_raw -= 0.03
        elif away_rest >= 0:
            if away_rest <= 5:
                away_raw -= 0.03

        # Bye-week boost
        home_bye = get_team_had_bye(home_team, before_season, before_round)
        away_bye = get_team_had_bye(away_team, before_season, before_round)
        if home_bye:
            home_raw += 0.04
        if away_bye:
            away_raw += 0.04

        # Season progression — discount early-round H2H and season stats
        if before_round <= 4:
            # Early season: reduce weight of season WR (unreliable small sample)
            home_raw -= home_season_wr * 0.05
            away_raw -= away_season_wr * 0.05
            home_raw += 0.5 * 0.05  # Replace with neutral
            away_raw += 0.5 * 0.05

    display_home_form = round(home_recent5_wr, 3)
    display_away_form = round(away_recent5_wr, 3)

    # Home advantage boost — venue-specific where we have enough data
    if venue:
        venue_data = get_venue_stats(venue, home_team)
        if venue_data.get("team_games", 0) >= 5:
            venue_wr = venue_data["team_win_rate"]
            venue_advantage = (venue_wr - 0.5) * 0.3
            home_raw += max(0.02, min(venue_advantage, 0.15))
        else:
            home_raw += HOME_ADVANTAGE_WIN
    else:
        home_raw += HOME_ADVANTAGE_WIN

    # Normalise to probabilities
    total = home_raw + away_raw
    if total <= 0:
        home_prob = 0.5
        away_prob = 0.5
    else:
        home_prob = home_raw / total
        away_prob = away_raw / total

    # Clamp
    home_prob = max(0.10, min(home_prob, 0.90))
    away_prob = 1 - home_prob

    # --- Predicted score ---
    home_attack_ppg = home_form["avg_scored_recent"] * 0.6 + home_form["avg_scored"] * 0.4
    away_attack_ppg = away_form["avg_scored_recent"] * 0.6 + away_form["avg_scored"] * 0.4
    home_defence_ppg = home_form["avg_conceded_recent"] * 0.6 + home_form["avg_conceded"] * 0.4
    away_defence_ppg = away_form["avg_conceded_recent"] * 0.6 + away_form["avg_conceded"] * 0.4

    home_pred_score = (home_attack_ppg * 0.5 + away_defence_ppg * 0.5) * 1.03
    away_pred_score = (away_attack_ppg * 0.5 + home_defence_ppg * 0.5) * 0.97

    home_pred_score = max(4, round(home_pred_score / 2) * 2)
    away_pred_score = max(4, round(away_pred_score / 2) * 2)

    # Weather impact on scoring
    w_lower = (weather or "").lower()
    g_lower = (ground_conditions or "").lower()
    is_wet = any(kw in w_lower for kw in _WET_KEYWORDS)
    is_heavy = any(kw in g_lower for kw in _HEAVY_GROUND)
    if is_wet or is_heavy:
        score_reduction = 0.88 if (is_wet and is_heavy) else 0.93
        home_pred_score = max(4, round(home_pred_score * score_reduction / 2) * 2)
        away_pred_score = max(4, round(away_pred_score * score_reduction / 2) * 2)

    # Ensure predicted winner aligns with win probability
    if home_prob >= 0.5 and home_pred_score <= away_pred_score:
        home_pred_score = away_pred_score + 2
    elif away_prob > home_prob and away_pred_score <= home_pred_score:
        away_pred_score = home_pred_score + 2

    result = {
        "home_win_prob": round(home_prob, 3),
        "away_win_prob": round(away_prob, 3),
        "predicted_winner": home_team if home_prob >= 0.5 else away_team,
        "predicted_home_score": int(home_pred_score),
        "predicted_away_score": int(away_pred_score),
        "factors": {
            "home_recent_form": display_home_form,
            "away_recent_form": display_away_form,
            "h2h_home_wins": h2h.get("team_a_wins", 0),
            "h2h_away_wins": h2h.get("team_b_wins", 0),
            "h2h_played": h2h.get("played", 0),
        },
    }
    if is_wet or is_heavy:
        result["weather_impact"] = True
    return result


def generate_predictions(
    home_players: list,
    away_players: list,
    home_stats: dict,
    away_stats: dict,
    home_team_name: str = "",
    away_team_name: str = "",
    before_season=None,
    before_round=None,
    weather: str = "",
    ground_conditions: str = "",
) -> dict:
    """Generate try predictions for all players in a match."""
    # Get team form from DB
    home_attack = get_team_attack_defence(home_team_name, last_n_games=10,
                                           before_season=before_season, before_round=before_round)
    away_attack = get_team_attack_defence(away_team_name, last_n_games=10,
                                           before_season=before_season, before_round=before_round)

    if home_attack["played"] == 0:
        home_attack["avg_scored"] = home_stats.get("avg_points_scored", LEAGUE_AVG_PPG)
        home_attack["avg_conceded"] = home_stats.get("avg_points_conceded", LEAGUE_AVG_PPG)
    if away_attack["played"] == 0:
        away_attack["avg_scored"] = away_stats.get("avg_points_scored", LEAGUE_AVG_PPG)
        away_attack["avg_conceded"] = away_stats.get("avg_points_conceded", LEAGUE_AVG_PPG)

    away_edge_vuln = get_team_tries_conceded_by_edge(away_team_name, last_n_games=15,
                                                      before_season=before_season, before_round=before_round)
    home_edge_vuln = get_team_tries_conceded_by_edge(home_team_name, last_n_games=15,
                                                      before_season=before_season, before_round=before_round)

    # Batch-fetch all player histories in ONE query instead of 34+ individual queries
    all_player_names = [p["name"] for p in home_players + away_players if p.get("name")]
    _histories = get_players_try_histories_batch(all_player_names, before_season, before_round)

    # Build situational context (rest days, bye week, calibration, etc.) when
    # we know the match's season/round — skipped gracefully otherwise.
    v3_home_ctx = None
    v3_away_ctx = None
    if before_season and before_round:
        calibration = get_calibration_data()
        # Bench minutes for interchange players
        bench_names = [p["name"] for p in home_players + away_players
                       if p.get("name") and (p.get("is_interchange") or p.get("number", 0) >= 14)]
        bench_mins = get_bench_minutes_batch(bench_names, before_season, before_round) if bench_names else {}
        # Quality-adjusted try rates for all players (single batch query)
        quality_adj = get_quality_adjusted_tries_batch(all_player_names, before_season, before_round)

        home_margin = get_team_margin_weighted_form(home_team_name, last_n_games=10,
                                                     before_season=before_season, before_round=before_round)
        away_margin = get_team_margin_weighted_form(away_team_name, last_n_games=10,
                                                     before_season=before_season, before_round=before_round)
        home_rest = get_team_rest_days(home_team_name, before_season, before_round)
        away_rest = get_team_rest_days(away_team_name, before_season, before_round)
        home_bye = get_team_had_bye(home_team_name, before_season, before_round)
        away_bye = get_team_had_bye(away_team_name, before_season, before_round)

        v3_home_ctx = {
            "calibration": calibration, "bench_minutes": bench_mins,
            "quality_adj_rates": quality_adj, "margin_form": home_margin,
            "rest_days": home_rest, "bye_week": home_bye,
            "round_number": before_round,
        }
        v3_away_ctx = {
            "calibration": calibration, "bench_minutes": bench_mins,
            "quality_adj_rates": quality_adj, "margin_form": away_margin,
            "rest_days": away_rest, "bye_week": away_bye,
            "round_number": before_round,
        }

    def process_team(players, team_name, opp_name, is_home, team_atk, opp_atk, opp_edge, v3_ctx):
        results = []
        for p in players:
            prob = _predict_try_with_history(
                p, _histories.get(p["name"], []),
                team_name, opp_name, is_home, team_atk, opp_atk, opp_edge,
                weather=weather, ground_conditions=ground_conditions,
                v3_context=v3_ctx,
            )
            jersey = p.get("number", 0)
            field_side = JERSEY_FIELD_SIDE.get(jersey, "") if 1 <= jersey <= 13 else ""
            results.append({
                **p,
                "try_probability": prob,
                "try_percentage": round(prob * 100, 1),
                "field_side": field_side,
            })
        results.sort(key=lambda x: x["try_probability"], reverse=True)
        return results

    home_preds = process_team(
        home_players, home_team_name, away_team_name, True,
        home_attack, away_attack, away_edge_vuln, v3_home_ctx
    )
    away_preds = process_team(
        away_players, away_team_name, home_team_name, False,
        away_attack, home_attack, home_edge_vuln, v3_away_ctx
    )

    return {
        "home": home_preds,
        "away": away_preds,
    }


def generate_multi_suggestion(home_predictions: list, away_predictions: list,
                               home_nickname: str, away_nickname: str) -> list:
    """
    Suggest the best 3-player try scorer multi across both teams.
    Picks the 3 players with highest try probability regardless of team.
    Returns list of 3 picks with combined multi probability.
    """
    all_players = []
    for p in home_predictions:
        all_players.append({**p, "team": home_nickname})
    for p in away_predictions:
        all_players.append({**p, "team": away_nickname})

    # Sort by probability descending
    all_players.sort(key=lambda x: x["try_probability"], reverse=True)

    # Pick top 3 (but try to get at least 1 from each team for diversity)
    picks = []

    # Take the absolute top 3 first; the passes below only intervene when
    # that lands entirely on one team.
    raw_top3 = all_players[:3]
    raw_teams = set(p["team"] for p in raw_top3)

    if len(raw_teams) >= 2:
        # Already have both teams, use top 3
        picks = raw_top3[:3]
    else:
        # All from one team - take top 2 from that team, top 1 from other
        picks = raw_top3[:2]
        for p in all_players:
            if p["team"] != raw_top3[0]["team"]:
                picks.append(p)
                break
        if len(picks) < 3:
            picks = raw_top3[:3]

    # Calculate combined multi probability (all 3 must score)
    multi_prob = 1.0
    for p in picks:
        multi_prob *= p["try_probability"]

    multi_result = []
    for p in picks:
        multi_result.append({
            "name": p["name"],
            "team": p["team"],
            "position": p["position"],
            "try_percentage": p["try_percentage"],
        })

    return {
        "picks": multi_result,
        "multi_percentage": round(multi_prob * 100, 2),
    }


EDGE_LABELS = {"left": "left edge", "right": "right edge", "middle": "middle", "fullback": "fullbacks"}


def find_value_picks(predictions: list, opp_team_name: str, team_nickname: str,
                     before_season=None, before_round=None) -> list:
    """
    Find value picks — players ranked lower in raw probability but who are
    good picks based on:
    1. The opponent concedes more tries than average to their position
    2. The opponent is weak on the player's side of the field
    3. The player is in recent scoring form

    A value pick must:
    - NOT already be in the top 3 (they're already highlighted)
    - Play a position the opponent is vulnerable to (vulnerability > 1.2)
      OR attack an edge the opponent is weak on
    - Be in form (scored in recent games, or high recent try rate)
    """
    if len(predictions) <= 3:
        return []

    opp_vulnerabilities = get_team_tries_conceded_by_position(opp_team_name, last_n_games=15,
                                                              before_season=before_season, before_round=before_round)
    opp_edge_vuln = get_team_tries_conceded_by_edge(opp_team_name, last_n_games=15,
                                                     before_season=before_season, before_round=before_round)

    # Normalise position names for matching
    def normalise_pos(pos):
        if not pos:
            return ""
        p = pos.lower()
        if "wing" in p:
            return "Winger"
        if "full" in p:
            return "Fullback"
        if "centre" in p or "center" in p:
            return "Centre"
        if "five" in p or "5/8" in p:
            return "Five-Eighth"
        if "half" in p:
            return "Halfback"
        if "prop" in p:
            return "Prop"
        if "hook" in p:
            return "Hooker"
        if "row" in p or "2nd" in p:
            return "2nd Row"
        if "lock" in p:
            return "Lock"
        return pos

    # Pre-filter candidates with vulnerability, then batch-fetch their recent form
    candidates = []
    for rank, player in enumerate(predictions[3:], start=4):
        pos = normalise_pos(player.get("position", ""))
        if not pos or pos in ("Interchange", "Reserve", ""):
            continue
        jersey = player.get("number", 0)
        vuln = opp_vulnerabilities.get(pos, {})
        vulnerability = vuln.get("vulnerability", 1.0)
        player_edge = JERSEY_FIELD_SIDE.get(jersey, "") if 1 <= jersey <= 13 else ""
        edge_info = opp_edge_vuln.get(player_edge, {}) if player_edge else {}
        edge_vuln_val = edge_info.get("vulnerability", 1.0)
        has_pos_vuln = vulnerability >= 1.15
        # Scale edge vuln threshold by position — low-sensitivity positions
        # need a much higher raw edge vuln to qualify
        pos_edge_sens = POSITION_EDGE_SENSITIVITY.get(pos, _DEFAULT_EDGE_SENSITIVITY)
        edge_vuln_threshold = 1.15 if pos_edge_sens >= 0.15 else 1.40
        has_edge_vuln = edge_vuln_val >= edge_vuln_threshold
        if not has_pos_vuln and not has_edge_vuln:
            continue
        candidates.append((rank, player, pos, vulnerability, vuln, player_edge, edge_info, edge_vuln_val, has_pos_vuln, has_edge_vuln))

    # Batch-fetch recent form for all candidates in ONE query
    candidate_names = [c[1]["name"] for c in candidates]
    all_form = get_players_recent_form_batch(candidate_names, last_n_games=5,
                                              before_season=before_season, before_round=before_round)

    value_picks = []
    for rank, player, pos, vulnerability, vuln, player_edge, edge_info, edge_vuln, has_pos_vuln, has_edge_vuln in candidates:
        form = all_form.get(player["name"], {"games": 0, "tries": 0, "rate": 0, "streak": 0})
        if form["games"] == 0:
            continue

        # Value pick criteria
        is_in_form = form["rate"] >= 0.2 or form["streak"] >= 1
        is_high_vuln = vulnerability >= 1.3 or edge_vuln >= 1.3

        if not is_in_form and not is_high_vuln:
            continue

        # Calculate a value score combining position vuln, edge vuln, and form
        form_score = min(form["rate"] * 2, 1.0) + (form["streak"] * 0.15)
        pos_vuln_score = (max(vulnerability, 1.0) - 1.0) * 2
        # Scale edge vuln score by position sensitivity
        edge_sensitivity = POSITION_EDGE_SENSITIVITY.get(pos, _DEFAULT_EDGE_SENSITIVITY)
        edge_vuln_score = (max(edge_vuln, 1.0) - 1.0) * (edge_sensitivity / 0.30) * 1.5
        value_score = form_score + pos_vuln_score + edge_vuln_score

        if value_score < 0.4:
            continue

        reasons = []

        # Edge vulnerability reason (field side specific)
        if has_edge_vuln and player_edge:
            edge_label = EDGE_LABELS.get(player_edge, player_edge)
            reasons.append(
                f"{opp_team_name} concede {edge_info.get('rate_per_game', 0):.1f} tries/game to {edge_label} ({edge_vuln:.1f}x league avg)"
            )

        # Position vulnerability reason
        if has_pos_vuln:
            if vulnerability >= 1.3:
                reasons.append(f"{opp_team_name} concede {vuln.get('rate_per_game', 0):.1f} tries/game to {pos}s ({vulnerability:.1f}x league avg)")
            else:
                reasons.append(f"{opp_team_name} leak tries to {pos}s ({vulnerability:.1f}x avg)")

        if form["streak"] >= 2:
            reasons.append(f"Try-scoring streak: {form['streak']} games in a row")
        elif form["streak"] == 1:
            reasons.append("Scored last game")

        if form["rate"] >= 0.4:
            reasons.append(f"Scoring at {form['rate']:.0%} in last {form['games']} games")
        elif form["rate"] >= 0.2:
            reasons.append(f"{form['tries']} tries in last {form['games']} games")

        value_picks.append({
            "name": player["name"],
            "team": team_nickname,
            "position": player.get("position", ""),
            "number": player.get("number", 0),
            "try_percentage": player["try_percentage"],
            "rank": rank,
            "field_side": player_edge,
            "value_score": round(value_score, 2),
            "vulnerability": vulnerability,
            "edge_vulnerability": edge_vuln,
            "form": {
                "games": form["games"],
                "tries": form["tries"],
                "rate": form["rate"],
                "streak": form["streak"],
            },
            "reasons": reasons,
        })

    # Sort by value score and return top picks
    value_picks.sort(key=lambda x: x["value_score"], reverse=True)
    return value_picks[:3]


def generate_team_summary(team_name: str, before_season=None, before_round=None) -> dict:
    """
    Generate a short attack/defence summary with strengths and weaknesses.
    Uses a blended last-5/last-10 window.
    """
    form = get_team_attack_defence(team_name, last_n_games=10,
                                    before_season=before_season, before_round=before_round)
    edge_vuln = get_team_tries_conceded_by_edge(team_name, last_n_games=15,
                                                 before_season=before_season, before_round=before_round)
    ha = get_home_away_win_rate(team_name, before_season=before_season, before_round=before_round)

    if form["played"] == 0:
        return {"attack": [], "defence": [], "attack_rating": "unknown", "defence_rating": "unknown"}

    recent_n = min(5, form["played"])
    win_rate = form["wins_recent"] / recent_n if recent_n > 0 else 0.5
    avg_scored = form["avg_scored_recent"] * 0.6 + form["avg_scored"] * 0.4
    avg_conceded = form["avg_conceded_recent"] * 0.6 + form["avg_conceded"] * 0.4
    form_label = "last 5"

    attack_points = []
    defence_points = []

    # Attack assessment
    if avg_scored >= 28:
        attack_points.append({"type": "strong", "text": f"Averaging {avg_scored:.0f} pts/game ({form_label}) — elite attack"})
    elif avg_scored >= 22:
        attack_points.append({"type": "strong", "text": f"Averaging {avg_scored:.0f} pts/game ({form_label}) — solid attack"})
    elif avg_scored >= 16:
        attack_points.append({"type": "weak", "text": f"Averaging {avg_scored:.0f} pts/game ({form_label}) — below average attack"})
    else:
        attack_points.append({"type": "weak", "text": f"Averaging {avg_scored:.0f} pts/game ({form_label}) — struggling to score"})

    # Form / momentum
    if win_rate >= 0.8:
        attack_points.append({"type": "strong", "text": f"Hot form — {win_rate:.0%} win rate ({form_label})"})
    elif win_rate >= 0.6:
        attack_points.append({"type": "strong", "text": f"Winning form — {win_rate:.0%} win rate ({form_label})"})
    elif win_rate <= 0.2:
        attack_points.append({"type": "weak", "text": f"Poor form — {win_rate:.0%} win rate ({form_label})"})
    elif win_rate <= 0.4:
        attack_points.append({"type": "weak", "text": f"Struggling — {win_rate:.0%} win rate ({form_label})"})

    # Home/away strength
    if ha["home_win_rate"] >= 0.7:
        attack_points.append({"type": "strong", "text": f"Strong at home ({ha['home_win_rate']:.0%} win rate)"})
    elif ha["home_win_rate"] <= 0.35:
        attack_points.append({"type": "weak", "text": f"Poor at home ({ha['home_win_rate']:.0%} win rate)"})

    # Defence assessment
    if avg_conceded <= 16:
        defence_points.append({"type": "strong", "text": f"Conceding {avg_conceded:.0f} pts/game ({form_label}) — elite defence"})
    elif avg_conceded <= 22:
        defence_points.append({"type": "strong", "text": f"Conceding {avg_conceded:.0f} pts/game ({form_label}) — solid defence"})
    elif avg_conceded <= 28:
        defence_points.append({"type": "weak", "text": f"Conceding {avg_conceded:.0f} pts/game ({form_label}) — leaky defence"})
    else:
        defence_points.append({"type": "weak", "text": f"Conceding {avg_conceded:.0f} pts/game ({form_label}) — very poor defence"})

    # Edge vulnerability
    if edge_vuln:
        weak_edges = []
        strong_edges = []
        for edge, info in edge_vuln.items():
            v = info.get("vulnerability", 1.0)
            label = EDGE_LABELS.get(edge, edge)
            if v >= 1.25:
                weak_edges.append((label, v, info.get("rate_per_game", 0)))
            elif v <= 0.75:
                strong_edges.append((label, v, info.get("rate_per_game", 0)))

        for label, v, rpg in sorted(weak_edges, key=lambda x: -x[1]):
            defence_points.append({"type": "weak", "text": f"Vulnerable on {label} — {rpg:.1f} tries/game conceded ({v:.1f}x avg)"})
        for label, v, rpg in sorted(strong_edges, key=lambda x: x[1]):
            defence_points.append({"type": "strong", "text": f"Strong on {label} — {rpg:.1f} tries/game conceded ({v:.1f}x avg)"})

    # Margin-weighted quality and situational factors
    if before_season and before_round:
        mwf = get_team_margin_weighted_form(team_name, last_n_games=10,
                                             before_season=before_season, before_round=before_round)
        if mwf["blowout_wins"] >= 2:
            attack_points.append({"type": "strong", "text": f"{mwf['blowout_wins']} blowout wins (18+ pts) in last 10 — dominant"})
        if mwf["avg_margin"] >= 10:
            attack_points.append({"type": "strong", "text": f"Winning by avg {mwf['avg_margin']:.0f} pts — quality wins"})
        elif mwf["avg_margin"] <= -10:
            attack_points.append({"type": "weak", "text": f"Losing by avg {abs(mwf['avg_margin']):.0f} pts — outclassed"})
        if mwf["close_losses"] >= 3:
            defence_points.append({"type": "neutral", "text": f"{mwf['close_losses']} close losses (within 6 pts) — unlucky or lack composure"})

        rest = get_team_rest_days(team_name, before_season, before_round)
        if rest >= 0 and rest <= 5:
            attack_points.append({"type": "weak", "text": f"Short turnaround ({rest} days rest) — fatigue risk"})
        elif rest >= 9:
            attack_points.append({"type": "strong", "text": f"Well rested ({rest} days since last game)"})

        bye = get_team_had_bye(team_name, before_season, before_round)
        if bye:
            attack_points.append({"type": "strong", "text": "Coming off a bye — extra rest and preparation"})

    # Overall ratings
    attack_score = avg_scored / LEAGUE_AVG_PPG
    if attack_score >= 1.2:
        attack_rating = "strong"
    elif attack_score >= 0.9:
        attack_rating = "average"
    else:
        attack_rating = "weak"

    defence_score = LEAGUE_AVG_PPG / avg_conceded if avg_conceded > 0 else 1.0
    if defence_score >= 1.2:
        defence_rating = "strong"
    elif defence_score >= 0.9:
        defence_rating = "average"
    else:
        defence_rating = "weak"

    return {
        "attack": attack_points,
        "defence": defence_points,
        "attack_rating": attack_rating,
        "defence_rating": defence_rating,
    }


def invalidate_cache():
    """Clear all caches (call after scraping)."""
    global _position_rates_cache
    _position_rates_cache = None
    clear_query_cache()

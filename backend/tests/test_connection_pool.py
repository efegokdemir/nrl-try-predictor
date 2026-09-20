"""Regression tests for the connection leak fixed in PR #36.

Every query function in database.py checks a connection out of a bounded pool
and must give it back, including when the query raises. It previously did not:
39 of 47 call sites ran `conn = get_db()` ... `conn.close()` with no
try/finally, so a failing query lost a connection for good. The pool caps at
20, so twenty failures bricked the process -- every later call raised
PoolError("connection pool exhausted") until a restart, while main.py's global
exception handler kept turning each one into a tidy 500. It read as a flaky
database rather than a code bug.

The failure modes that reach this are ordinary: the pool sets
statement_timeout=30000, and ADR-0003 exists because Supabase's free tier
drops connections outright when a project pauses.
"""

import inspect

import pytest

import database


# One representative call per shape of query function: plain reads, cached
# reads, batch reads, the bulk prefetch, writes, and schema init.
QUERY_CALLS = [
    ("get_total_match_count", lambda: database.get_total_match_count()),
    ("get_total_try_count", lambda: database.get_total_try_count()),
    ("get_team_attack_defence", lambda: database.get_team_attack_defence("Storm")),
    ("get_h2h_record", lambda: database.get_h2h_record("Storm", "Eels")),
    ("get_h2h_recent_tries", lambda: database.get_h2h_recent_tries("Storm", "Eels")),
    ("get_home_away_win_rate", lambda: database.get_home_away_win_rate("Storm")),
    ("get_team_tries_conceded_by_edge", lambda: database.get_team_tries_conceded_by_edge("Storm")),
    ("get_team_tries_conceded_by_position", lambda: database.get_team_tries_conceded_by_position("Storm")),
    ("get_team_margin_weighted_form", lambda: database.get_team_margin_weighted_form("Storm")),
    ("get_team_rest_days", lambda: database.get_team_rest_days("Storm", 2026, 5)),
    ("get_team_had_bye", lambda: database.get_team_had_bye("Storm", 2026, 5)),
    ("get_venue_stats", lambda: database.get_venue_stats("AAMI Park")),
    ("get_position_try_rates", lambda: database.get_position_try_rates()),
    ("get_calibration_data", lambda: database.get_calibration_data()),
    ("get_players_try_histories_batch", lambda: database.get_players_try_histories_batch(["A B"])),
    ("get_players_recent_form_batch", lambda: database.get_players_recent_form_batch(["A B"])),
    ("get_bench_minutes_batch", lambda: database.get_bench_minutes_batch(["A B"])),
    ("get_quality_adjusted_tries_batch", lambda: database.get_quality_adjusted_tries_batch(["A B"])),
    ("prefetch_round_data", lambda: database.prefetch_round_data(["Storm", "Eels"],
                                                                [("Storm", "Eels", "AAMI Park")])),
    ("get_player_game_log", lambda: database.get_player_game_log("A B")),
    ("get_accuracy_stats", lambda: database.get_accuracy_stats()),
    ("get_betting_summary", lambda: database.get_betting_summary()),
    ("get_prediction_seasons", lambda: database.get_prediction_seasons()),
    ("get_unrecorded_completed_matches", lambda: database.get_unrecorded_completed_matches()),
    ("get_edge_pick", lambda: database.get_edge_pick("/draw/x")),
    ("search_players", lambda: database.search_players("smith")),
    ("search_teams", lambda: database.search_teams("sto")),
    ("get_all_teams", lambda: database.get_all_teams()),
    ("get_all_players", lambda: database.get_all_players()),
    ("get_team_roster", lambda: database.get_team_roster("Storm")),
    ("get_team_recent_results", lambda: database.get_team_recent_results("Storm")),
    ("get_season_rounds", lambda: database.get_season_rounds(2026)),
    ("get_season_round_matches", lambda: database.get_season_round_matches(2026, 1)),
    ("get_team_season_matches", lambda: database.get_team_season_matches("Storm", 2026)),
    ("get_player_headshot", lambda: database.get_player_headshot("A B")),
    ("load_all_cache_entries", lambda: database.load_all_cache_entries()),
    ("is_round_scraped", lambda: database.is_round_scraped(2026, 1)),
    ("repair_scrape_progress", lambda: database.repair_scrape_progress(2026)),
    ("init_db", lambda: database.init_db()),
    ("mark_round_scraped", lambda: database.mark_round_scraped(2026, 1)),
    ("insert_match", lambda: database.insert_match(2026, 1, "Round 1", "/draw/x", "FullTime",
                                                   "Storm", "Eels", 30, 12, "AAMI Park", "2026-03-05")),
    ("bulk_insert_match_data", lambda: database.bulk_insert_match_data(1, [], [], [])),
    ("upsert_prediction", lambda: database.upsert_prediction("/draw/x", 2026, 1, 3, "Storm", "Eels",
                                                             "Storm", 0.6, 24, 18)),
    ("save_edge_pick", lambda: database.save_edge_pick("/draw/x", 2026, 1, "Storm", "Eels",
                                                       "A B", "Storm", 0.4, 3.0, "TAB", 0.33, 0.07, 1)),
    ("update_player_headshots", lambda: database.update_player_headshots({"A B": "http://x/y.png"})),
    ("save_cache_entry", lambda: database.save_cache_entry("k", "{}", 0.0)),
    ("insert_log_entry", lambda: database.insert_log_entry("backend", "INFO", "x", "hello")),
    ("prune_old_logs", lambda: database.prune_old_logs()),
]


@pytest.mark.parametrize("name,call", QUERY_CALLS, ids=[n for n, _ in QUERY_CALLS])
def test_connection_is_returned_when_the_query_raises(failing_pool, name, call):
    with pytest.raises(Exception):
        call()
    assert failing_pool.handed_out > 0, f"{name} never opened a connection -- test is vacuous"
    assert failing_pool.checked_out == 0, (
        f"{name} leaked {failing_pool.checked_out} connection(s). "
        "Wrap its body in try/finally with conn.close() in the finally."
    )


@pytest.mark.parametrize("name,call", QUERY_CALLS, ids=[n for n, _ in QUERY_CALLS])
def test_connection_is_returned_on_the_happy_path(working_pool, name, call):
    try:
        call()
    except Exception:
        # A fake cursor returns empty results, so a few functions legitimately
        # fail on the shape of what comes back. The pool invariant still holds,
        # and that is what this asserts.
        pass
    assert working_pool.checked_out == 0, f"{name} leaked a connection on success"


def test_repeated_failures_cannot_exhaust_the_pool(failing_pool):
    """The specific production symptom: 20 failures used to be terminal."""
    for _ in range(50):
        database.clear_query_cache()
        with pytest.raises(Exception):
            database.get_total_match_count()
    assert failing_pool.checked_out == 0
    assert failing_pool.handed_out >= 50


def test_every_get_db_call_site_is_guarded():
    """Structural backstop for the call sites the list above doesn't reach.

    The parametrised tests cover a representative call per function, but new
    code is the risk here -- this reads the source and fails on any
    `conn = get_db()` that isn't immediately followed by `try:`.
    """
    lines = inspect.getsource(database).split("\n")
    unguarded = [
        i + 1
        for i, line in enumerate(lines)
        if line == "    conn = get_db()"
        and next((l for l in lines[i + 1:] if l.strip()), "") != "    try:"
    ]
    assert not unguarded, (
        f"get_db() without a following try: at line(s) {unguarded}. "
        "A query that raises there leaks its connection out of the pool."
    )


def test_close_is_only_ever_called_from_a_finally():
    """The other half of the invariant, and a bug this suite already caught.

    A connection returned twice is not a harmless double-decrement: psycopg2
    raises PoolError("trying to put unkeyed connection") on the second
    putconn. get_team_rest_days closed inside an early-return branch *and*
    in its finally, so it raised for any team with no prior completed match
    -- a team's first-ever game, or the earliest season in the data.
    """
    lines = inspect.getsource(database).split("\n")
    stray = [
        i + 1
        for i, line in enumerate(lines)
        if line.strip() == "conn.close()"
        and lines[i - 1].strip() != "finally:"
    ]
    assert not stray, (
        f"conn.close() outside a finally: at line(s) {stray}. "
        "The finally will close it again, and the second putconn raises."
    )

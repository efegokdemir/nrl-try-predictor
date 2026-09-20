"""Regression tests for the bookmaker-odds mismatch fixed in PR #37.

lookup_bookmaker_odds used to fall back to "same surname, same first initial"
and return the first hit. `players` covers both teams in a match and the NRL
has no shortage of shared surnames, so that could hand a player their
namesake's -- or an opponent's -- price. The wrong price then flows into
compute_best_edge_picks, whose edge_picks rows are captured once pre-kickoff
and never recomputed, so it settles as though it were a real bet.
"""

import odds_client
from odds_client import compute_best_edge_picks, lookup_bookmaker_odds

MATCHUP = "melbourne storm v parramatta eels"


def feed(**players):
    """Build a bookmaker payload. Underscores in kwargs become spaces."""
    return {MATCHUP: {name.replace("_", " "): [{"bookmaker": "TAB", "decimal": price}]
                      for name, price in players.items()}}


def price(data, player, home="Storm", away="Eels"):
    hit = lookup_bookmaker_odds(data, home, away, player)
    return hit[0]["decimal"] if hit else None


# --- the bug ----------------------------------------------------------------

def test_two_players_sharing_surname_and_initial_resolve_to_nothing():
    data = feed(josh_smith=2.0, jacob_smith=3.0)
    assert price(data, "J Smith") is None
    assert price(data, "J. Smith") is None


def test_a_different_first_name_is_not_a_match():
    """The sharper edge of the same bug: asking for Jacob used to return
    Josh's price outright, with no exact match involved."""
    assert price(feed(josh_smith=2.0), "Jacob Smith") is None
    assert price(feed(jacob_smith=3.0), "Josh Smith") is None


def test_an_opponents_namesake_is_not_borrowed():
    """`players` spans both teams, so the loose rule could cross sides."""
    data = feed(josh_smith=2.0, jordan_smith=5.0)   # one per team
    assert price(data, "J Smith") is None


def test_ambiguity_is_logged(caplog):
    with caplog.at_level("WARNING", logger=odds_client.__name__):
        price(feed(josh_smith=2.0, jacob_smith=3.0), "J Smith")
    assert "Ambiguous bookmaker odds" in caplog.text
    assert "josh smith" in caplog.text and "jacob smith" in caplog.text


# --- what must keep working -------------------------------------------------

def test_exact_match():
    assert price(feed(ryan_papenhuyzen=1.5), "Ryan Papenhuyzen") == 1.5


def test_case_and_whitespace_are_normalised():
    assert price(feed(ryan_papenhuyzen=1.5), "  ryan   PAPENHUYZEN ") == 1.5


def test_exact_match_wins_over_any_fuzzy_candidate():
    data = feed(josh_smith=2.0, jacob_smith=3.0)
    assert price(data, "Josh Smith") == 2.0
    assert price(data, "Jacob Smith") == 3.0


def test_an_initial_in_the_feed_matches_a_full_name():
    assert price(feed(j_smith=2.0), "Josh Smith") == 2.0
    assert price(feed(**{"j._smith": 2.0}), "Josh Smith") == 2.0


def test_a_short_form_matches_its_longer_spelling():
    assert price(feed(matthew_burton=2.2), "Matt Burton") == 2.2
    assert price(feed(matt_burton=2.2), "Matthew Burton") == 2.2


def test_unmatched_inputs_return_none():
    data = feed(ryan_papenhuyzen=1.5)
    assert price(data, "Nathan Cleary") is None                      # unknown player
    assert price(data, "Ryan Papenhuyzen", home="Panthers") is None  # wrong matchup
    assert price(data, "Papenhuyzen") is None                        # single token
    assert price({}, "Ryan Papenhuyzen") is None                     # empty feed


# --- what the odds are used for --------------------------------------------

def test_edge_picks_rank_by_edge_and_skip_players_without_odds():
    home = [
        {"name": "Big Edge", "try_percentage": 50.0,
         "bookmaker_odds": [{"bookmaker": "TAB", "decimal": 4.0}]},      # implied .25 -> +.25
        {"name": "No Odds", "try_percentage": 90.0},                     # skipped
    ]
    away = [
        {"name": "Small Edge", "try_percentage": 30.0,
         "bookmaker_odds": [{"bookmaker": "TAB", "decimal": 4.0}]},      # implied .25 -> +.05
        {"name": "Negative", "try_percentage": 10.0,
         "bookmaker_odds": [{"bookmaker": "TAB", "decimal": 4.0}]},      # -.15, dropped
    ]
    picks = compute_best_edge_picks(home, away, "Storm", "Eels", 2026, 1, "/draw/x")
    assert [p["player_name"] for p in picks] == ["Big Edge", "Small Edge"]
    assert picks[0]["edge"] > picks[1]["edge"]
    assert picks[0]["player_team"] == "Storm" and picks[1]["player_team"] == "Eels"


def test_edge_picks_use_the_best_available_price():
    """Several bookmakers means several prices; the edge is against the best
    one on offer, since that is the bet you would actually place."""
    home = [{"name": "A B", "try_percentage": 50.0, "bookmaker_odds": [
        {"bookmaker": "TAB", "decimal": 2.5},
        {"bookmaker": "SportsBet", "decimal": 4.0},
    ]}]
    picks = compute_best_edge_picks(home, [], "Storm", "Eels", 2026, 1, "/draw/x")
    assert picks[0]["bookmaker_decimal_odds"] == 4.0
    assert picks[0]["bookmaker_name"] == "SportsBet"
    assert picks[0]["edge"] == 0.25

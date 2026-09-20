"""Jersey-to-field-side attribution.

This underpins the model's edge-vulnerability factor and the scraper's
per-try edge attribution, which is why the table lives in one module now
(PR #34) instead of being copied into both with a "keep in sync" comment.
"""

import field_side
import model
import scraper
from field_side import JERSEY_FIELD_SIDE
from scraper import _determine_field_side, _parse_minute_to_seconds


def test_the_model_and_the_scraper_read_the_same_table():
    assert model.JERSEY_FIELD_SIDE is field_side.JERSEY_FIELD_SIDE
    assert scraper.JERSEY_FIELD_SIDE is field_side.JERSEY_FIELD_SIDE


def test_every_starting_jersey_has_a_side():
    assert sorted(JERSEY_FIELD_SIDE) == list(range(1, 14))
    assert set(JERSEY_FIELD_SIDE.values()) == {"left", "right", "middle", "fullback"}


def test_the_edges_are_balanced():
    """Wing/centre/second-row pair off left and right; the spine and the
    forwards pack the middle. A lopsided table would skew every edge
    vulnerability figure in one direction."""
    sides = JERSEY_FIELD_SIDE
    assert (sides[2], sides[3]) == ("right", "right")    # right wing, right centre
    assert (sides[4], sides[5]) == ("left", "left")      # left centre, left wing
    assert (sides[11], sides[12]) == ("left", "right")   # second rowers
    assert sides[1] == "fullback"
    assert [j for j, s in sides.items() if s == "middle"] == [8, 9, 10, 13]


STARTERS = [
    {"name": "Left Winger", "jersey_number": 5, "side": "home", "is_interchange": False},
    {"name": "Right Winger", "jersey_number": 2, "side": "home", "is_interchange": False},
    {"name": "Prop", "jersey_number": 8, "side": "home", "is_interchange": False},
    {"name": "Bench Forward", "jersey_number": 14, "side": "home", "is_interchange": True},
]


def test_a_starter_uses_their_own_jersey():
    assert _determine_field_side("Left Winger", 5, "home", STARTERS, [], 600) == "left"
    assert _determine_field_side("Right Winger", 2, "home", STARTERS, [], 600) == "right"
    assert _determine_field_side("Prop", 8, "home", STARTERS, [], 600) == "middle"


def test_a_bench_player_inherits_the_edge_of_whoever_they_replaced():
    swap = [{"player_on": "Bench Forward", "player_off": "Left Winger",
             "jersey_off": 5, "side": "home", "game_seconds": 1200}]
    assert _determine_field_side("Bench Forward", 14, "home", STARTERS, swap, 1800) == "left"


def test_a_bench_player_who_was_not_on_yet_is_not_attributed():
    """A try before they came on must not inherit that starter's edge."""
    swap = [{"player_on": "Bench Forward", "player_off": "Left Winger",
             "jersey_off": 5, "side": "home", "game_seconds": 1200}]
    assert _determine_field_side("Bench Forward", 14, "home", STARTERS, swap, 600) == "middle"


def test_the_most_recent_swap_before_the_try_wins():
    """Bench players rotate; the edge they hold is the one from their latest
    stint, not their first."""
    swaps = [
        {"player_on": "Bench Forward", "player_off": "Left Winger",
         "jersey_off": 5, "side": "home", "game_seconds": 600},
        {"player_on": "Bench Forward", "player_off": "Right Winger",
         "jersey_off": 2, "side": "home", "game_seconds": 2400},
    ]
    assert _determine_field_side("Bench Forward", 14, "home", STARTERS, swaps, 3000) == "right"
    assert _determine_field_side("Bench Forward", 14, "home", STARTERS, swaps, 1200) == "left"


def test_a_swap_on_the_other_team_is_ignored():
    swap = [{"player_on": "Bench Forward", "player_off": "Left Winger",
             "jersey_off": 5, "side": "away", "game_seconds": 1200}]
    assert _determine_field_side("Bench Forward", 14, "home", STARTERS, swap, 1800) == "middle"


def test_the_jersey_is_recovered_from_the_team_list_when_the_swap_omits_it():
    swap = [{"player_on": "Bench Forward", "player_off": "Left Winger",
             "side": "home", "game_seconds": 1200}]          # no jersey_off
    assert _determine_field_side("Bench Forward", 14, "home", STARTERS, swap, 1800) == "left"


def test_an_unattributable_bench_try_falls_back_to_middle():
    assert _determine_field_side("Bench Forward", 14, "home", STARTERS, [], 1800) == "middle"


def test_minute_strings_become_game_seconds():
    assert _parse_minute_to_seconds("27'") == 27 * 60
    assert _parse_minute_to_seconds("0'") == 0
    assert _parse_minute_to_seconds("") == 0
    assert _parse_minute_to_seconds(None) == 0
    assert _parse_minute_to_seconds("golden point") == 0

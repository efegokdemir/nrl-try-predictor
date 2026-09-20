"""Round numbering from NRL match URLs.

AGENTS.md calls this out as a trap that has already bitten once: a finals
match's URL carries `finals-week-N` rather than `round-N`, so a regex that
only handles the latter silently returns None for every finals match. That
killed the match-to-match nav bar mid-finals, and it also feeds the model's
prediction-history cutoff -- a None round means no temporal filter, which is
the look-ahead the before_season/before_round arguments exist to prevent.
"""

import main
from main import REGULAR_SEASON_ROUNDS, _round_number_from_url
from nrl_client import TOTAL_ROUNDS


def url(segment):
    return f"/draw/nrl-premiership/2026/{segment}/storm-v-eels"


def test_regular_season_rounds():
    assert _round_number_from_url(url("round-1")) == 1
    assert _round_number_from_url(url("round-9")) == 9
    assert _round_number_from_url(url("round-27")) == 27


def test_finals_weeks_continue_the_numbering():
    assert _round_number_from_url(url("finals-week-1")) == REGULAR_SEASON_ROUNDS + 1
    assert _round_number_from_url(url("finals-week-4")) == REGULAR_SEASON_ROUNDS + 4


def test_the_grand_final_is_the_last_round():
    assert _round_number_from_url(url("finals-week-4")) == TOTAL_ROUNDS


def test_unparseable_urls_return_none():
    assert _round_number_from_url("/draw/nrl-premiership/2026/storm-v-eels") is None
    assert _round_number_from_url("") is None


def test_the_three_round_counts_stay_in_sync():
    """TOTAL_ROUNDS is duplicated in three places that AGENTS.md says must
    agree, none derived from the others: nrl_client.py (the backend's own),
    scraper.py's CURRENT_SEASON_ROUNDS, and Draw.jsx on the frontend. The
    first two are importable; the third is checked by reading the file.
    """
    import re
    from pathlib import Path

    import scraper

    assert scraper.CURRENT_SEASON_ROUNDS == TOTAL_ROUNDS

    draw_jsx = Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "Draw.jsx"
    if not draw_jsx.exists():          # backend checked out on its own
        return
    match = re.search(r"const TOTAL_ROUNDS = (\d+)", draw_jsx.read_text(encoding="utf-8"))
    assert match, "Draw.jsx no longer declares TOTAL_ROUNDS -- update this test"
    assert int(match.group(1)) == TOTAL_ROUNDS, (
        f"Draw.jsx says {match.group(1)} rounds, nrl_client.py says {TOTAL_ROUNDS}"
    )


def test_regular_season_rounds_leaves_room_for_four_finals_weeks():
    assert TOTAL_ROUNDS - REGULAR_SEASON_ROUNDS == 4


def test_champion_model_version_is_what_gets_recorded():
    """main.py stopped hardcoding 3 in PR #34; this keeps the cache key, the
    accuracy default and the recorded rows reading from one constant."""
    assert main.CHAMPION_MODEL_VERSION == 3
    assert main._match_cache_key_str("/draw/x") == "match:v3:/draw/x"

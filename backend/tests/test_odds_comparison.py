"""Model price vs. bookmaker price.

_build_odds_comparison is the single implementation behind both the round
fixture list and the match detail response. It used to be duplicated in
_predict_single_fixture (PR #34), which is exactly the sort of arithmetic
that drifts when it lives in two places.
"""

import main
from main import _build_odds_comparison, _predict_single_fixture


def test_value_is_flagged_when_the_model_beats_the_implied_price():
    # 1.50 implies 0.667; the model says 0.70 -> value on the home side.
    out = _build_odds_comparison("1.50", "2.60", 0.70, 0.30)
    assert out["home_implied_prob"] == 0.6667
    assert out["home_value"] is True
    assert out["home_edge"] == round(0.70 - 1 / 1.5, 4)
    assert out["away_value"] is False
    assert out["away_edge"] < 0


def test_decimals_and_model_probabilities_are_carried_through():
    out = _build_odds_comparison("2.00", "2.00", 0.55, 0.45)
    assert out["home_decimal"] == 2.0 and out["away_decimal"] == 2.0
    assert out["home_model_prob"] == 0.55 and out["away_model_prob"] == 0.45


def test_unusable_odds_yield_no_comparison():
    assert _build_odds_comparison("", "2.6", 0.5, 0.5) is None
    assert _build_odds_comparison("1.5", "", 0.5, 0.5) is None
    assert _build_odds_comparison("abc", "2.6", 0.5, 0.5) is None      # not a number
    assert _build_odds_comparison("0", "2.6", 0.5, 0.5) is None        # would divide by zero
    assert _build_odds_comparison("-1.5", "2.6", 0.5, 0.5) is None     # nonsense price


def _fixture(**overrides):
    f = {"home_team": "Storm", "away_team": "Eels", "venue": "AAMI Park",
         "match_state": "Upcoming"}
    f.update(overrides)
    return f


def _stub_win_probability(monkeypatch, **overrides):
    wp = {"predicted_winner": "Storm", "home_win_prob": 0.7, "away_win_prob": 0.3,
          "predicted_home_score": 26, "predicted_away_score": 18}
    wp.update(overrides)
    monkeypatch.setattr(main, "predict_win_probability", lambda *a, **k: wp)


def test_fixture_prediction_routes_odds_through_the_shared_helper(monkeypatch):
    _stub_win_probability(monkeypatch)
    out = _predict_single_fixture(_fixture(home_odds="1.50", away_odds="2.60"), 5)
    assert out["odds_comparison"] == _build_odds_comparison("1.50", "2.60", 0.7, 0.3)


def test_a_fixture_without_odds_has_no_comparison_key(monkeypatch):
    _stub_win_probability(monkeypatch)
    assert "odds_comparison" not in _predict_single_fixture(_fixture(), 5)


def test_completed_fixtures_are_marked_right_or_wrong(monkeypatch):
    _stub_win_probability(monkeypatch)
    won = _predict_single_fixture(
        _fixture(match_state="FullTime", home_score=30, away_score=12), 5)
    assert won["actual_winner"] == "Storm" and won["prediction_correct"] is True

    lost = _predict_single_fixture(
        _fixture(match_state="FullTime", home_score=12, away_score=30), 5)
    assert lost["actual_winner"] == "Eels" and lost["prediction_correct"] is False

    drawn = _predict_single_fixture(
        _fixture(match_state="FullTime", home_score=18, away_score=18), 5)
    assert drawn["actual_winner"] == "Draw" and drawn["prediction_correct"] is False


def test_an_unplayed_fixture_is_not_scored(monkeypatch):
    _stub_win_probability(monkeypatch)
    out = _predict_single_fixture(_fixture(), 5)
    assert "actual_winner" not in out and "prediction_correct" not in out


def test_a_fixture_missing_a_team_is_left_alone(monkeypatch):
    """Byes arrive in the fixture list without both sides."""
    _stub_win_probability(monkeypatch)
    out = _predict_single_fixture({"home_team": "Storm", "away_team": ""}, 5)
    assert "predicted_winner" not in out


def test_team_colours_fall_back_rather_than_raise():
    assert main._theme_to_colour({"key": "storm"}) == "#552D6D"
    assert main._theme_to_colour({"key": "not-a-team"}) == "#333333"
    assert main._theme_to_colour({}) == "#333333"
    assert main._theme_to_colour(None) == "#333333"

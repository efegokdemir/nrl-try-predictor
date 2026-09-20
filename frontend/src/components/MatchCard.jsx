import React from 'react'

const TEAM_COLOURS = {
  'broncos': '#6D2735',
  'raiders': '#56B947',
  'bulldogs': '#005DB5',
  'sharks': '#00A5DB',
  'titans': '#E8B825',
  'sea-eagles': '#6D2735',
  'storm': '#552D6D',
  'knights': '#005DB5',
  'cowboys': '#002B5C',
  'eels': '#005DB5',
  'panthers': '#2A2A2A',
  'rabbitohs': '#003B2F',
  'dragons': '#E2231A',
  'roosters': '#003B7B',
  'warriors': '#636466',
  'wests-tigers': '#F47920',
  'dolphins': '#C8102E',
}

export function getMatchStatus(match) {
  const state = (match.match_state || '').toLowerCase()
  if (state === 'fulltime' || state === 'postmatch') return 'completed'
  if (state === 'halftime' || state === 'inprogress') return 'live'
  return 'upcoming'
}

export function formatMatchDate(dt) {
  if (!dt) return ''
  const d = new Date(dt)
  return d.toLocaleDateString('en-AU', { weekday: 'short', day: 'numeric', month: 'short' })
}

export function formatMatchTime(dt) {
  if (!dt) return ''
  const d = new Date(dt)
  return d.toLocaleTimeString('en-AU', { hour: '2-digit', minute: '2-digit' })
}

// The route a match card navigates to. Lives here beside the card itself so
// the two screens that list matches don't each carry their own copy of the
// URL shape — /match reads the NRL path from `url`, and a fixture without
// one (a bye, or a round NRL hasn't fleshed out yet) isn't navigable.
export function matchPath(match) {
  if (!match.match_url) return null
  return `/match?url=${encodeURIComponent(match.match_url)}`
}

// Shared match-card presentation, used by both the round tab (Draw.jsx) and
// any other view that lists matches (e.g. a team's season schedule) — keeps
// them visually identical instead of maintaining two copies of this markup.
export default function MatchCard({ match, onClick, roundLabel }) {
  const status = getMatchStatus(match)
  const homeColour = TEAM_COLOURS[match.home_theme_key] || '#333'
  const awayColour = TEAM_COLOURS[match.away_theme_key] || '#333'

  const homePick = match.predicted_winner === match.home_team
  const awayPick = match.predicted_winner === match.away_team

  return (
    <div className="match-card clickable" onClick={onClick}>
      <div className="match-meta">
        <span className="match-date">
          {roundLabel ? `${roundLabel} · ` : ''}{formatMatchDate(match.kickoff)} &middot; {formatMatchTime(match.kickoff)}
        </span>
        <span className={`match-status-badge ${status}`}>
          {status === 'live' ? 'Live' : status === 'completed' ? 'Full Time' : 'Upcoming'}
        </span>
      </div>
      <div className="match-teams">
        <div className={`team home-team ${homePick ? 'predicted' : ''}`}>
          <div className="team-badge-wrap" style={{ boxShadow: homePick ? '0 0 0 3px #22c55e' : 'none' }}>
            <img
              className="team-logo"
              src={`https://www.nrl.com/.theme/${match.home_theme_key || 'nrl'}/badge.svg`}
              alt={match.home_team}
              onError={(e) => { e.target.style.display = 'none'; e.target.nextSibling.style.display = 'flex' }}
            />
            <div className="team-badge-fallback" style={{ backgroundColor: homeColour, display: 'none' }}>
              {(match.home_team || '').substring(0, 3).toUpperCase()}
            </div>
          </div>
          <span className="team-name">{match.home_team}</span>
          {homePick && <span className="pick-tag">PICK {match.home_win_prob ? `${(match.home_win_prob * 100).toFixed(0)}%` : ''}</span>}
          {match.odds_comparison && (
            <span className="team-odds">
              ${match.odds_comparison.home_decimal.toFixed(2)}
              {match.odds_comparison.home_value && <span className="value-tag">VALUE</span>}
            </span>
          )}
        </div>
        <div className="vs-container">
          {status === 'completed' && match.home_score != null ? (
            <span className="score">{match.home_score} - {match.away_score}</span>
          ) : status === 'live' ? (
            <span className="score live">LIVE</span>
          ) : (
            <span className="vs">VS</span>
          )}
          {match.predicted_home_score != null && (
            <span className="predicted-score">Predicted: {match.predicted_home_score} - {match.predicted_away_score}</span>
          )}
          {match.odds_comparison && (
            <span className="odds-label-centre">Odds</span>
          )}
        </div>
        <div className={`team away-team ${awayPick ? 'predicted' : ''}`}>
          <div className="team-badge-wrap" style={{ boxShadow: awayPick ? '0 0 0 3px #22c55e' : 'none' }}>
            <img
              className="team-logo"
              src={`https://www.nrl.com/.theme/${match.away_theme_key || 'nrl'}/badge.svg`}
              alt={match.away_team}
              onError={(e) => { e.target.style.display = 'none'; e.target.nextSibling.style.display = 'flex' }}
            />
            <div className="team-badge-fallback" style={{ backgroundColor: awayColour, display: 'none' }}>
              {(match.away_team || '').substring(0, 3).toUpperCase()}
            </div>
          </div>
          <span className="team-name">{match.away_team}</span>
          {awayPick && <span className="pick-tag">PICK {match.away_win_prob ? `${(match.away_win_prob * 100).toFixed(0)}%` : ''}</span>}
          {match.odds_comparison && (
            <span className="team-odds">
              ${match.odds_comparison.away_decimal.toFixed(2)}
              {match.odds_comparison.away_value && <span className="value-tag">VALUE</span>}
            </span>
          )}
        </div>
      </div>
      <div className="match-venue">{match.venue}{match.venue_city ? `, ${match.venue_city}` : ''}</div>
      {status === 'completed' && match.prediction_correct !== undefined && (
        <div className={`prediction-tag ${match.prediction_correct ? 'correct' : 'wrong'}`}>
          {match.prediction_correct ? 'Prediction correct' : `Predicted ${match.predicted_winner} — ${match.actual_winner} won`}
        </div>
      )}
      <div className="team-list-ready">
        {status === 'completed' ? 'Full Time — View predictions →' :
         status === 'live' ? 'Match in progress — View predictions →' :
         'View try predictions →'}
      </div>
    </div>
  )
}

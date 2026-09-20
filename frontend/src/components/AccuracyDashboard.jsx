import React, { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import LoadingSpinner from './LoadingSpinner'
import { fetchJson } from '../api'
import SeasonSelect from './SeasonSelect'

// The API returns profit per $1 staked so the dashboard can scale it by
// whatever stake the user types. This page has no stake control, so it shows a
// flat $10 bet -- the same figure Dashboard.jsx starts from (DEFAULT_STAKE),
// so the two pages agree rather than quietly using different units.
const PL_STAKE = 10

// How the P/L column is derived, surfaced as a tooltip. Mirrors
// get_betting_summary(): a won pick returns the decimal odds less the stake, a
// lost pick costs the stake, and a pick whose match has no score yet is left
// out entirely rather than counted as zero.
const PL_EXPLAINER =
  `Profit or loss from a flat $${PL_STAKE} bet on each of this round's top edge ` +
  "picks - the try-scorer selections where the model's probability most exceeded " +
  "the bookmaker's implied probability. A winning pick returns the stake times the " +
  "decimal odds, less the stake; a losing pick loses the stake. Picks whose match " +
  "has not been scored yet are excluded, so a round with none settled shows a dash."

// Signed dollar amount, e.g. +$30.00 / -$10.00.
function fmtPL(profitPerUnit) {
  const amount = profitPerUnit * PL_STAKE
  const sign = amount > 0 ? '+' : amount < 0 ? '-' : ''
  return `${sign}$${Math.abs(amount).toFixed(2)}`
}

/**
 * Per-round betting profit/loss, shown as dollars on a flat PL_STAKE bet. A
 * round whose picks have not settled yet is still "no result", so it reads as
 * a dash rather than $0.00, which would wrongly look like a break-even round.
 */
function PLCell({ entry }) {
  if (!entry || !entry.settled) {
    const pending = entry ? entry.picks : 0
    return (
      <span
        className="round-bar-pl round-bar-pl-none"
        title={pending
          ? `${pending} pick${pending === 1 ? '' : 's'} captured, none settled yet. ${PL_EXPLAINER}`
          : `No edge picks captured for this round. ${PL_EXPLAINER}`}
      >
        &mdash;
      </span>
    )
  }
  const pl = entry.profit_per_unit_stake
  const cls = pl > 0 ? 'round-bar-pl-up' : pl < 0 ? 'round-bar-pl-down' : ''
  const unsettled = entry.picks - entry.settled
  return (
    <span
      className={`round-bar-pl ${cls}`.trim()}
      title={`${entry.wins}W / ${entry.losses}L from ${entry.settled} settled pick${entry.settled === 1 ? '' : 's'}` +
        (unsettled ? `, ${unsettled} still pending` : '') +
        `. ${PL_EXPLAINER}`}
    >
      {fmtPL(pl)}
    </span>
  )
}

/**
 * Spoken label for a round row. The row is a link with an aria-label, which
 * replaces its contents for assistive tech -- so the P/L has to be said here
 * too, or it is simply absent for anyone using a screen reader.
 */
function rowLabel(r, winPct, entry) {
  const base = `Round ${r.round_number}: ${winPct.toFixed(0)}% of win predictions correct, ${r.win_correct} of ${r.total}`
  if (!entry || !entry.settled) return `${base}. No settled betting picks.`
  const pl = entry.profit_per_unit_stake * PL_STAKE
  return `${base}. Profit and loss ${pl >= 0 ? 'plus' : 'minus'} ${Math.abs(pl).toFixed(2)} dollars on ${PL_STAKE} dollar stakes.`
}

export default function AccuracyDashboard({ apiBase }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [retryCount, setRetryCount] = useState(0)
  const [season, setSeason] = useState(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    fetchJson(`${apiBase}/accuracy${season ? `?season=${season}` : ''}`)
      .then(d => { if (!cancelled) { setData(d); setLoading(false) } })
      .catch(e => { if (!cancelled) { setError(e.message); setLoading(false) } })
    return () => { cancelled = true }
  }, [apiBase, retryCount, season])

  if (loading) return <LoadingSpinner text="Loading accuracy data..." />
  if (error) return (
    <div className="error-container">
      <Link to="/" className="back-link">&larr; Dashboard</Link>
      <div className="error-message">{error}</div>
      <button className="nav-btn" onClick={() => setRetryCount(c => c + 1)}>Retry</button>
    </div>
  )
  if (!data) return <div className="error-message">No accuracy data available yet. View some completed matches to start tracking.</div>

  const win = data.win_prediction
  const tries = data.try_picks
  const multi = data.multi

  const noData = win.total === 0

  // Betting P/L keyed by round, so each bar row can look its own up. Rounds
  // with no captured edge picks simply have no entry.
  const plByRound = {}
  for (const r of data.betting_by_round || []) plByRound[r.round_number] = r

  return (
    <div className="accuracy-dashboard">
      <div className="accuracy-header">
        <Link to="/" className="back-link">&larr; Dashboard</Link>
        <h2>Prediction Accuracy</h2>
        <SeasonSelect
          value={data.season}
          options={data.seasons}
          onChange={setSeason}
        />
      </div>

      {noData ? (
        <div className="accuracy-empty">
          <p>No predictions recorded yet.</p>
          <p>Browse completed matches to start tracking prediction accuracy. Each match you view will be automatically recorded.</p>
        </div>
      ) : (
        <>
          <div className="accuracy-section">
            <h3 className="section-title">Season Totals</h3>
            <div className="accuracy-cards">
              <div className="accuracy-card">
              <div className="accuracy-card-title">Win Predictions</div>
              <div className="accuracy-pct">{(win.accuracy * 100).toFixed(1)}%</div>
              <div className="accuracy-detail">{win.correct} / {win.total} correct</div>
              <div className="accuracy-bar-track">
                <div className="accuracy-bar-fill" style={{ width: `${win.accuracy * 100}%` }} />
              </div>
            </div>
            <div className="accuracy-card">
              <div className="accuracy-card-title">Try Picks (Top 3/team)</div>
              <div className="accuracy-pct">{(tries.hit_rate * 100).toFixed(1)}%</div>
              <div className="accuracy-detail">{tries.hits} / {tries.total_picks} scored</div>
              <div className="accuracy-bar-track">
                <div className="accuracy-bar-fill" style={{ width: `${tries.hit_rate * 100}%` }} />
              </div>
            </div>
            <div className="accuracy-card">
              <div className="accuracy-card-title">Multi (All 3 Score)</div>
              <div className="accuracy-pct">{(multi.hit_rate * 100).toFixed(1)}%</div>
              <div className="accuracy-detail">{multi.all_scored} / {multi.total} all scored, avg {multi.avg_hits}/3 hits</div>
              <div className="accuracy-bar-track">
                  <div className="accuracy-bar-fill" style={{ width: `${multi.hit_rate * 100}%` }} />
                </div>
              </div>
            </div>
          </div>

          {/* By round */}
          {data.by_round && data.by_round.length > 0 && (
            <div className="accuracy-section">
              <h3 className="section-title">Accuracy by Round</h3>
              <div className="round-accuracy-chart">
                <div className="round-bar-head">
                  <span>Rd</span>
                  <span>Win rate</span>
                  <span />
                  <span className="round-bar-head-right">Record</span>
                  <span className="round-bar-head-right" title={PL_EXPLAINER}>
                    P/L ($10) <span className="round-bar-head-info" aria-hidden="true">&#9432;</span>
                  </span>
                </div>
                {data.by_round.map(r => {
                  const winPct = r.total > 0 ? (r.win_correct / r.total) * 100 : 0
                  return (
                    <Link
                      key={r.round_number}
                      to={`/round/${r.round_number}?season=${data.season}`}
                      className="round-bar-row"
                      aria-label={rowLabel(r, winPct, plByRound[r.round_number])}
                    >
                      <span className="round-bar-label">R{r.round_number}</span>
                      <div className="round-bar-track">
                        <div className="round-bar-fill" style={{ width: `${winPct}%` }} />
                      </div>
                      <span className="round-bar-pct">{winPct.toFixed(0)}%</span>
                      <span className="round-bar-count">{r.win_correct}/{r.total}</span>
                      <PLCell entry={plByRound[r.round_number]} />
                    </Link>
                  )
                })}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}

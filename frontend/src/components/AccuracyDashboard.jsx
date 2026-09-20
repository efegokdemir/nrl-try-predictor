import React, { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import LoadingSpinner from './LoadingSpinner'
import { fetchJson } from '../api'
import SeasonSelect from './SeasonSelect'

/**
 * Per-round betting profit/loss. Shown per $1 staked, matching the units the
 * API returns; the dashboard is where a stake can be applied. A round with
 * picks that have not settled yet is still "no result", so it reads as a dash
 * rather than 0.00, which would wrongly look like a break-even round.
 */
function PLCell({ entry }) {
  if (!entry || !entry.settled) {
    return <span className="round-bar-pl round-bar-pl-none">&mdash;</span>
  }
  const pl = entry.profit_per_unit_stake
  const cls = pl > 0 ? 'round-bar-pl-up' : pl < 0 ? 'round-bar-pl-down' : ''
  return (
    <span className={`round-bar-pl ${cls}`.trim()}>
      {pl > 0 ? '+' : ''}{pl.toFixed(2)}
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
  const pl = entry.profit_per_unit_stake
  return `${base}. Profit and loss ${pl >= 0 ? 'plus' : 'minus'} ${Math.abs(pl).toFixed(2)} per dollar staked.`
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

          {/* By round */}
          {data.by_round && data.by_round.length > 0 && (
            <div className="accuracy-section">
              <h3 className="section-title">Accuracy by Round</h3>
              <p className="section-note">
                P/L is profit or loss per $1 staked on that round&rsquo;s edge picks.
                Rounds with no settled picks show &mdash;.
              </p>
              <div className="round-accuracy-chart">
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

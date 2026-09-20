import React, { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import LoadingSpinner from './LoadingSpinner'
import { fetchJson } from '../api'
import SeasonSelect from './SeasonSelect'

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
              <div className="round-accuracy-chart">
                {data.by_round.map(r => {
                  const winPct = r.total > 0 ? (r.win_correct / r.total) * 100 : 0
                  return (
                    <div key={r.round_number} className="round-bar-row">
                      <span className="round-bar-label">R{r.round_number}</span>
                      <div className="round-bar-track">
                        <div className="round-bar-fill" style={{ width: `${winPct}%` }} />
                      </div>
                      <span className="round-bar-pct">{winPct.toFixed(0)}%</span>
                      <span className="round-bar-count">{r.win_correct}/{r.total}</span>
                    </div>
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

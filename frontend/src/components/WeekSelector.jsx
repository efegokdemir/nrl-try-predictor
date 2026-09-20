import React, { useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import LoadingSpinner from './LoadingSpinner'
import MatchCard, { matchPath } from './MatchCard'
import TeamSelect from './TeamSelect'
import { fetchJson, getCurrentSeasonYear } from '../api'

const SEASONS = [2026, 2025, 2024, 2023, 2022, 2021, 2020]
const CURRENT_SEASON = getCurrentSeasonYear()

export default function WeekSelector({ apiBase }) {
  const [searchParams] = useSearchParams()
  const [rounds, setRounds] = useState({})
  const [teams, setTeams] = useState([])
  const [selectedTeam, setSelectedTeam] = useState('')
  const [selectedYear, setSelectedYear] = useState(() => {
    const fromUrl = Number(searchParams.get('season'))
    return SEASONS.includes(fromUrl) ? fromUrl : CURRENT_SEASON
  })
  const [roundOrder, setRoundOrder] = useState('desc')
  const [schedule, setSchedule] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [retryCount, setRetryCount] = useState(0)
  const navigate = useNavigate()

  // Team list for the filter dropdown — fetched once, independent of
  // whichever view (rounds grid vs. team schedule) is currently showing.
  useEffect(() => {
    fetchJson(`${apiBase}/teams`).then(setTeams).catch(() => {})
  }, [apiBase])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)

    if (selectedTeam) {
      fetchJson(`${apiBase}/team-schedule?name=${encodeURIComponent(selectedTeam)}&season=${selectedYear}`)
        .then(data => { if (!cancelled) { setSchedule(data); setLoading(false) } })
        .catch(e => { if (!cancelled) { setError(e.message); setLoading(false) } })
    } else {
      fetchJson(`${apiBase}/rounds?season=${selectedYear}`)
        .then(data => { if (!cancelled) { setRounds(data); setLoading(false) } })
        .catch(e => { if (!cancelled) { setError(e.message); setLoading(false) } })
    }

    return () => { cancelled = true }
  }, [apiBase, selectedTeam, selectedYear, retryCount])

  const sortedRoundEntries = useMemo(() => {
    const entries = Object.entries(rounds)
    entries.sort((a, b) => roundOrder === 'desc' ? Number(b[0]) - Number(a[0]) : Number(a[0]) - Number(b[0]))
    return entries
  }, [rounds, roundOrder])

  const handleMatchClick = (match) => {
    const path = matchPath(match)
    if (path) navigate(path)
  }

  return (
    <div className="week-selector">
      <Hero />

      <div className="list-page-header">
        <h2>{selectedTeam ? `${selectedTeam} — ${selectedYear}` : selectedYear === CURRENT_SEASON ? 'All Rounds' : `All Rounds — ${selectedYear}`}</h2>
        <div className="predictions-filters">
          <TeamSelect teams={teams} value={selectedTeam} onChange={setSelectedTeam} includeAllOption allLabel="All Teams" />
          <select
            className="season-select"
            value={selectedYear}
            onChange={(e) => setSelectedYear(Number(e.target.value))}
            title="Season"
          >
            {SEASONS.map(y => <option key={y} value={y}>{y}</option>)}
          </select>
          {!selectedTeam && (
            <select
              className="season-select"
              value={roundOrder}
              onChange={(e) => setRoundOrder(e.target.value)}
              title="Round order"
            >
              <option value="desc">Newest first</option>
              <option value="asc">Oldest first</option>
            </select>
          )}
        </div>
      </div>

      {loading && <LoadingSpinner text={selectedTeam ? 'Loading team schedule...' : 'Loading rounds...'} />}

      {!loading && error && (
        <>
          <div className="error-message">{error}</div>
          <button className="nav-btn" onClick={() => setRetryCount(c => c + 1)}>Retry</button>
        </>
      )}

      {!loading && !error && selectedTeam && (
        schedule && schedule.matches.length > 0 ? (
          <div className="matches-grid">
            {schedule.matches.map((match, idx) => (
              <MatchCard
                key={match.match_id || match.match_url || idx}
                match={match}
                roundLabel={match.round_name}
                onClick={() => handleMatchClick(match)}
              />
            ))}
          </div>
        ) : (
          <div className="error-message">No games found for {selectedTeam} in {selectedYear}.</div>
        )
      )}

      {!loading && !error && !selectedTeam && (
        sortedRoundEntries.length > 0 ? (
          <div className="rounds-grid">
            {sortedRoundEntries.map(([num, info]) => (
              <button
                key={num}
                className="round-card"
                onClick={() => navigate(`/round/${num}?season=${selectedYear}`)}
              >
                <span className="round-number">{info.name || `Round ${num}`}</span>
              </button>
            ))}
          </div>
        ) : (
          <div className="error-message">No rounds found for {selectedYear}.</div>
        )
      )}
    </div>
  )
}

function Hero() {
  return (
    <div className="home-hero">
      <h1>Know who's <span className="accent">scoring tries</span> before kickoff</h1>
      <p>Player try-scoring probabilities, win predictions and value picks for every match, built from years of NRL data.</p>
    </div>
  )
}

import React, { useState } from 'react'
import { useNavigate } from 'react-router-dom'

export default function PlayerCard({ player, rank, teamColor, actualTries, isCompleted, edgeThreshold }) {
  const [imgFailed, setImgFailed] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const navigate = useNavigate()

  // Check if this player actually scored in completed matches
  const didScore = isCompleted && actualTries
    ? actualTries.some(t => t.player === player.name)
    : false
  const tryCount = isCompleted && actualTries
    ? actualTries.filter(t => t.player === player.name).length
    : 0

  const goToPlayer = (e) => {
    e?.stopPropagation()
    const params = new URLSearchParams({ name: player.name })
    if (player.headshot && !imgFailed) params.set('headshot', player.headshot)
    if (teamColor) params.set('color', teamColor)
    navigate(`/player?${params.toString()}`)
  }

  const bookmakers = player.bookmaker_odds || []
  const hasBookmakers = bookmakers.length > 0
  const hasExtraDetail = hasBookmakers || isCompleted

  // On mobile, the odds breakdown used to force each card into its own
  // horizontally-scrolling strip — tap to expand it inline instead.
  // There's nothing to expand into on a wide screen (or when there's no
  // odds/result detail at all), so the row still just navigates there.
  const handleCardClick = () => {
    const isMobile = window.matchMedia('(max-width: 640px)').matches
    if (isMobile && hasExtraDetail) {
      setExpanded(e => !e)
    } else {
      goToPlayer()
    }
  }

  // Best edge for this player
  const modelProb = player.model_odds ? 1 / player.model_odds : 0
  const playerBestEdge = hasBookmakers && modelProb > 0
    ? Math.max(...bookmakers.map(bk => modelProb - 1 / bk.decimal))
    : -Infinity
  const isTopEdge = playerBestEdge > 0 && edgeThreshold != null && playerBestEdge >= edgeThreshold

  return (
    <div
      className={`player-card clickable ${isCompleted ? (didScore ? 'scored' : '') : ''} ${isTopEdge ? 'top-edge' : ''} ${expanded ? 'expanded' : ''}`}
      onClick={handleCardClick}
    >
      <div className="player-card-main">
        {rank && <div className="player-rank">#{rank}</div>}
        <div className="player-avatar" onClick={goToPlayer}>
          {player.headshot && !imgFailed ? (
            <img
              className="player-headshot"
              src={player.headshot}
              alt={player.name}
              onError={() => setImgFailed(true)}
            />
          ) : (
            <div className="player-number-circle" style={{ backgroundColor: teamColor }}>
              {player.number}
            </div>
          )}
          <span className="player-jersey" style={{ borderColor: teamColor, color: teamColor }}>
            #{player.number}
          </span>
        </div>
        <div className="player-info" onClick={goToPlayer}>
          <span className="player-name">
            {player.name}
            {player.is_captain && <span className="captain-badge">C</span>}
          </span>
          <span className="player-position">
            {player.position}
            {player.field_side && (
              <span className="field-side-tag">{player.field_side === 'left' ? 'L' : player.field_side === 'right' ? 'R' : player.field_side === 'fullback' ? 'FB' : 'M'}</span>
            )}
          </span>
          {isTopEdge && <span className="best-edge-badge">BEST EDGE</span>}
        </div>
        <div className="player-model-block">
          <span className="player-model-label">Model</span>
          {player.model_odds && (
            <span className="player-model-price">${player.model_odds.toFixed(2)}</span>
          )}
          <span className="player-try-pct">{player.try_percentage}%</span>
        </div>
        {hasExtraDetail && (
          <span className="player-card-chevron" aria-hidden>{expanded ? '▾' : '▸'}</span>
        )}
      </div>
      {hasExtraDetail && (
        <div className="player-card-extra">
          {hasBookmakers && (
            <div className="player-odds-row">
              {bookmakers.map((bk, i) => {
                const mProb = player.model_odds ? 1 / player.model_odds : 0
                const bkProb = 1 / bk.decimal
                const edge = mProb - bkProb
                const isValue = edge > 0
                return (
                  <div key={i} className={`odds-block ${isValue ? 'value' : ''}`}>
                    <span className="odds-block-label">{bk.bookmaker}</span>
                    <span className="odds-block-value">${bk.decimal.toFixed(2)}</span>
                    {player.model_odds && (
                      <span className={`odds-block-edge ${isValue ? 'positive' : 'negative'}`}>
                        {isValue ? '+' : ''}{(edge * 100).toFixed(1)}%
                      </span>
                    )}
                  </div>
                )
              })}
            </div>
          )}
          {isCompleted && (
            <div className={`actual-result ${didScore ? 'scored' : 'no-try'}`}>
              {didScore ? (tryCount > 1 ? `${tryCount} TRIES` : 'TRY') : '—'}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

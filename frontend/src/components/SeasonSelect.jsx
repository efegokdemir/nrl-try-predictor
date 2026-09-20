import React from 'react'

/**
 * Year picker for the accuracy and dashboard pages.
 *
 * `value` is the season the API actually used — both endpoints echo it back —
 * so the control reflects what the server returned rather than optimistic
 * local state. `options` is the server's list of selectable seasons, which
 * always contains the current one even before it has any settled predictions.
 *
 * Rendered even when there is only one season to pick, because it is also
 * labelling the scope of the numbers beside it: these pages are per-season,
 * not career totals, and that is worth stating on screen.
 */
export default function SeasonSelect({ value, options, onChange }) {
  const seasons = options && options.length ? options : (value ? [value] : [])
  if (!seasons.length) return null

  return (
    <label className="season-picker">
      <span className="season-picker-label">Season</span>
      <select
        className="season-select"
        value={value ?? ''}
        onChange={e => onChange(Number(e.target.value))}
      >
        {seasons.map(s => (
          <option key={s} value={s}>{s}</option>
        ))}
      </select>
    </label>
  )
}

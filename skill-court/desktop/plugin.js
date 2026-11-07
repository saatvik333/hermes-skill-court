// Skill Court page for Hermes Desktop: the latest verdict per skill and model, and the quarantine
// switch. Data comes from this plugin's backend (dashboard/plugin_api.py) through ctx.rest.
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  Loader,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  cn,
  relativeTime,
  useQuery,
  useQueryClient
} from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const VERDICT_ORDER = { hurts: 0, inconclusive: 1, neutral: 2, helps: 3 }
const VERDICT_BADGE = { hurts: 'destructive', inconclusive: 'warn', neutral: 'muted', helps: 'success' }
const ANY_MODEL = '*'

const pct = x => (x === null || x === undefined ? '–' : `${Math.round(x * 100)}%`)
const signed = x => (x === null || x === undefined ? '–' : `${x >= 0 ? '+' : '−'}${Math.abs(Math.round(x * 100))}%`)

function successCell(v) {
  if (v.success_without === null || v.success_without === undefined) {
    return jsx('span', { className: 'text-(--ui-text-tertiary)', children: 'no outcome' })
  }
  const ci = v.success_ci
  return jsxs('div', {
    children: [
      jsx('div', { className: 'tabular-nums', children: `${pct(v.success_without)} → ${pct(v.success_with)}` }),
      ci
        ? jsx('div', {
            className: 'text-[0.6875rem] tabular-nums text-(--ui-text-tertiary)',
            children: `change ${signed(v.success_delta)}, 95% CI ${signed(ci[0])} to ${signed(ci[1])}`
          })
        : null
    ]
  })
}

function tokenCell(v) {
  if (v.token_change === null || v.token_change === undefined) {
    return jsx('span', { className: 'text-(--ui-text-tertiary)', children: '–' })
  }
  const ci = v.token_ci
  return jsxs('div', {
    children: [
      jsx('div', { className: 'tabular-nums', children: signed(v.token_change) }),
      ci
        ? jsx('div', {
            className: 'text-[0.6875rem] tabular-nums text-(--ui-text-tertiary)',
            children: `95% CI ${signed(ci[0])} to ${signed(ci[1])}`
          })
        : null
    ]
  })
}

function ActionCell({ row, act, pending, error }) {
  const hidden = row.quarantined
  if (!hidden && row.verdict !== 'hurts') {
    return null
  }
  const label = hidden ? 'Release' : 'Quarantine'
  const description = hidden
    ? `Show ${row.skill} to ${row.model} again`
    : `Hide ${row.skill} from ${row.model}; its files are not changed`
  return jsxs('div', {
    className: 'flex flex-col items-start gap-1',
    children: [
      // One button whose label flips keeps keyboard focus on it across the change.
      jsx(Button, {
        'aria-description': description,
        'aria-disabled': pending || undefined,
        onClick: () => (pending ? undefined : act(hidden ? 'release' : 'quarantine', row)),
        size: 'sm',
        title: description,
        variant: hidden ? 'outline' : 'secondary',
        children: pending ? `${label}…` : label
      }),
      error ? jsx('span', { className: 'text-[0.6875rem] text-destructive', role: 'alert', children: error }) : null
    ]
  })
}

function VerdictRow({ row, act, pending, error }) {
  return jsxs('tr', {
    className: 'border-t border-(--ui-stroke-secondary) align-top',
    children: [
      jsx('th', {
        className: 'py-2 pr-4 text-left font-medium',
        scope: 'row',
        children: jsxs('div', {
          children: [
            jsx('div', { children: row.skill }),
            row.skill_changed
              ? jsx('div', {
                  className: 'text-[0.6875rem] font-normal text-(--ui-text-tertiary)',
                  children: 'skill changed since this verdict'
                })
              : null
          ]
        })
      }),
      jsx('td', { className: 'py-2 pr-4 font-mono text-[0.75rem] text-(--ui-text-secondary)', children: row.model }),
      jsx('td', {
        className: 'py-2 pr-4',
        children: jsxs('div', {
          className: 'flex flex-col items-start gap-1',
          children: [
            jsx(Badge, { variant: VERDICT_BADGE[row.verdict] ?? 'muted', children: row.verdict }),
            row.reason
              ? jsx('span', { className: 'max-w-72 text-[0.6875rem] text-(--ui-text-tertiary)', children: row.reason })
              : null,
            row.quarantined
              ? jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-secondary)', children: 'hidden for this model' })
              : null
          ]
        })
      }),
      jsx('td', { className: 'py-2 pr-4', children: successCell(row) }),
      jsx('td', { className: 'py-2 pr-4', children: tokenCell(row) }),
      jsx('td', { className: 'py-2 pr-4 tabular-nums', children: row.pairs ?? '–' }),
      jsx('td', {
        className: 'py-2 pr-4 text-(--ui-text-secondary)',
        children: jsx('time', { dateTime: row.judged_at, title: row.judged_at, children: relativeTime(row.judged_at) })
      }),
      jsx('td', { className: 'py-2', children: jsx(ActionCell, { act, error, pending, row }) })
    ]
  })
}

function ManualQuarantine({ entries, act, pendingKey, errors }) {
  if (!entries.length) {
    return null
  }
  return jsxs('section', {
    'aria-labelledby': 'skill-court-manual',
    className: 'flex flex-col gap-2',
    children: [
      jsx('h2', { className: 'text-sm font-medium', id: 'skill-court-manual', children: 'Quarantined without a verdict' }),
      jsx('ul', {
        className: 'flex flex-col gap-1',
        children: entries.map(q => {
          const key = `${q.skill}\u0000${q.model}`
          const pending = pendingKey === key
          return jsxs(
            'li',
            {
              className: 'flex items-center gap-3 text-sm',
              children: [
                jsx('span', { className: 'font-medium', children: q.skill }),
                jsx('span', {
                  className: 'text-(--ui-text-secondary)',
                  children: q.model === ANY_MODEL ? 'every model' : q.model
                }),
                jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: q.reason }),
                jsx(Button, {
                  'aria-disabled': pending || undefined,
                  onClick: () => (pending ? undefined : act('release', { skill: q.skill, model: q.model })),
                  size: 'sm',
                  variant: 'outline',
                  children: pending ? 'Release…' : 'Release'
                }),
                errors[key] ? jsx('span', { className: 'text-[0.6875rem] text-destructive', role: 'alert', children: errors[key] }) : null
              ]
            },
            key
          )
        })
      })
    ]
  })
}

function Summary({ verdicts, quarantine }) {
  const counts = verdicts.reduce((acc, v) => ({ ...acc, [v.verdict]: (acc[v.verdict] ?? 0) + 1 }), {})
  const parts = ['hurts', 'helps', 'neutral', 'inconclusive']
    .filter(k => counts[k])
    .map(k => `${counts[k]} ${k === 'hurts' ? 'hurt' : k === 'helps' ? 'help' : k}`)
  return jsx('p', {
    className: 'text-sm text-(--ui-text-secondary)',
    children: `${verdicts.length} verdict${verdicts.length === 1 ? '' : 's'}: ${parts.join(', ')}. ${quarantine.length} quarantine${quarantine.length === 1 ? '' : 's'} active.`
  })
}

function SkillCourtPage({ rest, source }) {
  const queryClient = useQueryClient()
  const queryKey = [source, 'verdicts']
  const query = useQuery({ queryKey, queryFn: () => rest('/verdicts'), refetchInterval: 30_000 })
  const [pendingKey, setPendingKey] = useState(null)
  const [errors, setErrors] = useState({})
  const [announcement, setAnnouncement] = useState('')

  const act = async (action, row) => {
    const key = `${row.skill}\u0000${row.model}`
    setPendingKey(key)
    setErrors(prev => ({ ...prev, [key]: undefined }))
    try {
      // A verdict row's release lifts every quarantine that hides the skill from that model,
      // including one set for every model; a manual row releases exactly its own record.
      const body =
        action === 'release'
          ? { skill: row.skill, model: row.fromVerdict ? null : row.model }
          : { skill: row.skill, model: row.model, reason: `quarantined from Hermes Desktop after verdict: ${row.reason ?? row.verdict}` }
      await rest(`/${action}`, { method: 'POST', body })
      await queryClient.invalidateQueries({ queryKey })
      setAnnouncement(
        action === 'release'
          ? `${row.skill} is visible again${row.fromVerdict ? '' : ` for ${row.model === ANY_MODEL ? 'every model' : row.model}`}.`
          : `${row.skill} is now hidden from ${row.model}. Its files are unchanged.`
      )
    } catch (err) {
      setErrors(prev => ({ ...prev, [key]: `Could not ${action}: ${err?.message ?? err}` }))
    } finally {
      setPendingKey(null)
    }
  }

  let body
  if (query.isPending) {
    body = jsx(Loader, {})
  } else if (query.isError) {
    body = jsx(ErrorState, {
      description: String(query.error?.message ?? query.error),
      title: 'Could not load Skill Court verdicts',
      children: jsx(Button, { onClick: () => query.refetch(), size: 'sm', variant: 'outline', children: 'Try again' })
    })
  } else {
    const { verdicts, quarantine } = query.data
    const covered = new Set(verdicts.filter(v => v.quarantined).map(v => v.skill))
    const manual = quarantine.filter(q => !covered.has(q.skill))
    const rows = [...verdicts]
      .sort((a, b) => (VERDICT_ORDER[a.verdict] ?? 9) - (VERDICT_ORDER[b.verdict] ?? 9) || a.skill.localeCompare(b.skill))
      .map(v => ({ ...v, fromVerdict: true }))
    body = verdicts.length
      ? jsxs('div', {
          className: 'flex flex-col gap-6',
          children: [
            jsx(Summary, { quarantine, verdicts }),
            jsx('div', {
              className: 'overflow-x-auto',
              children: jsxs('table', {
                className: 'w-full border-collapse text-sm',
                children: [
                  jsx('caption', {
                    className: 'sr-only',
                    children: 'Latest verdict per skill and model; harmful skills first'
                  }),
                  jsx('thead', {
                    children: jsx('tr', {
                      className: 'text-left text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)',
                      children: [
                        'Skill',
                        'Model',
                        'Verdict',
                        'Success without → with',
                        'Tokens with skill',
                        'Pairs',
                        'Judged',
                        'Action'
                      ].map(h => jsx('th', { className: 'pb-2 pr-4 font-medium', scope: 'col', children: h }, h))
                    })
                  }),
                  jsx('tbody', {
                    children: rows.map(row => {
                      const key = `${row.skill}\u0000${row.model}`
                      return jsx(VerdictRow, { act, error: errors[key], pending: pendingKey === key, row }, key)
                    })
                  })
                ]
              })
            }),
            jsx(ManualQuarantine, { act, entries: manual, errors, pendingKey })
          ]
        })
      : jsx(EmptyState, {
          description: 'Run `hermes court trial --all` in a terminal. Each trial replays tasks with and without a skill.',
          title: 'No verdicts yet'
        })
  }

  return jsxs('div', {
    className: 'flex h-full flex-col gap-4 overflow-y-auto p-6',
    children: [
      jsxs('header', {
        className: 'flex items-start justify-between gap-4',
        children: [
          jsxs('div', {
            className: 'flex flex-col gap-1',
            children: [
              jsx('h1', { className: 'text-lg font-semibold', children: 'Skill Court' }),
              jsx('p', {
                className: 'max-w-prose text-sm text-(--ui-text-secondary)',
                children:
                  'Each verdict compares the same tasks run with and without the skill, on one model. Quarantine hides a skill from that model only and never edits its files.'
              })
            ]
          }),
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              query.dataUpdatedAt
                ? jsx('span', {
                    className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                    children: `updated ${relativeTime(new Date(query.dataUpdatedAt).toISOString())}`
                  })
                : null,
              jsx(Button, {
                'aria-disabled': query.isFetching || undefined,
                onClick: () => (query.isFetching ? undefined : query.refetch()),
                size: 'sm',
                variant: 'outline',
                children: query.isFetching ? 'Refreshing…' : 'Refresh'
              })
            ]
          })
        ]
      }),
      jsx('div', { 'aria-live': 'polite', className: cn('sr-only'), role: 'status', children: announcement }),
      body
    ]
  })
}

export default {
  id: 'skill-court',
  name: 'Skill Court',
  register(ctx) {
    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: '/skill-court' },
        render: () => jsx(SkillCourtPage, { rest: ctx.rest, source: ctx.source })
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        data: { codicon: 'law', label: 'Skill Court', path: '/skill-court' }
      }
    ])
  }
}

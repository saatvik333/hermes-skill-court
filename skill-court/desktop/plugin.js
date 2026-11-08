// Skill Court page for Hermes Desktop: the latest verdict per skill and model, the quarantine
// switch, and fixes proposed by appeals. Data comes from this plugin's backend
// (dashboard/plugin_api.py) through ctx.rest.
import {
  Badge,
  Button,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  Loader,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  cn,
  formatAgo,
  useQuery,
  useQueryClient
} from '@hermes/plugin-sdk'
import { useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const VERDICT_ORDER = { hurts: 0, inconclusive: 1, neutral: 2, helps: 3 }
const VERDICT_BADGE = { hurts: 'destructive', inconclusive: 'warn', neutral: 'muted', helps: 'success' }
const ANY_MODEL = '*'

const pct = x => (x === null || x === undefined ? '–' : `${Math.round(x * 100)}%`)
const signed = x => {
  if (x === null || x === undefined) {
    return '–'
  }
  const r = Math.round(x * 100)
  return `${r < 0 ? '−' : '+'}${Math.abs(r)}%`
}

const AGO = {
  ageNow: 'just now',
  ageSeconds: s => `${s}s ago`,
  ageMinutes: m => `${m}m ago`,
  ageHours: h => `${h}h ago`,
  ageDays: d => `${d}d ago`
}

function Ago({ ms }) {
  const date = new Date(ms)
  return jsx('time', { dateTime: date.toISOString(), title: date.toLocaleString(), children: formatAgo(ms, AGO) })
}

function successCell(v) {
  if (v.success_without === null || v.success_without === undefined) {
    return jsx('span', { className: 'text-(--ui-text-tertiary)', children: 'no outcome' })
  }
  const ci = v.success_ci
  return jsxs('div', {
    children: [
      jsx('div', { className: 'tabular-nums', children: `${pct(v.success_without)} → ${pct(v.success_with)}` }),
      ci && signed(ci[0]) !== signed(ci[1])
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
      ci && signed(ci[0]) !== signed(ci[1])
        ? jsx('div', {
            className: 'text-[0.6875rem] tabular-nums text-(--ui-text-tertiary)',
            children: `95% CI ${signed(ci[0])} to ${signed(ci[1])}`
          })
        : null
    ]
  })
}

const scopeName = model => (model === ANY_MODEL ? 'every model' : model)

const ANSWER_ONLY = 'answer-only'

// Where a verdict's pair outcomes came from, as the CLI report words it: "2 checked, 1 judged", or
// "3 judged, answer-only" for a trial that ran without sandboxes.
const describeSources = (sources, mode) => {
  const text =
    [
      ['check', 'checked'],
      ['judge', 'judged']
    ]
      .filter(([key]) => sources[key])
      .map(([key, label]) => `${sources[key]} ${label}`)
      .join(', ') || 'no outcomes'
  return mode === ANSWER_ONLY ? `${text}, ${ANSWER_ONLY}` : text
}

function Cause({ row }) {
  if (!row.cause) {
    return null
  }
  return jsxs('div', {
    className: 'flex max-w-80 flex-col gap-1 text-[0.6875rem] text-(--ui-text-secondary)',
    children: [
      jsx('p', { children: `Why: ${row.cause}` }),
      row.cause_quote
        ? jsxs('p', {
            children: ['Skill line: ', jsx('q', { className: 'font-mono', children: row.cause_quote })]
          })
        : null
    ]
  })
}

// A fix an appeal drafted and tried. Only a granted draft of the skill as it is can be applied; the
// dialog says which file is overwritten and where the old text stays.
function AppealCell({ row, applyFix }) {
  const [open, setOpen] = useState(false)
  const appeal = row.appeal
  if (!appeal) {
    return null
  }
  const note = text => jsx('p', { className: 'max-w-80 text-[0.6875rem] text-(--ui-text-secondary)', children: text })
  if (appeal.applied) {
    return note(`Fix applied (appeal #${appeal.id}). Undo: hermes court appeal undo ${row.skill} --model ${row.model}`)
  }
  if (appeal.verdict !== 'granted') {
    return note(appeal.verdict ? `Fix denied: ${appeal.reason}` : 'A drafted fix is still on trial.')
  }
  if (appeal.stale) {
    return note('A fix was proven for an earlier version of this skill; draft a new appeal.')
  }
  return jsxs('div', {
    className: 'flex max-w-80 flex-col items-start gap-1 text-[0.6875rem]',
    children: [
      jsx(Badge, { variant: 'success', children: 'Fix proposed' }),
      jsx('p', { className: 'text-(--ui-text-secondary)', children: appeal.reason }),
      jsxs('details', {
        children: [
          jsx('summary', { className: 'cursor-pointer text-(--ui-text-secondary)', children: 'Show the change' }),
          jsx('pre', {
            className: 'mt-1 max-h-64 overflow-auto rounded border border-(--ui-stroke-secondary) p-2 font-mono',
            // Inline: the host's stylesheet is built without this plugin's classes, and an uncapped
            // diff widens the table until the other columns scroll out of view.
            style: { maxWidth: '36rem', overflowWrap: 'anywhere', whiteSpace: 'pre-wrap' },
            children: appeal.diff
          })
        ]
      }),
      jsx(Button, {
        'aria-label': `Apply fix to ${row.skill}: overwrite its SKILL.md with the draft that passed its trial`,
        onClick: () => setOpen(true),
        size: 'sm',
        variant: 'secondary',
        children: 'Apply fix'
      }),
      jsx(ConfirmDialog, {
        busyLabel: 'Applying…',
        confirmLabel: 'Apply fix',
        description: `This overwrites ${appeal.path} with the draft from appeal #${appeal.id}. The current text is kept in Skill Court's store; \`hermes court appeal undo ${row.skill} --model ${row.model}\` puts it back. The quarantine for ${row.model} is lifted.`,
        destructive: true,
        doneLabel: 'Applied',
        onClose: () => setOpen(false),
        onConfirm: () => applyFix(row),
        open,
        title: `Apply the fix to ${row.skill}?`
      })
    ]
  })
}

function ActionCell({ row, act, pending, error }) {
  const hidden = row.quarantined
  if (!hidden && row.verdict !== 'hurts') {
    return null
  }
  const label = hidden ? 'Release' : 'Quarantine'
  // Releasing a verdict row lifts every record that hides the skill from its model, which can
  // include one set for every model; the label says so.
  const releaseScope = row.hiddenBy?.includes(ANY_MODEL) ? 'every model' : row.model
  const accessibleName = hidden
    ? `Release ${row.skill}: show it to ${releaseScope} again`
    : `Quarantine ${row.skill} for ${row.model}; its files are not changed`
  return jsxs('div', {
    className: 'flex flex-col items-start gap-1',
    children: [
      // One button whose label flips keeps keyboard focus on it across the change.
      jsx(Button, {
        'aria-disabled': pending || undefined,
        'aria-label': accessibleName,
        onClick: () => (pending ? undefined : act(hidden ? 'release' : 'quarantine', row)),
        size: 'sm',
        title: accessibleName,
        variant: hidden ? 'outline' : 'secondary',
        children: pending ? `${label}…` : hidden && releaseScope === 'every model' ? 'Release everywhere' : label
      }),
      error ? jsx('span', { className: 'text-[0.6875rem] text-destructive', role: 'alert', children: error }) : null
    ]
  })
}

function VerdictRow({ row, act, applyFix, pending, error }) {
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
              : null,
            row.inUse
              ? jsx('div', {
                  className: 'max-w-64 text-[0.6875rem] font-normal text-(--ui-text-tertiary)',
                  children: `In use: ${row.inUse}`
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
            jsx(Cause, { row }),
            jsx(AppealCell, { applyFix, row }),
            row.quarantined
              ? jsx('span', {
                  className: 'text-[0.6875rem] text-(--ui-text-secondary)',
                  children: row.hiddenBy?.includes(ANY_MODEL) ? 'hidden for every model' : 'hidden for this model'
                })
              : null
          ]
        })
      }),
      jsx('td', { className: 'py-2 pr-4', children: successCell(row) }),
      jsx('td', { className: 'py-2 pr-4', children: tokenCell(row) }),
      jsx('td', {
        className: 'py-2 pr-4 tabular-nums',
        children: jsxs('div', {
          children: [
            jsx('div', { children: row.tasks ?? row.pairs ?? '–' }),
            row.sources
              ? jsx('div', {
                  className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                  children: describeSources(row.sources, row.mode)
                })
              : null
          ]
        })
      }),
      jsx('td', {
        className: 'py-2 pr-4 text-(--ui-text-secondary)',
        children: jsxs('div', {
          children: [
            jsx(Ago, { ms: Date.parse(row.judged_at) }),
            row.auto ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: 'automatic' }) : null
          ]
        })
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
                  children: scopeName(q.model)
                }),
                jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: q.reason }),
                jsx(Button, {
                  'aria-disabled': pending || undefined,
                  'aria-label': `Release ${q.skill} for ${scopeName(q.model)}`,
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

const usd = x => `$${(x ?? 0).toFixed(2)}`

const NOTE = 'max-w-prose text-[0.6875rem] text-(--ui-text-tertiary)'

function Summary({ verdicts, quarantine, auto, usageLabel }) {
  const counts = verdicts.reduce((acc, v) => ({ ...acc, [v.verdict]: (acc[v.verdict] ?? 0) + 1 }), {})
  const parts = ['hurts', 'helps', 'neutral', 'inconclusive']
    .filter(k => counts[k])
    .map(k => `${counts[k]} ${k === 'hurts' ? 'hurt' : k === 'helps' ? 'help' : k}`)
  const judged = verdicts.some(v => v.sources?.judge)
  const answerOnly = verdicts.some(v => v.mode === ANSWER_ONLY)
  return jsxs('div', {
    className: 'flex flex-col gap-1 text-sm text-(--ui-text-secondary)',
    children: [
      jsx('p', {
        children: `${verdicts.length} verdict${verdicts.length === 1 ? '' : 's'}: ${parts.join(', ')}. ${quarantine.length} quarantine${quarantine.length === 1 ? '' : 's'} active.`
      }),
      jsx('p', {
        children: auto?.enabled
          ? `Automatic trials are on: ${usd(auto.spent_today_usd)} of ${usd(auto.daily_budget_usd)} spent today; the judge used ${(auto.judge_tokens_today ?? 0).toLocaleString()} of ${(auto.daily_judge_tokens ?? 0).toLocaleString()} tokens.`
          : 'Automatic trials are off. Turn them on with `hermes court auto on`.'
      }),
      judged
        ? jsx('p', {
            className: NOTE,
            children:
              'Judged tasks have no check: a judge model compared both runs’ answers and files, without knowing which run had the skill, in both orders. A run counts as failed only when the judge preferred the other run both times.'
          })
        : null,
      answerOnly
        ? jsx('p', {
            className: NOTE,
            children:
              'Answer-only verdicts ran without a Docker sandbox: the replays had read-only tools only, and every pair was judged on the final answers. They measure what a skill makes the agent say or decide, not procedures that need tools.'
          })
        : null,
      usageLabel ? jsx('p', { className: NOTE, children: `“In use” is ${usageLabel}.` }) : null
    ]
  })
}

function SkillCourtPage({ rest, source }) {
  const queryClient = useQueryClient()
  const queryKey = [source, 'verdicts']
  const query = useQuery({ queryKey, queryFn: () => rest('/verdicts'), refetchInterval: 30_000 })
  // Usage evidence reads the whole session history, so it is fetched when the page opens and on
  // Refresh, not on every poll.
  const usageQuery = useQuery({ queryKey: [source, 'usage'], queryFn: () => rest('/usage'), staleTime: Infinity })
  const [pendingKey, setPendingKey] = useState(null)
  const [errors, setErrors] = useState({})
  const [announcement, setAnnouncement] = useState('')
  const [manualRefresh, setManualRefresh] = useState(false)
  const headingRef = useRef(null)

  const act = async (action, row) => {
    const key = `${row.skill}\u0000${row.model}`
    const models = action === 'release' ? (row.hiddenBy ?? [row.model]) : [row.model]
    setPendingKey(key)
    setErrors(prev => ({ ...prev, [key]: undefined }))
    try {
      for (const model of models) {
        const body =
          action === 'release'
            ? { skill: row.skill, model }
            : { skill: row.skill, model, reason: `quarantined from Hermes Desktop: ${row.reason ?? row.verdict}` }
        await rest(`/${action}`, { method: 'POST', body })
      }
      const scope = models.includes(ANY_MODEL) ? 'every model' : scopeName(row.model)
      setAnnouncement(
        action === 'release'
          ? `${row.skill} is visible again to ${scope}.`
          : `${row.skill} is now hidden from ${scope}. Its files are unchanged.`
      )
    } catch (err) {
      setErrors(prev => ({ ...prev, [key]: `Could not ${action}: ${err?.message ?? err}` }))
    } finally {
      await queryClient.invalidateQueries({ queryKey })
      setPendingKey(null)
      // A released row without a harmful verdict loses its button; keep keyboard focus on the page.
      requestAnimationFrame(() => {
        if (!document.activeElement || document.activeElement === document.body) {
          headingRef.current?.focus()
        }
      })
    }
  }

  // Runs inside the confirmation dialog: a failure throws, and the dialog shows it beside its buttons.
  const applyFix = async row => {
    try {
      await rest('/appeal/apply', {
        method: 'POST',
        body: { skill: row.skill, model: row.model, appeal_id: row.appeal.id }
      })
      setAnnouncement(`${row.skill}: fix applied. ${row.appeal.path} was overwritten; the old text is kept by Skill Court.`)
    } finally {
      await queryClient.invalidateQueries({ queryKey })
      requestAnimationFrame(() => {
        if (!document.activeElement || document.activeElement === document.body) {
          headingRef.current?.focus()
        }
      })
    }
  }

  const refresh = async () => {
    if (query.isFetching) {
      return
    }
    setManualRefresh(true)
    try {
      await Promise.all([query.refetch(), usageQuery.refetch()])
    } finally {
      setManualRefresh(false)
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
    const { verdicts, quarantine, auto, appeals } = query.data
    const inUse = usageQuery.data?.skills ?? {}
    const hiddenBy = v =>
      quarantine.filter(q => q.skill === v.skill && (q.model === v.model || q.model === ANY_MODEL)).map(q => q.model)
    const shown = new Set(verdicts.flatMap(v => hiddenBy(v).map(m => `${v.skill}\u0000${m}`)))
    const manual = quarantine.filter(q => !shown.has(`${q.skill}\u0000${q.model}`))
    // Rows of one skill stay together so its verdicts on different models read side by side;
    // skills are ordered by their worst verdict, harmful first.
    const worst = {}
    for (const v of verdicts) {
      worst[v.skill] = Math.min(worst[v.skill] ?? 9, VERDICT_ORDER[v.verdict] ?? 9)
    }
    const rows = [...verdicts]
      .sort(
        (a, b) =>
          worst[a.skill] - worst[b.skill] ||
          a.skill.localeCompare(b.skill) ||
          (VERDICT_ORDER[a.verdict] ?? 9) - (VERDICT_ORDER[b.verdict] ?? 9) ||
          a.model.localeCompare(b.model)
      )
      // The usage line goes on a skill's first row only: it is about the skill, not one model's verdict.
      .map((v, index, sorted) => ({
        ...v,
        hiddenBy: hiddenBy(v),
        appeal: (appeals ?? []).find(a => a.skill === v.skill && a.model === v.model),
        inUse: index === 0 || sorted[index - 1].skill !== v.skill ? inUse[v.skill] : undefined
      }))
    body = verdicts.length
      ? jsxs('div', {
          className: 'flex flex-col gap-6',
          children: [
            jsx(Summary, {
              auto,
              quarantine,
              usageLabel: Object.keys(inUse).length ? usageQuery.data.label : undefined,
              verdicts
            }),
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
                        'Tasks',
                        'Judged',
                        'Action'
                      ].map(h => jsx('th', { className: 'pb-2 pr-4 font-medium', scope: 'col', children: h }, h))
                    })
                  }),
                  jsx('tbody', {
                    children: rows.map(row => {
                      const key = `${row.skill}\u0000${row.model}`
                      return jsx(
                        VerdictRow,
                        { act, applyFix, error: errors[key], pending: pendingKey === key, row },
                        key
                      )
                    })
                  })
                ]
              })
            }),
            jsx(ManualQuarantine, { act, entries: manual, errors, pendingKey })
          ]
        })
      : jsx(EmptyState, {
          description:
            'With automatic trials on, a skill gets a verdict after the agent writes or changes it. To try existing skills now, run `hermes court trial --all` in a terminal.',
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
              jsx('h1', { className: 'text-lg font-semibold outline-none', ref: headingRef, tabIndex: -1, children: 'Skill Court' }),
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
                    children: jsxs('span', { children: ['updated ', jsx(Ago, { ms: query.dataUpdatedAt })] })
                  })
                : null,
              jsx(Button, {
                'aria-disabled': manualRefresh || undefined,
                onClick: refresh,
                size: 'sm',
                variant: 'outline',
                children: manualRefresh ? 'Refreshing…' : 'Refresh'
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

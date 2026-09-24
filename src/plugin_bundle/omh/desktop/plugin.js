// The OMH desktop half: the status line and plan todo that the modern-TUI
// widget renders (src/omh/tui_widgets/omh-status.mjs), for Hermes Desktop.
//
// Plain ESM, loaded uncompiled through the app's disk-plugin door: only the
// '@hermes/plugin-sdk' and React shims resolve, and JSX syntax is a
// SyntaxError there, so the tree is written with jsx()/jsxs().
//
// Electron copies this file from plugins/omh/desktop/ to desktop-plugins/omh/
// beside a .hermes-package.json marker, and that marker keeps the half OFF
// until the user switches it on under Capabilities -> Plugins. Every value
// shown comes from the sibling backend (dashboard/plugin_api.py) through
// ctx.rest('/hud'), which reaches /api/plugins/omh/hud inside the gateway
// process; nothing is computed here, so a number the reader did not produce
// is never displayed.
import { cn, host, PANES_AREA, STATUSBAR_AREAS, Tip, useQuery, useValue } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

export const PLUGIN_ID = 'omh'
export const POLL_MS = 5000
export const UNAVAILABLE_LINE = 'omh: backend unavailable'

// Bound in register(); ctx.rest is scoped to this plugin's namespace.
let rest = null

export function hudPath(sessionId) {
  return sessionId ? `/hud?session=${encodeURIComponent(sessionId)}` : '/hud'
}

function fetchHud(sessionId) {
  if (!rest) {
    return Promise.reject(new Error('omh plugin context is not registered'))
  }
  return rest(hudPath(sessionId))
}

function useHud() {
  const gateway = useValue(host.state.gateway)
  const sessionId = useValue(host.state.focusedStoredSessionId)
  const query = useQuery({
    queryKey: [PLUGIN_ID, 'hud', sessionId || ''],
    queryFn: () => fetchHud(sessionId),
    refetchInterval: POLL_MS,
    enabled: gateway === 'open'
  })
  return { gateway, sessionId, data: query.data, error: query.error }
}

// The status-bar text: the reader's own display.line verbatim once it has
// answered, otherwise a short state. Never a line assembled here.
export function statusLine({ gateway, data, error }) {
  if (gateway !== 'open') {
    return `omh: gateway ${gateway}`
  }
  if (error) {
    return UNAVAILABLE_LINE
  }
  if (!data) {
    return 'omh: reading'
  }
  if (data.error) {
    return 'omh: reader error'
  }
  const line = data.display && typeof data.display.line === 'string' ? data.display.line : ''
  return line || 'omh: no status line'
}

// What the pane shows: either one state sentence, or the widget lines and
// the todo lines exactly as the reader produced them.
export function paneView({ gateway, data, error }) {
  if (gateway !== 'open') {
    return { state: `gateway ${gateway}`, widget: [], todo: [] }
  }
  if (error) {
    return { state: `backend unavailable: ${error.message || String(error)}`, widget: [], todo: [] }
  }
  if (!data) {
    return { state: 'reading', widget: [], todo: [] }
  }
  if (data.error) {
    return { state: `reader error: ${data.error}`, widget: [], todo: [] }
  }
  const display = data.display || {}
  return {
    state: '',
    widget: Array.isArray(display.widget_lines) ? display.widget_lines : [],
    todo: Array.isArray(display.todo_lines) ? display.todo_lines : []
  }
}

function StatusChip() {
  const line = statusLine(useHud())
  return jsx(Tip, {
    label: 'oh-my-hermes status line, polled from /api/plugins/omh/hud',
    children: jsx('span', {
      className: cn('inline-flex h-full items-center px-1.5 text-[0.6875rem]', 'text-(--ui-text-tertiary)'),
      children: line
    })
  })
}

function LineList({ lines, tone }) {
  return jsx('ul', {
    className: 'flex flex-col gap-0.5 whitespace-pre-wrap break-words font-mono text-[0.75rem]',
    children: lines.map((line, index) => jsx('li', { className: tone, children: line }, `${index}:${line}`))
  })
}

function HudPane() {
  const hud = useHud()
  const view = paneView(hud)
  const children = [
    jsx('div', { className: 'font-medium', children: 'oh-my-hermes' }),
    jsx('div', {
      className: 'text-[0.6875rem] text-(--ui-text-quaternary)',
      children: hud.sessionId ? `session ${hud.sessionId}` : 'no focused session'
    })
  ]
  if (view.state) {
    children.push(jsx('div', { className: 'text-(--ui-text-tertiary)', children: view.state }))
  } else {
    children.push(jsx(LineList, { lines: view.widget, tone: 'text-(--ui-text-secondary)' }))
    children.push(
      view.todo.length
        ? jsx(LineList, { lines: view.todo, tone: '' })
        : jsx('div', { className: 'text-(--ui-text-quaternary)', children: 'no plan todo declared for this session' })
    )
  }
  return jsxs('div', { className: 'flex h-full flex-col gap-2 overflow-auto p-3 text-sm', children })
}

export default {
  id: PLUGIN_ID,
  name: 'oh-my-hermes',
  description: 'OMH status line and plan todo, read from the omh plugin backend inside the Hermes gateway.',
  defaultEnabled: false,
  register(ctx) {
    rest = ctx.rest
    ctx.onDispose(() => {
      rest = null
    })
    ctx.registerMany([
      {
        id: 'status',
        area: STATUSBAR_AREAS.right,
        order: 130,
        render: () => jsx(StatusChip, {})
      },
      {
        id: 'hud',
        area: PANES_AREA,
        title: 'omh',
        data: { placement: 'right', width: '300px' },
        render: () => jsx(HudPane, {})
      }
    ])
  }
}

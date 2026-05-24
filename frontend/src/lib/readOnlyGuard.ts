/**
 * Module-level switch for the self-read-only experiment.
 *
 * Lives outside any kea logic so it can be read from `lib/api.ts` without
 * pulling the layout/navigation logic into the lib graph. The kea logic
 * (`selfReadOnlyModeLogic`) registers a getter that reads its current state.
 */

export class ReadOnlyModeError extends Error {
    // Many call sites in the app catch api errors with the shape
    // `lemonToast.error(error.detail || 'Failed to ...')`. Without `detail`,
    // a read-only block would surface as the misleading fallback ("Failed to
    // launch experiment" etc.) on top of the dedicated read-only toast.
    // Setting `detail` here keeps that secondary toast at least truthful.
    detail = 'Read-only mode is on — change blocked. Use Max or the MCP to make this change.'

    constructor(message = 'You are in read-only mode') {
        super(message)
        this.name = 'ReadOnlyModeError'
    }
}

type Notifier = (method: 'PATCH' | 'PUT' | 'POST' | 'DELETE') => void
type Getter = () => boolean

let getter: Getter | null = null
let notifier: Notifier | null = null

export function setReadOnlyGetter(fn: Getter | null): void {
    if (fn && getter) {
        // eslint-disable-next-line no-console
        console.warn(
            '[readOnlyGuard] setReadOnlyGetter called while a getter is already registered — overwriting. This usually means selfReadOnlyModeLogic was mounted twice.'
        )
    }
    getter = fn
}

export function setReadOnlyNotifier(fn: Notifier | null): void {
    notifier = fn
}

export function isReadOnly(): boolean {
    return getter?.() ?? false
}

type Method = 'PATCH' | 'PUT' | 'POST' | 'DELETE'

// An entry is either a bare regex (matches any write method on a path that is
// only used for reads/passive telemetry — DELETE is safe because no destructive
// DELETE exists on the same path), or `{ pattern, methods }` for paths where
// some methods are passive telemetry but others would mutate (e.g. PATCH is
// view-tracking but DELETE destroys the resource — only PATCH may pass).
type AllowedPattern = RegExp | { pattern: RegExp; methods: ReadonlyArray<Method> }

// Writes that should pass through in read-only mode. Three categories:
//   1. Reads disguised as writes — /query serves HogQL / trends / funnels /
//      retention via POST because the payload is too large for a query string.
//      Block-listing it would make the entire app unusable.
//   2. Passive telemetry that fires automatically on view/mount and should not
//      raise: /file_system/log_view, /insights/viewed, /insights/timing
//      (time-to-see-data), /metalytics (side-panel scene view tracking), and
//      PATCH /session_recordings/:id (markViewed view-tracking — restricted to
//      PATCH because DELETE on the same path is the destructive recording
//      delete endpoint and must stay blocked).
//   3. PostHog AI (Max) conversations — the read-only toast tells users to
//      "Use Max or the MCP to make this change", so Max must remain usable.
//      Matches /conversations except the two ticket sub-features (`views` and
//      `tickets`). Mount-path-agnostic — works under /environments/ or
//      /projects/ — so the discriminator is the sub-feature, not the prefix.
const READ_ONLY_ALLOWED_PATTERNS: ReadonlyArray<AllowedPattern> = [
    /\/query(?:\/|$|\?)/, // /api/environments/:team_id/query, /api/environments/:team_id/query/:queryId/log, etc.
    /\/file_system\/log_view(?:\/|$|\?)/, // /api/environments/:team_id/file_system/log_view
    /\/insights\/viewed(?:\/|$|\?)/, // /api/environments/:team_id/insights/viewed — passive "recently viewed" telemetry
    /\/insights\/timing(?:\/|$|\?)/, // /api/projects/:team_id/insights/timing — time-to-see-data telemetry fired after every dashboard/insight load
    /\/metalytics(?:\/|$|\?)/, // /api/projects/:team_id/metalytics — side-panel scene view tracking (only accepts metric_name=viewed)
    /\/conversations(?!\/(?:views|tickets))(?:\/|$|\?)/, // /api/.../conversations[/:id[/queue|/append_message|/cancel|...]] — PostHog AI (Max), excluding /conversations/views and /conversations/tickets
    { pattern: /\/session_recordings\/[^/]+(?:\/|$|\?)/, methods: ['PATCH'] }, // PATCH /api/.../session_recordings/:id — markViewed view-tracking ({viewed: true, ...} then {analyzed: true, ...}). DELETE on the same path stays blocked.
]

function isReadDisguisedAsWrite(method: Method, url: string): boolean {
    return READ_ONLY_ALLOWED_PATTERNS.some((entry) => {
        if (entry instanceof RegExp) {
            return entry.test(url)
        }
        return entry.methods.includes(method) && entry.pattern.test(url)
    })
}

export function assertNotReadOnly(method: Method, url: string): void {
    if (!isReadOnly()) {
        return
    }
    if (isReadDisguisedAsWrite(method, url)) {
        return
    }
    notifier?.(method)
    throw new ReadOnlyModeError()
}

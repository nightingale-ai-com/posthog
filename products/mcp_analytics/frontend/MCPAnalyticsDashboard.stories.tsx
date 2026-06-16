import { Meta, StoryObj } from '@storybook/react'

import { FEATURE_FLAGS } from 'lib/constants'
import { App } from 'scenes/App'
import { urls } from 'scenes/urls'

import { mswDecorator } from '~/mocks/browser'

const KPI_RESULTS = [
    ['2026-05-25', 320, 4100, 120, 1900, false],
    ['2026-05-26', 310, 3950, 140, 2050, false],
    ['2026-05-27', 298, 3800, 110, 1880, false],
    ['2026-06-01', 340, 4300, 150, 2100, true],
    ['2026-06-02', 355, 4500, 165, 2200, true],
    ['2026-06-03', 372, 4720, 158, 2080, true],
    ['2026-06-04', 360, 4600, 170, 2290, true],
    ['2026-06-05', 388, 4900, 182, 2150, true],
    ['2026-06-06', 401, 5100, 176, 2240, true],
    ['2026-06-07', 415, 5300, 168, 2290, true],
]

const TOOL_RESULTS = [
    ['exec', 5200, 208, 4.0, 2290],
    ['execute-sql', 1480, 144, 9.7, 3525],
    ['read-data-schema', 760, 3, 0.4, 1298],
    ['query-trends', 540, 5, 1.0, 2122],
    ['insight-create', 410, 8, 2.0, 727],
    ['dashboard-create', 260, 2, 0.8, 940],
    ['feature-flag-list', 180, 1, 0.6, 510],
    ['cohort-create', 95, 6, 6.3, 1620],
]

const SESSION_RESULTS = [
    ['0193f2a1-aaaa-bbbb-cccc-000000000001', 42, 18, 42.9, 610, 7, '2026-06-07T10:00:00Z'],
    ['0193f2a1-aaaa-bbbb-cccc-000000000002', 6, 6, 100.0, 95, 2, '2026-06-07T09:30:00Z'],
    ['0193f2a1-aaaa-bbbb-cccc-000000000003', 31, 0, 0.0, 240, 11, '2026-06-07T08:15:00Z'],
    ['0193f2a1-aaaa-bbbb-cccc-000000000004', 14, 1, 7.1, 180, 5, '2026-06-07T07:45:00Z'],
    ['0193f2a1-aaaa-bbbb-cccc-000000000005', 9, 0, 0.0, 120, 4, '2026-06-07T07:00:00Z'],
    ['0193f2a1-aaaa-bbbb-cccc-000000000006', 22, 3, 13.6, 410, 6, '2026-06-06T16:20:00Z'],
]

const HARNESS_RESULTS = [
    ['claude-code/1.2.0', 6200, 240, 820],
    ['cursor-vscode/0.42', 2100, 96, 410],
    ['codex-cli', 980, 71, 180],
    ['claude-ai', 760, 22, 240],
    ['visual studio code', 540, 12, 120],
]

// Daily success/error split powering the "Daily tool calls and errors" chart. [day, successes, errors]
const ACTIVITY_RESULTS = [
    ['2026-06-01', 4180, 120],
    ['2026-06-02', 4360, 140],
    ['2026-06-03', 4560, 160],
    ['2026-06-04', 4430, 170],
    ['2026-06-05', 4720, 180],
    ['2026-06-06', 4920, 176],
    ['2026-06-07', 5130, 168],
]

// Daily calls per tool powering the "Daily tool breakdown" stacked bars. [day, tool, calls]
const TOOL_DAILY_RESULTS = [
    ['exec', [720, 760, 800, 740, 820, 880, 910]],
    ['execute-sql', [210, 230, 250, 240, 260, 280, 300]],
    ['read-data-schema', [110, 120, 95, 130, 140, 150, 120]],
    ['query-trends', [60, 80, 70, 90, 100, 80, 110]],
].flatMap(([tool, series]) => (series as number[]).map((calls, i) => [`2026-06-0${i + 1}`, tool as string, calls]))

// Tool quality tab fixtures. Columns mirror the SELECT order in
// mcpAnalyticsToolQualityLogic / backend/templates/tool_quality.sql.
const TOOL_QUALITY_CATEGORIES = [['read'], ['write'], ['admin']]

const TOOL_QUALITY_CATEGORY_COUNTS = [
    ['read', 8200],
    ['write', 3100],
    ['admin', 540],
]

// [tool, total_calls, errors, error_rate_pct, p50, p95, p99, users, sessions, first_seen, last_seen]
const TOOL_QUALITY_ROWS = [
    ['exec', 5200, 208, 4.0, 880, 2290, 4100, 320, 410, '2026-05-08T09:12:00Z', '2026-06-07T10:09:00Z'],
    ['execute-sql', 1480, 144, 9.7, 1450, 3525, 6200, 210, 260, '2026-05-08T11:30:00Z', '2026-06-07T09:58:00Z'],
    ['read-data-schema', 760, 3, 0.4, 540, 1298, 2105, 180, 240, '2026-05-09T08:00:00Z', '2026-06-07T08:40:00Z'],
    ['query-trends', 540, 5, 1.0, 980, 2122, 3300, 120, 160, '2026-05-10T14:00:00Z', '2026-06-06T18:20:00Z'],
    ['insight-create', 410, 8, 2.0, 410, 727, 1190, 96, 140, '2026-05-11T10:00:00Z', '2026-06-07T07:10:00Z'],
    ['dashboard-create', 260, 2, 0.8, 520, 940, 1480, 70, 100, '2026-05-12T13:00:00Z', '2026-06-06T16:00:00Z'],
    ['feature-flag-list', 180, 1, 0.6, 240, 510, 880, 54, 70, '2026-05-14T09:00:00Z', '2026-06-05T12:00:00Z'],
    ['cohort-create', 95, 6, 6.3, 760, 1620, 2400, 32, 44, '2026-05-15T15:00:00Z', '2026-06-04T11:00:00Z'],
]

// [day, calls, errors, p50, p95, p99]
const TOOL_QUALITY_DAILY = [
    ['2026-06-01', 4300, 150, 820, 2100, 3900],
    ['2026-06-02', 4500, 165, 840, 2200, 4050],
    ['2026-06-03', 4720, 158, 800, 2080, 3850],
    ['2026-06-04', 4600, 170, 860, 2290, 4200],
    ['2026-06-05', 4900, 182, 880, 2150, 3980],
    ['2026-06-06', 5100, 176, 850, 2240, 4100],
    ['2026-06-07', 5300, 168, 830, 2290, 4150],
]

const SESSION_LIST = {
    results: [
        {
            session_id: '0193f2a1-aaaa-bbbb-cccc-000000000001',
            tool_calls: 42,
            session_start: '2026-06-07T10:00:00Z',
            session_end: '2026-06-07T10:10:10Z',
            distinct_id_count: 1,
            tools_used: ['execute-sql', 'read-data-schema'],
            mcp_client_name: 'claude-code/1.2.0',
            distinct_id: 'user-1-distinct-id',
            person_email: 'annika@example.com',
            person_name: 'Annika Hansen',
            intent: 'Investigate slow dashboard queries and create a tuned insight.',
        },
        {
            session_id: '0193f2a1-aaaa-bbbb-cccc-000000000002',
            tool_calls: 6,
            session_start: '2026-06-07T09:30:00Z',
            session_end: '2026-06-07T09:31:35Z',
            distinct_id_count: 1,
            tools_used: ['query-trends'],
            mcp_client_name: 'cursor-vscode/0.42',
            distinct_id: 'user-2-distinct-id',
            person_email: '',
            person_name: '',
            intent: '',
        },
        {
            session_id: '0193f2a1-aaaa-bbbb-cccc-000000000003',
            tool_calls: 31,
            session_start: '2026-06-07T08:15:00Z',
            session_end: '2026-06-07T08:19:00Z',
            distinct_id_count: 1,
            tools_used: ['exec', 'insight-create'],
            mcp_client_name: 'codex-cli',
            distinct_id: 'user-3-distinct-id',
            person_email: 'sven@example.com',
            person_name: '',
            intent: '',
        },
    ],
    has_next: true,
}

const TOOL_CALL_LIST = {
    results: [
        {
            event_id: 'evt-1',
            timestamp: '2026-06-07T10:00:05Z',
            tool_name: 'read-data-schema',
            intent: 'Look up the events schema before writing SQL.',
            is_error: false,
            error_message: '',
            duration_ms: 420,
        },
        {
            event_id: 'evt-2',
            timestamp: '2026-06-07T10:01:10Z',
            tool_name: 'execute-sql',
            intent: 'Run the slow dashboard query with EXPLAIN.',
            is_error: false,
            error_message: '',
            duration_ms: 8125,
        },
        {
            event_id: 'evt-3',
            timestamp: '2026-06-07T10:04:42Z',
            tool_name: 'execute-sql',
            intent: 'Retry the tuned query.',
            is_error: true,
            error_message: 'Estimated query execution time is too long (max_execution_time=600)',
            duration_ms: 610000,
        },
    ],
}

const CLUSTER_SNAPSHOT = {
    status: 'ready',
    error_message: '',
    last_computed_at: '2026-06-07T06:00:00Z',
    last_computed_by_email: 'paul@posthog.com',
    computed_with: null,
    clusters: Array.from({ length: 6 }, (_, i) => ({
        id: i + 1,
        label: `Cluster ${i + 1}`,
        summary: '',
        session_count: 10 * (i + 1),
        tool_call_count: 40 * (i + 1),
        error_rate_pct: i,
        sample_session_ids: [],
        top_tools: [],
    })),
}

const meta: Meta = {
    component: App,
    title: 'Scenes-App/MCP Analytics',
    decorators: [
        mswDecorator({
            get: {
                '/api/projects/:project_id/mcp_analytics/intent_clusters/': CLUSTER_SNAPSHOT,
                '/api/projects/:project_id/mcp_analytics/sessions/': SESSION_LIST,
                '/api/projects/:project_id/mcp_analytics/sessions/:session_id/tool_calls/': TOOL_CALL_LIST,
            },
            post: {
                '/api/environments/:team_id/query/:kind': (req, res, ctx) => {
                    const body = req.body as Record<string, any>
                    const query: string = body?.query?.query ?? ''
                    // Tool quality tab — checked before the dashboard handlers because the tool
                    // rows query also contains `p95_duration_ms`, so its `p99_duration_ms` guard
                    // must win first.
                    if (query.includes('DISTINCT') && query.includes('AS category')) {
                        return res(ctx.json({ results: TOOL_QUALITY_CATEGORIES }))
                    }
                    if (query.includes('$mcp_tool_category') && query.includes('count() AS calls')) {
                        return res(ctx.json({ results: TOOL_QUALITY_CATEGORY_COUNTS }))
                    }
                    if (query.includes('p99_duration_ms')) {
                        return res(ctx.json({ results: TOOL_QUALITY_ROWS }))
                    }
                    if (query.includes('AS day') && query.includes('AS p50')) {
                        return res(ctx.json({ results: TOOL_QUALITY_DAILY }))
                    }
                    if (query.includes('$mcp_client_name')) {
                        return res(ctx.json({ results: HARNESS_RESULTS }))
                    }
                    if (query.includes('AS successes')) {
                        return res(ctx.json({ results: ACTIVITY_RESULTS }))
                    }
                    if (query.includes('AS day') && query.includes('AS tool')) {
                        return res(ctx.json({ results: TOOL_DAILY_RESULTS }))
                    }
                    if (query.includes('AS session_id')) {
                        return res(ctx.json({ results: SESSION_RESULTS }))
                    }
                    if (query.includes('p95_duration_ms')) {
                        return res(ctx.json({ results: TOOL_RESULTS }))
                    }
                    if (query.includes('AS bucket')) {
                        return res(ctx.json({ results: KPI_RESULTS }))
                    }
                    return res(ctx.json({ results: [] }))
                },
            },
        }),
    ],
    parameters: {
        layout: 'fullscreen',
        viewMode: 'story',
        mockDate: '2026-06-07',
        pageUrl: urls.mcpAnalyticsDashboard(),
        featureFlags: [FEATURE_FLAGS.MCP_ANALYTICS],
    },
}
export default meta

type Story = StoryObj<{}>

export const Dashboard: Story = {}

export const Sessions: Story = {
    parameters: {
        pageUrl: urls.mcpAnalyticsSessions(),
    },
}

export const ToolQuality: Story = {
    parameters: {
        pageUrl: urls.mcpAnalyticsToolQuality(),
    },
}

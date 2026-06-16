import { Meta, StoryObj } from '@storybook/react'

import { App } from 'scenes/App'
import { urls } from 'scenes/urls'

import { mswDecorator } from '~/mocks/browser'
import { AccessControlLevel, AccessControlResourceType } from '~/types'

// The SQL editor scene gates on warehouse-objects access; grant it in the storybook app context.
function grantWarehouseAccess(Story: () => JSX.Element): JSX.Element {
    const ctx = (window as any).POSTHOG_APP_CONTEXT
    if (ctx) {
        ctx.resource_access_control = {
            ...ctx.resource_access_control,
            [AccessControlResourceType.WarehouseObjects]: AccessControlLevel.Editor,
        }
    }
    return <Story />
}

// Top tools per server — mirrors the first recipe in the MCP analytics docs (queries.mdx).
const SAMPLE_SQL = `SELECT
    properties.$mcp_server_name AS server,
    properties.$mcp_tool_name AS tool,
    count() AS calls,
    round(avg(toFloat(properties.$mcp_duration_ms))) AS avg_duration_ms,
    countIf(toBool(properties.$mcp_is_error)) AS errors
FROM events
WHERE event = 'mcp_tool_call' AND timestamp > now() - INTERVAL 7 DAY
GROUP BY server, tool
ORDER BY calls DESC
LIMIT 20`

const SQL_RESULTS = {
    columns: ['server', 'tool', 'calls', 'avg_duration_ms', 'errors'],
    types: ['String', 'String', 'UInt64', 'Float64', 'UInt64'],
    hasMore: false,
    results: [
        ['posthog', 'execute-sql', 1480, 3525, 144],
        ['posthog', 'read-data-schema', 760, 1298, 3],
        ['posthog', 'query-trends', 540, 2122, 5],
        ['posthog', 'insight-create', 410, 727, 8],
        ['posthog', 'dashboard-create', 260, 940, 2],
        ['posthog', 'feature-flag-list', 180, 510, 1],
        ['posthog', 'cohort-create', 95, 1620, 6],
        ['filesystem', 'exec', 5200, 2290, 208],
        ['filesystem', 'read-file', 980, 180, 3],
        ['filesystem', 'write-file', 420, 240, 11],
    ],
}

const meta: Meta = {
    component: App,
    title: 'Scenes-App/Data Warehouse/SQL Editor',
    decorators: [
        grantWarehouseAccess,
        mswDecorator({
            post: {
                '/api/environments/:team_id/query/:kind': (req, res, ctx) => {
                    const kind = (req.body as any)?.query?.kind
                    if (kind === 'DatabaseSchemaQuery') {
                        return res(ctx.json({ tables: {} }))
                    }
                    if (kind === 'HogQLMetadata') {
                        return res(ctx.json({ errors: [], warnings: [], notices: [], isValid: true }))
                    }
                    return res(ctx.json(SQL_RESULTS))
                },
            },
        }),
    ],
    parameters: {
        layout: 'fullscreen',
        viewMode: 'story',
        mockDate: '2026-06-07',
        // open_query pre-fills the editor but does not auto-run — the screenshot capture clicks
        // "Run" before snapshotting so the results grid is populated.
        pageUrl: urls.sqlEditor({ query: SAMPLE_SQL }),
        testOptions: {
            waitForSelector: '.monaco-editor',
            viewport: { width: 1600, height: 900 },
        },
    },
}
export default meta

type Story = StoryObj<{}>
export const TopToolsPerServer: Story = {}

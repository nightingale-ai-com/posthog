import { useValues } from 'kea'
import { router } from 'kea-router'
/**
 * Quest-shaped onboarding view rendered as an RPG-style skill tree.
 *
 * A central company badge anchors the tree; the team's current product orbits
 * just above it; 2-3 ranked next-product branches fan out with setup
 * intermediates on the path. Other in-app products are wired into the graph
 * with cross-links, capped at 3 connections per node. Locked products
 * (plan-gated) sit on the periphery.
 *
 * Used in place of `ProductSelection` when `ONBOARDING_PRODUCT_TREE` is
 * enabled. Data shape comes from `productTreeLogic`; this file owns the
 * visual.
 */
import { useMemo, useState } from 'react'

import { IconCheck, IconLock } from '@posthog/icons'
import { LemonButton } from '@posthog/lemon-ui'

import { organizationLogic } from 'scenes/organizationLogic'
import { urls } from 'scenes/urls'
import { brandingForProduct } from 'scenes/welcome/productBranding'

import { ProductKey } from '~/queries/schema/schema-general'

import { OnboardingExitModal } from '../exit'
import type { ProductGraph } from './graph'
import { handleToKey } from './handleAdapter'
import { productTreeLogic } from './productTreeLogic'
import type { TreeBranch, TreeShape } from './productTreeLogic'
import type { ProductHandle } from './relationships'

const CANVAS = { w: 1100, h: 980 }

const SLOTS = {
    company: { x: 540, y: 540 },
    root: { x: 540, y: 360 },
    rank1: { x: 400, y: 140 },
    rank2: { x: 870, y: 440 },
    rank3: { x: 160, y: 320 },
    availA: { x: 670, y: 130 },
    availB: { x: 690, y: 720 },
    availC: { x: 1020, y: 600 },
    lockedA: { x: 410, y: 820 },
    lockedB: { x: 180, y: 870 },
    lockedC: { x: 650, y: 870 },
} as const

type NodeRole = 'root' | 'rank1' | 'rank2' | 'rank3' | 'avail' | 'locked'

const NODE_RADIUS: Record<NodeRole, number> = {
    root: 54,
    rank1: 52,
    rank2: 46,
    rank3: 40,
    avail: 34,
    locked: 34,
}

const RANK_ROLES: readonly NodeRole[] = ['rank1', 'rank2', 'rank3']

// v1 heuristic for which products are plan-gated. Refine against billing data
// once we wire upgrade flow.
const PREMIUM_HANDLES = new Set<ProductHandle>(['data_warehouse', 'llm_analytics'])

interface PlacedNode {
    handle: ProductHandle
    productKey: ProductKey
    role: NodeRole
    pos: { x: number; y: number }
    rgb: string
    label: string
    branch?: TreeBranch
}

type EdgeKind = 'company-root' | 'rec1' | 'rec2' | 'rec3-bridge' | 'cross-link'

interface RenderedEdge {
    from: ProductHandle | 'company'
    to: ProductHandle
    kind: EdgeKind
}

function rgba(rgb: string, alpha: number): string {
    return `rgb(${rgb} / ${alpha})`
}

function monogram(productKey: ProductKey): string {
    const parts = String(productKey).split('_')
    if (parts.length >= 2) {
        return (parts[0][0] + parts[1][0]).toUpperCase()
    }
    return productKey.slice(0, 2).toUpperCase()
}

function placeNodes(treeShape: TreeShape, graph: ProductGraph): PlacedNode[] {
    const placed: PlacedNode[] = []
    const used = new Set<ProductHandle>()

    const add = (handle: ProductHandle, role: NodeRole, slot: { x: number; y: number }, branch?: TreeBranch): void => {
        const productKey = handleToKey(handle)
        if (!productKey) {
            return
        }
        const branding = brandingForProduct(productKey)
        placed.push({
            handle,
            productKey,
            role,
            pos: slot,
            rgb: branding.rgb,
            label: branding.label || productKey.replace(/_/g, ' '),
            branch,
        })
        used.add(handle)
    }

    if (treeShape.rootHandle) {
        add(treeShape.rootHandle, 'root', SLOTS.root)
    }

    const rankSlots = [SLOTS.rank1, SLOTS.rank2, SLOTS.rank3] as const
    treeShape.branches.slice(0, 3).forEach((branch, i) => {
        add(branch.handle, RANK_ROLES[i], rankSlots[i], branch)
    })

    const remaining = graph.nodes.map((n) => n.handle).filter((h) => !used.has(h))
    const available: ProductHandle[] = []
    const lockedList: ProductHandle[] = []
    for (const h of remaining) {
        if (PREMIUM_HANDLES.has(h)) {
            lockedList.push(h)
        } else {
            available.push(h)
        }
    }

    const availSlots = [SLOTS.availA, SLOTS.availB, SLOTS.availC]
    available.slice(0, 3).forEach((h, i) => add(h, 'avail', availSlots[i]))

    const lockedSlots = [SLOTS.lockedA, SLOTS.lockedB, SLOTS.lockedC]
    lockedList.slice(0, 3).forEach((h, i) => add(h, 'locked', lockedSlots[i]))

    return placed
}

function computeEdges(treeShape: TreeShape, graph: ProductGraph, placedSet: Set<ProductHandle>): RenderedEdge[] {
    const edges: RenderedEdge[] = []
    const degree = new Map<ProductHandle, number>()
    const seen = new Set<string>()

    const tryAdd = (from: ProductHandle, to: ProductHandle, kind: EdgeKind): boolean => {
        if ((degree.get(from) ?? 0) >= 3 || (degree.get(to) ?? 0) >= 3) {
            return false
        }
        const key = [from, to].sort().join('::')
        if (seen.has(key)) {
            return false
        }
        seen.add(key)
        edges.push({ from, to, kind })
        degree.set(from, (degree.get(from) ?? 0) + 1)
        degree.set(to, (degree.get(to) ?? 0) + 1)
        return true
    }

    if (treeShape.rootHandle) {
        edges.push({
            from: 'company',
            to: treeShape.rootHandle,
            kind: 'company-root',
        })
        // Company edge counts toward the root's degree budget.
        degree.set(treeShape.rootHandle, 1)
    }

    if (treeShape.rootHandle && treeShape.branches[0]) {
        tryAdd(treeShape.rootHandle, treeShape.branches[0].handle, 'rec1')
    }
    if (treeShape.rootHandle && treeShape.branches[1]) {
        tryAdd(treeShape.rootHandle, treeShape.branches[1].handle, 'rec2')
    }

    // Rank #3 reached through the graph rather than direct from root.
    if (treeShape.branches[2]) {
        const rank3 = treeShape.branches[2].handle
        const adj = graph.adjacency.get(rank3) ?? []
        for (const e of adj) {
            const other = e.from === rank3 ? e.to : e.from
            if (placedSet.has(other) && other !== rank3 && tryAdd(rank3, other, 'rec3-bridge')) {
                break
            }
        }
    }

    // Fill in remaining cross-links, heaviest edges first.
    const sortedEdges = [...graph.edges].sort((a, b) => b.weight - a.weight)
    for (const e of sortedEdges) {
        if (!placedSet.has(e.from) || !placedSet.has(e.to)) {
            continue
        }
        tryAdd(e.from, e.to, 'cross-link')
    }

    // Final pass: any placed node still isolated gets a forced edge so it isn't
    // floating off the graph (per the design — no orphans).
    for (const handle of placedSet) {
        if ((degree.get(handle) ?? 0) > 0) {
            continue
        }
        const adj = graph.adjacency.get(handle) ?? []
        for (const e of adj) {
            const other = e.from === handle ? e.to : e.from
            if (placedSet.has(other) && tryAdd(handle, other, 'cross-link')) {
                break
            }
        }
    }

    return edges
}

interface EdgeStyle {
    stroke: string
    strokeWidth: number
    opacity: number
    glow: boolean
    dashed: boolean
}

function edgeStyle(edge: RenderedEdge, byHandle: Map<ProductHandle, PlacedNode>): EdgeStyle {
    if (edge.kind === 'company-root') {
        return {
            stroke: '#dbe2ff',
            strokeWidth: 2.5,
            opacity: 0.9,
            glow: true,
            dashed: false,
        }
    }
    if (edge.kind === 'rec1') {
        const node = byHandle.get(edge.to)
        return {
            stroke: node ? rgba(node.rgb, 1) : '#f9bd2b',
            strokeWidth: 3.5,
            opacity: 1,
            glow: true,
            dashed: false,
        }
    }
    if (edge.kind === 'rec2') {
        const node = byHandle.get(edge.to)
        return {
            stroke: node ? rgba(node.rgb, 1) : '#29dbbb',
            strokeWidth: 2.8,
            opacity: 0.9,
            glow: true,
            dashed: false,
        }
    }
    if (edge.kind === 'rec3-bridge') {
        // Highlight the path-to-rank-3 in the rank3 product's colour but at
        // reduced intensity — brighter than a cross-link, dimmer than a rec arm.
        const rank3Node = [...byHandle.values()].find((n) => n.role === 'rank3')
        return {
            stroke: rank3Node ? rgba(rank3Node.rgb, 0.7) : '#36c5f0',
            strokeWidth: 2,
            opacity: 0.85,
            glow: true,
            dashed: false,
        }
    }
    // Cross-links: dim gray. Locked-to-locked is dimmer still.
    const fromNode = edge.from === 'company' ? null : byHandle.get(edge.from)
    const toNode = byHandle.get(edge.to)
    const isLockedPair = fromNode?.role === 'locked' && toNode?.role === 'locked'
    return {
        stroke: isLockedPair ? '#3a4055' : '#5a6275',
        strokeWidth: isLockedPair ? 1.2 : 1.5,
        opacity: isLockedPair ? 0.55 : 0.6,
        glow: false,
        dashed: false,
    }
}

function edgeEndpoints(
    edge: RenderedEdge,
    byHandle: Map<ProductHandle, PlacedNode>
): { x1: number; y1: number; x2: number; y2: number } | null {
    const fromPos = edge.from === 'company' ? SLOTS.company : byHandle.get(edge.from)?.pos
    const toPos = byHandle.get(edge.to)?.pos
    if (!fromPos || !toPos) {
        return null
    }
    const fromR = edge.from === 'company' ? 42 : NODE_RADIUS[byHandle.get(edge.from)!.role]
    const toR = NODE_RADIUS[byHandle.get(edge.to)!.role]
    const dx = toPos.x - fromPos.x
    const dy = toPos.y - fromPos.y
    const len = Math.hypot(dx, dy) || 1
    return {
        x1: fromPos.x + (dx * fromR) / len,
        y1: fromPos.y + (dy * fromR) / len,
        x2: toPos.x - (dx * toR) / len,
        y2: toPos.y - (dy * toR) / len,
    }
}

function midpoints(p: { x1: number; y1: number; x2: number; y2: number }, n: number): { x: number; y: number }[] {
    const pts: { x: number; y: number }[] = []
    for (let i = 1; i <= n; i++) {
        const t = i / (n + 1)
        pts.push({ x: p.x1 + (p.x2 - p.x1) * t, y: p.y1 + (p.y2 - p.y1) * t })
    }
    return pts
}

interface NodeProps {
    node: PlacedNode
    selected: boolean
    onSelect: () => void
}

function ProductNodeSvg({ node, selected, onSelect }: NodeProps): JSX.Element {
    const { role, pos, rgb, label, productKey } = node
    const r = NODE_RADIUS[role]
    const isLocked = role === 'locked'
    const isRoot = role === 'root'
    const isRanked = role === 'rank1' || role === 'rank2' || role === 'rank3'
    const isAvail = role === 'avail'
    const rankNumber = role === 'rank1' ? 1 : role === 'rank2' ? 2 : role === 'rank3' ? 3 : 0

    return (
        <g
            transform={`translate(${pos.x}, ${pos.y})`}
            onClick={onSelect}
            style={{ cursor: 'pointer' }}
            data-attr={`product-tree-node-${productKey}`}
        >
            {selected && <circle r={r + 8} fill="none" stroke={rgba(rgb, 1)} strokeWidth={2.5} />}
            {(isRoot || isRanked) && <circle r={r + 22} fill={rgba(rgb, 0.14)} filter="url(#glowBright)" />}
            {isAvail && <circle r={r + 12} fill={rgba(rgb, 0.18)} />}

            {isLocked ? (
                <circle r={r} fill="#1a1e2e" stroke="#3a3f4f" strokeWidth={1} />
            ) : isAvail ? (
                <>
                    <circle r={r} fill={rgba(rgb, 0.22)} stroke={rgba(rgb, 0.5)} strokeWidth={1} />
                    <text textAnchor="middle" y={5} fontSize={14} fontWeight={700} fill="#fff" opacity={0.75}>
                        {monogram(productKey)}
                    </text>
                </>
            ) : (
                <>
                    <circle r={r} fill={rgba(rgb, 1)} stroke={rgba(rgb, 0.95)} strokeWidth={2} />
                    <text textAnchor="middle" y={6} fontSize={isRoot ? 20 : 18} fontWeight={700} fill="#0d1124">
                        {monogram(productKey)}
                    </text>
                </>
            )}

            {isLocked && (
                <g transform="translate(-10, -10)">
                    <IconLock width={20} height={20} style={{ color: '#6a7088' }} />
                </g>
            )}

            <text textAnchor="middle" y={r + 18} fontSize={12} fontWeight={600} fill={isLocked ? '#5a6075' : '#fff'}>
                {label}
            </text>

            {isLocked && (
                <text textAnchor="middle" y={r + 32} fontSize={9} fill="#4a505f">
                    Upgrade required
                </text>
            )}

            {isRoot && (
                <g transform={`translate(-26, ${r + 26})`}>
                    <rect width={52} height={16} rx={8} fill={rgba(rgb, 1)} />
                    <text textAnchor="middle" x={26} y={11} fontSize={9} fontWeight={700} fill="#fff" letterSpacing={1}>
                        CURRENT
                    </text>
                </g>
            )}

            {isRanked && (
                <g transform={`translate(-30, ${r + 26})`}>
                    <rect width={60} height={16} rx={8} fill={rgba(rgb, 1)} />
                    <text
                        textAnchor="middle"
                        x={30}
                        y={11}
                        fontSize={9}
                        fontWeight={700}
                        fill="#0d1124"
                        letterSpacing={1}
                    >
                        RANK #{rankNumber}
                    </text>
                </g>
            )}

            {/* Off-screen tooltip target so screen readers / E2E tests can find it */}
            <title>{label}</title>
        </g>
    )
}

function CompanyNode({ name }: { name: string }): JSX.Element {
    const initial = (name?.[0] ?? 'A').toUpperCase()
    return (
        <g transform={`translate(${SLOTS.company.x}, ${SLOTS.company.y})`}>
            <rect x={-42} y={-42} width={84} height={84} rx={12} fill="#fff" opacity={0.04} filter="url(#glowBright)" />
            <rect x={-32} y={-32} width={64} height={64} rx={10} fill="#0d1124" stroke="#dbe2ff" strokeWidth={1.5} />
            <text textAnchor="middle" y={9} fontSize={28} fontWeight={700} fill="#dbe2ff">
                {initial}
            </text>
            <text textAnchor="middle" y={56} fontSize={12} fontWeight={700} fill="#fff" letterSpacing={1}>
                {name.toUpperCase()}
            </text>
        </g>
    )
}

interface SetupIntermediateProps {
    pos: { x: number; y: number }
    color: string
    done?: boolean
    label?: string
    sublabel?: string
    labelAnchor?: 'start' | 'end'
}

function SetupIntermediate({
    pos,
    color,
    done,
    label,
    sublabel,
    labelAnchor = 'start',
}: SetupIntermediateProps): JSX.Element {
    const dx = labelAnchor === 'end' ? -18 : 18
    return (
        <g transform={`translate(${pos.x}, ${pos.y})`}>
            <circle r={13} fill={done ? '#22c55e' : '#0d1124'} stroke={done ? '#22c55e' : color} strokeWidth={2} />
            {done && (
                <g transform="translate(-7, -7)">
                    <IconCheck width={14} height={14} style={{ color: '#fff' }} />
                </g>
            )}
            {label && (
                <text x={dx} y={0} fontSize={11} fill="#c8d0e0" textAnchor={labelAnchor}>
                    {label}
                </text>
            )}
            {sublabel && (
                <text x={dx} y={13} fontSize={9} fill="#5a6075" textAnchor={labelAnchor}>
                    {sublabel}
                </text>
            )}
        </g>
    )
}

interface SidePanelProps {
    node: PlacedNode | null
    onLaunch: () => void
}

function SidePanel({ node, onLaunch }: SidePanelProps): JSX.Element {
    if (!node) {
        return (
            <div className="rounded-lg border border-border bg-bg-light p-6 text-sm text-muted">
                Hover or click a product in the tree to see details.
            </div>
        )
    }

    const { role, rgb, label, branch, productKey } = node
    const isLocked = role === 'locked'
    const isRanked = role === 'rank1' || role === 'rank2' || role === 'rank3'
    const rankNumber = role === 'rank1' ? 1 : role === 'rank2' ? 2 : role === 'rank3' ? 3 : 0

    return (
        <div
            className="flex h-full flex-col gap-4 rounded-lg border p-6 text-sm"
            style={{
                background: '#0d1124',
                borderColor: '#1f2540',
                color: '#c8d0e0',
            }}
        >
            <div className="flex items-start gap-3">
                <div
                    className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full font-bold"
                    style={{ background: rgba(rgb, 1), color: '#0d1124' }}
                >
                    {monogram(productKey)}
                </div>
                <div className="flex flex-col">
                    {isRanked && (
                        <span
                            className="text-xs font-semibold uppercase tracking-widest"
                            style={{ color: rgba(rgb, 1) }}
                        >
                            Recommended · Rank #{rankNumber}
                        </span>
                    )}
                    {role === 'root' && (
                        <span className="text-xs font-semibold uppercase tracking-widest text-white">
                            Currently using
                        </span>
                    )}
                    {role === 'avail' && (
                        <span className="text-xs font-semibold uppercase tracking-widest text-muted">
                            Also available
                        </span>
                    )}
                    {isLocked && (
                        <span className="text-xs font-semibold uppercase tracking-widest text-muted">
                            Upgrade required
                        </span>
                    )}
                    <h3 className="text-xl font-bold text-white">{label}</h3>
                </div>
            </div>

            <div className="border-t" style={{ borderColor: '#1f2540' }} />

            {branch?.rationale ? (
                <p className="leading-relaxed">{branch.rationale}</p>
            ) : (
                <p className="leading-relaxed text-muted">
                    {isLocked
                        ? 'This product is part of a paid plan. Upgrade to unlock it for your team.'
                        : `Explore ${label} alongside the products you already use.`}
                </p>
            )}

            {isRanked && (
                <div className="rounded-lg p-4" style={{ background: '#161c33' }}>
                    <div className="mb-2 text-xs font-semibold uppercase tracking-widest" style={{ color: '#7a85a8' }}>
                        Why this recommendation
                    </div>
                    <p className="text-sm">
                        {branch?.rationale ?? `Teams using your current product often adopt ${label} next.`}
                    </p>
                </div>
            )}

            {isRanked && !isLocked && (
                <div>
                    <div className="mb-3 text-xs font-semibold uppercase tracking-widest" style={{ color: '#9aa0b0' }}>
                        Setup steps
                    </div>
                    <ul className="flex flex-col gap-3">
                        <li className="flex items-center gap-3">
                            <span
                                className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full"
                                style={{ background: '#22c55e' }}
                            >
                                <IconCheck width={14} height={14} style={{ color: '#fff' }} />
                            </span>
                            <span className="line-through text-muted">Capture events</span>
                        </li>
                        <li className="flex items-start gap-3">
                            <span
                                className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full border-2"
                                style={{
                                    borderColor: rgba(rgb, 1),
                                    background: '#0d1124',
                                }}
                            />
                            <div className="flex flex-col">
                                <span className="font-semibold text-white">Install {label} SDK</span>
                                <span className="text-xs text-muted">Add a single snippet to your app</span>
                            </div>
                        </li>
                        <li className="flex items-center gap-3">
                            <span
                                className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full border-2"
                                style={{
                                    borderColor: '#3a4055',
                                    background: '#0d1124',
                                }}
                            />
                            <span>Verify first event</span>
                        </li>
                    </ul>
                </div>
            )}

            <div className="mt-auto">
                {isLocked ? (
                    <LemonButton
                        type="primary"
                        fullWidth
                        onClick={() => router.actions.push(urls.organizationBilling([productKey]))}
                        data-attr="product-tree-upgrade"
                    >
                        Upgrade to unlock
                    </LemonButton>
                ) : role === 'root' ? (
                    <LemonButton
                        type="secondary"
                        fullWidth
                        onClick={() => router.actions.push(urls.onboarding({ productKey: productKey }))}
                        data-attr="product-tree-revisit"
                    >
                        Revisit setup
                    </LemonButton>
                ) : (
                    <LemonButton
                        type="primary"
                        fullWidth
                        onClick={onLaunch}
                        data-attr={`product-tree-launch-${productKey}`}
                    >
                        Start {label} setup →
                    </LemonButton>
                )}
            </div>
        </div>
    )
}

export function ProductTree(): JSX.Element {
    const { treeShape, isFreshTeam, graph } = useValues(productTreeLogic)
    const { currentOrganization } = useValues(organizationLogic)
    const companyName = currentOrganization?.name || 'Your team'

    const { placed, byHandle, edges, intermediatePositions, defaultSelected } = useMemo(() => {
        const placedNodes = placeNodes(treeShape, graph)
        const byHandleMap = new Map<ProductHandle, PlacedNode>(placedNodes.map((n) => [n.handle, n]))
        const placedSet = new Set(placedNodes.map((n) => n.handle))
        const edgeList = computeEdges(treeShape, graph, placedSet)

        // Pre-compute setup intermediates for the rec arms (rank1 + rank2).
        const intermediates: {
            edge: RenderedEdge
            points: { x: number; y: number }[]
            color: string
        }[] = []
        for (const e of edgeList) {
            if (e.kind !== 'rec1' && e.kind !== 'rec2') {
                continue
            }
            const endpoints = edgeEndpoints(e, byHandleMap)
            if (!endpoints) {
                continue
            }
            const target = byHandleMap.get(e.to)
            intermediates.push({
                edge: e,
                points: midpoints(endpoints, 2),
                color: target ? rgba(target.rgb, 1) : '#fff',
            })
        }

        const firstRank = placedNodes.find((n) => n.role === 'rank1')
        return {
            placed: placedNodes,
            byHandle: byHandleMap,
            edges: edgeList,
            intermediatePositions: intermediates,
            defaultSelected: firstRank?.handle ?? null,
        }
    }, [treeShape, graph])

    const [selectedHandle, setSelectedHandle] = useState<ProductHandle | null>(defaultSelected)
    const effectiveSelected = selectedHandle ?? defaultSelected
    const selectedNode = effectiveSelected ? (byHandle.get(effectiveSelected) ?? null) : null

    if (isFreshTeam || !treeShape.rootProductKey) {
        return <div className="hidden" />
    }

    const onLaunch = (): void => {
        if (!selectedNode) {
            return
        }
        router.actions.push(urls.onboarding({ productKey: selectedNode.productKey }))
    }

    return (
        <div className="flex w-full flex-col gap-4 lg:flex-row">
            <div
                className="relative flex-1 overflow-hidden rounded-lg"
                style={{
                    background: 'radial-gradient(circle at 50% 54%, #141a2e 0%, #070912 100%)',
                    minHeight: 720,
                }}
            >
                <svg
                    viewBox={`0 0 ${CANVAS.w} ${CANVAS.h}`}
                    className="block h-auto w-full"
                    preserveAspectRatio="xMidYMid meet"
                >
                    <defs>
                        <filter id="glowBright" x="-50%" y="-50%" width="200%" height="200%">
                            <feGaussianBlur stdDeviation={6} />
                        </filter>
                        <filter id="glowSoft" x="-30%" y="-30%" width="160%" height="160%">
                            <feGaussianBlur stdDeviation={2} />
                        </filter>
                    </defs>

                    {/* Atmospheric rings */}
                    <g fill="none" stroke="#1a2238">
                        <circle cx={SLOTS.company.x} cy={SLOTS.company.y} r={260} opacity={0.4} />
                        <circle cx={SLOTS.company.x} cy={SLOTS.company.y} r={480} opacity={0.25} />
                    </g>

                    {/* Edges */}
                    {edges.map((edge, i) => {
                        const ep = edgeEndpoints(edge, byHandle)
                        if (!ep) {
                            return null
                        }
                        const style = edgeStyle(edge, byHandle)
                        return (
                            <line
                                key={`edge-${i}-${edge.from}-${edge.to}`}
                                x1={ep.x1}
                                y1={ep.y1}
                                x2={ep.x2}
                                y2={ep.y2}
                                stroke={style.stroke}
                                strokeWidth={style.strokeWidth}
                                opacity={style.opacity}
                                strokeLinecap="round"
                                filter={style.glow ? 'url(#glowSoft)' : undefined}
                                strokeDasharray={style.dashed ? '5 4' : undefined}
                            />
                        )
                    })}

                    {/* Setup intermediates (only on the rank #1 / rank #2 rec arms) */}
                    {intermediatePositions.map((entry, i) => {
                        const isRec1 = entry.edge.kind === 'rec1'
                        const targetLabel = byHandle.get(entry.edge.to)?.label ?? 'product'
                        return (
                            <g key={`int-${i}-${entry.edge.to}`}>
                                <SetupIntermediate
                                    pos={entry.points[0]}
                                    color={entry.color}
                                    done
                                    label="Capture events"
                                    sublabel="done"
                                    labelAnchor={entry.points[0].x < SLOTS.root.x ? 'end' : 'start'}
                                />
                                <SetupIntermediate
                                    pos={entry.points[1]}
                                    color={entry.color}
                                    label={isRec1 ? `Install ${targetLabel} SDK` : `Set up ${targetLabel}`}
                                    sublabel={isRec1 ? 'next up · ~2 min' : undefined}
                                    labelAnchor={entry.points[1].x < SLOTS.root.x ? 'end' : 'start'}
                                />
                            </g>
                        )
                    })}

                    {/* Nodes (locked first so they sit behind ranked nodes if any overlap) */}
                    {placed
                        .slice()
                        .sort((a, b) => {
                            const order: Record<NodeRole, number> = {
                                locked: 0,
                                avail: 1,
                                rank3: 2,
                                rank2: 3,
                                rank1: 4,
                                root: 5,
                            }
                            return order[a.role] - order[b.role]
                        })
                        .map((node) => (
                            <ProductNodeSvg
                                key={node.handle}
                                node={node}
                                selected={node.handle === effectiveSelected}
                                onSelect={() => setSelectedHandle(node.handle)}
                            />
                        ))}

                    {/* Company badge at the center */}
                    <CompanyNode name={companyName} />
                </svg>

                {/* Browse all + exit modal */}
                <div className="absolute bottom-4 right-4">
                    <LemonButton
                        type="tertiary"
                        size="small"
                        onClick={() => router.actions.push(urls.onboarding())}
                        data-attr="product-tree-browse-all"
                    >
                        Browse all products →
                    </LemonButton>
                </div>
            </div>

            <div className="w-full shrink-0 lg:w-96">
                <SidePanel node={selectedNode} onLaunch={onLaunch} />
            </div>

            <OnboardingExitModal />
        </div>
    )
}

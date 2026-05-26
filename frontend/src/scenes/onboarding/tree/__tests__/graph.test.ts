import { buildGraph, recommend } from '../graph'
import { IN_APP_HANDLES } from '../handleAdapter'

describe('graph', () => {
    describe('buildGraph', () => {
        it('drops worksWith edges from the in-app graph', () => {
            const graph = buildGraph(IN_APP_HANDLES)
            for (const edge of graph.edges) {
                expect(edge.type).not.toBe('worksWith')
            }
        })

        it('excludes edges that reference handles outside the universe', () => {
            const graph = buildGraph(IN_APP_HANDLES)
            const universe = new Set(IN_APP_HANDLES)
            for (const edge of graph.edges) {
                expect(universe.has(edge.from)).toBe(true)
                expect(universe.has(edge.to)).toBe(true)
            }
        })

        it('emits one node per handle in the universe', () => {
            const graph = buildGraph(IN_APP_HANDLES)
            expect(graph.nodes).toHaveLength(IN_APP_HANDLES.length)
        })
    })

    describe('recommend', () => {
        const graph = buildGraph(IN_APP_HANDLES)

        it('returns highest-degree nodes when the fleet is empty', () => {
            const suggestions = recommend(new Set(), graph, 3)
            expect(suggestions.length).toBeGreaterThan(0)
            expect(suggestions.length).toBeLessThanOrEqual(3)
            // First suggestion's score equals the degree of the most-connected node.
            const topDegree = Math.max(...graph.nodes.map((n) => n.degree))
            expect(suggestions[0].score).toBe(topDegree)
        })

        it('excludes products already in the fleet', () => {
            const fleet = new Set<string>(['product_analytics'])
            const suggestions = recommend(fleet, graph, 3)
            for (const s of suggestions) {
                expect(s.handle).not.toBe('product_analytics')
            }
        })

        it('caps results at k', () => {
            const suggestions = recommend(new Set(['product_analytics']), graph, 2)
            expect(suggestions.length).toBeLessThanOrEqual(2)
        })

        it('surfaces session_replay as a top suggestion for a product_analytics fleet', () => {
            const suggestions = recommend(new Set(['product_analytics']), graph, 5)
            const handles = suggestions.map((s) => s.handle)
            expect(handles).toContain('session_replay')
        })
    })
})

import type { ProductIntentType } from '~/types'

import { deriveAdoptionState } from '../adoptionState'

function intent(overrides: Partial<ProductIntentType> & { product_type: string }): ProductIntentType {
    return {
        created_at: '2024-01-01T00:00:00Z',
        ...overrides,
    }
}

describe('deriveAdoptionState', () => {
    it('returns an empty state for a team with no intents', () => {
        const state = deriveAdoptionState(undefined)
        expect(state.rootHandle).toBeNull()
        expect(state.fleet.size).toBe(0)
        expect(state.statusByHandle.size).toBe(0)
    })

    it('marks a product with activated_at as activated and adds it to the fleet', () => {
        const state = deriveAdoptionState([
            intent({ product_type: 'product_analytics', activated_at: '2024-02-01T00:00:00Z' }),
        ])
        expect(state.statusByHandle.get('product_analytics')).toBe('activated')
        expect(state.fleet.has('product_analytics')).toBe(true)
        expect(state.rootHandle).toBe('product_analytics')
    })

    it('marks a product without activated_at as intent and keeps it out of the fleet', () => {
        const state = deriveAdoptionState([intent({ product_type: 'session_replay' })])
        expect(state.statusByHandle.get('session_replay')).toBe('intent')
        expect(state.fleet.has('session_replay')).toBe(false)
        expect(state.rootHandle).toBe('session_replay')
    })

    it('roots at the most recently activated product', () => {
        const state = deriveAdoptionState([
            intent({ product_type: 'product_analytics', activated_at: '2024-02-01T00:00:00Z' }),
            intent({ product_type: 'session_replay', activated_at: '2024-03-01T00:00:00Z' }),
        ])
        expect(state.rootHandle).toBe('session_replay')
    })

    it('falls back to the most recent intent when nothing is activated', () => {
        const state = deriveAdoptionState([
            intent({ product_type: 'product_analytics', updated_at: '2024-02-01T00:00:00Z' }),
            intent({ product_type: 'session_replay', updated_at: '2024-03-01T00:00:00Z' }),
        ])
        expect(state.rootHandle).toBe('session_replay')
    })

    it('prefers an activated product as root over a more recent unactivated intent', () => {
        const state = deriveAdoptionState([
            intent({ product_type: 'product_analytics', activated_at: '2024-02-01T00:00:00Z' }),
            intent({ product_type: 'session_replay', updated_at: '2024-12-01T00:00:00Z' }),
        ])
        expect(state.rootHandle).toBe('product_analytics')
    })

    it('skips intents whose product_type is not in the in-app universe', () => {
        const state = deriveAdoptionState([
            intent({ product_type: 'cdp', activated_at: '2024-02-01T00:00:00Z' }),
            intent({ product_type: 'product_analytics', activated_at: '2024-01-01T00:00:00Z' }),
        ])
        expect(state.fleet.has('cdp')).toBe(false)
        expect(state.rootHandle).toBe('product_analytics')
    })
})

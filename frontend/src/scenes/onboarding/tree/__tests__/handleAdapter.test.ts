import { ProductKey } from '~/queries/schema/schema-general'

import { IN_APP_HANDLES, handleToKey, keyToHandle } from '../handleAdapter'

describe('handleAdapter', () => {
    describe('handleToKey', () => {
        it('round-trips snake_case handles that match a ProductKey verbatim', () => {
            expect(handleToKey('product_analytics')).toBe(ProductKey.PRODUCT_ANALYTICS)
            expect(handleToKey('session_replay')).toBe(ProductKey.SESSION_REPLAY)
            expect(handleToKey('feature_flags')).toBe(ProductKey.FEATURE_FLAGS)
        })

        it('resolves the workflows_emails handle to the WORKFLOWS key', () => {
            expect(handleToKey('workflows_emails')).toBe(ProductKey.WORKFLOWS)
        })

        it('returns null for handles dropped from the in-app universe', () => {
            expect(handleToKey('realtime_destinations')).toBeNull()
            expect(handleToKey('cdp')).toBeNull()
            expect(handleToKey('posthog_ai')).toBeNull()
            expect(handleToKey('revenue_analytics')).toBeNull()
            expect(handleToKey('endpoints')).toBeNull()
        })

        it('returns null for handles that are not in the galaxy at all', () => {
            expect(handleToKey('not_a_real_product')).toBeNull()
        })
    })

    describe('keyToHandle', () => {
        it('round-trips ProductKey values back to their handle', () => {
            expect(keyToHandle(ProductKey.PRODUCT_ANALYTICS)).toBe('product_analytics')
            expect(keyToHandle(ProductKey.WORKFLOWS)).toBe('workflows_emails')
        })
    })

    describe('IN_APP_HANDLES', () => {
        it('excludes every dropped handle', () => {
            for (const dropped of ['realtime_destinations', 'cdp', 'posthog_ai', 'revenue_analytics', 'endpoints']) {
                expect(IN_APP_HANDLES).not.toContain(dropped)
            }
        })

        it('includes the core analytics products', () => {
            expect(IN_APP_HANDLES).toContain('product_analytics')
            expect(IN_APP_HANDLES).toContain('session_replay')
            expect(IN_APP_HANDLES).toContain('feature_flags')
        })
    })
})

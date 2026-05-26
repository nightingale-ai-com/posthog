/**
 * Two-way bridge between the marketing-site `ProductHandle` (snake_case strings used in
 * `relationships.ts`) and the in-app `ProductKey` enum.
 *
 * Most galaxy handles already match a `ProductKey` value verbatim. A handful of
 * galaxy handles don't have an in-app equivalent (marketing-only or pipeline-level
 * concepts not represented in `availableOnboardingProducts`) and are dropped from
 * the in-app universe — `handleToKey` returns `null` for those.
 */
import { availableOnboardingProducts } from 'scenes/onboarding/utils'

import { ProductKey } from '~/queries/schema/schema-general'

import { MAIN_PRODUCT_HANDLES } from './relationships'
import type { ProductHandle } from './relationships'

/**
 * Handles that the marketing site exposes but the in-app onboarding does not
 * surface — usually because they have no onboarding metadata in `availableOnboardingProducts`.
 * The tree filters these out before calling `buildGraph`, so edges that touch
 * them never reach the renderer.
 */
const DROPPED_HANDLES: ReadonlySet<ProductHandle> = new Set([
    'realtime_destinations',
    'cdp',
    'posthog_ai',
    'revenue_analytics',
    'endpoints',
])

/**
 * Handle → ProductKey mapping for galaxy handles whose snake_case form is not the
 * literal `ProductKey` value. Direct matches (where handle === ProductKey value)
 * don't need an entry here.
 */
const HANDLE_KEY_OVERRIDES: Record<ProductHandle, ProductKey> = {
    workflows_emails: ProductKey.WORKFLOWS,
}

/**
 * Reverse of HANDLE_KEY_OVERRIDES, used by `keyToHandle`.
 */
const KEY_HANDLE_OVERRIDES: Partial<Record<ProductKey, ProductHandle>> = {
    [ProductKey.WORKFLOWS]: 'workflows_emails',
}

/**
 * The subset of galaxy handles that the in-app tree actually renders.
 * Excludes `DROPPED_HANDLES` and anything that doesn't resolve to a ProductKey
 * with onboarding metadata.
 */
export const IN_APP_HANDLES: ReadonlyArray<ProductHandle> = MAIN_PRODUCT_HANDLES.filter((handle) => {
    if (DROPPED_HANDLES.has(handle)) {
        return false
    }
    const key = handleToKey(handle)
    return key !== null && key in availableOnboardingProducts
})

/** Resolve a galaxy handle to the in-app ProductKey, or `null` if dropped. */
export function handleToKey(handle: ProductHandle): ProductKey | null {
    if (DROPPED_HANDLES.has(handle)) {
        return null
    }
    if (handle in HANDLE_KEY_OVERRIDES) {
        return HANDLE_KEY_OVERRIDES[handle]
    }
    // Most galaxy handles match a ProductKey value verbatim.
    const candidate = handle as ProductKey
    if (Object.values(ProductKey).includes(candidate)) {
        return candidate
    }
    return null
}

/** Resolve a ProductKey to the galaxy handle, or `null` if the key is not in the galaxy universe. */
export function keyToHandle(key: ProductKey): ProductHandle | null {
    if (key in KEY_HANDLE_OVERRIDES) {
        return KEY_HANDLE_OVERRIDES[key] ?? null
    }
    // Most ProductKeys round-trip to themselves as a handle.
    const candidate: ProductHandle = key
    if (MAIN_PRODUCT_HANDLES.includes(candidate) && !DROPPED_HANDLES.has(candidate)) {
        return candidate
    }
    return null
}

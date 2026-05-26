/**
 * Derives a team's product adoption fleet from `ProductIntent` rows.
 *
 * Two related concepts:
 * - `fleet`: the set of products the team is *using* (has activated). This is the
 *   input to `recommend()` — recommendations are computed against the fleet.
 * - `status` per product: `activated` > `intent` > `none`. Drives node visuals
 *   (checkmark, pulse, locked) in the tree renderer.
 *
 * The "current" product is the trunk the tree is rooted at. We pick the most
 * recently *activated* product, falling back to the most recent intent if the
 * team has no activations yet. If neither exists (brand-new team), the caller
 * is responsible for showing the variant picker instead of a rooted tree.
 */
import type { ProductIntentType } from '~/types'

import { keyToHandle } from './handleAdapter'
import type { ProductHandle } from './relationships'

export type AdoptionStatus = 'activated' | 'intent' | 'none'

export interface AdoptionState {
    /** Per-handle status, only includes handles in the in-app universe. */
    statusByHandle: ReadonlyMap<ProductHandle, AdoptionStatus>
    /** Set of activated handles — the fleet passed to `recommend()`. */
    fleet: ReadonlySet<ProductHandle>
    /** The handle to root the tree at, or `null` if the team has no intents at all. */
    rootHandle: ProductHandle | null
}

const EMPTY_STATE: AdoptionState = {
    statusByHandle: new Map(),
    fleet: new Set(),
    rootHandle: null,
}

/**
 * The `ProductIntent.product_type` column stores a string that may be either a
 * `ProductKey` value or the legacy snake_case slug. Both round-trip through
 * `keyToHandle` because most ProductKeys *are* the snake_case handle.
 */
function intentTypeToHandle(productType: string): ProductHandle | null {
    return keyToHandle(productType as Parameters<typeof keyToHandle>[0])
}

function parseTimestamp(value: string | undefined): number {
    if (!value) {
        return 0
    }
    const ms = Date.parse(value)
    return Number.isFinite(ms) ? ms : 0
}

export function deriveAdoptionState(intents: ReadonlyArray<ProductIntentType> | undefined): AdoptionState {
    if (!intents || intents.length === 0) {
        return EMPTY_STATE
    }

    const statusByHandle = new Map<ProductHandle, AdoptionStatus>()
    const fleet = new Set<ProductHandle>()
    let latestActivated: { handle: ProductHandle; at: number } | null = null
    let latestIntent: { handle: ProductHandle; at: number } | null = null

    for (const intent of intents) {
        const handle = intentTypeToHandle(intent.product_type)
        if (!handle) {
            continue
        }

        const isActivated = Boolean(intent.activated_at)
        const nextStatus: AdoptionStatus = isActivated ? 'activated' : 'intent'
        const prev = statusByHandle.get(handle)
        // `activated` wins over `intent` — but two rows for the same product is unexpected; defensive.
        if (prev !== 'activated') {
            statusByHandle.set(handle, nextStatus)
        }

        if (isActivated) {
            fleet.add(handle)
            const at = parseTimestamp(intent.activated_at)
            if (!latestActivated || at > latestActivated.at) {
                latestActivated = { handle, at }
            }
        } else {
            const at = parseTimestamp(intent.updated_at ?? intent.created_at)
            if (!latestIntent || at > latestIntent.at) {
                latestIntent = { handle, at }
            }
        }
    }

    const rootHandle = latestActivated?.handle ?? latestIntent?.handle ?? null

    return { statusByHandle, fleet, rootHandle }
}

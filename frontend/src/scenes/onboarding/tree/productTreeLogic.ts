/**
 * Logic for the quest-shaped onboarding Product Tree.
 *
 * Shape: the team's most-recently-touched product is the trunk; 2-3 ranked
 * branches dangle off it, each a recommended next product. The picks come from
 * `recommend(fleet, graph)` — fleet = activated handles; graph = vendored
 * Product Galaxy. Branches that point at already-activated products collapse
 * into checkmarks; branches at unlocked products surface as nodes the user can
 * launch into.
 */
import { connect, kea, path, selectors } from 'kea'

import { teamLogic } from 'scenes/teamLogic'

import { ProductKey } from '~/queries/schema/schema-general'

import { deriveAdoptionState } from './adoptionState'
import type { AdoptionState, AdoptionStatus } from './adoptionState'
import { buildGraph, recommend } from './graph'
import type { ProductGraph, Suggestion } from './graph'
import { IN_APP_HANDLES, handleToKey, keyToHandle } from './handleAdapter'
import type { productTreeLogicType } from './productTreeLogicType'
import type { ProductHandle } from './relationships'

export const MAX_BRANCHES = 3

export interface TreeBranch {
    handle: ProductHandle
    productKey: ProductKey
    status: AdoptionStatus
    /**
     * Marketing copy explaining *why* this product pairs with the trunk, taken
     * from the highest-weighted `pairsWith` edge among the suggestion's
     * contributors. May be `undefined` if no pairsWith contributor exists
     * (suggestion was driven by `billedWith`/`sharesFreeTier` only).
     */
    rationale?: string
    score: number
}

export interface TreeShape {
    /** The trunk: the team's current product, or `null` for fresh teams. */
    rootHandle: ProductHandle | null
    rootProductKey: ProductKey | null
    /** Branches sorted by recommendation score, capped at MAX_BRANCHES. */
    branches: TreeBranch[]
}

const EMPTY_TREE: TreeShape = {
    rootHandle: null,
    rootProductKey: null,
    branches: [],
}

function pickRationale(suggestion: Suggestion, rootHandle: ProductHandle): string | undefined {
    // Prefer a pairsWith contributor that's anchored on the root — that's the
    // copy the user is most likely to relate to ("you just installed X, this is
    // why Y is the natural next thing").
    const rootedPairs = suggestion.contributors.find(
        (c) => c.type === 'pairsWith' && c.from === rootHandle && c.description
    )
    if (rootedPairs) {
        return rootedPairs.description
    }
    const anyPairs = suggestion.contributors.find((c) => c.type === 'pairsWith' && c.description)
    return anyPairs?.description
}

export const productTreeLogic = kea<productTreeLogicType>([
    path(['scenes', 'onboarding', 'tree', 'productTreeLogic']),
    connect(() => ({
        values: [teamLogic, ['currentTeam']],
    })),
    selectors({
        adoptionState: [
            (s) => [s.currentTeam],
            (currentTeam): AdoptionState => deriveAdoptionState(currentTeam?.product_intents),
        ],
        graph: [() => [], (): ProductGraph => buildGraph(IN_APP_HANDLES)],
        treeShape: [
            (s) => [s.adoptionState, s.graph],
            (adoptionState, graph): TreeShape => {
                const { fleet, rootHandle, statusByHandle } = adoptionState
                if (!rootHandle) {
                    return EMPTY_TREE
                }

                const rootProductKey = handleToKey(rootHandle)
                if (!rootProductKey) {
                    return EMPTY_TREE
                }

                // `recommend()` already excludes anything in `fleet`. We then drop
                // suggestions that don't resolve to an in-app product key.
                const suggestions = recommend(fleet, graph, MAX_BRANCHES)
                const branches: TreeBranch[] = []
                for (const suggestion of suggestions) {
                    const productKey = handleToKey(suggestion.handle)
                    if (!productKey) {
                        continue
                    }
                    branches.push({
                        handle: suggestion.handle,
                        productKey,
                        status: statusByHandle.get(suggestion.handle) ?? 'none',
                        rationale: pickRationale(suggestion, rootHandle),
                        score: suggestion.score,
                    })
                }

                return { rootHandle, rootProductKey, branches }
            },
        ],
        /**
         * True when the team has no product intents at all (brand-new). The
         * scene falls back to the legacy product selection variant in this case
         * because there's no trunk to root the tree at.
         */
        isFreshTeam: [(s) => [s.adoptionState], (adoptionState): boolean => adoptionState.rootHandle === null],
        /**
         * Returns whether the given product key is gated (locked) for the team.
         * A product is locked when it's neither activated nor a top-ranked
         * branch — i.e. the team would have to bypass the recommended quest to
         * reach it.
         */
        isProductGated: [
            (s) => [s.treeShape, s.adoptionState],
            (treeShape, adoptionState): ((productKey: ProductKey) => boolean) =>
                (productKey: ProductKey) => {
                    const handle = keyToHandle(productKey)
                    if (!handle) {
                        return true
                    }
                    if (adoptionState.fleet.has(handle)) {
                        return false
                    }
                    if (handle === treeShape.rootHandle) {
                        return false
                    }
                    return !treeShape.branches.some((b) => b.handle === handle)
                },
        ],
    }),
])

/**
 * Quest-shaped onboarding view: a trunk node (the team's current product) with
 * 2-3 ranked branches that lead to the next recommended product. Used in
 * place of `ProductSelection` when `ONBOARDING_PRODUCT_TREE` is enabled.
 *
 * Fresh teams (no product intents) get sent back to the legacy product picker
 * by the parent — this component assumes a non-empty `treeShape`.
 */
import { useActions, useValues } from 'kea'
import { router } from 'kea-router'

import { IconCheck, IconLock } from '@posthog/icons'
import { LemonButton } from '@posthog/lemon-ui'

import { urls } from 'scenes/urls'

import { ProductKey } from '~/queries/schema/schema-general'
import type { OnboardingProduct } from '~/types'

import { OnboardingExitModal } from '../exit'
import { availableOnboardingProducts, getProductIcon } from '../utils'
import type { AdoptionStatus } from './adoptionState'
import { productTreeLogic } from './productTreeLogic'
import type { TreeBranch } from './productTreeLogic'

function getProduct(productKey: ProductKey): OnboardingProduct | undefined {
    return (availableOnboardingProducts as Partial<Record<string, OnboardingProduct>>)[productKey]
}

interface NodeProps {
    productKey: ProductKey
    status: AdoptionStatus
    rationale?: string
    /** If true, render the node but disable clicks (gated/locked). */
    locked?: boolean
}

function ProductNode({ productKey, status, rationale, locked = false }: NodeProps): JSX.Element | null {
    const product = getProduct(productKey)
    if (!product) {
        return null
    }

    const isActivated = status === 'activated'
    const onLaunch = (): void => {
        router.actions.push(urls.onboarding({ productKey }))
    }

    return (
        <div
            className={`flex flex-col items-center gap-2 rounded-lg border p-4 text-center transition-colors ${
                isActivated
                    ? 'border-success/40 bg-success-highlight'
                    : locked
                      ? 'border-border bg-bg-light opacity-60'
                      : 'border-border bg-bg-light hover:border-primary'
            }`}
        >
            <div className="relative">
                {getProductIcon(product.icon, {
                    iconColor: product.iconColor,
                    className: 'text-3xl',
                    productType: productKey,
                })}
                {isActivated && (
                    <span className="absolute -right-2 -bottom-2 rounded-full bg-success p-0.5 text-white">
                        <IconCheck className="text-sm" />
                    </span>
                )}
                {locked && (
                    <span className="absolute -right-2 -bottom-2 rounded-full bg-muted p-0.5 text-white">
                        <IconLock className="text-sm" />
                    </span>
                )}
            </div>
            <h3 className="text-base font-semibold leading-tight">{product.name}</h3>
            {rationale ? (
                <p className="text-xs text-muted">{rationale}</p>
            ) : (
                <p className="text-xs text-muted">{product.description}</p>
            )}
            {!isActivated && !locked && (
                <LemonButton type="primary" size="small" onClick={onLaunch}>
                    Set up
                </LemonButton>
            )}
            {isActivated && <span className="text-xs font-medium text-success uppercase tracking-wide">Active</span>}
        </div>
    )
}

function Branch({ branch }: { branch: TreeBranch }): JSX.Element {
    return (
        <ProductNode
            productKey={branch.productKey}
            status={branch.status}
            rationale={branch.rationale}
            locked={branch.status === 'activated'}
        />
    )
}

export function ProductTree(): JSX.Element {
    const { treeShape, isFreshTeam } = useValues(productTreeLogic)
    // Touch the logic so kea connects it.
    useActions(productTreeLogic)

    if (isFreshTeam || !treeShape.rootProductKey) {
        // Parent is responsible for sending these users to the legacy picker;
        // render an empty placeholder defensively so we never crash here.
        return <div className="hidden" />
    }

    return (
        <div className="flex flex-col items-center gap-8 py-8">
            <div className="flex flex-col items-center gap-2">
                <p className="text-sm text-muted uppercase tracking-wide">You're using</p>
                <ProductNode productKey={treeShape.rootProductKey} status="activated" />
            </div>

            {treeShape.branches.length > 0 && (
                <>
                    <div className="flex flex-col items-center gap-1">
                        <p className="text-sm font-medium">Try next</p>
                        <p className="text-xs text-muted">Pairs naturally with what you already use</p>
                    </div>
                    <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
                        {treeShape.branches.map((branch) => (
                            <Branch key={branch.handle} branch={branch} />
                        ))}
                    </div>
                </>
            )}

            <LemonButton
                type="tertiary"
                size="small"
                onClick={() => router.actions.push(urls.onboarding())}
                data-attr="product-tree-browse-all"
            >
                Browse all products
            </LemonButton>

            <OnboardingExitModal />
        </div>
    )
}

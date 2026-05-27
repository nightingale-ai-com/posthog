import { useValues } from 'kea'

import { FEATURE_FLAGS } from 'lib/constants'
import { featureFlagLogic } from 'lib/logic/featureFlagLogic'

import { OnboardingExitModal } from '../exit'
import { ProductTree } from '../tree/ProductTree'
import { productTreeLogic } from '../tree/productTreeLogic'
import { LegacyProductSelection } from './variants/legacy/LegacyProductSelection'
import { MultiproductProductSelection } from './variants/multiproduct/MultiproductProductSelection'
import { SpotlightProductSelection } from './variants/spotlight/SpotlightProductSelection'

export function ProductSelection(): JSX.Element {
    const { featureFlags } = useValues(featureFlagLogic)
    // Tree replaces the picker for returning teams when the flag is on; brand-new
    // teams (no product intents) still hit the picker variants below because the
    // tree has no trunk to root at.
    const { isFreshTeam } = useValues(productTreeLogic)
    const treeEnabled = !!featureFlags[FEATURE_FLAGS.ONBOARDING_PRODUCT_TREE]
    if (treeEnabled && !isFreshTeam) {
        return <ProductTree />
    }

    const defaultVariant = 'control'
    const variant = featureFlags[FEATURE_FLAGS.PRODUCT_SELECTION_SCREEN_VARIANT] ?? defaultVariant

    const productSelection = (() => {
        switch (variant) {
            case 'spotlight':
                return <SpotlightProductSelection />
            case 'multiproduct':
                return <MultiproductProductSelection />
            case 'control':
            default:
                return <LegacyProductSelection />
        }
    })()

    return (
        <>
            {productSelection}
            <OnboardingExitModal />
        </>
    )
}

export default ProductSelection

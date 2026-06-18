import { expectLogic } from 'kea-test-utils'

import { initKeaTests } from '~/test/init'

import { logsViewerFiltersLogic } from '../Filters/logsViewerFiltersLogic'
import { facetRailLogic } from './facetRailLogic'

describe('facetRailLogic', () => {
    let filtersLogic: ReturnType<typeof logsViewerFiltersLogic.build>
    let logic: ReturnType<typeof facetRailLogic.build>

    beforeEach(() => {
        initKeaTests()
        filtersLogic = logsViewerFiltersLogic({ id: 'test' })
        filtersLogic.mount()
        logic = facetRailLogic({ id: 'test' })
        logic.mount()
    })

    afterEach(() => {
        logic.unmount()
        filtersLogic.unmount()
    })

    describe('severity level toggling', () => {
        it('adds, accumulates (OR), and removes levels on the shared filters logic', async () => {
            await expectLogic(logic, () => logic.actions.toggleSeverityLevel('error')).toFinishAllListeners()
            expect(filtersLogic.values.severityLevels).toEqual(['error'])

            await expectLogic(logic, () => logic.actions.toggleSeverityLevel('warn')).toFinishAllListeners()
            expect(filtersLogic.values.severityLevels).toEqual(['error', 'warn'])

            await expectLogic(logic, () => logic.actions.toggleSeverityLevel('error')).toFinishAllListeners()
            expect(filtersLogic.values.severityLevels).toEqual(['warn'])
        })
    })

    describe('service name toggling', () => {
        it('adds then removes a service on the shared filters logic', async () => {
            await expectLogic(logic, () => logic.actions.toggleServiceName('api')).toFinishAllListeners()
            expect(filtersLogic.values.serviceNames).toEqual(['api'])

            await expectLogic(logic, () => logic.actions.toggleServiceName('api')).toFinishAllListeners()
            expect(filtersLogic.values.serviceNames).toEqual([])
        })
    })

    describe('shared state', () => {
        it('toggles relative to selections already on the filters logic', async () => {
            filtersLogic.actions.setSeverityLevels(['info'])
            await expectLogic(filtersLogic).toFinishAllListeners()

            await expectLogic(logic, () => logic.actions.toggleSeverityLevel('error')).toFinishAllListeners()
            expect(filtersLogic.values.severityLevels).toEqual(['info', 'error'])
        })
    })
})

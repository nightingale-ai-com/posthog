/**
 * Recording API Types
 *
 * Re-exports shared encryption types from session-replay/types.ts and adds
 * Recording API-specific types.
 */
import { CommonConfig } from '../../common/config'
import {
    KAFKA_CLICKHOUSE_SESSION_REPLAY_EVENTS,
    KAFKA_CLICKHOUSE_SESSION_REPLAY_FEATURES,
} from '../../config/kafka-topics'
import { DEFAULT_PRODUCER, type DefaultProducer, type WarpstreamProducer } from '../../ingestion/common/outputs'
import { SessionRecordingApiConfig, SessionRecordingConfig } from '../../session-recording/config'
import { isProdEnv } from '../../utils/env-utils'

/**
 * Recording API only needs DEFAULT + WARPSTREAM producers — its outputs are
 * ClickHouse-bound deletion tombstones, so the ingestion-internal cluster
 * (INGESTION) is not a relevant target here.
 */
export type RecordingApiProducerName = DefaultProducer | WarpstreamProducer

// Re-export all shared encryption types so existing recording-api imports still work
export {
    SessionKeyDeletedError,
    type DecryptResult,
    type DeleteKeyResult,
    type EncryptResult,
    type KeyStore,
    type RecordingDecryptor,
    type RecordingEncryptor,
    type SerializedSessionKey,
    type SessionKey,
    type SessionState,
} from '../shared/types'

/**
 * Recording-api authorization config.
 *
 * `RECORDING_API_JWT_SECRET` is a dedicated signing secret (comma-separated `new_key,old_key` for
 * rotation), kept off the fleet-wide `JWT_SIGNING_KEY` so only the designated minters can produce
 * valid tokens. `RECORDING_API_ALLOW_LEGACY_SECRET` keeps the old `X-Internal-Api-Secret` accepted
 * during migration; flip it off at cutover.
 */
export type RecordingApiAuthConfig = {
    RECORDING_API_JWT_SECRET: string
    RECORDING_API_ALLOW_LEGACY_SECRET: boolean
}

export function getDefaultRecordingApiAuthConfig(): RecordingApiAuthConfig {
    return {
        // Dev/test default matches the Django/Temporal minters so local end-to-end calls validate.
        RECORDING_API_JWT_SECRET: isProdEnv() ? '' : 'dev-recording-api-jwt-secret',
        RECORDING_API_ALLOW_LEGACY_SECRET: true,
    }
}

/**
 * Configuration for the Recording API.
 * Postgres is passed as an explicit constructor param, not included here.
 */
export type RecordingApiConfig = Pick<
    CommonConfig,
    'KAFKA_CLIENT_RACK' | 'REDIS_POOL_MIN_SIZE' | 'REDIS_POOL_MAX_SIZE' | 'INTERNAL_API_SECRET'
> &
    Pick<
        SessionRecordingApiConfig,
        | 'SESSION_RECORDING_API_REDIS_HOST'
        | 'SESSION_RECORDING_API_REDIS_PORT'
        | 'SESSION_RECORDING_KMS_ENDPOINT'
        | 'SESSION_RECORDING_DYNAMODB_ENDPOINT'
        | 'CLICKHOUSE_HOST'
        | 'CLICKHOUSE_DATABASE'
        | 'CLICKHOUSE_USER'
        | 'CLICKHOUSE_PASSWORD'
        | 'CLICKHOUSE_SECURE'
    > &
    Pick<
        SessionRecordingConfig,
        | 'SESSION_RECORDING_V2_S3_REGION'
        | 'SESSION_RECORDING_V2_S3_ENDPOINT'
        | 'SESSION_RECORDING_V2_S3_ACCESS_KEY_ID'
        | 'SESSION_RECORDING_V2_S3_SECRET_ACCESS_KEY'
        | 'SESSION_RECORDING_V2_S3_BUCKET'
        | 'SESSION_RECORDING_V2_S3_PREFIX'
    > &
    RecordingApiAuthConfig

/**
 * Recording API outputs — topic and producer routing per output. All keys
 * follow the `RECORDING_API_OUTPUT_*` convention. Topic values default to
 * the same Kafka topics the session-replay ingestion consumer writes to,
 * since recording-api emits deletion tombstones into the same streams.
 */
export type RecordingApiOutputsConfig = {
    RECORDING_API_OUTPUT_REPLAY_EVENTS_TOPIC: string
    RECORDING_API_OUTPUT_REPLAY_EVENTS_PRODUCER: RecordingApiProducerName

    RECORDING_API_OUTPUT_SESSION_FEATURES_TOPIC: string
    RECORDING_API_OUTPUT_SESSION_FEATURES_PRODUCER: RecordingApiProducerName
}

export function getDefaultRecordingApiOutputsConfig(): RecordingApiOutputsConfig {
    return {
        RECORDING_API_OUTPUT_REPLAY_EVENTS_TOPIC: KAFKA_CLICKHOUSE_SESSION_REPLAY_EVENTS,
        RECORDING_API_OUTPUT_REPLAY_EVENTS_PRODUCER: DEFAULT_PRODUCER,
        RECORDING_API_OUTPUT_SESSION_FEATURES_TOPIC: KAFKA_CLICKHOUSE_SESSION_REPLAY_FEATURES,
        RECORDING_API_OUTPUT_SESSION_FEATURES_PRODUCER: DEFAULT_PRODUCER,
    }
}

export interface RecordingBlock {
    key: string
    start_byte: number
    end_byte: number
    start_timestamp: string
    end_timestamp: string
}

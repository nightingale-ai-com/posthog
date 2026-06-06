import crypto from 'crypto'
import { NextFunction, Request, Response } from 'ultimate-express'

import { INTERNAL_SERVICE_CALL_HEADER_NAME } from '../../api/middleware/internal-api-auth'
import { JWT, PosthogJwtAudience, parseJwtKeys } from '../../cdp/utils/jwt-utils'
import { logger } from '../../utils/logger'

/**
 * Authorization for recording-api routes.
 *
 * Each route requires a team + operation scoped JWT (audience `posthog:recording_api`) minted by
 * an authorized service: the token's `team_id` must match the `:team_id` in the path and its `op`
 * must match the route (read vs delete). During migration the legacy `X-Internal-Api-Secret` is
 * accepted as a fallback while `allowLegacySecret` is true; the final cutover flips that flag off.
 */

export type RecordingApiOp = 'read' | 'delete'

// jsonwebtoken tolerates up to this many seconds of clock skew between minter and verifier.
const CLOCK_TOLERANCE_SECONDS = 30

export interface RecordingApiAuthOptions {
    // Comma-separated keys (newest first) for zero-downtime rotation. Empty disables JWT auth (dev only).
    jwtSecret: string
    legacySecret: string
    allowLegacySecret: boolean
    op: RecordingApiOp
}

function getHeader(req: Request, name: string): string | undefined {
    // Node lowercases all incoming header field names, so a single lowercased lookup is sufficient.
    const value = req.headers[name.toLowerCase()]
    return typeof value === 'string' ? value : undefined
}

function getBearerToken(req: Request): string | undefined {
    const header = getHeader(req, 'authorization')
    return header && /^Bearer /i.test(header) ? header.slice('Bearer '.length) : undefined
}

function timingSafeEqual(a: string, b: string): boolean {
    const ab = Buffer.from(a)
    const bb = Buffer.from(b)
    return ab.length === bb.length && crypto.timingSafeEqual(ab, bb)
}

/**
 * Fail closed in production: recording routes must have at least one auth mechanism active. Before the
 * JWT scheme is rolled out the legacy X-Internal-Api-Secret protects prod; once RECORDING_API_JWT_SECRET
 * is set, the JWT does. Only refuse to boot if neither is configured. Uses the same key parsing as the
 * verifier so a malformed-but-truthy value (e.g. ',') counts as "no JWT configured".
 */
export function assertRecordingApiAuthConfigured(opts: {
    isProd: boolean
    jwtSecret: string
    allowLegacySecret: boolean
    legacySecret: string
}): void {
    const hasJwtSecret = parseJwtKeys(opts.jwtSecret || '').length > 0
    const hasLegacySecret = opts.allowLegacySecret && !!opts.legacySecret
    if (opts.isProd && !hasJwtSecret && !hasLegacySecret) {
        throw new Error(
            'recording-api has no auth configured in production: set RECORDING_API_JWT_SECRET, ' +
                'or keep RECORDING_API_ALLOW_LEGACY_SECRET enabled with INTERNAL_API_SECRET set'
        )
    }
}

export function createRecordingApiAuthMiddleware(options: RecordingApiAuthOptions) {
    const { jwtSecret, legacySecret, allowLegacySecret, op } = options
    // Build the verifier once. Comma-separated keys give zero-downtime rotation (verify against all).
    // Parse keys the same way the JWT class (and the Python minter) does, so a malformed-but-truthy
    // value like ',' yields no keys and disables JWT rather than throwing at construction.
    const jwt = parseJwtKeys(jwtSecret || '').length > 0 ? new JWT(jwtSecret) : null
    const legacyEnabled = allowLegacySecret && !!legacySecret

    return (req: Request, res: Response, next: NextFunction): void => {
        // Nothing configured at all (local dev): allow. In every other case some auth is required —
        // before the JWT scheme is enabled in an environment, the legacy secret still protects it.
        if (!jwt && !legacyEnabled) {
            next()
            return
        }

        if (jwt) {
            const bearer = getBearerToken(req)
            if (bearer) {
                let payload: ReturnType<JWT['verify']>
                try {
                    payload = jwt.verify(bearer, PosthogJwtAudience.RECORDING_API, {
                        clockTolerance: CLOCK_TOLERANCE_SECONDS,
                    })
                } catch (err) {
                    // Log the failure class (e.g. TokenExpiredError, JsonWebTokenError) — never the token —
                    // so rollout/rotation 401s are diagnosable. Then fall through to the legacy secret or 401.
                    logger.warn('Recording API JWT verification failed', {
                        reason: err instanceof Error ? err.name : 'unknown',
                        path: req.path,
                    })
                    payload = undefined
                }
                if (payload && typeof payload === 'object') {
                    const claims = payload as { team_id?: unknown; op?: unknown }
                    const tokenTeamId = Number(claims.team_id)
                    const pathTeamId = Number(req.params.team_id)
                    if (!Number.isInteger(tokenTeamId) || tokenTeamId !== pathTeamId) {
                        logger.warn('Recording API token team mismatch', { path: req.path })
                        res.status(403).json({ error: 'Forbidden: token not scoped to this team' })
                        return
                    }
                    if (claims.op !== op) {
                        logger.warn('Recording API token operation mismatch', { path: req.path, expected: op })
                        res.status(403).json({ error: 'Forbidden: token not valid for this operation' })
                        return
                    }
                    next()
                    return
                }
                // Bearer present but invalid — fall through to the legacy secret (transition) or reject.
            }
        }

        if (legacyEnabled) {
            const provided = getHeader(req, INTERNAL_SERVICE_CALL_HEADER_NAME)
            if (provided && timingSafeEqual(provided, legacySecret)) {
                next()
                return
            }
        }

        res.status(401).json({ error: 'Unauthorized' })
    }
}

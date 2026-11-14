// The `hermes:api` conditional-GET seam, shared by the main-process handler
// and the renderer so both sides agree on one literal (mirror of
// api-expected-404.ts).
//
// A renderer list read opts in with `request.conditional` and main answers
// with a BRANDED envelope `{ etag, status, data }` instead of the bare parsed
// body: only a GET /api/sessions read (the route that stamps content ETags)
// ever travels as one, so the general `hermes:api` surface stays unchanged
// for every other call. An older main process ignores the request fields and
// resolves the plain body — the renderer detects the missing brand and falls
// back to today's unconditional behavior (the capability gate).

const HERMES_API_CONDITIONAL = '__hermesConditional__'

export interface HermesApiConditionalPayload {
  etag: string | null
  status: number
  data: unknown
}

export type HermesApiConditionalEnvelope = HermesApiConditionalPayload & {
  [HERMES_API_CONDITIONAL]: true
}

// True when `value` is the handler-resolved conditional envelope, not real
// response data. The brand is the only discriminator: a session list payload
// is `{ sessions, total, ... }` and can never carry it.
export function isConditionalEnvelope(value: unknown): value is HermesApiConditionalEnvelope {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return false
  }

  const obj = value as Record<string, unknown>

  return (
    obj[HERMES_API_CONDITIONAL] === true &&
    typeof obj.status === 'number' &&
    (obj.etag === null || typeof obj.etag === 'string') &&
    'data' in obj
  )
}

export function buildConditionalEnvelope(payload: HermesApiConditionalPayload): HermesApiConditionalEnvelope {
  return { [HERMES_API_CONDITIONAL]: true, ...payload }
}

// Main-side unwrap: the post-processing steps (profile-preference recording,
// registry row tagging) must see the payload's data, not the envelope.
export function unwrapConditionalEnvelope(value: unknown): HermesApiConditionalPayload | null {
  return isConditionalEnvelope(value)
    ? { etag: value.etag, status: value.status, data: value.data }
    : null
}

export { HERMES_API_CONDITIONAL }

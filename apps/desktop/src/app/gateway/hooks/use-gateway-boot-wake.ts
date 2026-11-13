/**
 * Wake recovery for one window's primary gateway socket (#C2a).
 *
 * Extracted from the useGatewayBoot effect: it is the one part of that hook
 * with state of its own (a park latch, a holdoff stamp, the sockets a wake kept
 * alive), and the effect is over its length cap. The host supplies the window's
 * primary and the four things only the hook can do (route resolution,
 * attach/promote, post-connect work, reauth surfacing); everything decided here
 * is decided from renderer-wide stores.
 *
 * Two behaviours live here:
 *
 * 1. PARK. On `visibilitychange → hidden` the window stops paying reconnect
 *    costs: the primary ladder and every pool ladder are disarmed and latched,
 *    and the routes are STALE from then on — the app stops trusting that any
 *    socket survived the time the user was away. Nothing is torn down: the
 *    cached transcript keeps painting and a genuinely alive socket stays up.
 *    A hidden window dials NOTHING, whatever signal arrives (an interface
 *    change, a lid resume that leaves the app backgrounded), which is what
 *    makes a backgrounded app free and a ten-minute background mint zero
 *    tickets.
 *
 * 2. DIAL, DON'T PROBE. On the return — and on focus / online / power-resume —
 *    a socket that still reports OPEN is presumptively dead: the renderer's
 *    WebSocket object keeps `readyState === OPEN` while the OS threw the
 *    transport away, and no close event ever fires. The old path PROBED that
 *    socket with a 5 s ping before deciding, which is strictly slower than the
 *    dial it protects (measured ticket→accept p50 0.02-0.07 s on the test
 *    instance) and left the user watching a dead socket be proven dead. A wake
 *    now MINTS AND DIALS a replacement generation at once — while the old
 *    socket stays open, so the window is never un-owned — and retires the old
 *    generation at the earliest safe point:
 *      * nothing was riding it → retire it as soon as the replacement is open;
 *      * a turn was riding it  → keep it (that is #95327: a busy backend must
 *        never have a socket torn out from under a running turn) and retire it
 *        once that turn's claim drains.
 *    The liveness probe keeps exactly that #95327 job on the paths that still
 *    need it: the restart-followthrough and the pool's forced sweep.
 */
import type { HermesConnection } from '@/global'
import { HermesGateway } from '@/hermes'
import { shouldRetireReplacedSocket } from '@/lib/gateway-liveness-policy'
import { ensureReconnectEpisode, noteReconnectDetection } from '@/store/desktop-metrics'
import { reconnectSecondaryGateways } from '@/store/gateway'
import {
  parkSecondaryReconnects,
  secondaryReconnectsParked,
  unparkSecondaryReconnects
} from '@/store/gateway-park'
import { reconnectGateway } from '@/store/gateway-reconnect'
import { $workingSessionIds } from '@/store/session-states'

export interface WakeRoute {
  conn: HermesConnection
  wsUrl: string
}

export interface WakeHost {
  /** Holdoff that coalesces the DESTRUCTIVE secondary sweep (never the dial). */
  holdoffMs: number
  /** True while a recovery step may proceed: booted, not switching, not torn down. */
  ready: () => boolean
  /** The window's CURRENT primary client. */
  primary: () => HermesGateway
  /** Subscribe a new generation to the window's primary handlers; returns its detach. */
  attach: (client: HermesGateway) => () => void
  /** Make `client` this window's primary: bind it, republish, announce it. */
  promote: (client: HermesGateway, detach: () => void, conn: HermesConnection) => void
  /** Resolve the primary route and mint its ticket; null when a newer route owns the socket. */
  resolveRoute: () => Promise<null | WakeRoute>
  /** Dial in place, for a signal with no socket to protect. */
  redial: () => Promise<void>
  /** Post-connect work for a replacement generation (no respawn reconcile). */
  finish: (conn: HermesConnection) => Promise<void>
  /** Surface a stale credential once; every other failure leaves the live socket alone. */
  noteReauth: (error: unknown) => void
}

export interface WakeRecovery {
  /**
   * Wire the window's recovery signals to `signal` (the caller's "the primary
   * ladder should stand down and dial again" step). Visibility changes park and
   * unpark on the way through; returns one detach.
   */
  bindSignals: (signal: () => void) => () => void
  /** Retire any replaced generation whose in-flight work has drained. */
  drain: () => void
  /** Close every socket this module kept alive and drop its subscriptions. */
  dispose: () => void
  /** Park: disarmed ladders, stale routes, nothing torn down. */
  park: () => void
  /** Return: the routes are live again. Never dials by itself — the caller wakes. */
  unpark: () => void
  /** One wake signal: dial once (single-flighted), sweep the pool once per holdoff. */
  wake: () => void
  /** The `'wake'` reconnect handler: dial a replacement generation. */
  dial: () => Promise<void>
}

export function createWakeRecovery(host: WakeHost): WakeRecovery {
  let lastForcedSweepAt = 0
  /** Sockets a wake replaced but kept, because a turn was riding them. */
  const replacedGenerations: HermesGateway[] = []

  const closeReplaced = () => {
    for (const previous of replacedGenerations.splice(0)) {
      try {
        previous.close()
      } catch {
        // Already torn down by the backend or the OS.
      }
    }
  }

  const drain = () => {
    if (replacedGenerations.length === 0 || !shouldRetireReplacedSocket($workingSessionIds.get().length)) {
      return
    }

    closeReplaced()
  }

  // A replaced socket that was kept for a turn is retired the moment that work
  // drains (#95327): the module owns the trigger, so the window does not have
  // to remember to call drain() from the right subscription.
  const offWorkDrained = $workingSessionIds.subscribe(drain)

  /**
   * Retire a replaced socket now, or keep it until its work drains (#95327).
   * The in-flight-work signal is read HERE, at the signal boundary: the dial
   * that follows reconciles runtime bindings for the replaced socket, and that
   * reconcile is not the turn ending.
   */
  const retireReplaced = (previous: HermesGateway) => {
    if (!shouldRetireReplacedSocket($workingSessionIds.get().length)) {
      replacedGenerations.push(previous)

      return
    }

    previous.close()
  }

  const sweep = (): { forceOpenSockets: boolean } => {
    const now = Date.now()
    const forced = now - lastForcedSweepAt >= host.holdoffMs

    if (forced) {
      lastForcedSweepAt = now
    }

    return { forceOpenSockets: forced }
  }

  const dial = async (): Promise<void> => {
    if (!host.ready() || secondaryReconnectsParked()) {
      return
    }

    const previous = host.primary()

    if (previous.connectionState !== 'open') {
      // Nothing to protect and nothing to replace: the ordinary dial. A window
      // parked while its socket died comes back through exactly this branch.
      await host.redial()

      return
    }

    // C1 episode: a wake fires no close edge, so the signal starts the clock and
    // the redial decision below ends detection.
    ensureReconnectEpisode({ closeReason: 'wake', visibilityAtClose: windowVisibility() })
    noteReconnectDetection()

    try {
      const route = await host.resolveRoute()

      if (!route) {
        return
      }

      const successor = new HermesGateway()
      const detach = host.attach(successor)

      try {
        await successor.connect(route.wsUrl)
      } catch (error) {
        detach()
        successor.close()
        throw error
      }

      if (!host.ready() || successor.connectionState !== 'open') {
        detach()
        successor.close()

        return
      }

      host.promote(successor, detach, route.conn)
      await host.finish(route.conn)
      retireReplaced(previous)
    } catch (error) {
      // A stale ticket still has to surface once (it can never succeed); a
      // transport failure is not a lost connection — the live socket stays.
      host.noteReauth(error)
    } finally {
      drain()
    }
  }

  return {
    bindSignals: (signal: () => void) => {
      // A fresh bind RE-READS the document: the latch is a function of what the
      // window is doing, so a stale park (an editor reload while hidden, a mount
      // that missed the event) can never latch a visible window's ladders off.
      if (document.visibilityState === 'hidden') {
        parkSecondaryReconnects()
      } else {
        unparkSecondaryReconnects()
      }

      const onOnline = () => signal()
      const onFocus = () => signal()

      const onVisible = () => {
        if (document.visibilityState === 'visible') {
          unparkSecondaryReconnects()
          signal()

          return
        }

        parkSecondaryReconnects()
      }

      window.addEventListener('online', onOnline)
      window.addEventListener('focus', onFocus)
      document.addEventListener('visibilitychange', onVisible)

      return () => {
        window.removeEventListener('online', onOnline)
        window.removeEventListener('focus', onFocus)
        document.removeEventListener('visibilitychange', onVisible)
      }
    },
    dial,
    drain,
    dispose: () => {
      offWorkDrained()
      closeReplaced()
    },
    park: parkSecondaryReconnects,
    unpark: unparkSecondaryReconnects,
    wake: () => {
      if (!host.ready() || secondaryReconnectsParked()) {
        return
      }

      reconnectSecondaryGateways(sweep())
      // Single-flight: a burst of wake signals (macOS resume + unlock + a
      // network change, seconds apart) joins ONE dial instead of stacking
      // replacement sockets.
      void reconnectGateway({ source: 'wake' }).catch(() => undefined)
    }
  }
}

function windowVisibility(): 'hidden' | 'visible' {
  return document.visibilityState === 'hidden' ? 'hidden' : 'visible'
}

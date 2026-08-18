/**
 * Alert bus — lets a NEW critical alert reach an ALREADY-OPEN alert screen
 * without navigating, so an aftershock can never destroy an answer that a
 * trapped person is halfway through giving.
 *
 * Why this exists (Paul, 2026-08-17): aftershocks arrive minutes after a
 * main shock. The foreground auto-routing added for #169 called
 * router.push("/alert?siren=1"), which mounts a SECOND alert screen: the
 * first one's React state — the open triage sheet, the severity they'd just
 * chosen, the mobility answer — is gone, and its siren player keeps looping
 * underneath the new one. Someone under rubble would have to report
 * themselves twice because the ground shook again, and hear two sirens while
 * doing it.
 *
 * Contract:
 *   - alert.tsx marks itself mounted/unmounted.
 *   - _layout.tsx publishes instead of navigating whenever it's mounted.
 *   - Nothing here touches the user's answer. The screen decides what to do
 *     with the new event; the bus only carries it.
 */

export type CriticalAlertEvent = {
  magnitude?: string | null;
  distance_km?: string | null;
  intensity?: string | null;
  depth_km?: string | null;
  region?: string | null;
  unid?: string | null;
};

type Listener = (event: CriticalAlertEvent) => void;

let alertScreenMounted = false;
const listeners = new Set<Listener>();

export function setAlertScreenMounted(mounted: boolean): void {
  alertScreenMounted = mounted;
}

export function isAlertScreenMounted(): boolean {
  return alertScreenMounted;
}

export function subscribeToAlerts(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function publishAlert(event: CriticalAlertEvent): void {
  listeners.forEach((l) => {
    try {
      l(event);
    } catch {
      // a broken listener must never stop the others
    }
  });
}

// Dev-only handle so the aftershock path can be exercised in a browser,
// where there are no push notifications to trigger it. Stripped from
// production builds by the __DEV__ guard.
if (typeof __DEV__ !== "undefined" && __DEV__) {
  (globalThis as any).__quakeAngelAlertBus = { publishAlert, isAlertScreenMounted };
}

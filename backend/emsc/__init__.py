"""EMSC/USGS earthquake-monitoring subsystem — Phase 1: shadow mode.

Phase 1 goal (2026-08 → soak period 1-2 weeks):
  Poll two independent earthquake providers (EMSC seismicportal + USGS
  earthquake.usgs.gov) once per minute, evaluate every event against
  several candidate threshold sets per country, and log every decision
  to MongoDB — WITHOUT firing any user-facing push notifications.

Why shadow mode:
  The threshold sets that determine which real earthquakes trigger
  which tier of user alert are the single most important product
  decision for this feature. Getting them wrong at go-live means either
  (a) users get notified for irrelevant events (alert fatigue → uninstalls)
  or (b) users get no notification for a genuine event they needed (which
  in a life-safety product is the worst possible outcome). We do not have
  enough historical data to pick thresholds a priori; instead we log every
  provider event against MULTIPLE candidate threshold sets for two weeks
  and pick based on observed outcomes.

Public API is exposed through the top-level submodules:
  emsc.providers   — provider abstraction + EMSCProvider / USGSProvider
  emsc.evaluator   — severity scoring and threshold-set evaluation
  emsc.poller      — background asyncio poll loop + health tracking
  emsc.seed        — Malta country_config seed for first boot

See PRD.md §EMSC-monitoring for the full design write-up.
"""

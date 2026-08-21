/**
 * Timestamp parsing that cannot be silently two hours wrong.
 *
 * Backend timestamps come from MongoDB via Motor, which returns NAIVE
 * datetimes. A naive `isoformat()` produces "2026-08-18T08:07:10.750000"
 * with no offset, and ECMAScript parses an offset-less date-time as LOCAL
 * time. On a Malta phone (CEST, UTC+2) an 08:07 UTC earthquake therefore
 * rendered as "08:07" instead of "10:07" — two hours early, on the one
 * timestamp a user compares against the moment the notification arrived.
 * It made an 11-minute delivery look like a three-hour one (Paul,
 * 2026-08-18).
 *
 * The backend now always emits an offset. This is the belt-and-braces half:
 * anything that still arrives without one is treated as UTC, because every
 * timestamp this app receives is UTC.
 */
export function parseUtc(iso?: string | null): Date | null {
  if (!iso) return null;
  const s = String(iso).trim();
  // Has an offset (Z, +HH:MM, -HH:MM) or is not an ISO date-time at all?
  // Hand it straight to Date. Otherwise pin it to UTC.
  const hasOffset = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(s);
  const d = new Date(hasOffset || !s.includes("T") ? s : s + "Z");
  return isNaN(d.getTime()) ? null : d;
}

/**
 * #272 (2026-08-21 — Paul): one clock, named, everywhere a person reads a
 * time. The dashboard, both PDFs and this app all show Malta time, so the
 * same event can never appear at two different times across the product.
 * The instant itself is always stored and sent in UTC.
 *
 *   "This is a legal record that will be read back in an inquiry
 *    alongside radio logs kept in local time."
 *
 * Daylight saving is handled by the platform's timezone database for the
 * instant in question — we never do arithmetic on local clock times.
 */
export const MALTA_TZ = "Europe/Malta";

export function maltaTime(d?: Date | null): string {
  if (!d || isNaN(d.getTime())) return "Unknown";
  try {
    return d.toLocaleString("en-GB", {
      day: "numeric", month: "short", year: "numeric",
      hour: "2-digit", minute: "2-digit",
      timeZone: MALTA_TZ,
    });
  } catch {
    // Some older engines lack full timezone data — never crash a screen
    // over a timestamp.
    return d.toISOString().slice(0, 16).replace("T", " ") + " UTC";
  }
}

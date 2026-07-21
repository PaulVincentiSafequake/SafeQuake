/**
 * OPTIONAL add-on: auto-triggers real alerts from genuine earthquakes,
 * instead of relying only on the app's manual "DEMO: simulate alert"
 * button. Uses the US Geological Survey's free, public Earthquake API
 * - no account or API key needed.
 *
 * This is a standalone script, not wired into server.js by default -
 * a developer should run it alongside the server (or fold it in) and
 * connect the TODO below to Firebase Cloud Messaging using your own
 * Firebase project's server credentials. See README, Step 3.
 *
 * Usage: node usgs_poller.js
 */

const USGS_FEED_URL =
  "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_hour.geojson";
// USGS publishes several feeds (past hour / day / week, by magnitude
// threshold). "significant_hour" is a sensible default for a live
// pilot demo; swap for a feed matching your actual test region and
// desired magnitude threshold.

const POLL_INTERVAL_MS = 60_000; // once a minute is plenty for a pilot

// Roughly, the area your test users are in - replace with real values.
const PILOT_REGION = {
  minLat: 34.0,
  maxLat: 37.0,
  minLon: 12.0,
  maxLon: 16.0,
};

let alreadyAlerted = new Set();

async function poll() {
  try {
    const res = await fetch(USGS_FEED_URL);
    const data = await res.json();

    for (const quake of data.features || []) {
      const [lon, lat] = quake.geometry.coordinates;
      const id = quake.id;

      const inRegion =
        lat >= PILOT_REGION.minLat &&
        lat <= PILOT_REGION.maxLat &&
        lon >= PILOT_REGION.minLon &&
        lon <= PILOT_REGION.maxLon;

      if (inRegion && !alreadyAlerted.has(id)) {
        alreadyAlerted.add(id);
        console.log(
          `[usgs] Real earthquake detected near pilot region: ${quake.properties.place} (M${quake.properties.mag})`
        );
        await sendAlertToAllTestUsers(quake);
      }
    }
  } catch (err) {
    console.error("[usgs] Poll failed:", err.message);
  }
}

async function sendAlertToAllTestUsers(quake) {
  // TODO: replace with a real call to the Firebase Admin SDK, using
  // your Firebase project's service account credentials, sending a
  // push notification with data: { type: "earthquake_alert" } to
  // every registered device token. See:
  // https://firebase.google.com/docs/cloud-messaging/send-message
  console.log(
    `[usgs] TODO: send push notification to all test devices about ${quake.properties.place}`
  );
}

console.log(`[usgs] Polling USGS every ${POLL_INTERVAL_MS / 1000}s for quakes near the pilot region...`);
poll();
setInterval(poll, POLL_INTERVAL_MS);

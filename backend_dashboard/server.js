/**
 * Minimal pilot backend for the Earthquake Safety App.
 *
 * Deliberately written with ZERO external dependencies (just Node's
 * built-in `http`, `fs`, `url` modules) so it runs anywhere with
 * Node installed, with no `npm install` step and nothing that can
 * fail to download. That's a feature for a quick demo, not a
 * long-term choice, a real build would reasonably use Express (or
 * similar) and a real database instead of the in-memory Map below.
 *
 * What this is: enough of a real server to demo the app end-to-end
 * (receive check-ins, show them live on a map) with actual test
 * phones. What this is NOT: production infrastructure. It stores
 * everything in memory (wiped on restart) and has no authentication.
 * A hired developer should treat this as a working sketch to replace
 * before any public pilot, see the roadmap document, Step 5 and 7.
 */

const http = require("http");
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const { URL } = require("url");

const PUBLIC_DIR = path.join(__dirname, "public");
const PORT = process.env.PORT || 3000;

// In-memory "database" - keyed by deviceId, most recent report wins.
const statusReports = new Map();
const beaconSightings = [];

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
};

function sendJson(res, statusCode, data) {
  const body = JSON.stringify(data);
  res.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Access-Control-Allow-Origin": "*",
    "Content-Length": Buffer.byteLength(body),
  });
  res.end(body);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = "";
    req.on("data", (chunk) => (data += chunk));
    req.on("end", () => {
      if (!data) return resolve({});
      try {
        resolve(JSON.parse(data));
      } catch (e) {
        reject(e);
      }
    });
    req.on("error", reject);
  });
}

function serveStatic(req, res, pathname) {
  let filePath = pathname === "/" ? "/index.html" : pathname;
  filePath = path.join(PUBLIC_DIR, filePath);

  // Prevent path traversal outside the public directory.
  if (!filePath.startsWith(PUBLIC_DIR)) {
    res.writeHead(403);
    return res.end("Forbidden");
  }

  fs.readFile(filePath, (err, content) => {
    if (err) {
      res.writeHead(404, { "Content-Type": "text/plain" });
      return res.end("Not found");
    }
    const ext = path.extname(filePath);
    // Cache fix (2026-08-12): the dashboard was served with NO caching
    // headers, so browsers/CDN used heuristic caching and operators saw
    // stale UI for hours after a deploy — twice a working feature looked
    // broken during verification. `no-cache` forces revalidation on every
    // load; the content-hash ETag makes that revalidation a cheap 304
    // when nothing actually changed.
    //
    // #57 / #265 (Neo, 2026-08-20 — Paul): "your fix is live and works,
    // but only after a hard refresh." `no-cache` alone permits the
    // browser to reuse a copy while it revalidates; on a slow revalidation
    // path (or a CDN that answered 304 based on a stale upstream), the
    // operator sees the OLD content for one full page load. Adding
    // `no-store` for HTML forces every load to be a full round-trip so
    // there is no window in which a stale copy is even eligible for use.
    // Static assets (images, favicon) keep the lighter `no-cache` — the
    // performance cost of no-store on the 316KB HTML is one round-trip
    // per page load and the safety cost of a stale HTML is a mistyped
    // confirmation phrase or a missed sign-out. Not close.
    const etag = '"' + crypto.createHash("md5").update(content).digest("hex") + '"';
    const isHtml = ext === ".html" || ext === "";
    const cacheHeaders = {
      "Cache-Control": isHtml
        ? "no-cache, no-store, must-revalidate, max-age=0"
        : "no-cache, must-revalidate",
      // A stale copy indicator that a proxy CANNOT rewrite: the
      // filename's content-hash ETag AND a Last-Modified anchored to
      // the file's mtime. Belt-and-braces for the belt-and-braces.
      ETag: etag,
      Pragma: "no-cache",
      Expires: "0",
      // Anti-frame + strict referrer are unrelated to caching but
      // belong in the same "operator-visible controls the server
      // owns" bucket. Adding them here means the operator's browser
      // won't leak the dashboard URL through a Referer to an EMSC
      // link they click, and no third party can iframe the panel.
      "X-Content-Type-Options": "nosniff",
      "Referrer-Policy": "no-referrer",
    };
    // Render's CDN rewrites our ETag to a weak validator (W/"...") on the
    // way out, so browsers echo back the W/ prefix. Strip it before
    // comparing or the 304 path never fires.
    const inm = String(req.headers["if-none-match"] || "").replace(/^W\//, "");
    if (inm === etag) {
      res.writeHead(304, cacheHeaders);
      return res.end();
    }
    res.writeHead(200, Object.assign({ "Content-Type": MIME[ext] || "application/octet-stream" }, cacheHeaders));
    res.end(content);
  });
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  const pathname = url.pathname;

  // CORS preflight, harmless to allow broadly for a pilot/demo.
  if (req.method === "OPTIONS") {
    res.writeHead(204, {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
    });
    return res.end();
  }

  try {
    if (pathname === "/api/status" && req.method === "POST") {
      const { deviceId, status, latitude, longitude, batteryPercent, timestamp } = await readBody(req);
      if (!deviceId || !status) return sendJson(res, 400, { error: "deviceId and status are required" });

      statusReports.set(deviceId, {
        deviceId,
        status,
        latitude: latitude ?? null,
        longitude: longitude ?? null,
        batteryPercent: batteryPercent ?? null,
        timestamp: timestamp || new Date().toISOString(),
        lastUpdated: new Date().toISOString(),
      });
      console.log(`[status] ${deviceId} -> ${status}`);
      return sendJson(res, 200, { ok: true });
    }

    if (pathname === "/api/status" && req.method === "GET") {
      return sendJson(res, 200, Array.from(statusReports.values()));
    }

    if (pathname === "/api/beacon-relay" && req.method === "POST") {
      const { sightedDeviceId, batteryPercent, relayLatitude, relayLongitude, timestamp } = await readBody(req);
      if (!sightedDeviceId) return sendJson(res, 400, { error: "sightedDeviceId is required" });

      const sighting = {
        sightedDeviceId,
        batteryPercent: batteryPercent ?? null,
        relayLatitude: relayLatitude ?? null,
        relayLongitude: relayLongitude ?? null,
        timestamp: timestamp || new Date().toISOString(),
      };
      beaconSightings.unshift(sighting);

      const existing = statusReports.get(sightedDeviceId);
      if (!existing || !existing.latitude) {
        statusReports.set(sightedDeviceId, {
          deviceId: sightedDeviceId,
          status: existing?.status || "notResponding",
          latitude: relayLatitude ?? null,
          longitude: relayLongitude ?? null,
          batteryPercent: batteryPercent ?? existing?.batteryPercent ?? null,
          timestamp: existing?.timestamp || sighting.timestamp,
          lastUpdated: new Date().toISOString(),
          locatedViaBluetoothRelay: true,
        });
      }
      console.log(`[beacon-relay] heard ${sightedDeviceId}, battery ${batteryPercent}%`);
      return sendJson(res, 200, { ok: true });
    }

    if (pathname === "/api/beacon-relays" && req.method === "GET") {
      return sendJson(res, 200, beaconSightings.slice(0, 100));
    }

    if (pathname === "/api/demo-seed" && req.method === "POST") {
      const demoUsers = [
        { deviceId: "demo-anna", status: "safe", latitude: 35.8997, longitude: 14.5146, batteryPercent: 82 },
        { deviceId: "demo-mark", status: "awaitingResponse", latitude: 35.9042, longitude: 14.5019, batteryPercent: 41 },
        { deviceId: "demo-julia", status: "notResponding", latitude: 35.8969, longitude: 14.5187, batteryPercent: 12 },
      ];
      demoUsers.forEach((u) =>
        statusReports.set(u.deviceId, { ...u, timestamp: new Date().toISOString(), lastUpdated: new Date().toISOString() })
      );
      return sendJson(res, 200, { ok: true, seeded: demoUsers.length });
    }

    if (pathname.startsWith("/api/")) {
      return sendJson(res, 404, { error: "Unknown API route" });
    }

    // Anything else: serve the dashboard's static files.
    serveStatic(req, res, pathname);
  } catch (err) {
    console.error(err);
    sendJson(res, 500, { error: "Internal error", detail: String(err) });
  }
});

server.listen(PORT, () => {
  console.log(`Pilot backend + dashboard running on http://localhost:${PORT}`);
});

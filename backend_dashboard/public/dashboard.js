/**
 * Dashboard logic. Polls the real QuakeGuard backend every 4 seconds.
 * Shows live device status: Safe, Trapped (with triage severity), Not Responding, Unknown.
 */

const DEVICES_ENDPOINT = "https://quake-alert-18.emergent.host/api/devices";

const map = L.map("map").setView([35.8997, 14.5146], 13); // default: Malta
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  attribution: "&copy; OpenStreetMap contributors",
}).addTo(map);

const markers = new Map();

function colorFor(status, severity) {
  if (status === "trapped") {
    if (severity === "red") return "#c62828";
    if (severity === "yellow") return "#f9a825";
    return "#2e7d32"; // green severity, or trapped with no severity yet
  }
  if (status === "safe") return "#2e7d32";
  if (status === "not_responding") return "#757575";
  return "#455a64"; // unknown
}

function statusLabel(status, severity) {
  if (status === "trapped") {
    if (severity === "red") return "TRAPPED — IMMEDIATE";
    if (severity === "yellow") return "Trapped — Delayed";
    if (severity === "green") return "Trapped — Minor";
    return "Trapped";
  }
  if (status === "safe") return "Safe";
  if (status === "not_responding") return "Not responding";
  return "Unknown";
}

function statusClass(status, severity) {
  if (status === "trapped") {
    if (severity === "red") return "trapped-red";
    if (severity === "yellow") return "trapped-yellow";
    return "trapped-green";
  }
  if (status === "safe") return "safe";
  if (status === "not_responding") return "danger";
  return "waiting";
}

function isUrgent(status, severity) {
  return status === "trapped" && severity === "red";
}

function normalizeDevice(d) {
  return {
    deviceId: d.device_id,
    status: d.status,
    severity: d.severity,
    latitude: d.latitude,
    longitude: d.longitude,
    batteryPercent: d.battery_pct,
    lastUpdated: d.updated_at,
    platform: d.platform,
  };
}

async function refresh() {
  try {
    const res = await fetch(DEVICES_ENDPOINT);
    const data = await res.json();
    const users = (data.devices || []).map(normalizeDevice);
    renderMap(users);
    renderSidebar(users);
  } catch (e) {
    console.error("Failed to refresh dashboard:", e);
  }
}

function renderMap(users) {
  const seen = new Set();
  users.forEach((u) => {
    if (u.latitude == null || u.longitude == null) return;
    seen.add(u.deviceId);
    const color = colorFor(u.status, u.severity);
    const urgent = isUrgent(u.status, u.severity);

    if (markers.has(u.deviceId)) {
      const m = markers.get(u.deviceId);
      m.setLatLng([u.latitude, u.longitude]);
      m.setStyle({ color, fillColor: color, weight: urgent ? 4 : 2 });
      m.setRadius(urgent ? 13 : 10);
      m.setPopupContent(popupHtml(u));
    } else {
      const marker = L.circleMarker([u.latitude, u.longitude], {
        radius: urgent ? 13 : 10,
        color,
        fillColor: color,
        fillOpacity: 0.85,
        weight: urgent ? 4 : 2,
      }).addTo(map);
      marker.bindPopup(popupHtml(u));
      if (urgent) {
        marker.bindTooltip("SOS", {
          permanent: true,
          direction: "center",
          className: "sos-label",
        });
      }
      markers.set(u.deviceId, marker);
    }
  });

  // Remove markers for devices no longer in the list
  for (const id of markers.keys()) {
    if (!seen.has(id)) {
      map.removeLayer(markers.get(id));
      markers.delete(id);
    }
  }
}

function popupHtml(u) {
  return `<b>${u.deviceId}</b><br>${statusLabel(u.status, u.severity)}<br>Battery: ${u.batteryPercent ?? "?"}%`;
}

function renderSidebar(users) {
  const list = document.getElementById("userlist");
  list.innerHTML = "";

  let safe = 0, trapped = 0, waiting = 0, danger = 0;

  users
    .slice()
    .sort((a, b) => {
      const rank = (u) =>
        isUrgent(u.status, u.severity) ? 0 :
        u.status === "trapped" ? 1 :
        u.status === "not_responding" ? 2 : 3;
      return rank(a) - rank(b);
    })
    .forEach((u) => {
      if (u.status === "trapped") trapped++;
      else if (u.status === "safe") safe++;
      else if (u.status === "not_responding") danger++;
      else waiting++;

      const li = document.createElement("li");
      li.className = statusClass(u.status, u.severity);
      li.innerHTML = `
        <div class="id">${u.deviceId}${isUrgent(u.status, u.severity) ? '<span class="badge sos">SOS</span>' : ""}</div>
        <div class="meta">${statusLabel(u.status, u.severity)} · Battery ${u.batteryPercent ?? "?"}%</div>
        <div class="meta">Updated: ${u.lastUpdated ? new Date(u.lastUpdated).toLocaleTimeString() : "—"}</div>
      `;
      list.appendChild(li);
    });

  document.getElementById("count-safe").textContent = safe;
  const trappedEl = document.getElementById("count-trapped");
  if (trappedEl) trappedEl.textContent = trapped;
  document.getElementById("count-waiting").textContent = waiting;
  document.getElementById("count-danger").textContent = danger;
}

async function seedDemoData() {
  await fetch("/api/demo-seed", { method: "POST" });
  refresh();
}

refresh();
setInterval(refresh, 4000);

(function initQuakeGuardTriggerButton() {
  // === EDIT THIS ==========================================================
  var QUAKEGUARD_BACKEND = "https://quake-alert-18.emergent.host"; // no trailing /
  // ========================================================================

  var btn    = document.getElementById("qg-trigger-btn");
  var status = document.getElementById("qg-trigger-status");
  if (!btn || !status) {
    // Button not on this page — nothing to wire up.
    return;
  }

  function setStatus(msg, kind) {
    status.textContent = msg || "";
    status.className = kind || "";
  }

  btn.addEventListener("click", async function onTriggerClick() {
    var confirmed = window.confirm(
      "Broadcast an EARTHQUAKE ALERT to every registered device?\n\n" +
      "This will push a notification to all installed apps and flip their " +
      "dashboard status to 'not responding' until they mark themselves safe."
    );
    if (!confirmed) return;

    var pwd = window.prompt("Enter emergency personnel password:");
    if (!pwd) { setStatus("Cancelled.", "info"); return; }

    btn.disabled = true;
    setStatus("Broadcasting…", "info");

    try {
      var res = await fetch(QUAKEGUARD_BACKEND + "/api/trigger-alert", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Admin-Token": pwd
        },
        body: JSON.stringify({
          triggeredBy: "dashboard",
          magnitude: 6.4,
          distance_km: 12,
          intensity: "VII"
        })
      });

      if (res.status === 401) {
        setStatus("Wrong password. Alert not sent.", "err");
        return;
      }
      if (!res.ok) {
        setStatus("Server error (" + res.status + "). Alert not sent.", "err");
        return;
      }

      var data = await res.json();
      var n = (data && typeof data.recipients === "number") ? data.recipients : "?";
      var delivered = data && data.push_delivered;
      setStatus(
        "Alert broadcast to " + n + " device" + (n === 1 ? "" : "s") +
          (delivered === false
            ? " (push queued — check EMERGENT_PUSH_KEY on the backend if this persists)."
            : "."),
        "ok"
      );
    } catch (e) {
      setStatus("Network error: " + (e && e.message ? e.message : e), "err");
    } finally {
      btn.disabled = false;
    }
  });
})();

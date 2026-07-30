/**
 * Dashboard logic. Polls the real QuakeGuard backend every 4 seconds.
 * Shows live device status grouped by triage priority: Immediate (red),
 * Serious/Stable (yellow), Minor (green), and Other (safe / not responding / unknown).
 * The map can show all groups at once, or be filtered to one group at a time.
 */

const DEVICES_ENDPOINT = "https://quake-alert-18.emergent.host/api/devices";

const map = L.map("map").setView([35.8997, 14.5146], 13); // default: Malta
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  attribution: "&copy; OpenStreetMap contributors",
}).addTo(map);

const markers = new Map();
let lastUsers = [];
let currentFilter = "all"; // "all" | "red" | "yellow" | "green"

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
    if (severity === "yellow") return "Trapped — Serious/Stable";
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
    lastUsers = users;
    renderMap(users);
    applyFilterVisibility();
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

function matchesFilter(u) {
  if (currentFilter === "all") return true;
  if (u.status !== "trapped") return false;
  const sev = u.severity || "green";
  return sev === currentFilter;
}

function applyFilterVisibility() {
  lastUsers.forEach((u) => {
    const m = markers.get(u.deviceId);
    if (!m) return;
    const show = matchesFilter(u);
    if (show && !map.hasLayer(m)) map.addLayer(m);
    if (!show && map.hasLayer(m)) map.removeLayer(m);
  });
}

function setFilter(f) {
  currentFilter = f;
  applyFilterVisibility();
  document.querySelectorAll(".map-filter-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.filter === currentFilter);
  });
}
window.setFilter = setFilter;

function popupHtml(u) {
  return `<b>${u.deviceId}</b><br>${statusLabel(u.status, u.severity)}<br>Battery: ${u.batteryPercent ?? "?"}%`;
}

function itemHtml(u) {
  return `
    <div class="id">${u.deviceId}${isUrgent(u.status, u.severity) ? '<span class="badge sos">SOS</span>' : ""}</div>
    <div class="meta">${statusLabel(u.status, u.severity)} · Battery ${u.batteryPercent ?? "?"}%</div>
    <div class="meta">Updated: ${u.lastUpdated ? new Date(u.lastUpdated).toLocaleTimeString() : "—"}</div>
  `;
}

function buildGroup(title, cls, items, alwaysOpen) {
  const details = document.createElement("details");
  details.className = "triage-group " + cls;
  details.open = alwaysOpen || items.length > 0;

  const summary = document.createElement("summary");
  summary.innerHTML = `<span>${title}</span><span class="group-count">${items.length}</span>`;
  details.appendChild(summary);

  const ul = document.createElement("ul");
  ul.className = "userlist";

  if (items.length === 0) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "None";
    ul.appendChild(li);
  } else {
    items.forEach((u) => {
      const li = document.createElement("li");
      li.className = statusClass(u.status, u.severity);
      li.innerHTML = itemHtml(u);
      ul.appendChild(li);
    });
  }

  details.appendChild(ul);
  return details;
}

function renderSidebar(users) {
  const container = document.getElementById("userlist");
  container.innerHTML = "";

  const byRecency = (a, b) => new Date(b.lastUpdated || 0) - new Date(a.lastUpdated || 0);

  const red = users.filter((u) => u.status === "trapped" && u.severity === "red").sort(byRecency);
  const yellow = users.filter((u) => u.status === "trapped" && u.severity === "yellow").sort(byRecency);
  const green = users.filter((u) => u.status === "trapped" && (u.severity === "green" || !u.severity)).sort(byRecency);
  const other = users.filter((u) => u.status !== "trapped").sort(byRecency);

  container.appendChild(buildGroup("🔴 IMMEDIATE — seriously injured / can't move", "group-red", red, true));
  container.appendChild(buildGroup("🟡 SERIOUS — STABLE — hurt but stable", "group-yellow", yellow, true));
  container.appendChild(buildGroup("🟢 MINOR — walking wounded", "group-green", green, true));
  container.appendChild(buildGroup("⚪ Other — Safe / Not Responding / Unknown", "group-other", other, false));

  const safe = users.filter((u) => u.status === "safe").length;
  const trapped = red.length + yellow.length + green.length;
  const danger = users.filter((u) => u.status === "not_responding").length;
  const waiting = users.length - safe - trapped - danger;

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

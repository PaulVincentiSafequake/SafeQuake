"public"/**
 * Dashboard logic. Polls the backend every 4 seconds, this is
 * intentionally simple (no websockets) so it's easy for a developer
 * to read and swap for something more real-time later if needed.
 */

const map = L.map("map").setView([35.8997, 14.5146], 13); // default: Malta
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  attribution: "&copy; OpenStreetMap contributors",
}).addTo(map);

const markers = new Map();

function colorFor(status, locatedViaBluetoothRelay) {
  if (status === "safe") return "#2e7d32";
  if (status === "awaitingResponse") return "#f9a825";
  return "#c62828"; // notResponding / unknown
}

function statusLabel(status) {
  return { safe: "Safe", awaitingResponse: "Awaiting response", notResponding: "Not responding", unknown: "Unknown" }[status] || status;
}

function statusClass(status) {
  if (status === "safe") return "safe";
  if (status === "awaitingResponse") return "waiting";
  return "danger";
}

async function refresh() {
  try {
    const res = await fetch("/api/status");
    const users = await res.json();
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
    const color = colorFor(u.status);

    if (markers.has(u.deviceId)) {
      const m = markers.get(u.deviceId);
      m.setLatLng([u.latitude, u.longitude]);
      m.setStyle({ color, fillColor: color });
      m.setPopupContent(popupHtml(u));
    } else {
      const marker = L.circleMarker([u.latitude, u.longitude], {
        radius: 10,
        color,
        fillColor: color,
        fillOpacity: 0.85,
        weight: 2,
      }).addTo(map);
      marker.bindPopup(popupHtml(u));
      markers.set(u.deviceId, marker);
    }
  });

  // Remove markers for users no longer in the list
  for (const id of markers.keys()) {
    if (!seen.has(id)) {
      map.removeLayer(markers.get(id));
      markers.delete(id);
    }
  }
}

function popupHtml(u) {
  return `<b>${u.deviceId}</b><br>${statusLabel(u.status)}<br>Battery: ${u.batteryPercent ?? "?"}%` +
    (u.locatedViaBluetoothRelay ? "<br><i>Located via nearby phone's Bluetooth</i>" : "");
}

function renderSidebar(users) {
  const list = document.getElementById("userlist");
  list.innerHTML = "";

  let safe = 0, waiting = 0, danger = 0;

  users
    .slice()
    .sort((a, b) => (a.status === "notResponding" ? -1 : 1))
    .forEach((u) => {
      if (u.status === "safe") safe++;
      else if (u.status === "awaitingResponse") waiting++;
      else danger++;

      const li = document.createElement("li");
      li.className = statusClass(u.status);
      li.innerHTML = `
        <div class="id">${u.deviceId}${u.locatedViaBluetoothRelay ? '<span class="badge bt">BLUETOOTH</span>' : ""}</div>
        <div class="meta">${statusLabel(u.status)} · Battery ${u.batteryPercent ?? "?"}%</div>
        <div class="meta">Updated: ${new Date(u.lastUpdated).toLocaleTimeString()}</div>
      `;
      list.appendChild(li);
    });

  document.getElementById("count-safe").textContent = safe;
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

/**
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

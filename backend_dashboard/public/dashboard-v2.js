/**
 * Dashboard logic. Polls the real Quake Angel backend every 4 seconds.
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

function mobilityLabel(m) {
  if (m === "mobile") return "🟢 can move";
  if (m === "trapped") return "🔴 trapped/pinned";
  return "";
}

function normalizeDevice(d) {
  return {
    deviceId: d.device_id,
    status: d.status,
    severity: d.severity,
    mobility: d.mobility,
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
  const mob = mobilityLabel(u.mobility);
  return `<b>${u.deviceId}</b><br>${statusLabel(u.status, u.severity)}${mob ? "<br>" + mob : ""}<br>Battery: ${u.batteryPercent ?? "?"}%`;
}

function itemHtml(u) {
  const mob = mobilityLabel(u.mobility);
  return `
    <div class="id">${u.deviceId}${isUrgent(u.status, u.severity) ? '<span class="badge sos">SOS</span>' : ""}</div>
    <div class="meta">${statusLabel(u.status, u.severity)}${mob ? " · " + mob : ""} · Battery ${u.batteryPercent ?? "?"}%</div>
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

/* ── Trigger button: confirm + password + broadcast, with prominent feedback ── */
(function initQuakeGuardTriggerButton() {
  // === EDIT THIS ==========================================================
  var QUAKEGUARD_BACKEND = "https://quake-alert-18.emergent.host"; // no trailing /
  // ========================================================================

  var btn        = document.getElementById("qg-trigger-btn");
  var banner     = document.getElementById("qg-banner");
  var bannerText = banner && banner.querySelector(".qg-banner-text");
  var bannerIcon = banner && banner.querySelector(".qg-banner-icon");
  var bannerX    = banner && banner.querySelector(".qg-banner-close");
  var modal        = document.getElementById("qg-modal-backdrop");
  var modalOk      = modal && modal.querySelector(".qg-modal-ok");
  var modalCancel  = modal && modal.querySelector(".qg-modal-cancel");
  if (!btn || !banner || !modal) {
    // Not on a page with the trigger UI — nothing to wire up.
    return;
  }

  var bannerTimer = null;
  function showBanner(kind, text, autoDismissMs) {
    if (bannerTimer) { clearTimeout(bannerTimer); bannerTimer = null; }
    banner.classList.remove("ok", "err");
    banner.classList.add(kind);
    bannerIcon.textContent = kind === "ok" ? "✓" : "!";
    bannerText.textContent = text;
    void banner.offsetWidth;
    banner.classList.add("show");
    if (autoDismissMs && autoDismissMs > 0) {
      bannerTimer = setTimeout(hideBanner, autoDismissMs);
    }
  }
  function hideBanner() {
    banner.classList.remove("show");
    if (bannerTimer) { clearTimeout(bannerTimer); bannerTimer = null; }
  }
  bannerX.addEventListener("click", hideBanner);

  function showWrongPasswordModal() {
    modal.classList.add("show");
    setTimeout(function () { modalOk && modalOk.focus(); }, 50);
  }
  function hideModal() { modal.classList.remove("show"); }

  // "Try Again" re-prompts for the password immediately — no need to
  // re-confirm the broadcast itself, they already agreed to that once.
  // "Cancel" just closes the modal; nothing is sent.
  modalOk.addEventListener("click", function () {
    hideModal();
    promptForPasswordAndSend();
  });
  modalCancel && modalCancel.addEventListener("click", function () {
    hideModal();
    showBanner("err", "Cancelled — no alert sent.", 4000);
  });
  modal.addEventListener("click", function (e) {
    if (e.target === modal) hideModal();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && modal.classList.contains("show")) hideModal();
  });

  async function promptForPasswordAndSend() {
    var pwd = window.prompt("Enter emergency personnel password:");
    if (!pwd) { showBanner("err", "Cancelled — no alert sent.", 4000); return; }

    btn.disabled = true;
    showBanner("ok", "Broadcasting alert…", 0);

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
        hideBanner();
        showWrongPasswordModal();
        return;
      }
      if (!res.ok) {
        showBanner("err", "Server error (" + res.status + "). Alert NOT sent.");
        return;
      }

      var data = await res.json();
      var n = (data && typeof data.recipients === "number") ? data.recipients : 0;
      var delivered = data && data.push_delivered;
      var text =
        "ALERT BROADCAST — " + n + " device" + (n === 1 ? "" : "s") +
        (delivered === false ? " (delivery issue — check /api/admin/last-push-events)" : "");
      showBanner(delivered === false ? "err" : "ok", text, delivered === false ? 0 : 6000);
    } catch (e) {
      showBanner("err", "Network error: " + (e && e.message ? e.message : e));
    } finally {
      btn.disabled = false;
    }
  }

  btn.addEventListener("click", function onTriggerClick() {
    var confirmed = window.confirm(
      "Broadcast an EARTHQUAKE ALERT to every registered device?\n\n" +
      "This will push a notification to all installed apps and flip their " +
      "dashboard status to 'not responding' until they mark themselves safe."
    );
    if (!confirmed) return;
    promptForPasswordAndSend();
  });
})();

/* ── Recent activity / audit log widget ── */
(function initQuakeGuardAuditLog() {
  var QUAKEGUARD_BACKEND = "https://quake-alert-18.emergent.host"; // no trailing /
  var LIMIT = 100;
  var POLL_MS = 10000;

  var body = document.getElementById("qg-audit-body");
  var meta = document.getElementById("qg-audit-meta");
  if (!body || !meta) return;

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function sevColor(s) {
    if (s === "red")    return "#C21818";
    if (s === "yellow") return "#EA9500";
    if (s === "green")  return "#2E7D32";
    return "#666";
  }
  function mobBadge(m) {
    if (m === "mobile")  return '<span class="qg-badge" style="background:#2E7D32">can move</span>';
    if (m === "trapped") return '<span class="qg-badge" style="background:#C21818">trapped/pinned</span>';
    return "";
  }

  function formatEvent(e) {
    if (e.kind === "trigger") {
      var delivered = !!e.delivered;
      var badge =
        '<span class="qg-badge" style="background:' +
        (delivered ? "#1F8A3A" : "#C21818") + '">' +
        (delivered ? "delivered" : "FAILED") + "</span>";
      var mag = e.magnitude != null ? "M" + esc(e.magnitude) : "M?";
      var counts =
        "iOS " + (e.ios_count || 0) + " · Android " + (e.android_count || 0);
      var err = e.error
        ? '<div style="color:#c21818;font-size:12px;margin-top:4px"><b>Error:</b> ' + esc(e.error) + "</div>"
        : "";
      return (
        '<div class="qg-audit-row trigger">' +
          '<div class="qg-audit-title">' +
            '⚠️ TRIGGER ' + badge +
            ' · ' + mag + ' · ' + esc(e.recipients_total || 0) + ' device' + (e.recipients_total === 1 ? '' : 's') +
            ' (' + counts + ')' +
            ' · by <code>' + esc(e.triggered_by || "dashboard") + '</code>' +
          '</div>' +
          err +
          '<div class="qg-audit-at">' + esc(e.at || "") + '</div>' +
        '</div>'
      );
    }
    // status
    var sev = e.severity
      ? '<span class="qg-badge" style="background:' + sevColor(e.severity) + '">' + esc(e.severity) + "</span>"
      : "";
    var mob = mobBadge(e.mobility);
    var loc = "";
    if (e.latitude != null && e.longitude != null) {
      loc = ' · <a href="https://www.google.com/maps/place/' +
            encodeURIComponent(e.latitude + "," + e.longitude) +
            '" target="_blank" rel="noopener">📍 map</a>';
    }
    var bat = e.battery_pct != null ? " · 🔋 " + esc(e.battery_pct) + "%" : "";
    return (
      '<div class="qg-audit-row status">' +
        '<div class="qg-audit-title">' +
          '📱 STATUS · <code>' + esc(e.device_id || "") + "</code> → " +
          '<b>' + esc(e.status || "") + '</b>' + sev + mob + loc + bat +
        '</div>' +
        '<div class="qg-audit-at">' + esc(e.at || "") + '</div>' +
      '</div>'
    );
  }

  async function refreshAudit() {
    try {
      var res = await fetch(QUAKEGUARD_BACKEND + "/api/audit?limit=" + LIMIT, {
        cache: "no-store"
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      var data = await res.json();
      var events = (data && data.events) || [];
      if (events.length === 0) {
        body.innerHTML = '<div class="qg-audit-empty">No activity yet.</div>';
      } else {
        body.innerHTML = events.map(formatEvent).join("");
      }
      meta.textContent = events.length + " events · updated " + new Date().toLocaleTimeString();
    } catch (e) {
      meta.textContent = "load error: " + (e && e.message ? e.message : e);
    }
  }

  refreshAudit();
  setInterval(refreshAudit, POLL_MS);
})();

"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = {
  sessionId: null,
  zone: "floor",
  capture: null,     // current capture detail in review
  editing: null,     // slot being edited (or {new:true})
  chosenSku: null,
};

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch { /* not json */ }
    throw new Error(msg);
  }
  return res.headers.get("content-type")?.includes("json") ? res.json() : res.text();
}
const jsonOpts = (method, body) => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

function store(key, val) { try { localStorage.setItem(key, val); } catch { /* storage blocked */ } }
function recall(key) { try { return localStorage.getItem(key); } catch { return null; } }

/* ------------------------------------------------------------ navigation */

const TAB_OF = { aisle: "walk" };
function show(view) {
  $$(".view").forEach((v) => (v.hidden = v.id !== `view-${view}`));
  const tab = view === "review" ? (TAB_OF[state.returnTo] || state.returnTo || "scan") : (TAB_OF[view] || view);
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.view === tab));
  if (view !== "review") state.currentView = view;
  window.scrollTo(0, 0);
  if (view === "report") loadReport();
  if (view === "catalog") searchCatalog();
  if (view === "scan") loadCaptures();
  if (view === "walk" && window.loadAisles) window.loadAisles();
  if (view === "aisle" && window.loadAisle) window.loadAisle();
}
$$(".tabs button").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));

/* ------------------------------------------------------------ sessions */

async function loadSessions(selectId) {
  let sessions = await api("/api/sessions");
  if (!sessions.length) {
    await api("/api/sessions", jsonOpts("POST", {}));
    sessions = await api("/api/sessions");
  }
  const remembered = Number(recall("sessionId"));
  state.sessionId = selectId || (sessions.some((s) => s.id === remembered) ? remembered : sessions[0].id);
  $("#sessionSelect").innerHTML = sessions
    .map((s) => `<option value="${s.id}" ${s.id === state.sessionId ? "selected" : ""}>${esc(s.name)} · ${s.bays} bays</option>`)
    .join("");
  store("sessionId", state.sessionId);
}
$("#sessionSelect").addEventListener("change", (e) => {
  state.sessionId = Number(e.target.value);
  store("sessionId", state.sessionId);
  show("scan");
});
$("#newSessionBtn").addEventListener("click", async () => {
  const name = prompt("Name this count", `Count ${new Date().toISOString().slice(0, 10)}`);
  if (name === null) return;
  const s = await api("/api/sessions", jsonOpts("POST", { name }));
  await loadSessions(s.id);
  show("scan");
});

/* ------------------------------------------------------------ scan view */

$$(".seg-btn").forEach((b) =>
  b.addEventListener("click", () => {
    state.zone = b.dataset.zone;
    $$(".seg-btn").forEach((x) => x.classList.toggle("active", x === b));
    $("#zoneHint").textContent = state.zone === "floor"
      ? "Stand back so the whole bay (all shelves, both edges) is in frame. Shelf tags should be readable."
      : "Photograph one rack or stack at a time with the printed carton side (brand, 12/750ML) facing you.";
  })
);

function setStatus(el, msg, kind = "") {
  el.hidden = !msg;
  el.className = `status ${kind}`;
  el.textContent = msg || "";
}

$("#photoInput").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  const aisle = $("#aisle").value.trim(), bay = $("#bay").value.trim();
  if (!aisle || !bay) return setStatus($("#scanStatus"), "Enter aisle and bay first.", "error");
  const fd = new FormData();
  fd.append("image", file);
  fd.append("zone", state.zone);
  fd.append("aisle", aisle);
  fd.append("bay", bay);
  setStatus($("#scanStatus"), "Reading labels and counting bottles… this can take up to a minute.");
  try {
    const cap = await api(`/api/sessions/${state.sessionId}/captures`, { method: "POST", body: fd });
    setStatus($("#scanStatus"), "");
    openReview(cap);
    loadSessions(state.sessionId);
  } catch (err) {
    setStatus($("#scanStatus"), `Could not process photo: ${err.message}`, "error");
  }
});

async function loadCaptures() {
  if (!state.sessionId) return;
  const caps = await api(`/api/sessions/${state.sessionId}/captures`);
  $("#captureList").innerHTML = caps.length
    ? caps.map((c) => `
      <li class="clickable" data-id="${c.id}">
        <div class="grow"><b>${esc(c.code)}</b> <span class="meta">${c.zone === "floor" ? "Floor" : "Back room"} · ${c.items} items</span></div>
        ${c.status === "confirmed" ? '<span class="badge ok">Confirmed</span>'
          : c.needs_review ? `<span class="badge warn">${c.needs_review} to check</span>` : '<span class="badge">Ready</span>'}
      </li>`).join("")
    : '<li class="empty">No bays yet. Photograph your first bay above.</li>';
  $$("#captureList li[data-id]").forEach((li) => li.addEventListener("click", async () => openReview(await api(`/api/captures/${li.dataset.id}`))));
}

/* ------------------------------------------------------------ review view */

function slotClass(s) {
  if (s.duplicate_of) return "dup";
  if (!s.sku_id) return "bad";
  if (s.review === "needs_review") return "warn";
  return "ok";
}
function qtyText(s) {
  if (s.duplicate_of) return "overlap · counted in previous snapshot";
  if (s.kind === "case") {
    const pack = s.units_per_case || s.case_pack || "?";
    return `${s.cases} cs × ${pack}${s.partial_units ? ` + ${s.partial_units}` : ""} = ${s.units}`;
  }
  return `${s.facings} × ${s.depth} deep = ${s.units}`;
}

function openReview(cap, from) {
  state.capture = cap;
  state.returnTo = from || state.currentView || "scan";
  renderReview();
  show("review");
}

function renderReview() {
  const cap = state.capture;
  $("#reviewTitle").textContent = `${cap.code} · ${cap.zone === "floor" ? "Sales floor" : "Back room"}`;
  const units = cap.slots.reduce((a, s) => a + s.units, 0);
  const modeName = { photo: "photo", map: `map snapshot #${cap.seq}`, count: "count run" }[cap.mode] || "photo";
  $("#reviewMeta").textContent = `${modeName} · ${cap.slots.length - (cap.duplicates || 0)} items · ${units} bottles counted`
    + `${cap.duplicates ? ` · ${cap.duplicates} overlap` : ""} · read by ${cap.recognizer === "mock" ? "demo recognizer" : "Claude vision"}`;
  const badge = $("#reviewBadge");
  if (cap.status === "confirmed") { badge.className = "badge ok"; badge.textContent = "Confirmed"; }
  else if (cap.needs_review) { badge.className = "badge warn"; badge.textContent = `${cap.needs_review} to check`; }
  else { badge.className = "badge"; badge.textContent = "Ready to confirm"; }

  const warn = [...(cap.image_quality && cap.image_quality !== "good" ? [`Photo quality: ${cap.image_quality}`] : []), ...cap.warnings];
  $("#reviewWarnings").innerHTML = warn.map((w) => `<div class="status error">${esc(w)}</div>`).join("");

  const img = $("#reviewImg");
  img.src = `/api/captures/${cap.id}/image`;
  $("#boxLayer").innerHTML = cap.slots.filter((s) => s.box).map((s) => {
    const b = s.box;
    return `<div class="box ${slotClass(s)}" data-id="${s.id}" style="left:${b.x0 * 100}%;top:${b.y0 * 100}%;width:${(b.x1 - b.x0) * 100}%;height:${(b.y1 - b.y0) * 100}%">
      <span>${s.shelf}.${s.position}</span></div>`;
  }).join("");

  const shelves = [...new Set(cap.slots.map((s) => s.shelf))].sort((a, b) => a - b);
  $("#blueprint").innerHTML = shelves.length ? shelves.map((sh) => `
    <div class="shelf">
      <div class="shelf-label">S${sh}</div>
      <div class="shelf-slots">
        ${cap.slots.filter((s) => s.shelf === sh).map((s) => `
          <button class="slot ${slotClass(s)}" data-id="${s.id}" style="flex-grow:${Math.max(1, s.kind === "case" ? 2 : s.facings)}">
            <span class="num">${sh}.${s.position}${s.kind === "case" ? " · case" : ""}</span>
            <span class="name">${esc(s.label)}</span>
            <span class="sz">${esc(s.size_label)}</span>
            <span class="qty">${esc(qtyText(s))}</span>
            ${s.note ? `<span class="note">${esc(s.note)}</span>` : ""}
          </button>`).join("")}
      </div>
    </div>`).join("") : '<div class="empty">Nothing detected. Add items manually or rescan.</div>';

  $$("#blueprint .slot, #boxLayer .box").forEach((el) => el.addEventListener("click", () => editSlot(Number(el.dataset.id))));
  renderDiff(cap);
  $("#rescanBtn").hidden = Boolean(cap.mode && cap.mode !== "photo");
  $("#confirmBtn").disabled = cap.status === "confirmed";
  $("#confirmBtn").textContent = cap.status === "confirmed" ? "Confirmed ✓" : "Confirm bay";
}

function renderDiff(cap) {
  if (!cap.has_blueprint) {
    $("#diffBox").innerHTML = '<p class="hint">First scan of this bay — confirming it saves this layout as the bay\'s blueprint.</p>';
    return;
  }
  const label = { same: ["ok", "Same"], moved: ["warn", "Moved"], qty_change: ["warn", "Facings changed"], missing: ["bad", "Missing"], new: ["warn", "New here"] };
  const changes = cap.diff.filter((d) => d.status !== "same");
  $("#diffBox").innerHTML = `<div class="diff"><h2>Compared with saved blueprint</h2>${changes.length ? `<ul class="list">${changes.map((d) => `
    <li><span class="badge ${label[d.status][0]}">${label[d.status][1]}</span><span class="grow">${esc(d.label)}</span>
    <span class="meta">${d.status === "moved" ? `S${d.shelf} → S${d.now_shelf}` : d.status === "qty_change" ? `${d.expected_facings} → ${d.facings} facings` : d.status === "missing" ? `was S${d.shelf}` : `S${d.now_shelf}`}</span></li>`).join("")}</ul>`
    : '<p class="hint">Layout matches the blueprint.</p>'}</div>`;
}

$("#backBtn").addEventListener("click", () => show(state.returnTo || "scan"));
$("#rescanBtn").addEventListener("click", () => {
  const cap = state.capture;
  state.zone = cap.zone;
  $$(".seg-btn").forEach((x) => x.classList.toggle("active", x.dataset.zone === cap.zone));
  $("#aisle").value = cap.aisle;
  $("#bay").value = cap.bay;
  show("scan");
  $("#photoInput").click();
});
$("#confirmBtn").addEventListener("click", async () => {
  try {
    state.capture = await api(`/api/captures/${state.capture.id}/confirm`, { method: "POST" });
    renderReview();
    if (state.capture.mode && state.capture.mode !== "photo") return show(state.returnTo || "walk");
    // advance to the next bay for a fast walk-through
    const n = Number($("#bay").value);
    if (String(state.capture.bay) === String(n) && Number.isFinite(n)) $("#bay").value = n + 1;
    setStatus($("#scanStatus"), `${state.capture.code} confirmed. Next: bay ${$("#bay").value}.`, "ok");
    show("scan");
  } catch (err) {
    alert(err.message);
  }
});

/* ------------------------------------------------------------ slot editor */

const dlg = $("#slotDialog");

function editSlot(id) {
  const s = state.capture.slots.find((x) => x.id === id);
  if (!s) return;
  state.editing = s;
  state.chosenSku = s.sku_id ? { id: s.sku_id, label: `${s.brand} ${s.name}`, size: s.size_label, case_pack: s.case_pack } : null;
  $$(".box, .slot").forEach((el) => el.classList.toggle("sel", Number(el.dataset.id) === id));
  $("#slotTitle").textContent = `Shelf ${s.shelf}, position ${s.position}`;
  $("#slotRead").textContent = s.source === "manual" ? "Added manually"
    : `Camera read: "${[s.raw_brand, s.raw_name, s.raw_size].filter(Boolean).join(" ")}" · vision ${Math.round(s.confidence * 100)}% · match ${Math.round(s.match_score * 100)}%`;
  $("#kindSwitch").hidden = true;
  fillForm(s.kind, s);
  $("#skuSearch").value = "";
  renderOptions(s.candidates.map((c) => ({ id: c.sku_id, label: c.label, score: c.score })));
  renderRefs(s);
  $("#slotDelete").hidden = false;
  dlg.showModal();
}

function newSlot() {
  state.editing = { new: true, kind: state.capture.zone === "backroom" ? "case" : "bottle" };
  state.chosenSku = null;
  $("#slotTitle").textContent = "Add item";
  $("#slotRead").textContent = "Search by name or type/scan the UPC.";
  $("#kindSwitch").hidden = false;
  $$("#kindSwitch input").forEach((r) => (r.checked = r.value === state.editing.kind));
  fillForm(state.editing.kind, { shelf: 1, facings: 1, depth: 1, cases: 1, partial_units: 0 });
  $("#skuSearch").value = "";
  renderOptions([]);
  renderRefs(null);
  $("#slotDelete").hidden = true;
  dlg.showModal();
  $("#skuSearch").focus();
}
$("#addItemBtn").addEventListener("click", newSlot);
$$("#kindSwitch input").forEach((r) => r.addEventListener("change", () => { state.editing.kind = r.value; toggleKind(r.value); updateTotal(); }));

function toggleKind(kind) {
  $$(".kind-bottle").forEach((el) => (el.hidden = kind !== "bottle"));
  $$(".kind-case").forEach((el) => (el.hidden = kind !== "case"));
}
function fillForm(kind, s) {
  toggleKind(kind);
  $("#fShelf").value = s.shelf ?? 1;
  $("#fFacings").value = s.facings ?? 0;
  $("#fDepth").value = s.depth ?? 1;
  $("#fCases").value = s.cases ?? 0;
  $("#fPack").value = s.units_per_case || s.case_pack || "";
  $("#fPartial").value = s.partial_units ?? 0;
  renderChosen();
  updateTotal();
}
async function renderRefs(slot) {
  const box = $("#refPhotos");
  const figs = [];
  if (slot && slot.has_crop) figs.push(`<figure><img src="/api/slots/${slot.id}/crop" alt=""><figcaption>this photo</figcaption></figure>`);
  const sku = state.chosenSku && state.chosenSku.id;
  if (sku) {
    const refs = await api(`/api/skus/${sku}/references?limit=5`).catch(() => []);
    refs.filter((r) => !slot || r.slot_id !== slot.id).forEach((r) =>
      figs.push(`<figure><img src="/api/slots/${r.slot_id}/crop" alt=""><figcaption>${r.review === "confirmed" ? "saved" : "earlier"}</figcaption></figure>`));
  }
  box.innerHTML = figs.join("");
}

function renderChosen() {
  const c = state.chosenSku;
  $("#skuChosen").innerHTML = c ? `✓ ${esc(c.label)} · ${esc(c.size)}` : '<span class="meta">No product selected</span>';
  if (c && !$("#fPack").value && c.case_pack) $("#fPack").value = c.case_pack;
}
function updateTotal() {
  const kind = state.editing?.kind;
  const n = (id) => Number($(id).value) || 0;
  const total = kind === "case" ? n("#fCases") * n("#fPack") + n("#fPartial") : n("#fFacings") * Math.max(1, n("#fDepth"));
  $("#slotTotal").textContent = `= ${total} bottles`;
}
$$("#slotForm input[type=number]").forEach((i) => i.addEventListener("input", updateTotal));

function renderOptions(opts) {
  $("#skuOptions").innerHTML = opts.map((o) => `
    <li data-id="${o.id}" data-label="${esc(o.label)}" data-size="${esc(o.size || "")}" data-pack="${o.case_pack || ""}" class="${state.chosenSku?.id === o.id ? "active" : ""}">
      <span>${esc(o.label)}${o.size ? ` · ${esc(o.size)}` : ""}</span>
      <small>${o.score != null ? `${Math.round(o.score * 100)}% match` : o.upc ? esc(o.upc) : ""}</small>
    </li>`).join("");
  $$("#skuOptions li").forEach((li) => li.addEventListener("click", () => {
    state.chosenSku = { id: Number(li.dataset.id), label: li.dataset.label, size: li.dataset.size, case_pack: Number(li.dataset.pack) || null };
    $$("#skuOptions li").forEach((x) => x.classList.toggle("active", x === li));
    renderChosen();
    renderRefs(state.editing && !state.editing.new ? state.editing : null);
    updateTotal();
  }));
}

let searchTimer;
$("#skuSearch").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  const q = e.target.value.trim();
  searchTimer = setTimeout(async () => {
    if (!q) return renderOptions(state.editing?.candidates?.map((c) => ({ id: c.sku_id, label: c.label, score: c.score })) || []);
    const res = await api(`/api/catalog?q=${encodeURIComponent(q)}&limit=20`);
    renderOptions(res.map((r) => ({ id: r.id, label: `${r.brand} ${r.name}`, size: r.size, upc: r.upc, case_pack: r.case_pack })));
  }, 200);
});

$("#slotCancel").addEventListener("click", () => dlg.close());
$("#slotDelete").addEventListener("click", async () => {
  if (!confirm("Remove this item from the count?")) return;
  state.capture = await api(`/api/slots/${state.editing.id}`, { method: "DELETE" });
  dlg.close();
  renderReview();
});
$("#slotSave").addEventListener("click", async () => {
  const e = state.editing;
  const num = (id) => Number($(id).value);
  try {
    if (e.new) {
      if (!state.chosenSku) return alert("Pick a product first.");
      state.capture = await api(`/api/captures/${state.capture.id}/slots`, jsonOpts("POST", {
        sku_id: state.chosenSku.id, kind: e.kind, shelf: num("#fShelf") || 1,
        facings: num("#fFacings"), depth: num("#fDepth") || 1, cases: num("#fCases"), partial_units: num("#fPartial"),
      }));
    } else {
      const body = { shelf: num("#fShelf") || 1 };
      if (state.chosenSku) body.sku_id = state.chosenSku.id;
      if (e.kind === "case") Object.assign(body, { cases: num("#fCases"), units_per_case: num("#fPack") || null, partial_units: num("#fPartial") });
      else Object.assign(body, { facings: num("#fFacings"), depth: num("#fDepth") || 1 });
      state.capture = await api(`/api/slots/${e.id}`, jsonOpts("PATCH", body));
    }
    dlg.close();
    renderReview();
  } catch (err) {
    alert(err.message);
  }
});

/* ------------------------------------------------------------ report */

async function loadReport() {
  const r = await api(`/api/sessions/${state.sessionId}/report`);
  $("#csvLink").href = `/api/sessions/${state.sessionId}/report.csv`;
  $("#kpis").innerHTML = [
    ["Bottles total", r.totals.units], ["Line items", r.totals.skus], ["Loose bottles", r.totals.loose], ["Sealed cases", r.totals.cases],
  ].map(([k, v]) => `<div class="kpi"><b>${v.toLocaleString()}</b><small>${k}</small></div>`).join("");
  if (!r.items.length) {
    $("#reportBody").innerHTML = '<p class="empty">Nothing counted yet in this session.</p>';
    return;
  }
  const unconf = r.captures.review ? `<p class="status">${r.captures.review} bay(s) not confirmed yet — their numbers are included but may change.</p>` : "";
  $("#reportBody").innerHTML = `${unconf}
    <div class="table-wrap"><table class="report-table">
      <thead><tr><th>Item / size</th><th>Floor</th><th>Back loose</th><th>Cases</th><th>Case btl</th><th>Total</th></tr></thead>
      <tbody>${r.products.map((p) => `
        <tr class="product"><td colspan="5">${esc(p.brand)} ${esc(p.name)}</td><td>${p.total_units}</td></tr>
        ${p.sizes.map((i) => `<tr class="size"><td>${esc(i.size)}${i.unconfirmed ? " *" : ""}<div class="meta">${esc(i.locations.join(", "))}</div></td>
          <td>${i.loose_floor}</td><td>${i.loose_backroom}</td><td>${i.cases}${i.cases ? `×${i.case_pack ?? "?"}` : ""}</td><td>${i.case_units}</td><td><b>${i.total_units}</b></td></tr>`).join("")}`).join("")}
      </tbody></table></div>
    ${r.unidentified.length ? `<h2 style="margin-top:1rem">Not identified (${r.unidentified.length})</h2><ul class="list">${r.unidentified.map((u) => `<li><span class="grow">${esc(u.read || "unknown")}</span><span class="meta">${esc(u.code)} · ${u.units} btl</span></li>`).join("")}</ul>` : ""}`;
}

/* ------------------------------------------------------------ catalog */

async function searchCatalog() {
  const q = $("#catalogSearch").value.trim();
  const res = await api(`/api/catalog?q=${encodeURIComponent(q)}&limit=100`);
  $("#catalogList").innerHTML = res.length
    ? res.map((r) => `<li><div class="grow"><b>${esc(r.brand)}</b> ${esc(r.name)}<div class="meta">${esc(r.upc || "no UPC")} · ${esc(r.category || "")}</div></div>
       <span class="badge">${esc(r.size)}</span><span class="meta">${r.case_pack ? `${r.case_pack}/cs` : ""}</span></li>`).join("")
    : '<li class="empty">No products. Import your POS catalog or load the sample.</li>';
}
let catTimer;
$("#catalogSearch").addEventListener("input", () => { clearTimeout(catTimer); catTimer = setTimeout(searchCatalog, 200); });
async function afterImport(res) {
  setStatus($("#catalogMsg"), `Imported: ${res.added} added, ${res.updated} updated, ${res.skipped} skipped.`, "ok");
  searchCatalog();
  refreshStatus();
}
$("#sampleBtn").addEventListener("click", async () => afterImport(await api("/api/catalog/load-sample", { method: "POST" })));
$("#csvInput").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  e.target.value = "";
  if (!f) return;
  const fd = new FormData();
  fd.append("file", f);
  try { afterImport(await api("/api/catalog/import", { method: "POST", body: fd })); }
  catch (err) { setStatus($("#catalogMsg"), err.message, "error"); }
});

/* ------------------------------------------------------------ QR location labels */

if ("BarcodeDetector" in window) $("#qrBtn").hidden = false;
let qrStream;
$("#qrBtn").addEventListener("click", async () => {
  const dlgQr = $("#qrDialog");
  try {
    qrStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } });
  } catch (err) {
    return alert(`Camera unavailable: ${err.message}`);
  }
  const video = $("#qrVideo");
  video.srcObject = qrStream;
  await video.play();
  dlgQr.showModal();
  const detector = new BarcodeDetector({ formats: ["qr_code"] });
  const tick = async () => {
    if (!dlgQr.open) return;
    const codes = await detector.detect(video).catch(() => []);
    const m = codes[0]?.rawValue?.match(/^([FB])-([^-]+)-([^-]+)$/i);
    if (m) {
      state.zone = m[1].toUpperCase() === "F" ? "floor" : "backroom";
      $$(".seg-btn").forEach((x) => x.classList.toggle("active", x.dataset.zone === state.zone));
      $("#aisle").value = m[2];
      $("#bay").value = m[3];
      dlgQr.close();
      return;
    }
    requestAnimationFrame(tick);
  };
  tick();
});
$("#qrDialog").addEventListener("close", () => qrStream?.getTracks().forEach((t) => t.stop()));
$("#qrClose").addEventListener("click", () => $("#qrDialog").close());

/* ------------------------------------------------------------ boot */

async function refreshStatus() {
  const st = await api("/api/status");
  const banner = $("#modeBanner");
  const msgs = [];
  if (st.recognizer === "mock") msgs.push("Demo mode: no Claude API key configured, photos return a sample scan.");
  if (!st.catalog_size) msgs.push("Catalog is empty — load it on the Catalog tab first.");
  banner.hidden = !msgs.length;
  banner.textContent = msgs.join(" ");
}

(async function boot() {
  await refreshStatus();
  await loadSessions();
  show("walk");
})();

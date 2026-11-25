"use strict";
/* Walk-through camera engine.
 *
 * Map run  (run 1): walk along the shelf with the camera on. The engine watches the video, and every time
 *   the camera has moved about half a frame AND the picture is steady and sharp, it takes a full-resolution
 *   snapshot and uploads it in the background. Each snapshot becomes a numbered section of the aisle; the
 *   server identifies the bottles, removes overlaps between neighbouring snapshots, and saves reference
 *   photos. Confirming the aisle saves it as the map (the "database").
 * Count run (run 2): point the camera at the shelf. When it is steady it snapshots automatically (or tap
 *   the shutter); the server finds which mapped section it is and counts each mapped slot.
 *
 * On-device bottle detection (COCO-SSD "bottle" class, TensorFlow.js) draws live boxes and a bottle count.
 * It is guidance only; if it can't load, snapshots still work from motion + sharpness.
 */

(function () {
  const AW = 96, AH = 72;              // analysis resolution
  const MAX_SHIFT = 14;                // px searched per analysis frame (at AW width)
  const MAP_TRAVEL = 0.5;              // fraction of the frame to move before the next map snapshot (~50% overlap)
  const COUNT_TRAVEL = 0.3;
  const UPLOADS = 3;                   // parallel uploads

  const w = {
    mode: "map", running: false, auto: true, stream: null, wakeLock: null,
    prev: null, travel: 0, steadySince: 0, sharpRef: 0, lastMotion: 0, ready: false,
    seq: 0, shots: 0, queue: [], active: 0, done: 0, failed: 0,
    detector: null, detecting: false, bottles: [], lastDetect: 0, detectMs: 100,
    zone: "floor", aisle: "1", sessionId: null,
  };
  window.walkState = w;

  const ana = document.createElement("canvas");
  ana.width = AW; ana.height = AH;
  const actx = ana.getContext("2d", { willReadFrequently: true });

  /* ---------------------------------------------------------- setup screen */

  const hints = {
    map: "Run 1 — Hold the phone sideways, point it at the shelf from about 4–6 ft away and walk slowly along the aisle. "
      + "Snapshots are taken automatically about every half frame. Afterwards, review and confirm the aisle map.",
    count: "Run 2 — Uses the confirmed map. Point the camera at a section and hold still; it counts automatically. "
      + "Move to the next section and repeat. Counts land in the selected count session.",
  };
  function setMode(m) {
    w.mode = m;
    $$(".walk-mode").forEach((b) => b.classList.toggle("active", b.dataset.mode === m));
    $("#walkHint").textContent = hints[m];
    $("#walkStart").textContent = m === "map" ? "🎥 Start map run" : "🎯 Start count run";
  }
  $$(".walk-mode").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));
  setMode("map");

  window.loadAisles = async function () {
    const locs = await api("/api/locations").catch(() => []);
    const aisles = {};
    for (const l of locs) {
      if (!l.blueprint_slots) continue;
      const key = `${l.zone}|${l.aisle}`;
      aisles[key] = aisles[key] || { zone: l.zone, aisle: l.aisle, sections: 0, slots: 0 };
      aisles[key].sections += 1;
      aisles[key].slots += l.blueprint_slots;
    }
    const list = Object.values(aisles);
    $("#aisleList").innerHTML = list.length
      ? list.map((a) => `<li class="clickable" data-zone="${a.zone}" data-aisle="${esc(a.aisle)}">
          <div class="grow"><b>Aisle ${esc(a.aisle)}</b> <span class="meta">${a.zone === "floor" ? "Floor" : "Back room"} · ${a.sections} sections · ${a.slots} products</span></div>
          <span class="badge ok">Mapped</span></li>`).join("")
      : '<li class="empty">No aisle mapped yet. Start with a map run.</li>';
    $$("#aisleList li[data-aisle]").forEach((li) => li.addEventListener("click", () => {
      $("#walkZone").value = li.dataset.zone;
      $("#walkAisle").value = li.dataset.aisle;
      setMode("count");
    }));
  };

  /* ---------------------------------------------------------- camera */

  $("#walkStart").addEventListener("click", start);

  async function start() {
    const msg = $("#walkMsg");
    w.zone = $("#walkZone").value;
    w.aisle = $("#walkAisle").value.trim().toUpperCase();
    w.sessionId = state.sessionId;
    if (!w.aisle) return setStatus(msg, "Enter the aisle first.", "error");
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      return setStatus(msg, "This browser can't open the camera here. Use HTTPS (the Replit URL) and Safari/Chrome.", "error");
    }
    if (w.mode === "map") {
      const secs = await api(`/api/sessions/${w.sessionId}/walk/sections?zone=${w.zone}&aisle=${encodeURIComponent(w.aisle)}`).catch(() => []);
      if (secs.length && !confirm(`Aisle ${w.aisle} already has ${secs.length} snapshots in this count. Start the map over? (Earlier snapshots will be replaced as you walk.)`)) return;
    }
    try {
      w.stream = await navigator.mediaDevices.getUserMedia({
        audio: false,
        video: { facingMode: { ideal: "environment" }, width: { ideal: 1920 }, height: { ideal: 1080 } },
      });
    } catch (err) {
      return setStatus(msg, `Camera unavailable: ${err.message}`, "error");
    }
    setStatus(msg, "");
    Object.assign(w, { running: true, prev: null, travel: 0, steadySince: 0, sharpRef: 0, seq: 0, shots: 0, done: 0, failed: 0, bottles: [] });
    $("#camResults").innerHTML = "";
    $("#camMode").textContent = w.mode === "map" ? "Map run" : "Count run";
    $("#camWhere").textContent = `${w.zone === "floor" ? "Floor" : "Back room"} · aisle ${w.aisle}`;
    $("#camAuto").textContent = `Auto: ${w.auto ? "on" : "off"}`;
    $("#cam").hidden = false;
    const v = $("#camVideo");
    v.srcObject = w.stream;
    await v.play().catch(() => {});
    try { w.wakeLock = await navigator.wakeLock?.request("screen"); } catch { /* not supported */ }
    loadDetector();
    updateHud();
    loop();
  }

  function stopCamera() {
    w.running = false;
    w.stream?.getTracks().forEach((t) => t.stop());
    w.stream = null;
    try { w.wakeLock?.release(); } catch { /* ignore */ }
    $("#cam").hidden = true;
  }

  $("#camStop").addEventListener("click", () => {
    stopCamera();
    if (w.mode === "map" && (w.shots || w.active || w.queue.length)) {
      state.aisle = { zone: w.zone, aisle: w.aisle };
      show("aisle");
    } else {
      show("walk");
    }
  });
  $("#camAuto").addEventListener("click", () => {
    w.auto = !w.auto;
    $("#camAuto").textContent = `Auto: ${w.auto ? "on" : "off"}`;
  });
  $("#camShutter").addEventListener("click", () => snapshot("manual"));

  /* ---------------------------------------------------------- motion + sharpness analysis */

  function grayFrame(v) {
    actx.drawImage(v, 0, 0, AW, AH);
    const d = actx.getImageData(0, 0, AW, AH).data;
    const g = new Float32Array(AW * AH);
    for (let i = 0, j = 0; i < g.length; i++, j += 4) g[i] = 0.299 * d[j] + 0.587 * d[j + 1] + 0.114 * d[j + 2];
    return g;
  }

  function sharpness(g) {
    let sum = 0, n = 0;
    for (let y = 1; y < AH - 1; y += 1) {
      for (let x = 1; x < AW - 1; x += 1) {
        const i = y * AW + x;
        sum += Math.abs(4 * g[i] - g[i - 1] - g[i + 1] - g[i - AW] - g[i + AW]);
        n++;
      }
    }
    return sum / n;
  }

  /** Horizontal shift (px at AW) between two frames, by minimising mean absolute difference. */
  function hShift(a, b) {
    let best = 0, bestErr = Infinity;
    for (let dx = -MAX_SHIFT; dx <= MAX_SHIFT; dx++) {
      let err = 0, n = 0;
      for (let y = 4; y < AH - 4; y += 2) {
        const row = y * AW;
        for (let x = MAX_SHIFT; x < AW - MAX_SHIFT; x += 2) {
          err += Math.abs(a[row + x] - b[row + x + dx]);
          n++;
        }
      }
      err /= n;
      if (err < bestErr) { bestErr = err; best = dx; }
    }
    return { dx: best, err: bestErr };
  }

  function loop() {
    if (!w.running) return;
    const v = $("#camVideo");
    if (v.readyState >= 2 && v.videoWidth) {
      const g = grayFrame(v);
      const sharp = sharpness(g);
      w.sharpRef = Math.max(w.sharpRef * 0.995, sharp);
      let dx = 0;
      if (w.prev) {
        const r = hShift(w.prev, g);
        dx = r.dx;
        if (r.err < 40) w.travel += dx;   // ignore frames that don't line up (sudden jumps)
      }
      w.prev = g;
      w.lastMotion = Math.abs(dx);
      const now = performance.now();
      if (Math.abs(dx) <= 1) w.steadySince = w.steadySince || now;
      else w.steadySince = 0;
      const sharpOk = sharp >= 0.7 * w.sharpRef && sharp > 2;
      w.debug = { dx, sharp: +sharp.toFixed(1), travel: w.travel, steadyMs: w.steadySince ? Math.round(now - w.steadySince) : 0, detectMs: Math.round(w.detectMs) };
      decide(sharpOk, now);
      // The detector is guidance only: it backs off when slow and pauses while we wait to snapshot,
      // so it never starves the motion check that drives the snapshots.
      if (w.detector && !w.detecting && !w.ready && now - w.lastDetect > Math.max(450, w.detectMs * 4)) detect(v);
      drawOverlay(v);
    }
    setTimeout(loop, 90);
  }

  function decide(sharpOk, now) {
    const travelled = Math.abs(w.travel) / AW;
    const need = w.mode === "map" ? MAP_TRAVEL : COUNT_TRAVEL;
    const guide = $("#camGuide");
    $("#camMotion").style.width = `${Math.min(100, (w.shots ? travelled / need : 1) * 100)}%`;
    let text, warn = false;
    if (w.lastMotion > 8) { text = "Slow down"; warn = true; }
    else if (!sharpOk) { text = "Focusing… hold steady"; warn = true; }
    else if (w.mode === "map") text = w.shots && travelled < need ? "Keep walking slowly →" : "Hold still…";
    else text = w.shots && travelled < COUNT_TRAVEL ? "Point at the next section" : "Hold still to count…";
    guide.textContent = text;
    guide.classList.toggle("warn", warn);
    w.ready = w.auto && (!w.shots || travelled >= need);
    if (!w.ready) return;
    // How long the picture must be still before an automatic snapshot (ms).
    const holdMs = w.mode === "map" ? (w.shots ? 200 : 500) : 700;
    if (sharpOk && w.steadySince && now - w.steadySince >= holdMs) snapshot("auto");
  }

  /* ---------------------------------------------------------- detector (guidance) */

  async function loadDetector() {
    if (w.detector) return;
    if (!window.cocoSsd || !window.tf) { $("#camBottles").textContent = "detector off"; return; }
    try {
      w.detector = await window.cocoSsd.load({ base: "lite_mobilenet_v2" });
    } catch (err) {
      $("#camBottles").textContent = "detector off";
    }
  }

  async function detect(v) {
    w.detecting = true;
    const t0 = performance.now();
    try {
      const preds = await w.detector.detect(v, 40, 0.3);
      w.bottles = preds.filter((p) => p.class === "bottle" || p.class === "wine glass");
      $("#camBottles").textContent = `${w.bottles.length} bottles in view`;
    } catch { /* skip frame */ }
    w.lastDetect = performance.now();
    w.detectMs = 0.7 * w.detectMs + 0.3 * (w.lastDetect - t0);
    w.detecting = false;
  }

  function drawOverlay(v) {
    const c = $("#camOverlay");
    const cw = c.clientWidth, ch = c.clientHeight;
    if (c.width !== cw || c.height !== ch) { c.width = cw; c.height = ch; }
    const ctx = c.getContext("2d");
    ctx.clearRect(0, 0, cw, ch);
    if (!w.bottles.length) return;
    // video is drawn with object-fit: contain
    const scale = Math.min(cw / v.videoWidth, ch / v.videoHeight);
    const ox = (cw - v.videoWidth * scale) / 2, oy = (ch - v.videoHeight * scale) / 2;
    ctx.lineWidth = 2;
    ctx.strokeStyle = "#6cc28d";
    for (const b of w.bottles) {
      const [x, y, bw, bh] = b.bbox;
      ctx.strokeRect(ox + x * scale, oy + y * scale, bw * scale, bh * scale);
    }
  }

  /* ---------------------------------------------------------- snapshots + uploads */

  function snapshot(why) {
    const v = $("#camVideo");
    if (!v.videoWidth) return;
    const c = document.createElement("canvas");
    c.width = v.videoWidth;
    c.height = v.videoHeight;
    c.getContext("2d").drawImage(v, 0, 0);
    w.travel = 0;
    w.steadySince = 0;
    w.ready = false;
    w.shots += 1;
    const job = { mode: w.mode, seq: w.mode === "map" ? ++w.seq : null, why };
    const flash = $("#camFlash");
    flash.classList.add("on");
    setTimeout(() => flash.classList.remove("on"), 60);
    if (navigator.vibrate) navigator.vibrate(30);
    c.toBlob((blob) => { job.blob = blob; w.queue.push(job); pump(); updateHud(); }, "image/jpeg", 0.9);
  }

  function pump() {
    while (w.active < UPLOADS && w.queue.length) {
      const job = w.queue.shift();
      w.active += 1;
      upload(job).finally(() => { w.active -= 1; updateHud(); pump(); if (window.loadAisle && !$("#view-aisle").hidden) window.loadAisle(); });
    }
  }

  async function upload(job) {
    const fd = new FormData();
    fd.append("image", job.blob, `walk-${job.seq || Date.now()}.jpg`);
    fd.append("zone", w.zone);
    fd.append("aisle", w.aisle);
    try {
      if (job.mode === "map") {
        fd.append("seq", job.seq);
        await api(`/api/sessions/${w.sessionId}/walk/map`, { method: "POST", body: fd });
        w.done += 1;
      } else {
        const cap = await api(`/api/sessions/${w.sessionId}/walk/count`, { method: "POST", body: fd });
        w.done += 1;
        countCard(cap);
      }
    } catch (err) {
      w.failed += 1;
      errorCard(job, err.message);
    }
  }

  function updateHud() {
    $("#camShots").textContent = `${w.shots} snapshot${w.shots === 1 ? "" : "s"}`;
    const pending = w.active + w.queue.length;
    $("#camQueue").textContent = `${pending} processing · ${w.done} done${w.failed ? ` · ${w.failed} failed` : ""}`;
  }

  function countCard(cap) {
    const units = cap.slots.reduce((a, s) => a + s.units, 0);
    const changes = cap.diff.filter((d) => d.status !== "same");
    const empties = cap.slots.filter((s) => s.blueprint_ref && s.facings === 0 && !s.note).length;
    const el = document.createElement("div");
    el.className = "cam-card";
    el.innerHTML = `<b>${esc(cap.code)}</b> · ${units} bottles · ${cap.slots.length} slots
      ${changes.length ? `<div class="chg">${changes.slice(0, 3).map((d) => esc(changeText(d))).join(" · ")}${changes.length > 3 ? ` · +${changes.length - 3} more` : ""}</div>` : "<div>Matches the map</div>"}
      ${empties ? `<div class="chg">${empties} empty spot(s)</div>` : ""}
      ${cap.needs_review ? `<div>${cap.needs_review} to check — tap</div>` : ""}`;
    el.addEventListener("click", () => { stopCamera(); openReview(cap, "walk"); });
    $("#camResults").prepend(el);
  }

  function changeText(d) {
    if (d.status === "qty_change") return `${d.label}: ${d.expected_facings}→${d.facings}`;
    if (d.status === "missing") return `${d.label}: missing`;
    if (d.status === "moved") return `${d.label}: moved`;
    if (d.status === "new") return `${d.label}: new`;
    return d.label;
  }

  function errorCard(job, message) {
    const el = document.createElement("div");
    el.className = "cam-card err";
    el.textContent = `${job.mode === "map" ? `Snapshot #${job.seq}` : "Count"}: ${message}`;
    $("#camResults").prepend(el);
  }

  /* ---------------------------------------------------------- aisle map review */

  window.loadAisle = async function () {
    const a = state.aisle;
    if (!a) return show("walk");
    $("#aisleTitle").textContent = `Aisle ${a.aisle} map · ${a.zone === "floor" ? "Sales floor" : "Back room"}`;
    const secs = await api(`/api/sessions/${state.sessionId}/walk/sections?zone=${a.zone}&aisle=${encodeURIComponent(a.aisle)}`);
    const pending = w.active + w.queue.length;
    setStatus($("#aislePending"), pending ? `${pending} snapshot(s) still processing… keep this page open.` : "");
    const items = secs.reduce((n, s) => n + s.items, 0);
    const check = secs.reduce((n, s) => n + s.needs_review, 0);
    $("#aisleMeta").textContent = `${secs.length} sections · ${items} product groups · ${secs.reduce((n, s) => n + s.duplicates, 0)} overlaps removed`;
    const badge = $("#aisleBadge");
    const allConfirmed = secs.length && secs.every((s) => s.status === "confirmed");
    badge.className = `badge ${allConfirmed ? "ok" : check ? "warn" : ""}`;
    badge.textContent = allConfirmed ? "Map saved" : check ? `${check} to check` : "Ready";
    $("#sectionList").innerHTML = secs.length ? secs.map((s) => `
      <li class="clickable" data-id="${s.id}">
        <img class="thumb" src="/api/captures/${s.id}/image" alt="" loading="lazy">
        <div class="grow"><b>${esc(s.code)}</b><div class="meta">${s.items} items${s.duplicates ? ` · ${s.duplicates} overlap` : ""}</div></div>
        ${s.status === "confirmed" ? '<span class="badge ok">Saved</span>' : s.needs_review ? `<span class="badge warn">${s.needs_review} to check</span>` : '<span class="badge">Ready</span>'}
      </li>`).join("") : `<li class="empty">${pending ? "Processing…" : "No snapshots for this aisle in the current count."}</li>`;
    $$("#sectionList li[data-id]").forEach((li) => li.addEventListener("click", async () => openReview(await api(`/api/captures/${li.dataset.id}`), "aisle")));
    $("#aisleConfirm").disabled = !secs.length || pending > 0;
  };

  $("#aisleBack").addEventListener("click", () => show("walk"));
  $("#aisleConfirm").addEventListener("click", async () => {
    const a = state.aisle;
    try {
      const r = await api(`/api/sessions/${state.sessionId}/walk/confirm?zone=${a.zone}&aisle=${encodeURIComponent(a.aisle)}`, { method: "POST" });
      await window.loadAisle();
      setStatus($("#walkMsg"), `Aisle ${a.aisle} map saved (${r.confirmed} sections). Next time use “Count run”.`, "ok");
      $("#walkZone").value = a.zone;
      $("#walkAisle").value = a.aisle;
      setMode("count");
      show("walk");
    } catch (err) {
      alert(err.message);
    }
  });
})();

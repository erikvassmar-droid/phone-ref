/* Equisport Field Logger - camera-mounted phone logger.
 * Produces a field_session export (raw, phone-time events) that field_logger.py ingest turns into the
 * canonical field_log.json (camera-time segments) consumed by Auto-sort -FieldLog. The page never types
 * rider data: the start list is loaded from startlist.json (generated from Equipe by field_logger.py build).
 *
 * Capture modes, all landing in one export:
 *   tap rider / activity -> a timed SEGMENT (start_no window -> deterministic rider routing)
 *   shot chip            -> a SHOT event (wide/tight/detail/...) for coverage tracking + gap warnings
 *   star                 -> flag the current segment (keeper) OR a standalone flag event
 *   mic                  -> a timestamped voice NOTE (transcribed on the PC by our Whisper)
 *   sync                 -> a camera<->phone clock offset (QR for camera-shoots-phone, or typed camera time)
 *   GPS                  -> a one-shot venue location (reverse-geocoded on the PC; fills Equipe's GPS gap)
 *
 * Export delivery: "Send" POSTs to ./upload when served from the PC (Tool 46 serve) - hands-free; otherwise
 * "Export" downloads the JSON.
 */
(function () {
  "use strict";
  const SCHEMA = "equisport.field_session/1";
  const LS_KEY = "equisport_field_session_v1";

  // label, field_log "type", emoji. Types align with field_log.py detect_type where one exists.
  const ACTIVITIES = [
    ["Round", "round", "🏇"], ["Warm-up", "warmup", "🤸"], ["Walk", "coursewalk", "🚶"],
    ["B-roll", "broll", "🎞"], ["Portrait", "portrait", "👤"], ["Interview", "interview", "🎙"],
    ["Ceremony", "ceremony", "🏆"], ["Scenery", "scenery", "🌄"], ["Stable", "stable", "🐎"],
  ];
  // shot-type grammar for coverage (label, key). Kept text-only + slim to stay out of the way.
  const SHOTS = [["Wide", "wide"], ["Tight", "tight"], ["Detail", "detail"], ["React", "reaction"], ["Follow", "follow"]];
  const SHOT_KEYS = SHOTS.map((s) => s[1]);
  // optional "why" for a flag (C7). fall/win are SHOOTING observations, not official results (those = Equipe).
  const FLAG_REASONS = [["Hero", "hero"], ["Emotion", "emotion"], ["Fall", "fall"], ["Win", "win"], ["Sponsor", "sponsor"], ["Funny", "funny"]];
  const CAMERAS = ["FX6", "1DX3"];   // A3: which body shot it -> per-camera clock offset at ingest
  // B6: standing b-roll/atmosphere "shopping list" - tick each through the day (keys match field_logger.py)
  const BROLL_TARGETS = [["Coursewalk", "coursewalk"], ["Crowd", "crowd"], ["Sponsor boards", "sponsor"],
    ["Stable", "stable"], ["Venue wide", "venue"], ["Weather/sky", "weather"], ["Details", "details"], ["Atmosphere", "atmo"]];
  const BROLL_KEYS = BROLL_TARGETS.map((b) => b[1]);
  const NO_RIDER = { start_no: null, rider: "General / B-roll", horse: "" };
  // haptic patterns (ms / [on,off,on...]) - distinct per action so you can FEEL which log fired (Android only)
  const HAPTIC = { rider: 18, activity: 25, shot: 35, flag: [14, 36, 14], unflag: 12, noteStart: 60, noteStop: [26, 30, 26] };
  const SET_KEY = "equisport_field_settings_v1";

  const $ = (id) => document.getElementById(id);
  const now = () => Date.now();
  const pad = (n) => String(n).padStart(2, "0");
  const hhmmss = (d) => pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());

  const state = {
    meta: { section_id: "", competition: "", class_name: "", sec_per_start: null },
    riders: [NO_RIDER],
    curIndex: 0,
    curActivity: null,
    openSeg: null,          // {start_no, rider, horse, activity, t_start, flag}
    events: [],             // segments + notes + flags + shots (one stream)
    sync: [],               // {epoch_ms, camera_time, camera}
    geo: null,              // {lat, lon, acc, t_ms} - one venue stamp
    loggedNos: {},          // start_no -> true (list marker)
    curCamera: "FX6",       // A3: active camera, tagged on each event for per-camera offsets
  };
  let uploadOK = false;     // true when served from the PC (./upload reachable)

  /* ---- settings + haptic feedback (eyes-free: log without looking away from the camera) ---- */
  const settings = { haptics: true, big: false, keepAwake: true };
  const canVibrate = typeof navigator !== "undefined" && typeof navigator.vibrate === "function";
  function buzz(p) { if (!settings.haptics || !canVibrate || p == null) return; try { navigator.vibrate(p); } catch (e) { /* no-op */ } }
  function loadSettings() {
    try {
      const s = JSON.parse(localStorage.getItem(SET_KEY) || "{}");
      if (typeof s.haptics === "boolean") settings.haptics = s.haptics;
      if (typeof s.big === "boolean") settings.big = s.big;
      if (typeof s.keepAwake === "boolean") settings.keepAwake = s.keepAwake;
    } catch (e) { /* keep defaults */ }
  }
  function saveSettings() { try { localStorage.setItem(SET_KEY, JSON.stringify(settings)); } catch (e) { /* ignore */ } }
  function applyBig() {
    document.body.classList.toggle("big", !!settings.big);
    const b = $("bigBtn"); if (b) b.classList.toggle("on", !!settings.big);
  }
  /* Pin the app shell to the real viewport height in px. CSS 100svh/100dvh can recompute to a sliver
     across a reflow (e.g. toggling big mode) → the flex column collapses and overflow:hidden blanks the
     screen with the footer stranded at the top. An explicit innerHeight px never collapses; we refresh
     it on resize/orientation (the URL bar show/hide fires resize) so it stays correct. */
  function fitShell() {
    try { if (window.innerHeight) document.body.style.height = window.innerHeight + "px"; } catch (e) { /* ignore */ }
  }
  function updateHapticBtn() {
    const b = $("hapticBtn"); if (b) b.textContent = "Haptics: " + (settings.haptics ? "on" : "off") + (canVibrate ? "" : " (n/a)");
  }

  /* ---- wake lock: keep the screen on during a shoot (camera-mounted, eyes on the viewfinder) ---- */
  const canWake = typeof navigator !== "undefined" && "wakeLock" in navigator;
  let wakeSentinel = null;
  async function acquireWake() {
    if (!settings.keepAwake || !canWake) return;
    if (typeof document !== "undefined" && document.visibilityState && document.visibilityState !== "visible") return;
    if (wakeSentinel) return;
    try {
      wakeSentinel = await navigator.wakeLock.request("screen");
      wakeSentinel.addEventListener("release", () => { wakeSentinel = null; updateWakeBtn(); });
    } catch (e) { wakeSentinel = null; }   // denied (e.g. low battery) — just carry on
    updateWakeBtn();
  }
  async function releaseWake() {
    try { if (wakeSentinel) await wakeSentinel.release(); } catch (e) { /* ignore */ }
    wakeSentinel = null; updateWakeBtn();
  }
  function updateWakeBtn() {
    const b = $("wakeBtn");
    if (!b) return;
    b.textContent = "Keep screen on: " + (settings.keepAwake ? "on" : "off")
      + (canWake ? (settings.keepAwake && wakeSentinel ? " ●" : "") : " (n/a)");
  }

  /* ---- install nudge: prefer the standalone PWA over a browser tab (reliable insets + full screen) ---- */
  const INSTALL_KEY = "equisport_fl_install_dismissed_v1";
  let deferredInstall = null;
  function isStandalone() {
    try {
      return (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) || window.navigator.standalone === true;
    } catch (e) { return false; }
  }
  function maybeShowInstall() {
    const bar = $("installBar"); if (!bar) return;
    let dismissed = false;
    try { dismissed = !!localStorage.getItem(INSTALL_KEY); } catch (e) { /* ignore */ }
    bar.classList.toggle("show", !isStandalone() && !dismissed);
  }
  function dismissInstall() {
    try { localStorage.setItem(INSTALL_KEY, "1"); } catch (e) { /* ignore */ }
    const bar = $("installBar"); if (bar) bar.classList.remove("show");
  }
  async function doInstall() {
    if (deferredInstall) {
      try { deferredInstall.prompt(); await deferredInstall.userChoice; } catch (e) { /* ignore */ }
      deferredInstall = null; dismissInstall();
    } else {
      const t = $("installBar") && $("installBar").querySelector(".txt");
      if (t) t.textContent = "Open the browser ⋮ menu → “Add to Home screen”.";
    }
  }

  /* ---- IndexedDB (audio note blobs as base64 data URLs) ---- */
  function idb() {
    return new Promise((res, rej) => {
      const r = indexedDB.open("eqfl", 1);
      r.onupgradeneeded = () => r.result.createObjectStore("audio");
      r.onsuccess = () => res(r.result);
      r.onerror = () => rej(r.error);
    });
  }
  async function idbPut(k, v) {
    const db = await idb();
    return new Promise((res, rej) => {
      const t = db.transaction("audio", "readwrite");
      t.objectStore("audio").put(v, k); t.oncomplete = res; t.onerror = () => rej(t.error);
    });
  }
  async function idbGet(k) {
    const db = await idb();
    return new Promise((res, rej) => {
      const t = db.transaction("audio", "readonly");
      const rq = t.objectStore("audio").get(k);
      rq.onsuccess = () => res(rq.result); rq.onerror = () => rej(rq.error);
    });
  }

  /* ---- persistence (everything except audio blobs) ---- */
  function save() {
    try {
      localStorage.setItem(LS_KEY, JSON.stringify({
        meta: state.meta, curIndex: state.curIndex, curActivity: state.curActivity,
        openSeg: state.openSeg, events: state.events, sync: state.sync, geo: state.geo, loggedNos: state.loggedNos,
        curCamera: state.curCamera,
      }));
    } catch (e) { /* quota / private mode - keep running from memory */ }
  }
  function restore() {
    try {
      const raw = localStorage.getItem(LS_KEY);
      if (!raw) return false;
      const s = JSON.parse(raw);
      if (s && s.meta && s.meta.section_id === state.meta.section_id) {
        Object.assign(state, {
          curIndex: s.curIndex || 0, curActivity: s.curActivity || null, openSeg: s.openSeg || null,
          events: s.events || [], sync: s.sync || [], geo: s.geo || null, loggedNos: s.loggedNos || {},
          curCamera: s.curCamera || "FX6",
        });
        return true;
      }
    } catch (e) { /* ignore corrupt state */ }
    return false;
  }

  /* ---- segment lifecycle ---- */
  function curRider() { return state.riders[state.curIndex] || NO_RIDER; }

  function closeOpen(tEnd) {
    if (!state.openSeg) return;
    const s = state.openSeg;
    s.t_end = tEnd || now();
    state.events.push(Object.assign({ kind: "segment" }, s));
    if (s.start_no != null) state.loggedNos[s.start_no] = true;
    state.openSeg = null;
  }
  function openFor(rider, activity, t) {
    state.openSeg = {
      start_no: rider.start_no, rider: rider.rider, horse: rider.horse || "",
      activity: activity || null, t_start: t || now(), flag: false, camera: state.curCamera || "",
    };
  }
  function cycleCamera() {
    const i = CAMERAS.indexOf(state.curCamera);
    state.curCamera = CAMERAS[(i + 1) % CAMERAS.length];
    if (state.openSeg) { const t = now(); closeOpen(t); openFor(curRider(), state.curActivity, t); }  // clean boundary
    buzz(HAPTIC.activity);
    render(); save();
  }

  function selectRider(i) {
    if (i < 0 || i >= state.riders.length) return;
    const t = now();
    closeOpen(t);
    hideReasonBar();   // moving on dismisses a lingering reason chooser
    state.curIndex = i;
    openFor(curRider(), state.curActivity, t);   // start a fresh window for this rider
    buzz(HAPTIC.rider);
    render(); save();
  }
  function nextRider() { selectRider(Math.min(state.curIndex + 1, state.riders.length - 1)); }
  function prevRider() { selectRider(Math.max(state.curIndex - 1, 0)); }

  function setActivity(type) {
    state.curActivity = type;
    if (state.openSeg) state.openSeg.activity = type;   // label the current window in place
    else openFor(curRider(), type, now());
    buzz(HAPTIC.activity);
    render(); save();
  }

  function toggleFlag() {
    let flaggedOn = false;
    if (state.openSeg) {
      state.openSeg.flag = !state.openSeg.flag;
      flaggedOn = state.openSeg.flag;
      if (!flaggedOn) state.openSeg.flag_reason = null;   // un-flag clears any reason
      buzz(flaggedOn ? HAPTIC.flag : HAPTIC.unflag);
    } else {
      const r = curRider();
      state.events.push({ kind: "flag", t_ms: now(), start_no: r.start_no, rider: r.rider, camera: state.curCamera || "" });
      flaggedOn = true;
      buzz(HAPTIC.flag);
    }
    if (flaggedOn) showReasonBar(); else hideReasonBar();   // optional one-tap "why", auto-dismisses
    render(); save();
  }

  /* ---- flag reasons (C7): optional typed keeper (hero/emotion/fall/win/sponsor/funny) ---- */
  let reasonTimer = null;
  function showReasonBar() {
    const bar = $("reasonBar"); if (!bar) return;
    bar.classList.add("show");
    if (reasonTimer) clearTimeout(reasonTimer);
    reasonTimer = setTimeout(hideReasonBar, 5000);   // the flag stays even if you pick nothing
  }
  function hideReasonBar() {
    const bar = $("reasonBar"); if (bar) bar.classList.remove("show");
    if (reasonTimer) { clearTimeout(reasonTimer); reasonTimer = null; }
  }
  function setFlagReason(reason) {
    if (state.openSeg && state.openSeg.flag) {
      state.openSeg.flag_reason = reason;
    } else {
      for (let i = state.events.length - 1; i >= 0; i--) {   // the most recent standalone flag
        if (state.events[i].kind === "flag") { state.events[i].reason = reason; break; }
      }
    }
    buzz(HAPTIC.flag);
    hideReasonBar();
    render(); save();
  }
  function buildReasonChips() {
    const wrap = $("reasonBar"); if (!wrap) return;
    wrap.innerHTML = "";
    FLAG_REASONS.forEach(([label, key]) => {
      const b = document.createElement("button");
      b.className = "rsn"; b.dataset.reason = key; b.textContent = label;
      b.onclick = () => setFlagReason(key);
      wrap.appendChild(b);
    });
  }

  /* ---- shot-type coverage ---- */
  function logShot(shot) {
    if (SHOT_KEYS.indexOf(shot) < 0) return;
    const r = curRider();
    state.events.push({ kind: "shot", t_ms: now(), shot: shot, start_no: r.start_no, rider: r.rider, activity: state.curActivity, camera: state.curCamera || "" });
    flashShot(shot);
    buzz(HAPTIC.shot);
    render(); save();
  }
  function flashShot(shot) {
    const b = document.querySelector('.shot[data-shot="' + shot + '"]');
    if (b) { b.classList.add("hit"); setTimeout(() => b.classList.remove("hit"), 320); }
  }
  function coverageSummary() {
    const byType = {}; SHOT_KEYS.forEach((k) => (byType[k] = 0));
    const subjects = {};
    for (const e of state.events) {
      if (e.kind !== "shot") continue;
      byType[e.shot] = (byType[e.shot] || 0) + 1;
      const key = e.start_no == null ? "general" : String(e.start_no);
      (subjects[key] = subjects[key] || {})[e.shot] = true;
    }
    const total = Object.values(byType).reduce((a, b) => a + b, 0);
    const missing = SHOT_KEYS.filter((k) => !byType[k]);
    // riders you logged a segment for but have NO shot of yet (coverage gaps to chase)
    const shotNos = new Set(state.events.filter((e) => e.kind === "shot" && e.start_no != null).map((e) => e.start_no));
    const loggedNos = Object.keys(state.loggedNos).map(Number).concat(state.openSeg && state.openSeg.start_no != null ? [state.openSeg.start_no] : []);
    const uncovered = [...new Set(loggedNos)].filter((n) => !shotNos.has(n)).sort((a, b) => a - b);  // start_nos (match Python)
    // priority lens (B4): must-cover riders (Swedish + top-3) and which of them still have no shot
    const priority = [...new Set(state.riders.filter((r) => r.priority && r.start_no != null).map((r) => r.start_no))].sort((a, b) => a - b);
    const priorityUncovered = priority.filter((n) => !shotNos.has(n));
    return { by_type: byType, total: total, missing: missing, subjects: Object.keys(subjects).length, uncovered: uncovered, priority: priority, priority_uncovered: priorityUncovered };
  }
  function priorityNos() { return [...new Set(state.riders.filter((r) => r.priority && r.start_no != null).map((r) => r.start_no))].sort((a, b) => a - b); }

  /* ---- b-roll shopping list (B6): tick standing cutaway targets through the day ---- */
  function logBroll(key) {
    if (BROLL_KEYS.indexOf(key) < 0) return;
    state.events.push({ kind: "broll", t_ms: now(), target: key, camera: state.curCamera || "" });
    buzz(HAPTIC.shot);
    updateBrollChips(); save();
  }
  function brollSummary() {
    const counts = {}; BROLL_KEYS.forEach((k) => (counts[k] = 0));
    state.events.forEach((e) => { if (e.kind === "broll" && e.target != null) counts[e.target] = (counts[e.target] || 0) + 1; });
    return { counts: counts, covered: BROLL_KEYS.filter((k) => counts[k]), missing: BROLL_KEYS.filter((k) => !counts[k]),
      total: Object.values(counts).reduce((a, b) => a + b, 0) };
  }
  function buildBrollChips() {
    const wrap = $("brollGrid"); if (!wrap) return;
    wrap.innerHTML = "";
    BROLL_TARGETS.forEach(([label, key]) => {
      const b = document.createElement("button");
      b.className = "broll"; b.dataset.broll = key; b.textContent = label;
      b.onclick = () => logBroll(key);
      wrap.appendChild(b);
    });
  }
  function updateBrollChips() {
    const s = brollSummary();
    document.querySelectorAll("#brollGrid .broll").forEach((b) => b.classList.toggle("covered", s.counts[b.dataset.broll] > 0));
    const c = $("brollCount"); if (c) c.textContent = s.covered.length + "/" + BROLL_KEYS.length;
  }

  /* ---- venue GPS (one-shot; needs a secure context = https / GitHub Pages) ---- */
  function captureGeo() {
    return new Promise((resolve) => {
      if (!navigator.geolocation) { resolve({ error: "no geolocation API" }); return; }
      if (!window.isSecureContext) { resolve({ error: "GPS needs https - use the GitHub Pages URL" }); return; }
      navigator.geolocation.getCurrentPosition(
        (pos) => {
          state.geo = { lat: +pos.coords.latitude.toFixed(6), lon: +pos.coords.longitude.toFixed(6), acc: Math.round(pos.coords.accuracy || 0), t_ms: now() };
          save(); render();
          resolve(state.geo);
        },
        (err) => resolve({ error: err.message || "GPS denied" }),
        { enableHighAccuracy: true, timeout: 12000, maximumAge: 60000 }
      );
    });
  }
  function setGeo(lat, lon) { state.geo = { lat: +lat, lon: +lon, acc: 0, t_ms: now() }; save(); render(); return state.geo; } // test hook

  /* ---- voice notes ---- */
  let mediaRec = null, recChunks = [], recStart = 0;
  function blobToDataURL(blob) {
    return new Promise((res) => { const fr = new FileReader(); fr.onload = () => res(fr.result); fr.readAsDataURL(blob); });
  }
  async function _commitNote(dataURL, durMs) {
    const r = curRider();
    const id = "note_" + (recStart || now());
    await idbPut(id, dataURL).catch(() => {});
    state.events.push({
      kind: "note", t_ms: recStart || now(), start_no: r.start_no, rider: r.rider,
      activity: state.curActivity, audio_id: id, dur_ms: durMs || 0, camera: state.curCamera || "",
    });
    recStart = 0;
    render(); save();
    return id;
  }
  async function toggleNote() {
    if (mediaRec && mediaRec.state === "recording") { mediaRec.stop(); return; }
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
    catch (e) { setStatus("Mic blocked - allow microphone access"); return; }
    recChunks = []; recStart = now();
    mediaRec = new MediaRecorder(stream);
    mediaRec.ondataavailable = (ev) => { if (ev.data && ev.data.size) recChunks.push(ev.data); };
    mediaRec.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      const dur = now() - recStart;
      const blob = new Blob(recChunks, { type: (recChunks[0] && recChunks[0].type) || "audio/webm" });
      const dataURL = await blobToDataURL(blob);
      await _commitNote(dataURL, dur);
      buzz(HAPTIC.noteStop);
      $("noteBtn").classList.remove("rec");
      render();
    };
    mediaRec.start();
    buzz(HAPTIC.noteStart);
    $("noteBtn").classList.add("rec");
    render();
  }
  // test hook: inject a note without a microphone
  async function addNote(dataURL, durMs, tMs) { recStart = tMs || now(); return _commitNote(dataURL || "data:audio/webm;base64,", durMs || 1000); }

  /* ---- clock sync ---- */
  function addSync(cameraTime, cameraName) {
    cameraTime = normTime(cameraTime);
    if (!cameraTime) return null;
    const rec = { epoch_ms: now(), camera_time: cameraTime, camera: cameraName || "camera" };
    state.sync.push(rec);
    render(); save();
    return rec;
  }
  function normTime(t) {
    if (!t) return null;
    const m = String(t).trim().match(/^(\d{1,2}):(\d{2})(?::(\d{2}))?$/);
    if (!m) return null;
    const d = new Date();
    const date = d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
    return date + " " + pad(+m[1]) + ":" + m[2] + ":" + (m[3] || "00");
  }

  /* ---- export + delivery ---- */
  async function buildExport() {
    const events = state.events.slice();
    if (state.openSeg) { // snapshot the still-open segment without closing it for real
      const s = Object.assign({ kind: "segment" }, state.openSeg, { t_end: now() });
      events.push(s);
    }
    const audio = {};
    for (const ev of events) {
      if (ev.kind === "note" && ev.audio_id && !(ev.audio_id in audio)) {
        const d = await idbGet(ev.audio_id).catch(() => null);
        if (d) audio[ev.audio_id] = d;
      }
    }
    return {
      schema: SCHEMA, generated_at: new Date().toISOString(),
      tz_offset_min: new Date().getTimezoneOffset(), section_id: state.meta.section_id,
      meta: state.meta, sync: state.sync.slice(), geo: state.geo, priority_nos: priorityNos(), events, audio,
    };
  }
  async function exportDownload() {
    const data = await buildExport();
    const blob = new Blob([JSON.stringify(data, null, 1)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "field_session_" + (state.meta.section_id || "log") + ".json";
    document.body.appendChild(a); a.click(); a.remove();
    setStatus("Exported " + data.events.length + " event(s)" + (Object.keys(data.audio).length ? " + " + Object.keys(data.audio).length + " note(s)" : ""));
  }
  async function sendToPC() {
    const data = await buildExport();
    try {
      const r = await fetch("upload", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const info = await r.json().catch(() => ({}));
      setStatus("Sent to PC -> " + (info.file || "saved") + " (" + data.events.length + " events)");
      return true;
    } catch (e) {
      setStatus("Send failed (" + e.message + ") - use Export instead");
      return false;
    }
  }
  async function probeUpload() {
    try { const r = await fetch("upload", { method: "GET" }); uploadOK = r.ok; }
    catch (e) { uploadOK = false; }
    $("sendBtn").classList.toggle("hidden", !uploadOK);
  }

  /* ---- rendering ---- */
  function buildActivityChips() {
    const wrap = $("activities"); wrap.innerHTML = "";
    ACTIVITIES.forEach(([label, type, em]) => {
      const b = document.createElement("button");
      b.className = "act"; b.dataset.type = type;
      b.innerHTML = '<span class="em">' + em + "</span>" + label;
      b.onclick = () => setActivity(type);
      wrap.appendChild(b);
    });
  }
  function buildShotChips() {
    const wrap = $("shots"); wrap.innerHTML = "";
    SHOTS.forEach(([label, key]) => {
      const b = document.createElement("button");
      b.className = "shot"; b.dataset.shot = key; b.textContent = label;
      b.onclick = () => logShot(key);
      wrap.appendChild(b);
    });
  }
  function buildList() {
    const list = $("list"); list.innerHTML = "";
    state.riders.forEach((r, i) => {
      const row = document.createElement("div");
      row.className = "row" + (i === state.curIndex ? " cur" : "") + (r.start_no != null && state.loggedNos[r.start_no] ? " logged" : "") + (r.priority ? " pri" : "");
      row.innerHTML =
        '<div class="rno">' + (r.start_no == null ? "▦" : r.start_no) + "</div>" +
        '<div class="rn">' + (r.priority ? '<span class="pristar">★</span>' : "") + escapeHtml(r.rider) + (r.horse ? ' <span class="rh">· ' + escapeHtml(r.horse) + "</span>" : "") + "</div>" +
        '<div class="mk">' + (r.start_no != null && state.loggedNos[r.start_no] ? "✓" : "") + "</div>";
      row.onclick = () => selectRider(i);
      list.appendChild(row);
    });
  }
  /* ---- predictive next-rider (A1): who's up + ETA. Uses class start_at + sec_per_start, but re-anchors
     on your live taps (one start after the current tap) so it self-corrects if the class runs ahead/behind. ---- */
  function timeToToday(str) {
    const m = String(str || "").match(/(\d{1,2}):(\d{2})(?::(\d{2}))?/);
    if (!m) return null;
    const d = new Date(); d.setHours(+m[1], +m[2], +(m[3] || 0), 0);
    return d.getTime();
  }
  function hhmm(ms) { const d = new Date(ms); return pad(d.getHours()) + ":" + pad(d.getMinutes()); }
  function etaRel(ms) {
    const mins = Math.round((ms - now()) / 60000);
    return mins > 0 ? " (in " + mins + "m)" : (mins === 0 ? " (now)" : " (" + (-mins) + "m ago)");
  }
  function nextExpected() {
    const ni = state.curIndex + 1;
    if (ni >= state.riders.length) return null;
    const r = state.riders[ni];
    if (!r || r.start_no == null) return null;
    const sps = state.meta.sec_per_start;
    let eta = null;
    if (state.openSeg && state.openSeg.t_start && sps) eta = state.openSeg.t_start + sps * 1000;
    else if (sps && state.meta.start_at) { const cs = timeToToday(state.meta.start_at); if (cs != null) eta = cs + (ni - 1) * sps * 1000; }
    return { rider: r, eta: eta };
  }
  function renderUpnext() {
    const up = $("upnext"); if (!up) return;
    const nx = nextExpected();
    if (!nx) { up.textContent = ""; up.classList.add("empty"); return; }
    let s = "#" + nx.rider.start_no + "  " + nx.rider.rider + (nx.rider.horse ? "  ·  " + nx.rider.horse : "");
    if (nx.eta != null) s += "   ~" + hhmm(nx.eta) + etaRel(nx.eta);
    up.textContent = s; up.classList.remove("empty");
  }

  function render() {
    const r = curRider();
    $("curNo").textContent = r.start_no == null ? "▦" : r.start_no;
    $("curRider").textContent = r.rider;
    $("curHorse").textContent = r.horse || "";
    document.getElementById("current").classList.toggle("pri", !!r.priority);
    $("curAngle").textContent = r.angle || "";
    document.querySelectorAll(".act").forEach((b) => b.classList.toggle("on", b.dataset.type === state.curActivity));
    $("flagBtn").classList.toggle("on", !!(state.openSeg && state.openSeg.flag));
    const fr = state.openSeg && state.openSeg.flag && state.openSeg.flag_reason;
    $("flagLbl").textContent = fr ? (fr.charAt(0).toUpperCase() + fr.slice(1)) : "Flag";
    document.querySelectorAll("#list .row").forEach((row, i) => {
      row.classList.toggle("cur", i === state.curIndex);
      const rr = state.riders[i];
      row.classList.toggle("logged", rr && rr.start_no != null && !!state.loggedNos[rr.start_no]);
      const mk = row.querySelector(".mk"); if (mk) mk.textContent = (rr && rr.start_no != null && state.loggedNos[rr.start_no]) ? "✓" : "";
    });
    const segs = state.events.filter((e) => e.kind === "segment").length + (state.openSeg ? 1 : 0);
    const notes = state.events.filter((e) => e.kind === "note").length;
    const shots = state.events.filter((e) => e.kind === "shot").length;
    const flags = state.events.filter((e) => e.kind === "flag").length + state.events.filter((e) => e.kind === "segment" && e.flag).length + (state.openSeg && state.openSeg.flag ? 1 : 0);
    setStatus(segs + " seg · " + flags + " ★ · " + shots + " ▢ · " + notes + " 🎤" + (state.openSeg ? "  ▶ #" + (state.openSeg.start_no == null ? "—" : state.openSeg.start_no) : ""));
    const sp = $("syncPill");
    if (state.sync.length) { sp.className = "pill ok"; sp.textContent = "synced ×" + state.sync.length; }
    else { sp.className = "pill warn"; sp.textContent = "no sync"; }
    $("camPill").textContent = state.curCamera;
    renderUpnext();
    if ($("cover").classList.contains("show")) renderCover();
  }
  function setStatus(t) { $("status").textContent = t; }
  function escapeHtml(s) { return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

  /* ---- coverage overlay ---- */
  function renderCover() {
    const c = coverageSummary();
    const cells = SHOTS.map(([label, key]) =>
      '<div class="cov ' + (c.by_type[key] ? "has" : "miss") + '"><div class="n">' + c.by_type[key] + '</div><div class="l">' + label + "</div></div>").join("");
    let html = '<div class="covgrid">' + cells + "</div>";
    html += '<div class="covline">' + c.total + " shots · " + c.subjects + " subject(s) covered</div>";
    if (c.priority && c.priority.length) {
      if (c.priority_uncovered.length) {
        const pnames = c.priority_uncovered.slice(0, 12).map((n) => { const r = state.riders.find((x) => x.start_no === n); return escapeHtml(r ? (n + " " + r.rider) : String(n)); });
        html += '<div class="covpri"><b>★ Priority — no shot yet (' + c.priority_uncovered.length + "/" + c.priority.length + "):</b><br>" + pnames.join("<br>") + (c.priority_uncovered.length > 12 ? "<br>+" + (c.priority_uncovered.length - 12) + " more" : "") + "</div>";
      } else {
        html += '<div class="covok">★ All ' + c.priority.length + " priority rider(s) have a shot ✓</div>";
      }
    }
    if (c.missing.length) html += '<div class="covwarn">No ' + c.missing.map((k) => SHOTS.find((s) => s[1] === k)[0]).join(", ") + " yet</div>";
    if (c.uncovered.length) {
      const names = c.uncovered.slice(0, 12).map((n) => { const r = state.riders.find((x) => x.start_no === n); return escapeHtml(r ? (n + " " + r.rider) : String(n)); });
      html += '<div class="covlist"><b>Logged but no shot yet:</b><br>' + names.join("<br>") + (c.uncovered.length > 12 ? "<br>+" + (c.uncovered.length - 12) + " more" : "") + "</div>";
    }
    else if (c.total) html += '<div class="covok">Every logged rider has at least one shot ✓</div>';
    $("coverBody").innerHTML = html;
    updateBrollChips();
  }
  function openCover() { $("cover").classList.add("show"); renderCover(); }
  function closeCover() { $("cover").classList.remove("show"); }

  /* ---- end-of-card debrief (C8): counts + gaps + closure + Send/Export ---- */
  function debriefSummary() {
    const segEvents = state.events.filter((e) => e.kind === "segment");
    const segs = segEvents.length + (state.openSeg ? 1 : 0);
    const notes = state.events.filter((e) => e.kind === "note").length;
    const flagEvents = state.events.filter((e) => e.kind === "flag");
    const flaggedSegs = segEvents.filter((e) => e.flag).concat(state.openSeg && state.openSeg.flag ? [state.openSeg] : []);
    const reasons = {};
    flagEvents.forEach((e) => { if (e.reason) reasons[e.reason] = (reasons[e.reason] || 0) + 1; });
    flaggedSegs.forEach((e) => { if (e.flag_reason) reasons[e.flag_reason] = (reasons[e.flag_reason] || 0) + 1; });
    const ridersLogged = new Set(Object.keys(state.loggedNos).map(Number).concat(state.openSeg && state.openSeg.start_no != null ? [state.openSeg.start_no] : [])).size;
    const totalRiders = state.riders.filter((r) => r.start_no != null).length;
    return { segs: segs, notes: notes, flags: flagEvents.length + flaggedSegs.length, reasons: reasons,
      ridersLogged: ridersLogged, totalRiders: totalRiders, cov: coverageSummary(), synced: state.sync.length, geo: !!state.geo };
  }
  function renderWrap() {
    const d = debriefSummary();
    const row = (l, v, warn) => '<div class="wrapstat"><span>' + l + '</span><span class="v' + (warn ? " warn" : "") + '">' + v + "</span></div>";
    let html = '<div class="wrapdone">Card logged — ' + d.ridersLogged + " of " + d.totalRiders + " rider(s), " + d.flags + " keeper(s) ✓</div>";
    html += row("Riders logged", d.ridersLogged + " / " + d.totalRiders, !!(d.totalRiders && d.ridersLogged < d.totalRiders));
    html += row("Segments", d.segs);
    let flagTxt = String(d.flags);
    const rk = Object.keys(d.reasons);
    if (rk.length) flagTxt += " · " + rk.map((k) => d.reasons[k] + " " + k).join(", ");
    html += row("Flags", flagTxt);
    html += row("Voice notes", d.notes);
    html += row("Shots", d.cov.total);
    if (d.cov.missing.length) html += row("Missing shot types", d.cov.missing.map((k) => SHOTS.find((s) => s[1] === k)[0]).join(", "), true);
    const brl = brollSummary();
    let brTxt = brl.covered.length + " / " + BROLL_KEYS.length;
    if (brl.missing.length && brl.total) brTxt += " · need " + brl.missing.slice(0, 3).map((k) => BROLL_TARGETS.find((b) => b[1] === k)[0]).join(", ") + (brl.missing.length > 3 ? "…" : "");
    html += row("B-roll", brTxt);
    if (d.cov.priority && d.cov.priority.length) html += row("Priority covered", (d.cov.priority.length - d.cov.priority_uncovered.length) + " / " + d.cov.priority.length, d.cov.priority_uncovered.length > 0);
    if (d.cov.uncovered.length) html += row("Logged, no shot", d.cov.uncovered.length, true);
    html += row("Clock sync", d.synced ? ("synced ×" + d.synced) : "not synced", !d.synced);
    html += row("Venue GPS", d.geo ? "tagged" : "—");
    $("wrapBody").innerHTML = html;
    $("wrapSend").classList.toggle("hidden", !uploadOK);
    $("wrapMsg").textContent = "";
  }
  function openWrap() { $("wrap").classList.add("show"); renderWrap(); }
  function closeWrap() { $("wrap").classList.remove("show"); }

  /* ---- clock + sync overlay ---- */
  function tick() {
    const d = new Date();
    $("clock").textContent = hhmmss(d);
    renderUpnext();   // keep the "(in Xm)" countdown live between actions
    if ($("sync").classList.contains("show")) {
      $("syncClock").textContent = hhmmss(d);
      drawQR();
    }
  }
  let lastQRsec = -1;
  function drawQR() {
    const sec = Math.floor(now() / 1000);
    if (sec === lastQRsec) return;        // regenerate once per second
    lastQRsec = sec;
    try {
      const qr = qrcode(0, "M");
      qr.addData("EQFL " + now());
      qr.make();
      $("qr").innerHTML = qr.createImgTag(5, 8);
    } catch (e) { $("qr").innerHTML = '<div style="color:#8aa093;font-size:12px">QR unavailable - use manual entry</div>'; }
  }
  function openSync() { $("sync").classList.add("show"); lastQRsec = -1; tick(); }
  function closeSync() { $("sync").classList.remove("show"); }

  /* ---- start list loading ---- */
  async function loadStartlist() {
    let data = null;
    for (const url of ["startlist.json", "startlist.sample.json"]) {
      try { const r = await fetch(url, { cache: "no-store" }); if (r.ok) { data = await r.json(); break; } } catch (e) { /* try next */ }
    }
    if (!data) { setStatus("No startlist.json - generate with: field_logger.py build --section <id>"); return; }
    applyStartlist(data);
  }
  function applyStartlist(data) {
    state.meta = {
      section_id: String(data.section_id || ""), competition: data.competition || "",
      class_name: data.class_name || "", sec_per_start: data.sec_per_start != null ? data.sec_per_start : null,
      start_at: data.start_at || "",
    };
    const riders = (data.riders || []).map((r) => ({
      start_no: r.start_no != null ? Number(r.start_no) : null,
      rider: r.rider || r.rider_name || "?", horse: r.horse || r.horse_name || "",
      priority: !!r.priority, angle: r.angle || "",
    }));
    state.riders = [NO_RIDER].concat(riders);
    $("clsName").textContent = state.meta.class_name || state.meta.competition || ("Section " + state.meta.section_id);
    $("clsSub").textContent = [state.meta.competition, state.meta.section_id ? "section " + state.meta.section_id : "", riders.length + " riders"].filter(Boolean).join(" · ");
  }

  /* ---- wire up ---- */
  async function init() {
    loadSettings();
    buildActivityChips();
    buildShotChips();
    buildReasonChips();
    buildBrollChips();
    await loadStartlist();
    const resumed = restore();
    if (!resumed) { state.curIndex = state.riders.length > 1 ? 1 : 0; }  // point at rider 1; no segment until you act
    buildList(); render();
    $("prevBtn").onclick = prevRider;
    $("nextBtn").onclick = nextRider;
    $("flagBtn").onclick = toggleFlag;
    $("noteBtn").onclick = toggleNote;
    $("syncBtn").onclick = openSync;
    $("syncClose").onclick = closeSync;
    $("exportBtn").onclick = exportDownload;
    $("sendBtn").onclick = sendToPC;
    $("status").onclick = openCover;
    $("coverClose").onclick = closeCover;
    $("bigBtn").onclick = () => { settings.big = !settings.big; saveSettings(); applyBig(); };
    $("hapticBtn").onclick = () => { settings.haptics = !settings.haptics; saveSettings(); updateHapticBtn(); };
    $("wakeBtn").onclick = () => { settings.keepAwake = !settings.keepAwake; saveSettings(); if (settings.keepAwake) acquireWake(); else releaseWake(); };
    $("installBtn").onclick = doInstall;
    $("installX").onclick = dismissInstall;
    $("camPill").onclick = cycleCamera;
    $("wrapBtn").onclick = openWrap;
    $("wrapClose").onclick = closeWrap;
    $("wrapExport").onclick = async () => { await exportDownload(); $("wrapMsg").textContent = $("status").textContent; };
    $("wrapSend").onclick = async () => { await sendToPC(); $("wrapMsg").textContent = $("status").textContent; };
    $("syncSave").onclick = () => {
      const rec = addSync($("camTime").value, $("camName").value);
      $("syncLog").textContent = rec ? ("saved: " + rec.camera + " @ " + rec.camera_time + "\n(phone " + new Date(rec.epoch_ms).toLocaleTimeString() + ")") : "enter time as HH:MM:SS";
      if (rec) $("camTime").value = "";
    };
    $("geoBtn").onclick = async () => {
      $("geoLog").textContent = "locating...";
      const g = await captureGeo();
      $("geoLog").textContent = g.error ? ("GPS: " + g.error) : ("venue: " + g.lat + ", " + g.lon + "  (±" + g.acc + "m)");
    };
    setInterval(tick, 250); tick();
    if ("serviceWorker" in navigator) { try { navigator.serviceWorker.register("sw.js"); } catch (e) { /* offline cache optional */ } }
    probeUpload();
    fitShell();
    if (typeof window !== "undefined") {
      window.addEventListener("resize", fitShell);
      window.addEventListener("orientationchange", fitShell);
    }
    applyBig(); updateHapticBtn(); updateWakeBtn();
    acquireWake();   // keep the screen on for the shoot (re-acquired below when the app returns to the foreground)
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") { acquireWake(); fitShell(); } });
    }
    if (typeof window !== "undefined") {
      window.addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); deferredInstall = e; maybeShowInstall(); });
      window.addEventListener("appinstalled", dismissInstall);
    }
    maybeShowInstall();

    // test/automation surface
    window.FL = {
      state, settings, getExport: buildExport, selectRider, setActivity, nextRider, prevRider,
      toggleFlag, setFlagReason, logShot, logBroll, brollSummary, coverageSummary, debriefSummary, openWrap, cycleCamera, addNote, addSync, setGeo, captureGeo, sendToPC, loadStartlistData,
      acquireWake, releaseWake, isStandalone, maybeShowInstall, dismissInstall, fitShell,
    };
    window.__FL_READY = true;
  }
  // allow tests to inject a start list object directly (no network)
  function loadStartlistData(data) {
    applyStartlist(data);
    state.events = []; state.sync = []; state.geo = null; state.loggedNos = {}; state.openSeg = null;
    state.curIndex = state.riders.length > 1 ? 1 : 0; state.curActivity = null;
    buildList(); render();   // no open segment until the user taps a rider/activity
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();

// Läuft im RaceRoom-Client (-broadcastUrl=<panel>/broadcast/?t=<token>) und spiegelt die Overlay-Daten ins Panel.
// Ohne Regie-Befehl aus dem Panel folgt die Kamera automatisch dem Führenden (Verfolgerkamera).
(function () {
  var params = {}; location.search.replace(/[?&]([^=&]+)=([^&]*)/g, function (_, k, v) { params[k] = decodeURIComponent(v) });
  var token = params.t || '', el = document.getElementById('s'), state = {}, fails = 0;
  var focus = null, manualUntil = 0, AUTO_CAM = 'swingman';
  function post(path, body) {
    return fetch(location.origin + path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Bot-Token': token }, body: JSON.stringify(body) });
  }
  if (!window.r3e) { el.textContent = 'Kein RaceRoom-Client (r3e.js)'; return }
  function list(d) {
    if (Array.isArray(d)) return d;
    if (d && Array.isArray(d.driversInfo)) return d.driversInfo;
    if (d && Array.isArray(d.drivers)) return d.drivers;
    return [];
  }
  function cam(name, slot) {
    var f = r3e.setCamera && r3e.setCamera[name];
    slot = parseInt(slot, 10);
    if (!f || isNaN(slot)) return false;
    try { f(slot); focus = slot; return true } catch (e) { return false }
  }
  function autoFocus() {
    if (Date.now() < manualUntil) return;
    var ds = state.drivers || [];
    if (!ds.length) return;
    var slots = ds.map(function (x) { return x.slotId });
    if (focus !== null && slots.indexOf(focus) >= 0) return;
    var lead = ds.slice().sort(function (a, b) {
      var pa = (a.scoreInfo && a.scoreInfo.positionOverall) || 999, pb = (b.scoreInfo && b.scoreInfo.positionOverall) || 999;
      return pa - pb;
    })[0];
    cam(AUTO_CAM, lead.slotId);
  }
  function tick() {
    try {
      r3e.getDriversInfo(function (d) { state.drivers = list(d) });
      r3e.getSessionInfo(function (s) { state.session = s });
      r3e.getEventInfo(function (e) { state.event = e });
    } catch (e) { }
    autoFocus();
    post('/api/bot/ingest', state).then(function (r) { return r.json() }).then(function (res) {
      fails = 0;
      var c = res && res.command;
      if (c && cam(c.camera, c.slotId)) manualUntil = Date.now() + 5 * 60 * 1000;  // Regie hat 5 min Vorrang
      el.textContent = 'Panel-Bridge verbunden · ' + (state.drivers || []).length + ' Fahrer' + (focus !== null ? ' · Kamera Slot ' + focus : '');
    }).catch(function () { fails++; el.textContent = 'Panel-Bridge: keine Verbindung (' + fails + ')' });
  }
  try { r3e.on.resultsUpdate(function (r) { post('/api/bot/results', r) }) } catch (e) { }
  try { r3e.on.eventOccurred(function (e) { post('/api/bot/event', e) }) } catch (e) { }
  setInterval(tick, 1000); tick();
})();

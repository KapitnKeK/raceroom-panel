// Läuft im RaceRoom-Client (-broadcastUrl=<panel>/broadcast/?t=<token>) und spiegelt die Overlay-Daten ins Panel.
(function () {
  var params = {}; location.search.replace(/[?&]([^=&]+)=([^&]*)/g, function (_, k, v) { params[k] = decodeURIComponent(v) });
  var token = params.t || '', el = document.getElementById('s'), state = {}, fails = 0;
  function post(path, body) {
    return fetch(location.origin + path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Bot-Token': token }, body: JSON.stringify(body) });
  }
  if (!window.r3e) { el.textContent = 'Kein RaceRoom-Client (r3e.js)'; return }
  function tick() {
    try {
      r3e.getDriversInfo(function (d) { state.drivers = Array.isArray(d) ? d : ((d && d.driversInfo) || []) });
      r3e.getSessionInfo(function (s) { state.session = s });
      r3e.getEventInfo(function (e) { state.event = e });
    } catch (e) { }
    post('/api/bot/ingest', state).then(function (r) { return r.json() }).then(function (res) {
      fails = 0; el.textContent = 'Panel-Bridge verbunden · ' + ((state.drivers || []).length) + ' Fahrer';
      var c = res && res.command;
      if (c && r3e.setCamera && r3e.setCamera[c.camera]) r3e.setCamera[c.camera]({ slotId: c.slotId });
    }).catch(function () { fails++; el.textContent = 'Panel-Bridge: keine Verbindung (' + fails + ')' });
  }
  try { r3e.on.resultsUpdate(function (r) { post('/api/bot/results', r) }) } catch (e) { }
  try { r3e.on.eventOccurred(function (e) { post('/api/bot/event', e) }) } catch (e) { }
  setInterval(tick, 1000); tick();
})();

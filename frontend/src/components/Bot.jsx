import { useEffect, useRef, useState } from 'react'
import { Camera, Copy, Eye, Flag, LayoutPanelLeft, Maximize2, Save, Trophy, Video, Volume2, VolumeX, Wand2 } from 'lucide-react'
import Hls from 'hls.js'
import { api } from '../api'
import { Badge, Card, Field, Spinner, useAsync, useToast } from './ui'
import { driverColor } from '../utils'

export const fmtLap = (ms) => {
  if (ms == null || ms <= 0) return '–'
  const m = Math.floor(ms / 60000), s = ((ms % 60000) / 1000).toFixed(3).padStart(6, '0')
  return m ? `${m}:${s}` : s
}

const CAMS = [['cockpit', 'Cockpit'], ['onboard', 'Onboard'], ['nosecam', 'Nase'], ['swingman', 'Verfolger'], ['tv', 'TV'], ['heli', 'Heli']]


// Ansichten: [Schlüssel, Name, Gruppe]
export const VIEWS = [
  ['cockpit', 'Fahrer-POV', 'Fahrer'], ['onboard', 'Onboard', 'Fahrer'], ['nosecam', 'Nase', 'Fahrer'], ['swingman', 'Verfolger', 'Fahrer'],
  ['frontCam', 'Front', 'Am Auto'], ['rearCam', 'Heck', 'Am Auto'], ['wing', 'Flügel', 'Am Auto'], ['exhaust', 'Auspuff', 'Am Auto'],
  ['flFront', 'Rad vorn links', 'Boden'], ['frFront', 'Rad vorn rechts', 'Boden'], ['rlRear', 'Rad hinten links', 'Boden'], ['rrRear', 'Rad hinten rechts', 'Boden'], ['rlFront', 'Boden links', 'Boden'], ['rrFront', 'Boden rechts', 'Boden'],
  ['tv', 'TV-Regie', 'Strecke'], ['trackside', 'Streckenrand', 'Strecke'], ['static', 'Tribüne / fest', 'Strecke'], ['action', 'Action', 'Strecke'], ['heli', 'Hubschrauber', 'Strecke'],
]
// Schnellwahl: [Schlüssel oder 'auto', Name, Beschreibung]
const QUICK = [
  ['cockpit', 'Fahrer-POV', 'Blick aus dem Cockpit'], ['swingman', 'Verfolger', 'Hinter dem Auto'], ['tv', 'TV-Regie', 'Wie im Fernsehen'],
  ['static', 'Tribüne', 'Feste Kamera an der Strecke'], ['trackside', 'Streckenrand', 'Seitlich am Rand'], ['flFront', 'Boden', 'Rad-Kamera knapp über dem Asphalt'],
  ['heli', 'Hubschrauber', 'Von oben'], ['auto', 'Automatisch', 'Folgt dem Führenden'],
]

export function Player({ className = '' }) {
  const ref = useRef(null)
  const [ok, setOk] = useState(null)
  const [muted, setMuted] = useState(true)
  useEffect(() => {
    const src = '/api/bot/hls/stream.m3u8'
    let hls
    fetch(src, { credentials: 'same-origin' }).then((r) => {
      if (!r.ok) { setOk(false); return }
      setOk(true)
      const v = ref.current
      if (Hls.isSupported()) {
        hls = new Hls({ enableWorker: false, liveSyncDurationCount: 2, xhrSetup: (x) => { x.withCredentials = true } })
        hls.loadSource(src); hls.attachMedia(v)
      } else if (v.canPlayType('application/vnd.apple.mpegurl')) { v.src = src }
    }).catch(() => setOk(false))
    return () => hls?.destroy()
  }, [])
  return (
    <div className={`relative aspect-video overflow-hidden rounded-lg border border-ink-600 bg-black ${className}`}>
      {ok !== false && <video ref={ref} className="h-full w-full" muted autoPlay playsInline controls onVolumeChange={(e) => setMuted(e.target.muted)} />}
      {ok && <button className="absolute right-3 top-3 flex items-center gap-1.5 rounded-lg bg-black/70 px-3 py-1.5 text-xs font-medium text-white backdrop-blur hover:bg-black/85" onClick={() => { const v = ref.current; if (v) { v.muted = !v.muted; if (!v.muted && v.volume === 0) v.volume = 0.8; v.play().catch(() => {}) } }}>{muted ? <><VolumeX size={14} /> Ton an</> : <><Volume2 size={14} /> Ton aus</>}</button>}
      {ok === null && <div className="absolute inset-0 grid place-items-center"><Spinner /></div>}
      {ok === false && <div className="absolute inset-0 grid place-items-center text-sm text-ink-300">Kein Videostream verfügbar</div>}
    </div>
  )
}

export function BotCamera({ bot, names, canHost, map }) {
  const [busy, run] = useAsync()
  const [view, setView] = useState('swingman')
  const [layout, setLayout] = useState('video')
  const [auto, setAuto] = useState(true)
  useEffect(() => { const c = bot?.last_cmd; if (c) { if (c.camera === 'auto') setAuto(true); else { setAuto(false); setView(c.camera) } } }, [bot?.last_cmd?.ts])
  if (!bot?.connected) return <Card title="Live-Kamera"><p className="py-8 text-center text-sm text-ink-300">Der Zuschauer-Bot ist gerade nicht mit diesem Server verbunden.</p></Card>
  const drivers = bot.drivers || []
  const focus = bot.focus
  const cur = drivers.find((d) => d.slot === focus)
  const nm = (d) => names[String(d.user_id)]?.name || d.name || `Slot ${d.slot}`
  const send = (camera, slot) => run(() => api('/api/bot/camera', { method: 'POST', body: { slot, camera } }), camera === 'auto' ? 'Kamera folgt dem Führenden' : 'Kamera umgeschaltet')
  const pickView = (k) => { setView(k); setAuto(false); const slot = focus ?? drivers[0]?.slot; if (slot != null) send(k, slot) }
  const pickDriver = (d) => { setAuto(false); send(view, d.slot) }
  const goAuto = () => { setAuto(true); send('auto', 0) }
  const viewLabel = VIEWS.find((v) => v[0] === view)?.[1] || view
  const groups = ['Fahrer', 'Am Auto', 'Boden', 'Strecke']
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="red" dot>Live</Badge>
        <span className="text-sm text-ink-200">{cur ? <>Folgt <b>{nm(cur)}</b></> : 'Kein Fahrer im Fokus'} · Ansicht <b>{viewLabel}</b>{auto ? ' · automatisch' : ''}</span>
        <div className="ml-auto flex gap-1">
          <button className={`btn-sm ${layout === 'video' ? 'btn-primary' : 'btn-ghost'}`} onClick={() => setLayout('video')}><Maximize2 size={13} /> Video</button>
          <button className={`btn-sm ${layout === 'split' ? 'btn-primary' : 'btn-ghost'}`} onClick={() => setLayout('split')}><LayoutPanelLeft size={13} /> Video + Karte</button>
        </div>
      </div>
      <div className={layout === 'split' ? 'grid gap-4 xl:grid-cols-2' : ''}>
        <Player />
        {layout === 'split' && <div className="h-[360px] overflow-hidden rounded-lg border border-ink-600 xl:h-auto xl:min-h-[360px]">{map}</div>}
      </div>
      {canHost ? (
        <Card title="Ansicht">
          <div className="mb-4 grid grid-cols-2 gap-2 sm:grid-cols-4">
            {QUICK.map(([k, l, d]) => {
              const on = k === 'auto' ? auto : (!auto && view === k)
              return <button key={k} disabled={busy} title={d} onClick={() => (k === 'auto' ? goAuto() : pickView(k))}
                className={`rounded-lg border px-3 py-2.5 text-left transition ${on ? 'border-accent bg-accent/15' : 'border-ink-600 hover:border-ink-400'}`}>
                <span className="block text-sm font-semibold">{l}</span><span className="block text-xs text-ink-300">{d}</span></button>
            })}
          </div>
          <div className="space-y-3 border-t border-ink-600 pt-3">
            <p className="text-xs uppercase tracking-wider text-ink-400">Alle Kameras</p>
            {groups.map((g) => (
              <div key={g} className="flex flex-wrap items-center gap-1.5">
                <span className="w-20 text-xs uppercase tracking-wider text-ink-400">{g}</span>
                {VIEWS.filter((v) => v[2] === g).map(([k, l]) => <button key={k} disabled={busy} className={`btn-sm ${view === k && !auto ? 'btn-primary' : 'btn-ghost'}`} onClick={() => pickView(k)}>{l}</button>)}
              </div>
            ))}
          </div>
        </Card>
      ) : <p className="text-xs text-ink-400">Die Kamera steuern Hosts und Admins.</p>}
      <Card title="Fahrer" pad={false}>
        <div className="flex flex-wrap gap-2 p-4">
          {drivers.length === 0 && <span className="text-sm text-ink-300">Noch keine Fahrer auf der Strecke.</span>}
          {drivers.map((d) => (
            <button key={d.slot} disabled={!canHost || busy} onClick={() => pickDriver(d)}
              className={`flex items-center gap-2 rounded-lg border px-3 py-2 text-left text-sm transition ${d.slot === focus ? 'border-accent bg-accent/10' : 'border-ink-600 hover:border-ink-400'} ${canHost ? '' : 'cursor-default'}`}>
              <span className="font-display text-lg font-semibold tabular-nums">{d.pos > 0 ? d.pos : '–'}</span>
              <span className="inline-block h-6 w-1 rounded" style={{ background: driverColor(d.user_id) }} />
              <span><span className="block leading-tight">{nm(d)}</span><span className="block text-xs text-ink-300 tabular-nums">{d.laps ?? 0} Rd. · {fmtLap(d.best_ms)}</span></span>
            </button>
          ))}
        </div>
      </Card>
    </div>
  )
}

export function BotStandings({ bot, names, canHost }) {
  const [busy, run] = useAsync()
  const [cam, setCam] = useState('cockpit')
  if (!bot?.connected) return null
  const s = bot.session || {}
  const setCamera = (slot) => run(() => api('/api/bot/camera', { method: 'POST', body: { slot, camera: cam } }), 'Kamera umgeschaltet')
  return (
    <Card title="Rangliste (Zuschauer-Bot)" pad={false} right={<span className="flex items-center gap-2 pr-1 text-xs text-ink-300"><Badge tone="green" dot>Bot live</Badge>{s.type} · {s.phase}{s.timeLeft > 0 ? ` · noch ${Math.floor(s.timeLeft / 60)}:${String(s.timeLeft % 60).padStart(2, '0')}` : ''}</span>}>
      {canHost && (
        <div className="flex flex-wrap items-center gap-1.5 px-5 pb-2">
          <span className="text-xs text-ink-300">Kamera:</span>
          {CAMS.map(([k, l]) => <button key={k} className={`btn-sm ${cam === k ? 'btn-primary' : 'btn-ghost'}`} onClick={() => setCam(k)}>{l}</button>)}
          <span className="text-xs text-ink-400">→ dann beim Fahrer auf <Camera size={12} className="inline" /> klicken</span>
        </div>
      )}
      <div className="overflow-x-auto">
        <table className="w-full min-w-[560px]">
          <thead><tr className="border-b border-ink-600"><th className="th w-12">Pos</th><th className="th">Fahrer</th><th className="th text-right">Runden</th><th className="th text-right">Aktuell</th><th className="th text-right">Beste</th><th className="th text-right">Abstand</th>{canHost && <th className="th text-right">Kamera</th>}</tr></thead>
          <tbody>
            {bot.drivers.map((d) => (
              <tr key={d.slot} className="border-b border-ink-700/70">
                <td className="td font-display text-xl font-semibold tabular-nums">{d.pos > 0 ? d.pos : '–'}</td>
                <td className="td"><span className="mr-2 inline-block h-3 w-1 rounded" style={{ background: driverColor(d.user_id) }} />{names[String(d.user_id)]?.name || d.name}{d.flags?.blue ? <Badge tone="blue" className="ml-2"><Flag size={10} /> blau</Badge> : null}{d.flags?.yellow ? <Badge tone="amber" className="ml-2"><Flag size={10} /> gelb</Badge> : null}</td>
                <td className="td text-right tabular-nums">{d.laps ?? '–'}</td>
                <td className="td text-right tabular-nums text-ink-300">{fmtLap(d.current_ms)}</td>
                <td className={`td text-right tabular-nums ${d.best_valid === false ? 'text-ink-400 line-through' : ''}`}>{fmtLap(d.best_ms)}</td>
                <td className="td text-right tabular-nums text-ink-300">{d.pos === 1 ? 'Führung' : d.gap_laps > 0 ? `+${d.gap_laps} Rd.` : d.gap_ms > 0 ? `+${(d.gap_ms / 1000).toFixed(1)} s` : '–'}</td>
                {canHost && <td className="td text-right"><button className="btn-ghost btn-sm !px-2" disabled={busy} title={`Kamera: ${cam}`} onClick={() => setCamera(d.slot)}><Camera size={14} /></button></td>}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  )
}

export function BotVideo() {
  return (
    <Card title="Bot-Kamera" right={<Badge tone="red" dot>Live</Badge>}>
      <Player />
    </Card>
  )
}

export function BotSetup() {
  const toast = useToast()
  const [cfg, setCfg] = useState(null)
  const [hls, setHls] = useState('')
  const [busy, run] = useAsync()
  useEffect(() => { api('/api/bot/config').then((c) => { setCfg(c); setHls(c.hls_url || '') }).catch(() => {}) }, [])
  if (!cfg) return null
  const url = `${location.origin}/broadcast/?t=${cfg.token}`
  const save = async (rotate) => { const r = await run(() => api('/api/bot/config', { method: 'PUT', body: { hls_url: hls, rotate } }), rotate ? 'Neuer Token erzeugt' : 'Gespeichert'); if (r) setCfg({ ...cfg, token: r.token, hls_url: hls }) }
  return (
    <Card title="Zuschauer-Bot" right={<Eye size={16} className="text-ink-400" />} className="lg:col-span-2">
      <p className="mb-3 text-sm text-ink-300">Ein RaceRoom-Client tritt dem Server als Zuschauer bei und liefert Rangliste, Rundenzeiten und ein ferngesteuertes Kamerabild. Startoption im Bot: <code className="rounded bg-ink-900 px-1">-broadcastUrl=&lt;Adresse unten&gt;</code> (im LAN statt der Domain die IP des Panels verwenden).</p>
      <Field label="Zuschauer-Adresse (enthält den geheimen Bot-Token)">
        <div className="flex gap-2"><input className="input font-mono text-xs" readOnly value={url} /><button className="btn-ghost !px-2.5" onClick={async () => { await navigator.clipboard?.writeText(url); toast('Kopiert', 'ok') }}><Copy size={15} /></button></div>
      </Field>
      <div className="mt-3 grid gap-3 sm:grid-cols-[1fr_auto_auto]">
        <Field label="Videostream (HLS-Basisadresse des Bots)" hint="z. B. http://192.0.2.10:8888/hls — das Panel reicht den Stream nur an angemeldete Benutzer durch."><input className="input" value={hls} onChange={(e) => setHls(e.target.value)} placeholder="http://<bot>:8888/hls" /></Field>
        <button className="btn-primary self-end" disabled={busy} onClick={() => save(false)}><Save size={15} /> Speichern</button>
        <button className="btn-danger self-end" disabled={busy} onClick={() => save(true)}>Token erneuern</button>
      </div>
    </Card>
  )
}

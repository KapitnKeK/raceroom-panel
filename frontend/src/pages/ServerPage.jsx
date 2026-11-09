import { useEffect, useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { ArrowLeft, Ban, FastForward, Gauge, Lock, MapPin, Megaphone, Play, Radio, RotateCcw, Save, Settings, Square, Trash2, Tv, Users, Video } from 'lucide-react'
import { api, can } from '../api'
import { useAuth } from '../auth'
import { useLive } from '../live'
import TrackMap from '../components/TrackMap'
import Drivers, { Avatar } from '../components/Drivers'
import SpeedChart from '../components/SpeedChart'
import { Chat, EventFeed } from '../components/Feed'
import Editor from './Editor'
import { BotCamera, BotSetup, BotStandings, BotVideo } from '../components/Bot'
import { Badge, Card, Confirm, Empty, Field, Spinner, useAsync, useToast } from '../components/ui'
import { INCIDENT_TYPES, driverColor, fmtClock, flagEmoji, liveryImg } from '../utils'

function StreamEmbed({ stream }) {
  const host = window.location.hostname
  const src = stream.provider === 'twitch'
    ? `https://player.twitch.tv/?channel=${encodeURIComponent(stream.id)}&parent=${encodeURIComponent(host)}&muted=true&autoplay=true`
    : `https://www.youtube-nocookie.com/embed/${encodeURIComponent(stream.id)}?autoplay=1&mute=1`
  return (
    <div className="aspect-video overflow-hidden rounded-lg border border-ink-600 bg-black">
      <iframe src={src} title="Live-Stream" className="h-full w-full" allow="autoplay; fullscreen; picture-in-picture" allowFullScreen referrerPolicy="strict-origin-when-cross-origin" />
    </div>
  )
}

function Tile({ icon: Icon, label, value, sub, tone = '' }) {
  return (
    <div className="card flex items-center gap-3 px-3 py-3 sm:px-4">
      <span className="hidden h-10 w-10 shrink-0 place-items-center rounded-lg bg-ink-700 sm:grid"><Icon size={18} className={tone || 'text-ink-200'} /></span>
      <div className="min-w-0">
        <div className="truncate font-display text-2xl font-semibold leading-none tabular-nums">{value}</div>
        <div className="mt-0.5 truncate text-xs uppercase tracking-wider text-ink-300">{label}{sub ? ` · ${sub}` : ''}</div>
      </div>
    </div>
  )
}

function DriverDetail({ p, info, history }) {
  const col = driverColor(p.user_id)
  const byType = {}
  for (const i of p.incidents) { byType[i.type] = byType[i.type] || { n: 0, pts: 0 }; byType[i.type].n++; byType[i.type].pts += i.points || 0 }
  return (
    <div>
      <div className="mb-3 flex items-center gap-3">
        <Avatar info={info} uid={p.user_id} size={44} />
        <div className="min-w-0">
          <div className="truncate font-display text-xl font-semibold leading-tight">{info?.name || `Fahrer ${p.user_id}`} <span className="text-base">{flagEmoji(info?.country?.code)}</span></div>
          <div className="truncate text-sm text-ink-300">{info?.team || 'Kein Team'}</div>
        </div>
      </div>
      <div className="mb-3 flex items-center gap-3 rounded-lg bg-ink-900/60 p-2.5">
        <img src={liveryImg(p.livery_id)} alt="" referrerPolicy="no-referrer" className="h-14 w-24 shrink-0 rounded border border-ink-600 bg-ink-900 object-cover" onError={(e) => { e.currentTarget.style.visibility = 'hidden' }} />
        <div className="min-w-0 text-sm">
          <div className="truncate font-medium">{p.car || `Livery ${p.livery_id}`}</div>
          <div className="truncate text-ink-300">{[p.class, p.livery].filter(Boolean).join(' · ')}</div>
        </div>
      </div>
      <div className="mb-3 grid grid-cols-3 gap-2 text-center">
        <div className="rounded-lg bg-ink-900/60 py-2"><div className="font-display text-2xl font-semibold tabular-nums leading-none" style={{ color: col }}>{Math.round(p.kmh)}</div><div className="mt-1 text-[11px] uppercase tracking-wider text-ink-300">km/h</div></div>
        <div className="rounded-lg bg-ink-900/60 py-2"><div className="font-display text-2xl font-semibold tabular-nums leading-none">{Math.round(p.ping)}</div><div className="mt-1 text-[11px] uppercase tracking-wider text-ink-300">ms Ping</div></div>
        <div className="rounded-lg bg-ink-900/60 py-2"><div className="font-display text-2xl font-semibold tabular-nums leading-none text-warn">{p.incident_points}</div><div className="mt-1 text-[11px] uppercase tracking-wider text-ink-300">Vorfallpkt.</div></div>
      </div>
      <SpeedChart samples={history} color={col} />
      {Object.keys(byType).length > 0 && (
        <ul className="mt-3 space-y-1 text-sm">
          {Object.entries(byType).map(([t, v]) => (
            <li key={t} className="flex justify-between rounded bg-ink-900/50 px-2.5 py-1.5"><span>{v.n}× {INCIDENT_TYPES[t] || 'Vorfall'}</span><span className="text-warn">+{v.pts}</span></li>
          ))}
        </ul>
      )}
    </div>
  )
}

/* ------------------------------ Steuerung ------------------------------ */
function ControlTab({ server, names, isAdmin, stream, setStream }) {
  const toast = useToast()
  const [busy, run] = useAsync()
  const [motd, setMotd] = useState('')
  const [wl, setWl] = useState('')
  const [bans, setBans] = useState([])
  const [streamUrl, setStreamUrl] = useState('')
  const [confirm, setConfirm] = useState(null)
  const running = server.state.running

  useEffect(() => {
    api(`/api/servers/${server.id}/motd`).then((r) => setMotd(r.message)).catch(() => {})
    api(`/api/servers/${server.id}/whitelist`).then((r) => setWl(r.users.join('\n'))).catch(() => {})
    api('/api/bans').then((r) => setBans(r.users || [])).catch(() => {})
  }, [server.id])

  const parseIds = () => [...new Set(wl.split(/[\s,;]+/).filter(Boolean).map(Number))]
  const idsOk = parseIds().every((n) => Number.isInteger(n) && n > 0)

  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <Card title="Session-Steuerung" right={!running && <Badge>Server gestoppt</Badge>}>
        <p className="mb-3 text-sm text-ink-300">Greift direkt in die laufende Session ein. Die Fahrer sehen den Wechsel sofort.</p>
        <div className="flex flex-wrap gap-2">
          <button className="btn-ghost" disabled={!running} onClick={() => setConfirm('restart')}><RotateCcw size={15} /> Session neu starten</button>
          <button className="btn-ghost" disabled={!running} onClick={() => setConfirm('next')}><FastForward size={15} /> Zur nächsten Session</button>
        </div>
      </Card>

      <Card title="Nachricht des Tages" right={<Megaphone size={16} className="text-ink-400" />}>
        <Field hint="Wird jedem Fahrer beim Beitritt angezeigt (max. 1000 Zeichen).">
          <textarea className="input min-h-24" maxLength={1000} value={motd} onChange={(e) => setMotd(e.target.value)} placeholder="Willkommen! Bitte fair fahren." />
        </Field>
        <button className="btn-primary mt-3" disabled={busy} onClick={() => run(() => api(`/api/servers/${server.id}/motd`, { method: 'PUT', body: { message: motd } }), 'Nachricht gespeichert')}>{busy ? <Spinner /> : <Save size={15} />} Speichern</button>
      </Card>

      <Card title="Live-Stream einbinden" right={<Video size={16} className="text-ink-400" />}>
        <p className="mb-3 text-sm text-ink-300">
          Der Server selbst sendet kein Bild. Wer das Rennen filmen möchte (z. B. ein Zuschauer-Client mit OBS), streamt zu Twitch oder YouTube und trägt den Link hier ein. Er erscheint dann auf der Live-Seite.
        </p>
        <Field label="Twitch-Kanal oder YouTube-Link" hint={stream ? `Aktiv: ${stream.provider === 'twitch' ? 'Twitch' : 'YouTube'} (${stream.id})` : 'z. B. https://www.twitch.tv/dein-kanal'}>
          <input className="input" value={streamUrl} onChange={(e) => setStreamUrl(e.target.value)} placeholder="https://www.twitch.tv/…" />
        </Field>
        <div className="mt-3 flex gap-2">
          <button className="btn-primary" disabled={busy || (!streamUrl && !stream)} onClick={async () => { const r = await run(() => api(`/api/servers/${server.id}/stream`, { method: 'PUT', body: { url: streamUrl } }), streamUrl ? 'Stream gespeichert' : 'Stream entfernt'); if (r) { setStream(r.stream); setStreamUrl('') } }}>{busy ? <Spinner /> : <Save size={15} />} {streamUrl ? 'Speichern' : 'Entfernen'}</button>
        </div>
      </Card>

      <Card title="Zuschauer-Whitelist" right={<Tv size={16} className="text-ink-400" />}>
        <Field hint="RaceRoom-Benutzer-IDs, die dem Server als Zuschauer beitreten dürfen. Eine ID pro Zeile.">
          <textarea className="input min-h-24 font-mono" value={wl} onChange={(e) => setWl(e.target.value)} placeholder="100230" />
        </Field>
        {!idsOk && <p className="mt-1 text-xs text-red-300">Nur Zahlen erlaubt (eine ID pro Zeile).</p>}
        <button className="btn-primary mt-3" disabled={busy || !idsOk} onClick={() => run(() => api(`/api/servers/${server.id}/whitelist`, { method: 'PUT', body: { users: parseIds() } }), 'Whitelist gespeichert')}>{busy ? <Spinner /> : <Save size={15} />} Speichern</button>
      </Card>

      {isAdmin && <BotSetup />}

      <Card title="Sperrliste" right={<Ban size={16} className="text-ink-400" />} className="lg:col-span-2">
        {bans.length === 0 ? <p className="text-sm text-ink-300">Es ist niemand gesperrt.</p> : (
          <ul className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {bans.map((id) => (
              <li key={id} className="flex items-center justify-between rounded-lg border border-ink-600 bg-ink-900/50 px-3 py-2 text-sm">
                <span>{names[String(id)]?.name || `Fahrer ${id}`} <span className="text-xs text-ink-400">#{id}</span></span>
                {isAdmin && <button className="btn-ghost btn-sm" onClick={async () => { await run(() => api(`/api/bans/${id}`, { method: 'DELETE' }), 'Sperre aufgehoben'); setBans((b) => b.filter((x) => x !== id)) }}>Aufheben</button>}
              </li>
            ))}
          </ul>
        )}
        <p className="hint">Die Sperrliste gilt für alle Server. Sperren verhängst du in der Fahrerliste, aufheben kann sie ein Admin.</p>
      </Card>

      <Confirm open={confirm === 'restart'} danger title="Session neu starten?" confirmLabel="Neu starten" busy={busy} onClose={() => setConfirm(null)}
        text="Die aktuelle Session beginnt von vorn. Bisherige Zeiten dieser Session gehen verloren."
        onConfirm={async () => { await run(() => api(`/api/servers/${server.id}/session/restart`, { method: 'POST' }), 'Session wird neu gestartet'); setConfirm(null) }} />
      <Confirm open={confirm === 'next'} title="Zur nächsten Session wechseln?" confirmLabel="Weiter" busy={busy} onClose={() => setConfirm(null)}
        text="Die aktuelle Session wird beendet und die nächste beginnt sofort."
        onConfirm={async () => { await run(() => api(`/api/servers/${server.id}/session/next`, { method: 'POST' }), 'Wechsel zur nächsten Session'); setConfirm(null) }} />
    </div>
  )
}

/* -------------------------------- Seite -------------------------------- */
export default function ServerPage() {
  const { id } = useParams()
  const sid = Number(id)
  const [params, setParams] = useSearchParams()
  const { user } = useAuth()
  const { snap, hist } = useLive()
  const [sel, setSel] = useState(null)
  const [stream, setStream] = useState(null)
  const [busy, run] = useAsync()
  const [stopAsk, setStopAsk] = useState(false)
  const canHost = can(user, 'host')
  const isAdmin = can(user, 'admin')
  const tab = canHost ? params.get('tab') || 'live' : 'live'

  const server = snap?.servers.find((s) => s.id === sid)
  const names = snap?.names || {}

  useEffect(() => { api(`/api/servers/${sid}/stream`).then((r) => setStream(r.stream)).catch(() => {}) }, [sid])
  useEffect(() => {
    const players = server?.state.players || []
    if (sel != null && !players.some((p) => p.user_id === sel)) setSel(null)
    if (sel == null && players.length) setSel(players[0].user_id)
  }, [server?.state.players]) // eslint-disable-line react-hooks/exhaustive-deps

  const selPlayer = useMemo(() => server?.state.players.find((p) => p.user_id === sel) || null, [server, sel])

  if (!snap) return <div className="grid place-items-center py-24"><Spinner className="text-accent" /></div>
  if (!server) return <div className="card"><Empty icon={Radio} title="Server nicht gefunden" text="Dieser Server existiert nicht (mehr)."><Link to="/" className="btn-ghost mt-2"><ArrowLeft size={15} /> Zur Übersicht</Link></Empty></div>

  const st = server.state
  const players = st.players
  const online = Math.max(players.length, st.players_online || 0)
  const waiting = st.running && online === 0
  const start = () => run(() => api(`/api/servers/${sid}/start`, { method: 'POST' }), 'Server wird gestartet')
  const stop = async () => { await run(() => api(`/api/servers/${sid}/stop`, { method: 'POST' }), 'Server wird gestoppt'); setStopAsk(false) }
  const setTab = (t) => setParams(t === 'live' ? {} : { tab: t }, { replace: true })

  return (
    <div>
      <Link to="/" className="mb-3 inline-flex items-center gap-1.5 text-sm text-ink-300 hover:text-ink-100"><ArrowLeft size={15} /> Alle Server</Link>

      <header className="mb-5 flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="truncate text-4xl font-bold tracking-wide">{server.name}</h1>
            {st.running ? <Badge tone="green" dot>Läuft</Badge> : <Badge>Gestoppt</Badge>}
            {server.has_password && <Badge><Lock size={11} /> Passwort</Badge>}
          </div>
          <div className="mt-1.5 flex items-center gap-1.5 text-ink-200"><MapPin size={15} className="text-ink-300" />{server.layout.track} <span className="text-ink-300">· {server.layout.name}</span></div>
        </div>
        {canHost && (
          <div className="flex gap-2">
            {st.running
              ? <button className="btn-danger" disabled={busy} onClick={() => (online ? setStopAsk(true) : stop())}>{busy ? <Spinner /> : <Square size={14} />} Server stoppen</button>
              : <button className="btn-ok" disabled={busy} onClick={start}>{busy ? <Spinner /> : <Play size={14} />} Server starten</button>}
          </div>
        )}
      </header>

      <div className="mb-5 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Tile icon={Users} label="Fahrer" value={st.running ? `${online}/${server.max_players}` : '–'} tone={online ? 'text-accent-hi' : ''} />
        <Tile icon={Radio} label="Session" value={st.running ? (waiting ? 'Wartet' : st.session_label || '–') : '–'} />
        <Tile icon={Gauge} label="Restzeit" value={st.running && !waiting ? fmtClock(st.time_left) : '–'} />
        <Tile icon={Video} label="Live-Takt" value={`${server.state_report_ms} ms`} sub={server.state_report_ms >= 3000 && canHost ? 'zu grob' : undefined} tone={server.state_report_ms >= 3000 ? 'text-warn' : 'text-ok'} />
      </div>

      <div className="mb-5 flex gap-1 border-b border-ink-600">
        <button className={`tab ${tab === 'live' ? 'tab-active' : ''}`} onClick={() => setTab('live')}><Radio size={14} className="mr-1.5 inline" />Live</button>
        {snap.bot?.connected && <button className={`tab ${tab === 'camera' ? 'tab-active' : ''}`} onClick={() => setTab('camera')}><Video size={14} className="mr-1.5 inline" />Kamera</button>}
        {canHost && <button className={`tab ${tab === 'control' ? 'tab-active' : ''}`} onClick={() => setTab('control')}><Megaphone size={14} className="mr-1.5 inline" />Steuerung</button>}
        {canHost && <button className={`tab ${tab === 'settings' ? 'tab-active' : ''}`} onClick={() => setTab('settings')}><Settings size={14} className="mr-1.5 inline" />Einstellungen</button>}
      </div>

      {tab === 'live' && (
        <div className="grid gap-5 xl:grid-cols-[1fr_350px]">
          <div className="min-w-0 space-y-5">
            <Card title="Live-Karte" pad={false} className="overflow-hidden">
              <div className="h-[460px] p-3 sm:h-[540px]">
                <TrackMap layoutId={server.layout_id} players={players} names={names} hist={hist} serverId={sid} selected={sel} onSelect={setSel} reportMs={server.state_report_ms} />
              </div>
            </Card>
            <BotStandings bot={snap.bot} names={names} canHost={canHost} />
            <Card title={`Fahrer${st.running ? ` (${online})` : ''}`} pad={false}>
              <Drivers server={server} names={names} selected={sel} onSelect={setSel} canHost={canHost} isAdmin={isAdmin} />
            </Card>
          </div>
          <div className="min-w-0 space-y-5">
            {snap.bot?.connected && <BotVideo />}
            {stream && <Card title="Live-Stream" right={<Badge tone="red" dot>Stream</Badge>}><StreamEmbed stream={stream} /></Card>}
            <Card title="Ausgewählter Fahrer">
              {selPlayer
                ? <DriverDetail p={selPlayer} info={names[String(selPlayer.user_id)]} history={hist.current.get(`${sid}:${selPlayer.user_id}`)} />
                : <p className="py-6 text-center text-sm text-ink-300">Klicke einen Fahrer in der Karte oder Liste an, um Details und den Geschwindigkeitsverlauf zu sehen.</p>}
            </Card>
            <Card title="Live-Feed"><EventFeed events={server.events} names={names} /></Card>
            <Card title="Chat"><Chat server={server} canHost={canHost} /></Card>
          </div>
        </div>
      )}

      {tab === 'camera' && (
        <BotCamera bot={snap.bot} names={names} canHost={canHost}
          map={<TrackMap layoutId={server.layout_id} players={players} names={names} hist={hist} serverId={sid} selected={sel} onSelect={setSel} reportMs={server.state_report_ms} />} />
      )}

      {tab === 'control' && canHost && <ControlTab server={server} names={names} isAdmin={isAdmin} stream={stream} setStream={setStream} />}
      {tab === 'settings' && canHost && <Editor serverId={sid} embedded running={st.running} />}

      <Confirm open={stopAsk} danger title="Server stoppen?" confirmLabel="Stoppen" busy={busy} onClose={() => setStopAsk(false)} onConfirm={stop}
        text={`Auf „${server.name}" sind gerade ${online} Fahrer. Sie werden getrennt und die laufende Session geht verloren.`} />
    </div>
  )
}

"""Live-Daten für das RaceRoom Panel.

* pollt den RaceRoom-Manager (alle 0,5-2 s) und baut einen Live-Snapshot
* reichert Fahrer an (Name/Avatar über game.raceroom.com, Auto/Klasse aus dem Content)
* berechnet Geschwindigkeit aus Positionssprüngen (Δ SessionTime des Servers)
* schreibt einen Live-Feed (Beitritte, Vorfälle, Session-Wechsel, Start/Stopp)
* lernt den Streckenumriss aus den tatsächlich gefahrenen X/Z-Punkten (pro Layout, SQLite)
* liefert alles per Server-Sent Events (/api/live)
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import time
from collections import deque
from typing import Any, Callable, Literal, Optional

import httpx
from . import geo_auto
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field


def is_spectator(p: dict) -> bool:
    """Zuschauer (z. B. der Broadcast-Bot) stehen in der Spielerliste ohne Auto: Position exakt 0/0, kein Ping."""
    ping = p.get("LatestPingMilliSec")
    return p.get("X") == 0 and p.get("Z") == 0 and (ping is None or ping < 0)


def drivers_only(players: Optional[list]) -> list:
    return [p for p in (players or []) if not is_spectator(p)]

GRID = 3.0            # Meter pro Zelle des Streckenumrisses
RECORD_MIN_KMH = 25.0  # darunter wird nicht aufgezeichnet (Box, Start, Stillstand)
MAX_SEGMENT_M = 70.0   # längere Sprünge werden nicht interpoliert
MAX_SUBSCRIBERS = 60
SESSION_ORDER = ["Practice", "Qualify", "Warmup", "Race1", "Race2", "Race3"]
SESSION_LABEL = {"Practice": "Training", "Qualify": "Qualifying", "Warmup": "Warm-up", "Race1": "Rennen 1", "Race2": "Rennen 2", "Race3": "Rennen 3"}


class Live:
    def __init__(self, d: dict[str, Callable]):
        self.d = d
        self.web = httpx.AsyncClient(timeout=8.0, headers={"User-Agent": "r3e-panel/1.0"}, follow_redirects=False)
        self.sem = asyncio.Semaphore(3)
        self.subs: set[asyncio.Queue] = set()
        self.snapshot: dict[str, Any] = {"ts": 0, "manager_ok": False, "servers": [], "names": {}}
        self._last_json = ""
        self.users: dict[int, dict] = {}          # Fahrer-Infos (Cache)
        self.retry: dict[int, float] = {}
        self.fetching: set[int] = set()
        self.srv: dict[int, dict] = {}            # Zustand pro Server
        self.pl: dict[tuple[int, int], dict] = {}  # Zustand pro (Server, Fahrer)
        self.events: dict[int, deque] = {}
        self.chat_ts: dict[int, float] = {}
        self.chat: dict[int, list] = {}
        self._idx_src: Any = None
        self.liv: dict[int, tuple[dict, str]] = {}
        self.classes: dict[int, str] = {}
        self.layouts: dict[int, tuple[str, str]] = {}
        self.delta: dict[int, dict] = {}
        self.bot: dict[str, Any] = {"ts": 0}
        self.bot_cmd: Optional[dict] = None
        self.bot_events: deque = deque(maxlen=50)
        self.visits: dict[tuple[int, int], dict] = {}
        self.visit_done: list[dict] = []
        self.stats = {"polls": 0, "errors": 0, "cells_written": 0}

    # ------------------------------------------------------------------ DB
    def init_db(self) -> None:
        with self.d["db"]() as c:
            c.execute("CREATE TABLE IF NOT EXISTS track_cells(layout_id INTEGER NOT NULL, gx INTEGER NOT NULL, gz INTEGER NOT NULL, n INTEGER NOT NULL DEFAULT 1, vsum REAL NOT NULL DEFAULT 0, PRIMARY KEY(layout_id,gx,gz))")
            if "vsum" not in [r["name"] for r in c.execute("PRAGMA table_info(track_cells)")]:
                c.execute("ALTER TABLE track_cells ADD COLUMN vsum REAL NOT NULL DEFAULT 0")
            c.execute("CREATE TABLE IF NOT EXISTS driver_stats(user_id INTEGER PRIMARY KEY, first_seen INTEGER, last_seen INTEGER, seconds REAL DEFAULT 0, drive_seconds REAL DEFAULT 0, meters REAL DEFAULT 0, top_kmh REAL DEFAULT 0, kmh_sum REAL DEFAULT 0, kmh_n INTEGER DEFAULT 0, ping_sum REAL DEFAULT 0, ping_n INTEGER DEFAULT 0, incident_points INTEGER DEFAULT 0, visits INTEGER DEFAULT 0)")
            c.execute("CREATE TABLE IF NOT EXISTS driver_cars(user_id INTEGER, car_id INTEGER, name TEXT, class TEXT, seconds REAL DEFAULT 0, PRIMARY KEY(user_id,car_id))")
            c.execute("CREATE TABLE IF NOT EXISTS driver_tracks(user_id INTEGER, layout_id INTEGER, seconds REAL DEFAULT 0, meters REAL DEFAULT 0, top_kmh REAL DEFAULT 0, PRIMARY KEY(user_id,layout_id))")
            c.execute("CREATE TABLE IF NOT EXISTS driver_visits(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, server_name TEXT, layout_id INTEGER, car TEXT, start INTEGER, end INTEGER, seconds REAL, meters REAL, top_kmh REAL, incident_points INTEGER)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_visits_user ON driver_visits(user_id, start)")
            c.execute("CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT)")
            c.execute("CREATE TABLE IF NOT EXISTS bot_results(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, data TEXT)")
            c.execute("CREATE TABLE IF NOT EXISTS geo_attempts(track_id INTEGER PRIMARY KEY, cells INTEGER, ts INTEGER, ok INTEGER, note TEXT)")
            c.execute("CREATE TABLE IF NOT EXISTS driver_cache(id INTEGER PRIMARY KEY, data TEXT NOT NULL, ts INTEGER NOT NULL)")
            for row in c.execute("SELECT id,data FROM driver_cache"):
                try:
                    self.users[row["id"]] = json.loads(row["data"])
                except Exception:
                    pass

    def _write_cells(self, rows: list[tuple[int, int, int, float]]) -> None:
        with self.d["db"]() as c:
            c.executemany(
                "INSERT INTO track_cells(layout_id,gx,gz,n,vsum) VALUES(?,?,?,1,?) ON CONFLICT(layout_id,gx,gz) DO UPDATE SET n=n+1, vsum=vsum+excluded.vsum",
                rows,
            )

    # ------------------------------------------------------------- Helfer
    def _index(self, cdata: dict) -> None:
        if self._idx_src is cdata:
            return
        self.liv = {}
        for e in cdata["Liveries"]:
            for l in e["Liveries"]:
                self.liv[l["Id"]] = (e["Car"], l["Name"])
        self.classes = {c["Id"]: c["Name"] for c in cdata["CarClasses"]}
        self.layouts = {l["Id"]: (t["Name"], l["Name"]) for t in cdata["Tracks"] for l in t["Layouts"]}
        self._idx_src = cdata

    async def _fetch_user(self, uid: int) -> None:
        if uid in self.fetching or self.retry.get(uid, 0) > time.time():
            return
        self.fetching.add(uid)
        try:
            async with self.sem:
                r = await self.web.get(f"https://game.raceroom.com/utils/user-info/{uid}")
            if r.status_code == 200:
                j = r.json()
                info = {
                    "name": j.get("name") or j.get("username") or f"Fahrer {uid}",
                    "username": j.get("username"),
                    "team": j.get("team") or "",
                    "avatar": j.get("avatar"),
                    "banner": j.get("banner"),
                    "vip": bool(j.get("vip")),
                    "country": j.get("country") if isinstance(j.get("country"), dict) else None,
                    "ts": int(time.time()),
                }
                self.users[uid] = info
                await asyncio.to_thread(self._save_user, uid, info)
            else:
                self.retry[uid] = time.time() + 300
        except Exception:
            self.retry[uid] = time.time() + 300
        finally:
            self.fetching.discard(uid)

    def _save_user(self, uid: int, info: dict) -> None:
        with self.d["db"]() as c:
            c.execute("INSERT OR REPLACE INTO driver_cache(id,data,ts) VALUES(?,?,?)", (uid, json.dumps(info), int(time.time())))

    def _event(self, sid: int, kind: str, text: str = "", user_id: Optional[int] = None, **extra: Any) -> None:
        q = self.events.setdefault(sid, deque(maxlen=80))
        q.append({"ts": time.time(), "kind": kind, "text": text, "user_id": user_id, **extra})

    def _session_label(self, gs: dict, idx: int) -> str:
        tl = (gs.get("TrackLayouts") or [{}])[0]
        sess = tl.get("Sessions", {})
        active = []
        for k in SESSION_ORDER:
            if k == "Warmup":
                if sess.get("WarmupDuration", 0) > 0:
                    active.append(k)
            elif k in sess and (sess[k].get("Duration", 0) > 0 or sess[k].get("Laps", 0) > 0):
                active.append(k)
        if 0 <= idx < len(active):
            return SESSION_LABEL.get(active[idx], active[idx])
        return f"Session {idx + 1}"

    # ------------------------------------------------------------ Polling
    async def process(self, items: list[dict]) -> tuple[dict, list[tuple[int, int, int, float]]]:
        cdata = await self.d["content"]()
        self._index(cdata)
        now = time.time()
        servers: list[dict] = []
        cells: list[tuple[int, int, int, float]] = []
        names_needed: set[int] = set()

        for item in items:
            gs, ps = item["GameSetting"], item["ProcessState"]
            sid = gs["Id"]
            tl = (gs.get("TrackLayouts") or [{}])[0]
            layout_id = tl.get("LayoutId")
            st = self.srv.setdefault(sid, {"t_ms": None, "uids": None, "session": None, "running": None})

            # --- Server-Ereignisse
            if st["running"] is None:
                st["running"] = ps["Running"]  # erster Blick: still übernehmen
            elif ps["Running"] != st["running"]:
                self._event(sid, "server", "Server gestartet" if ps["Running"] else "Server gestoppt")
                st["running"] = ps["Running"]
                st["t_ms"] = None
                st["uids"] = set() if ps["Running"] else None
                st["session"] = None
                for k in [k for k in self.pl if k[0] == sid]:
                    self.pl.pop(k, None)
                for k in [k for k in self.visits if k[0] == sid]:
                    self._end_visit(*k)
            if ps["Running"] and st["session"] != ps["CurrentSession"]:
                if st["session"] is not None and ps["PlayersOnServer"]:
                    self._event(sid, "session", f"Neue Session: {self._session_label(gs, ps['CurrentSession'])}", session=ps["CurrentSession"])
                st["session"] = ps["CurrentSession"]

            # --- Zeitbasis: Δ SessionTime des Servers; steht der still (z. B. 0), gilt der Report-Takt
            t_ms = ps["SessionTime"]
            interval = (gs.get("StateReportInterval") or 3000) / 1000.0
            sig = tuple((q["UserId"], q["X"], q["Z"]) for q in drivers_only(ps.get("Players")))
            ts_changed = bool(ps["Running"]) and st["t_ms"] != t_ms
            pos_changed = bool(ps["Running"]) and st.get("sig") is not None and sig != st.get("sig")
            new_sample = ts_changed or pos_changed
            dt = None
            if ts_changed and st["t_ms"] is not None and 0.05 <= (t_ms - st["t_ms"]) / 1000.0 <= 15.0:
                dt = (t_ms - st["t_ms"]) / 1000.0
            elif new_sample and st.get("sig") is not None:
                dt = interval
            if ps["Running"]:
                st["t_ms"] = t_ms
                st["sig"] = sig
            else:
                st["sig"] = None

            # --- Fahrer (Zuschauer wie der Broadcast-Bot ausblenden)
            players = []
            uids_now: set[int] = set()
            for p in drivers_only(ps.get("Players")):
                uid = p["UserId"]
                uids_now.add(uid)
                x, z = p["X"], p["Z"]
                step = None
                trk = self.pl.get((sid, uid))
                if trk is None:
                    trk = {"x": x, "z": z, "kmh": 0.0, "hdg": None, "inc": None}
                    self.pl[(sid, uid)] = trk
                elif new_sample and dt:
                    dx, dz = x - trk["x"], z - trk["z"]
                    dist = math.hypot(dx, dz)
                    step = (dist, dt)
                    if dist > 400:  # Teleport (Box/Neustart)
                        trk["kmh"] = 0.0
                    else:
                        raw = dist / dt * 3.6
                        if raw > 650:
                            raw = trk["kmh"]
                        trk["kmh"] = raw if trk["kmh"] is None else trk["kmh"] * 0.35 + raw * 0.65
                        if dist > 0.3:
                            trk["hdg"] = math.degrees(math.atan2(dx, dz))
                        if layout_id and trk["kmh"] >= RECORD_MIN_KMH and dist > 0:
                            if dist <= MAX_SEGMENT_M:
                                n = max(1, int(dist / GRID))
                                for k in range(1, n + 1):
                                    f = k / n
                                    cells.append((layout_id, round((trk["x"] + dx * f) / GRID), round((trk["z"] + dz * f) / GRID), trk["kmh"]))
                            else:
                                cells.append((layout_id, round(x / GRID), round(z / GRID), trk["kmh"]))
                    trk["x"], trk["z"] = x, z
                elif new_sample:
                    trk["x"], trk["z"] = x, z

                inc_pts = sum(i.get("Points", 0) for i in (p.get("Incidents") or []))
                if trk["inc"] is not None and inc_pts > trk["inc"]:
                    self._event(sid, "incident", f"+{inc_pts - trk['inc']} Punkte", user_id=uid, total=inc_pts)
                trk["inc"] = inc_pts

                car, livery_name = self.liv.get(p["CarLiveryId"], (None, None))
                if step:
                    self._acc(uid, sid, gs, layout_id, p, trk["kmh"] or 0.0, car, step[1], step[0], inc_pts)
                info = self.users.get(uid)
                if info is None:
                    names_needed.add(uid)
                players.append(
                    {
                        "user_id": uid,
                        "x": x,
                        "z": z,
                        "kmh": round(trk["kmh"] or 0.0, 1),
                        "heading": None if trk["hdg"] is None else round(trk["hdg"], 1),
                        "ping": round(p.get("LatestPingMilliSec") or 0.0, 1),
                        "livery_id": p["CarLiveryId"],
                        "car": car["Name"] if car else None,
                        "car_id": car["Id"] if car else None,
                        "class": self.classes.get(car["Class"]) if car else None,
                        "livery": livery_name,
                        "incident_points": inc_pts,
                        "incidents": [{"type": i.get("Type"), "points": i.get("Points")} for i in (p.get("Incidents") or [])],
                    }
                )

            # --- Beitritte / Abgänge
            if ps["Running"] and st["uids"] is not None:
                for uid in uids_now - st["uids"]:
                    self._event(sid, "join", "ist beigetreten", user_id=uid)
                for uid in st["uids"] - uids_now:
                    self._event(sid, "leave", "hat den Server verlassen", user_id=uid)
                    self.pl.pop((sid, uid), None)
                    self._end_visit(sid, uid)
            st["uids"] = uids_now if ps["Running"] else None

            view = self.d["to_view"](item, "viewer")
            view["state"]["players"] = players
            view["layout"] = {
                "track": self.layouts.get(layout_id, ("?", "?"))[0],
                "name": self.layouts.get(layout_id, ("?", "?"))[1],
            }
            view["state"]["session_label"] = self._session_label(gs, ps["CurrentSession"]) if ps["Running"] and ps["PlayersOnServer"] else None
            view["state"]["time_left"] = ps.get("TimeLeft") if ps["Running"] else None
            view["state_report_ms"] = gs.get("StateReportInterval", 3000)
            view["events"] = list(self.events.get(sid, []))[-40:]
            view["chat"] = self.chat.get(sid, [])
            servers.append(view)

        for uid in names_needed:
            asyncio.create_task(self._fetch_user(uid))

        used: set[int] = set()
        for s in servers:
            used.update(p["user_id"] for p in s["state"]["players"])
            used.update(e["user_id"] for e in s["events"] if e.get("user_id"))
        names = {
            str(u): {"name": self.users[u]["name"], "team": self.users[u].get("team"), "avatar": self.users[u].get("avatar"), "country": self.users[u].get("country")}
            for u in used
            if u in self.users
        }
        snap = {"ts": now, "manager_ok": True, "servers": servers, "names": names}
        return snap, cells

    async def poll_chat(self, servers: list[dict]) -> None:
        now = time.time()
        for s in servers:
            sid = s["id"]
            if not s["state"]["running"]:
                self.chat.pop(sid, None)
                continue
            if now - self.chat_ts.get(sid, 0) < 2.0:
                continue
            self.chat_ts[sid] = now
            try:
                res = await self.d["mgr"]("GET", f"/chat/{sid}")
                msgs = res.get("messages", []) if isinstance(res, dict) else []
                self.chat[sid] = [
                    {"id": m.get("Id"), "ts": m.get("CreatedAt"), "user": m.get("UserName"), "text": m.get("Message")}
                    for m in msgs[-60:]
                ]
            except Exception:
                pass

    # ------------------------------------------------------- Zuschauer-Bot
    def bot_view(self) -> Optional[dict]:
        b = self.bot
        if not b.get("ts") or time.time() - b["ts"] > 15:
            return {"connected": False, "last_seen": b.get("ts") or None} if b.get("ts") else None
        drivers = []
        for x in b.get("drivers") or []:
            si = x.get("scoreInfo") or {}
            best = si.get("bestLapInfo") or {}
            secs = [best.get("sector1", -1), best.get("sector2", -1), best.get("sector3", -1)]
            best_ms = secs[2] if secs[2] and secs[2] > 0 else None  # sector3 = kumulierte Rundenzeit
            drivers.append({
                "slot": x.get("slotId"), "user_id": x.get("portalId"), "name": x.get("name"), "class_id": x.get("classId"),
                "pos": si.get("positionOverall"), "pos_class": si.get("positionClass"), "laps": si.get("laps"),
                "current_ms": si.get("currentLapTime"), "best_ms": best_ms, "best_valid": best.get("valid"),
                "gap_ms": si.get("timeDiff"), "gap_laps": si.get("lapDiff"), "distance": si.get("distanceTravelled"),
                "flags": si.get("flagInfo") or {},
            })
        drivers.sort(key=lambda d: (d["pos"] if isinstance(d["pos"], int) and d["pos"] > 0 else 999))
        return {"connected": True, "session": b.get("session"), "event": b.get("event"), "drivers": drivers, "events": list(self.bot_events)[-15:]}

    # ------------------------------------------------------- Fahrer-Statistik
    def _acc(self, uid: int, sid: int, gs: dict, layout_id: Optional[int], p: dict, kmh: float, car: Optional[dict], dt: float, dist: float, inc_pts: int) -> None:
        now = int(time.time())
        d = self.delta.setdefault(uid, {"seconds": 0.0, "drive": 0.0, "meters": 0.0, "top": 0.0, "kmh_sum": 0.0, "kmh_n": 0, "ping_sum": 0.0, "ping_n": 0, "inc": 0, "visits": 0, "last": now, "cars": {}, "layouts": {}})
        v = self.visits.get((sid, uid))
        if v is None:
            v = {"start": now, "server": gs.get("Name", "?"), "layout": layout_id, "car": car["Name"] if car else None, "seconds": 0.0, "meters": 0.0, "top": 0.0, "inc": 0, "uid": uid}
            self.visits[(sid, uid)] = v
            d["visits"] += 1
        d["last"] = now
        d["seconds"] += dt
        v["seconds"] += dt
        if car:
            v["car"] = car["Name"]
            c = d["cars"].setdefault(car["Id"], {"name": car["Name"], "class": self.classes.get(car["Class"]), "seconds": 0.0})
            c["seconds"] += dt
        if layout_id:
            lay = d["layouts"].setdefault(layout_id, {"seconds": 0.0, "meters": 0.0, "top": 0.0})
            lay["seconds"] += dt
        if kmh >= 5 and dist < 400:
            d["drive"] += dt
            d["meters"] += dist
            v["meters"] += dist
            d["kmh_sum"] += kmh; d["kmh_n"] += 1
            d["top"] = max(d["top"], kmh); v["top"] = max(v["top"], kmh)
            if layout_id:
                lay = d["layouts"][layout_id]
                lay["meters"] += dist; lay["top"] = max(lay["top"], kmh)
        ping = p.get("LatestPingMilliSec") or 0.0
        if ping > 0:
            d["ping_sum"] += ping; d["ping_n"] += 1
        if inc_pts > v["inc"]:
            d["inc"] += inc_pts - v["inc"]
        v["inc"] = max(v["inc"], inc_pts)

    def _end_visit(self, sid: int, uid: int) -> None:
        v = self.visits.pop((sid, uid), None)
        if v and v["seconds"] >= 20:
            v["end"] = int(time.time())
            self.visit_done.append(v)

    def _flush_stats(self, delta: dict, visits: list[dict]) -> None:
        with self.d["db"]() as c:
            for uid, d in delta.items():
                c.execute(
                    "INSERT INTO driver_stats(user_id,first_seen,last_seen,seconds,drive_seconds,meters,top_kmh,kmh_sum,kmh_n,ping_sum,ping_n,incident_points,visits) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(user_id) DO UPDATE SET last_seen=excluded.last_seen, seconds=seconds+excluded.seconds, drive_seconds=drive_seconds+excluded.drive_seconds, meters=meters+excluded.meters, "
                    "top_kmh=MAX(top_kmh,excluded.top_kmh), kmh_sum=kmh_sum+excluded.kmh_sum, kmh_n=kmh_n+excluded.kmh_n, ping_sum=ping_sum+excluded.ping_sum, ping_n=ping_n+excluded.ping_n, "
                    "incident_points=incident_points+excluded.incident_points, visits=visits+excluded.visits",
                    (uid, d["last"], d["last"], d["seconds"], d["drive"], d["meters"], d["top"], d["kmh_sum"], d["kmh_n"], d["ping_sum"], d["ping_n"], d["inc"], d["visits"]),
                )
                for cid, cv in d["cars"].items():
                    c.execute("INSERT INTO driver_cars(user_id,car_id,name,class,seconds) VALUES(?,?,?,?,?) ON CONFLICT(user_id,car_id) DO UPDATE SET seconds=seconds+excluded.seconds, name=excluded.name, class=excluded.class",
                              (uid, cid, cv["name"], cv["class"], cv["seconds"]))
                for lid, lv in d["layouts"].items():
                    c.execute("INSERT INTO driver_tracks(user_id,layout_id,seconds,meters,top_kmh) VALUES(?,?,?,?,?) ON CONFLICT(user_id,layout_id) DO UPDATE SET seconds=seconds+excluded.seconds, meters=meters+excluded.meters, top_kmh=MAX(top_kmh,excluded.top_kmh)",
                              (uid, lid, lv["seconds"], lv["meters"], lv["top"]))
            for v in visits:
                c.execute("INSERT INTO driver_visits(user_id,server_name,layout_id,car,start,end,seconds,meters,top_kmh,incident_points) VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (v["uid"], v["server"], v["layout"], v["car"], v["start"], v["end"], v["seconds"], v["meters"], v["top"], v["inc"]))

    # ------------------------------------------------- automatische Ausrichtung
    def _tracks_overview(self, cdata: dict) -> dict[int, dict]:
        out: dict[int, dict] = {}
        for t in cdata["Tracks"]:
            out[t["Id"]] = {"name": t["Name"], "layouts": [l["Id"] for l in t["Layouts"]]}
        return out

    def _cells_for(self, layout_ids: list[int]) -> list[tuple[float, float]]:
        if not layout_ids:
            return []
        q = ",".join(str(int(i)) for i in layout_ids)
        with self.d["db"]() as c:
            rows = c.execute(f"SELECT gx,gz FROM track_cells WHERE layout_id IN ({q}) AND n>=1").fetchall()
        return list({(r["gx"] * GRID, r["gz"] * GRID) for r in rows})

    def run_auto_align(self, track_id: int, name: str, layout_ids: list[int], hint: Optional[tuple[float, float]] = None, who: str = "automatisch") -> dict:
        """Blockierend (Thread). Speichert bei Erfolg die Georeferenz und protokolliert jeden Versuch."""
        cells = self._cells_for(layout_ids)
        res = geo_auto.auto_align(name, cells, hint)
        with self.d["db"]() as c:
            c.execute("INSERT OR REPLACE INTO geo_attempts(track_id,cells,ts,ok,note) VALUES(?,?,?,?,?)",
                      (track_id, len(cells), int(time.time()), int(bool(res.get("ok"))), (res.get("reason") or f"ok, Median {res.get('median_m')} m")[:200]))
            if res.get("ok"):
                src = f"{who}: {res.get('place', '?')} (Median {res['median_m']} m, 90 % {res['p90_m']} m)"[:200]
                xs = [x for x, _ in cells]; zs = [z for _, z in cells]
                view = json.dumps([round(min(xs) - 300), round(min(zs) - 300), round(max(xs) + 300), round(max(zs) + 300)])
                c.execute("INSERT INTO track_geo(track_id,lat0,lon0,rot_deg,mirror,source,view) VALUES(?,?,?,?,?,?,?) ON CONFLICT(track_id) DO UPDATE SET lat0=excluded.lat0, lon0=excluded.lon0, rot_deg=0, mirror=0, source=excluded.source, view=excluded.view",
                          (track_id, res["lat0"], res["lon0"], 0.0, 0, src, view))
        res["cells"] = len(cells)
        return res

    async def geo_loop(self) -> None:
        await asyncio.sleep(90)
        while True:
            try:
                cdata = await self.d["content"]()
                tracks = self._tracks_overview(cdata)
                with self.d["db"]() as c:
                    counts = {r["layout_id"]: r["n"] for r in c.execute("SELECT layout_id, COUNT(*) n FROM track_cells GROUP BY layout_id")}
                    have = {r["track_id"] for r in c.execute("SELECT track_id FROM track_geo")}
                    tries = {r["track_id"]: dict(r) for r in c.execute("SELECT * FROM geo_attempts")}
                for tid, info in tracks.items():
                    n = sum(counts.get(l, 0) for l in info["layouts"])
                    if tid in have or n < 400:
                        continue
                    prev = tries.get(tid)
                    if prev and (time.time() - prev["ts"] < 86400 and n < prev["cells"] * 1.5):
                        continue
                    await asyncio.to_thread(self.run_auto_align, tid, info["name"], info["layouts"])
                    await asyncio.sleep(5)
            except Exception:
                pass
            await asyncio.sleep(600)

    async def run(self) -> None:
        asyncio.create_task(self.geo_loop())
        while True:
            delay = 2.0
            try:
                items = await self.d["mgr"]("GET", "/dedi/")
                # Die Listenabfrage enthält keine Fahrer-Details; die gibt es nur pro Server
                for it in items:
                    ps = it["ProcessState"]
                    if ps.get("Running") and ps.get("PlayersOnServer"):
                        try:
                            detail = await self.d["mgr"]("GET", f"/dedi/{it['GameSetting']['Id']}")
                            if isinstance(detail, dict) and "ProcessState" in detail:
                                it["ProcessState"] = detail["ProcessState"]
                        except Exception:
                            pass
                snap, cells = await self.process(items)
                await self.poll_chat(snap["servers"])
                for s in snap["servers"]:
                    s["chat"] = self.chat.get(s["id"], [])
                if cells:
                    await asyncio.to_thread(self._write_cells, cells)
                    self.stats["cells_written"] += len(cells)
                self.stats["polls"] += 1
                if time.time() - getattr(self, "_last_flush", 0) > 10 and (self.delta or self.visit_done):
                    self._last_flush = time.time()
                    delta, self.delta = self.delta, {}
                    done, self.visit_done = self.visit_done, []
                    await asyncio.to_thread(self._flush_stats, delta, done)
                snap["bot"] = self.bot_view()
                self.snapshot = snap
                any_players = any(s["state"]["players"] for s in snap["servers"])
                delay = 0.5 if any_players else 1.5
                self._broadcast(snap)
            except Exception as e:  # Manager nicht erreichbar etc.
                self.stats["errors"] += 1
                snap = {"ts": time.time(), "manager_ok": False, "servers": self.snapshot.get("servers", []), "names": self.snapshot.get("names", {}), "error": type(e).__name__}
                self.snapshot = snap
                self._broadcast(snap)
                delay = 5.0
            await asyncio.sleep(delay)

    def _broadcast(self, snap: dict) -> None:
        body = {k: v for k, v in snap.items() if k != "ts"}
        txt = json.dumps(body, separators=(",", ":"), default=str)
        if txt == self._last_json:
            return
        self._last_json = txt
        payload = json.dumps(snap, separators=(",", ":"), default=str)
        for q in list(self.subs):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(payload)


# ------------------------------------------------------------------ Router
class ChatIn(BaseModel):
    message: str = Field(..., min_length=1, max_length=300)


class PenaltyIn(BaseModel):
    type: Literal[0, 1, 2, 3] = Field(..., description="0 Slowdown, 1 Drive-Through, 2 Stop-and-Go, 3 Disqualifikation")
    duration: int = Field(0, ge=0, le=30)


class MotdIn(BaseModel):
    message: str = Field("", max_length=1000)


class WhitelistIn(BaseModel):
    users: list[int] = Field(default_factory=list, max_length=200)


class CameraIn(BaseModel):
    slot: int = Field(..., ge=0, le=128)
    camera: Literal["cockpit", "onboard", "onboard1", "onboard2", "nosecam", "swingman", "trackside", "frontCam", "rearCam", "wing", "exhaust"]


class BotCfgIn(BaseModel):
    hls_url: str = Field("", max_length=200)
    rotate: bool = False


class AutoIn(BaseModel):
    hint_lat: Optional[float] = Field(None, ge=-85, le=85)
    hint_lon: Optional[float] = Field(None, ge=-180, le=180)


class StreamIn(BaseModel):
    url: str = Field("", max_length=300)


_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_TW_CH = re.compile(r"^[A-Za-z0-9_]{3,25}$")


def parse_stream(url: str) -> Optional[dict]:
    """Erlaubt nur Twitch-Kanäle und YouTube-Videos/-Lives. Gibt {provider,id} zurück."""
    url = url.strip()
    if not url:
        return None
    m = re.match(r"^https?://(?:www\.|m\.)?twitch\.tv/([A-Za-z0-9_]{3,25})/?$", url)
    if m and _TW_CH.match(m.group(1)):
        return {"provider": "twitch", "id": m.group(1)}
    m = (
        re.match(r"^https?://(?:www\.|m\.)?youtube\.com/watch\?(?:.*&)?v=([A-Za-z0-9_-]{11})(?:&.*)?$", url)
        or re.match(r"^https?://youtu\.be/([A-Za-z0-9_-]{11})(?:\?.*)?$", url)
        or re.match(r"^https?://(?:www\.|m\.)?youtube\.com/(?:live|embed)/([A-Za-z0-9_-]{11})(?:\?.*)?$", url)
    )
    if m and _YT_ID.match(m.group(1)):
        return {"provider": "youtube", "id": m.group(1)}
    raise HTTPException(422, "Nur Twitch-Kanal-Links (twitch.tv/NAME) oder YouTube-Video-/Live-Links werden unterstützt")


def setup(app, d: dict[str, Callable]) -> Live:
    hub = Live(d)
    hub.init_db()
    with d["db"]() as c:
        c.execute("CREATE TABLE IF NOT EXISTS server_meta(server_id INTEGER PRIMARY KEY, stream_provider TEXT, stream_id TEXT)")
    r = APIRouter()
    require = d["require"]
    mgr = d["mgr"]
    audit = d["audit"]

    @app.on_event("startup")
    async def _start() -> None:  # noqa: D401
        asyncio.create_task(hub.run())

    @r.get("/api/live")
    async def live(request: Request, _=Depends(require("viewer"))):
        if len(hub.subs) >= MAX_SUBSCRIBERS:
            raise HTTPException(503, "Zu viele Live-Verbindungen")
        q: asyncio.Queue = asyncio.Queue(maxsize=1)
        hub.subs.add(q)

        async def gen():
            try:
                yield "retry: 3000\n\n"
                yield f"event: snapshot\ndata: {json.dumps(hub.snapshot, separators=(',', ':'), default=str)}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        payload = await asyncio.wait_for(q.get(), timeout=12)
                        yield f"event: snapshot\ndata: {payload}\n\n"
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            finally:
                hub.subs.discard(q)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})

    @r.get("/api/live/snapshot")
    async def live_snapshot(_=Depends(require("viewer"))):
        return hub.snapshot

    @r.get("/api/live/stats")
    async def live_stats(_=Depends(require("admin"))):
        return {**hub.stats, "subscribers": len(hub.subs), "drivers_cached": len(hub.users)}

    @r.get("/api/tracks/{layout_id}/outline")
    def outline(layout_id: int, _=Depends(require("viewer"))):
        with d["db"]() as c:
            rows = c.execute("SELECT gx,gz,n,vsum FROM track_cells WHERE layout_id=? ORDER BY n DESC LIMIT 40000", (layout_id,)).fetchall()
        return {"cell": GRID, "layout_id": layout_id, "points": [[x["gx"], x["gz"], x["n"], round(x["vsum"] / x["n"]) if x["vsum"] > 0 else 0] for x in rows]}

    @r.post("/api/tracks/{layout_id}/geo/auto")
    async def geo_auto_now(layout_id: int, body: AutoIn, user=Depends(require("admin"))):
        cdata = await d["content"]()
        info = next(((t["Id"], t["Name"], [l["Id"] for l in t["Layouts"]]) for t in cdata["Tracks"] if any(l["Id"] == layout_id for l in t["Layouts"])), None)
        if not info:
            raise HTTPException(404, "Unbekanntes Layout")
        hint = (body.hint_lat, body.hint_lon) if body.hint_lat is not None and body.hint_lon is not None else None
        res = await asyncio.to_thread(hub.run_auto_align, info[0], info[1], info[2], hint, f"von {user.username}")
        audit(user.username, "geo.auto", f"track={info[0]} ok={res.get('ok')}")
        return {k: res.get(k) for k in ("ok", "reason", "median_m", "p90_m", "ambiguity", "place", "cells")}

    def _driver_row(row, info) -> dict:
        i = info or {}
        n = row["kmh_n"] or 0
        return {
            "id": row["user_id"], "name": i.get("name") or f"Fahrer {row['user_id']}", "team": i.get("team") or "", "avatar": i.get("avatar"),
            "country": i.get("country"), "username": i.get("username"),
            "first_seen": row["first_seen"], "last_seen": row["last_seen"], "visits": row["visits"],
            "seconds": round(row["seconds"]), "drive_seconds": round(row["drive_seconds"]), "meters": round(row["meters"]),
            "top_kmh": round(row["top_kmh"]), "avg_kmh": round(row["kmh_sum"] / n) if n else 0,
            "avg_ping": round(row["ping_sum"] / row["ping_n"]) if row["ping_n"] else None, "incident_points": row["incident_points"],
        }

    def _info(uid: int) -> Optional[dict]:
        return hub.users.get(uid)

    @r.get("/api/drivers")
    def drivers(_=Depends(require("viewer"))):
        with d["db"]() as c:
            rows = c.execute("SELECT * FROM driver_stats ORDER BY last_seen DESC LIMIT 500").fetchall()
        return [_driver_row(x, _info(x["user_id"])) for x in rows]

    @r.get("/api/drivers/{uid}")
    def driver_detail(uid: int, _=Depends(require("viewer"))):
        with d["db"]() as c:
            row = c.execute("SELECT * FROM driver_stats WHERE user_id=?", (uid,)).fetchone()
            if not row:
                raise HTTPException(404, "Fahrer unbekannt")
            cars = [dict(x) for x in c.execute("SELECT car_id,name,class,seconds FROM driver_cars WHERE user_id=? ORDER BY seconds DESC LIMIT 12", (uid,))]
            tracks = [dict(x) for x in c.execute("SELECT layout_id,seconds,meters,top_kmh FROM driver_tracks WHERE user_id=? ORDER BY seconds DESC LIMIT 12", (uid,))]
            visits = [dict(x) for x in c.execute("SELECT server_name,layout_id,car,start,end,seconds,meters,top_kmh,incident_points FROM driver_visits WHERE user_id=? ORDER BY start DESC LIMIT 30", (uid,))]
        for t in tracks + visits:
            tn, ln = hub.layouts.get(t["layout_id"], ("?", "?")) if t.get("layout_id") else ("?", "?")
            t["track"], t["layout"] = tn, ln
        info = _info(uid) or {}
        out = _driver_row(row, info)
        out.update({"cars": [{"name": x["name"], "class": x["class"], "seconds": round(x["seconds"])} for x in cars],
                    "tracks": [{"track": t["track"], "layout": t["layout"], "seconds": round(t["seconds"]), "meters": round(t["meters"]), "top_kmh": round(t["top_kmh"])} for t in tracks],
                    "visits": [{"server": v["server_name"], "track": v["track"], "layout": v["layout"], "car": v["car"], "start": v["start"], "end": v["end"], "seconds": round(v["seconds"]), "meters": round(v["meters"]), "top_kmh": round(v["top_kmh"]), "incident_points": v["incident_points"]} for v in visits],
                    "profile_url": f"https://game.raceroom.com/users/{info['username']}" if info.get("username") else None,
                    "banner": info.get("banner")})
        return out

    def _kv(key: str, default: Optional[str] = None) -> Optional[str]:
        with d["db"]() as c:
            row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def _kv_set(key: str, value: str) -> None:
        with d["db"]() as c:
            c.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def _bot_token() -> str:
        t = _kv("bot_token")
        if not t:
            import secrets as _s
            t = _s.token_urlsafe(24)
            _kv_set("bot_token", t)
        return t

    def _check_bot(request: Request) -> None:
        import hmac as _h
        if not _h.compare_digest(request.headers.get("x-bot-token", ""), _bot_token()):
            raise HTTPException(403, "Falscher Bot-Token")

    _BC_DIR = __import__("pathlib").Path(__file__).with_name("broadcast")

    @r.get("/broadcast/")
    @r.get("/broadcast/{name}")
    def broadcast_file(name: str = "index.html"):
        from fastapi.responses import FileResponse
        if name not in ("index.html", "r3e.js", "bridge.js"):
            raise HTTPException(404, "Nicht gefunden")
        return FileResponse(_BC_DIR / name, headers={"Cache-Control": "no-cache"})

    @r.post("/api/bot/ingest")
    async def bot_ingest(request: Request):
        _check_bot(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(422, "Ungültig")
        drv = body.get("drivers")
        if isinstance(drv, dict):  # GetDriversInfo liefert {"driversInfo": [...]}
            drv = drv.get("driversInfo")
        drv = [d for d in drv if isinstance(d, dict)][:64] if isinstance(drv, list) else []
        hub.bot = {"ts": time.time(), "drivers": drv, "session": body.get("session"), "event": body.get("event")}
        cmd, hub.bot_cmd = hub.bot_cmd, None
        return {"ok": True, "command": cmd}

    @r.post("/api/bot/event")
    async def bot_event(request: Request):
        _check_bot(request)
        e = await request.json()
        if isinstance(e, dict):
            hub.bot_events.append({"ts": time.time(), **{k: e.get(k) for k in ("slotId", "type", "reason")}})
        return {"ok": True}

    @r.post("/api/bot/results")
    async def bot_results(request: Request):
        _check_bot(request)
        res = await request.json()
        with d["db"]() as c:
            c.execute("INSERT INTO bot_results(ts,data) VALUES(?,?)", (int(time.time()), json.dumps(res)[:500000]))
        return {"ok": True}

    @r.post("/api/bot/camera")
    def bot_camera(body: CameraIn, user=Depends(require("host"))):
        hub.bot_cmd = {"slotId": body.slot, "camera": body.camera}
        return {"ok": True}

    @r.get("/api/bot/config")
    def bot_config(_=Depends(require("admin"))):
        return {"token": _bot_token(), "hls_url": _kv("bot_hls_url", "")}

    @r.put("/api/bot/config")
    def bot_config_set(body: BotCfgIn, user=Depends(require("admin"))):
        u = body.hls_url.strip().rstrip("/")
        if u and not re.match(r"^https?://[A-Za-z0-9.\-]+(:\d+)?(/[A-Za-z0-9._\-/]*)?$", u):
            raise HTTPException(422, "Ungültige Stream-Adresse")
        _kv_set("bot_hls_url", u)
        if body.rotate:
            _kv_set("bot_token", "")
        audit(user.username, "bot.config", "token neu" if body.rotate else "hls")
        return {"ok": True, "token": _bot_token()}

    @r.get("/api/bot/hls/{name}")
    async def bot_hls(name: str, _=Depends(require("viewer"))):
        base = _kv("bot_hls_url", "")
        if not base or not re.match(r"^[A-Za-z0-9_.\-]{1,80}$", name) or not name.endswith((".m3u8", ".ts", ".m4s", ".mp4")):
            raise HTTPException(404, "Kein Stream")
        try:
            rr = await hub.web.get(f"{base}/{name}", timeout=8.0)
        except Exception:
            raise HTTPException(502, "Stream nicht erreichbar")
        if rr.status_code != 200:
            raise HTTPException(404, "Kein Stream")
        mt = "application/vnd.apple.mpegurl" if name.endswith(".m3u8") else ("video/mp2t" if name.endswith(".ts") else "video/mp4")
        return Response(rr.content, media_type=mt, headers={"Cache-Control": "no-cache" if name.endswith(".m3u8") else "max-age=60"})

    @r.delete("/api/tracks/{layout_id}/outline")
    def outline_reset(layout_id: int, user=Depends(require("admin"))):
        with d["db"]() as c:
            c.execute("DELETE FROM track_cells WHERE layout_id=?", (layout_id,))
        audit(user.username, "outline.reset", str(layout_id))
        return {"ok": True}

    # ----------------------------------------------------------- Steuerung
    async def _require_running(sid: int) -> dict:
        items = await mgr("GET", "/dedi/")
        cur = next((i for i in items if i["GameSetting"]["Id"] == sid), None)
        if not cur:
            raise HTTPException(404, "Server nicht gefunden")
        if not cur["ProcessState"]["Running"]:
            raise HTTPException(409, "Server läuft nicht")
        return cur

    @r.post("/api/servers/{sid}/chat")
    async def chat_send(sid: int, body: ChatIn, user=Depends(require("host"))):
        await _require_running(sid)
        await mgr("POST", f"/chat/{sid}/admin", {"Message": body.message.strip()})
        audit(user.username, "chat.send", f"id={sid}: {body.message[:80]}")
        hub.chat_ts[sid] = 0
        return {"ok": True}

    @r.post("/api/servers/{sid}/session/{action}")
    async def session_cmd(sid: int, action: Literal["restart", "next"], user=Depends(require("host"))):
        await _require_running(sid)
        await mgr("POST", f"/session/{sid}", {"Command": 1 if action == "restart" else 2})
        audit(user.username, f"session.{action}", f"id={sid}")
        return {"ok": True}

    @r.post("/api/servers/{sid}/users/{uid}/kick")
    async def kick(sid: int, uid: int, user=Depends(require("host"))):
        await _require_running(sid)
        await mgr("POST", "/user/kick", {"UserId": uid, "ProcessId": sid})
        audit(user.username, "user.kick", f"server={sid} driver={uid}")
        return {"ok": True}

    @r.post("/api/servers/{sid}/users/{uid}/ban")
    async def ban(sid: int, uid: int, user=Depends(require("admin"))):
        await _require_running(sid)
        await mgr("POST", "/user/ban", {"UserId": uid, "ProcessId": sid})
        audit(user.username, "user.ban", f"server={sid} driver={uid}")
        return {"ok": True}

    @r.post("/api/servers/{sid}/users/{uid}/penalty")
    async def penalty(sid: int, uid: int, body: PenaltyIn, user=Depends(require("host"))):
        await _require_running(sid)
        payload: dict[str, Any] = {"UserId": uid, "ProcessId": sid, "PenaltyType": body.type}
        if body.type == 0:
            payload["Duration"] = body.duration
        await mgr("POST", "/user/penalty", payload)
        audit(user.username, "user.penalty", f"server={sid} driver={uid} type={body.type}")
        return {"ok": True}

    @r.get("/api/bans")
    async def bans(_=Depends(require("host"))):
        return {"users": await mgr("GET", "/ban")}

    @r.delete("/api/bans/{uid}")
    async def unban(uid: int, user=Depends(require("admin"))):
        await mgr("DELETE", "/ban", {"UserId": uid})
        audit(user.username, "ban.remove", str(uid))
        return {"ok": True}

    @r.get("/api/servers/{sid}/motd")
    async def motd_get(sid: int, _=Depends(require("host"))):
        res = await mgr("GET", f"/motd/{sid}")
        return {"message": (res or {}).get("Message") or ""}

    @r.put("/api/servers/{sid}/motd")
    async def motd_set(sid: int, body: MotdIn, user=Depends(require("host"))):
        await mgr("POST", "/motd/", {"ProcessId": sid, "Message": body.message.strip()})
        audit(user.username, "motd.set", f"id={sid}")
        return {"ok": True}

    @r.get("/api/servers/{sid}/whitelist")
    async def wl_get(sid: int, _=Depends(require("host"))):
        res = await mgr("GET", f"/spectator-whitelist/{sid}")
        return {"users": (res or {}).get("Users") or []}

    @r.put("/api/servers/{sid}/whitelist")
    async def wl_set(sid: int, body: WhitelistIn, user=Depends(require("host"))):
        await mgr("POST", "/spectator-whitelist", {"ProcessId": sid, "Users": body.users})
        audit(user.username, "whitelist.set", f"id={sid} n={len(body.users)}")
        return {"ok": True}

    @r.get("/api/servers/{sid}/stream")
    def stream_get(sid: int, _=Depends(require("viewer"))):
        with d["db"]() as c:
            row = c.execute("SELECT stream_provider,stream_id FROM server_meta WHERE server_id=?", (sid,)).fetchone()
        if not row or not row["stream_provider"]:
            return {"stream": None}
        return {"stream": {"provider": row["stream_provider"], "id": row["stream_id"]}}

    @r.put("/api/servers/{sid}/stream")
    def stream_set(sid: int, body: StreamIn, user=Depends(require("host"))):
        parsed = parse_stream(body.url)
        with d["db"]() as c:
            c.execute(
                "INSERT INTO server_meta(server_id,stream_provider,stream_id) VALUES(?,?,?) ON CONFLICT(server_id) DO UPDATE SET stream_provider=excluded.stream_provider, stream_id=excluded.stream_id",
                (sid, parsed["provider"] if parsed else None, parsed["id"] if parsed else None),
            )
        audit(user.username, "stream.set", f"id={sid} {parsed['provider'] if parsed else 'entfernt'}")
        return {"stream": parsed}

    _meta_cache: dict[str, Any] = {"ts": 0.0, "data": None}

    @r.get("/api/meta")
    async def meta(_=Depends(require("viewer"))):
        if _meta_cache["data"] is None or time.time() - _meta_cache["ts"] > 3600:
            raw = await mgr("GET", "/content/meta")
            _meta_cache["data"] = {k: [{"value": o["Value"], "label": o["Label"]} for o in v] for k, v in raw.items() if isinstance(v, list)}
            _meta_cache["ts"] = time.time()
        return _meta_cache["data"]

    app.include_router(r)
    return hub

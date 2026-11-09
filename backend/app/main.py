"""RaceRoom Panel — Backend.

Kapselt die API des RaceRoom "RR Dedicated manager" (nur per Host 'localhost:8088'
erreichbar) und stellt sie mit Login, Rollen und sauberer Validierung bereit.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, Field, field_validator

MANAGER_URL = os.environ.get("MANAGER_URL", "http://127.0.0.1:8088").rstrip("/")
MANAGER_HOST_HEADER = os.environ.get("MANAGER_HOST_HEADER", "localhost:8088")
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
STATIC_DIR = Path(os.environ.get("STATIC_DIR", "../frontend/dist"))
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "auto").lower()  # "1" immer, "0" nie, "auto" bei HTTPS
SESSION_HOURS = int(os.environ.get("SESSION_HOURS", "12"))
TRUSTED_PROXIES = {x.strip() for x in os.environ.get("TRUSTED_PROXIES", "").split(",") if x.strip()}

DATA_DIR.mkdir(parents=True, exist_ok=True)

ROLES = {"viewer": 1, "host": 2, "admin": 3}


# --------------------------------------------------------------------------- DB
def _db_path() -> Path:
    return DATA_DIR / "panel.sqlite"


@contextmanager
def db():
    con = sqlite3.connect(_db_path())
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db() -> None:
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS track_geo(track_id INTEGER PRIMARY KEY, lat0 REAL NOT NULL, lon0 REAL NOT NULL, rot_deg REAL NOT NULL DEFAULT 0, mirror INTEGER NOT NULL DEFAULT 0, source TEXT, view TEXT)")
        seed = Path(__file__).with_name("geo_seed.json")
        if seed.exists():
            for tid, v in json.loads(seed.read_text(encoding="utf-8")).items():
                c.execute("INSERT OR IGNORE INTO track_geo(track_id,lat0,lon0,rot_deg,mirror,source,view) VALUES(?,?,?,?,?,?,?)",
                          (int(tid), v["lat0"], v["lon0"], v.get("rot_deg", 0), int(v.get("mirror", False)), v.get("source"), json.dumps(v.get("view"))))
        c.execute(
            """CREATE TABLE IF NOT EXISTS users(
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 username TEXT UNIQUE NOT NULL COLLATE NOCASE,
                 pw_hash TEXT NOT NULL,
                 role TEXT NOT NULL,
                 created INTEGER NOT NULL)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS audit(
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 ts INTEGER NOT NULL, user TEXT NOT NULL,
                 action TEXT NOT NULL, detail TEXT)"""
        )


def audit(user: str, action: str, detail: str = "") -> None:
    with db() as c:
        c.execute("INSERT INTO audit(ts,user,action,detail) VALUES(?,?,?,?)", (int(time.time()), user, action, detail[:300]))


# ------------------------------------------------------------------- Passwörter
def hash_pw(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def verify_pw(pw: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
        calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(calc.hex(), h)
    except Exception:
        return False


# ---------------------------------------------------------------------- Session
def _secret() -> str:
    f = DATA_DIR / "secret.key"
    if not f.exists():
        f.write_text(secrets.token_urlsafe(48))
        f.chmod(0o600)
    return f.read_text().strip()


serializer = URLSafeTimedSerializer(_secret(), salt="r3e-panel-session")
COOKIE = "r3e_session"

_fails: dict[str, list[float]] = {}


def _rate_limited(key: str, limit: int = 5, window: int = 300) -> bool:
    now = time.time()
    _fails[key] = [t for t in _fails.get(key, []) if now - t < window]
    return len(_fails[key]) >= limit


class User(BaseModel):
    id: int
    username: str
    role: Literal["viewer", "host", "admin"]


def _pv(pw_hash: str) -> str:
    return hashlib.sha256(pw_hash.encode()).hexdigest()[:16]


def _cookie_secure(request: Request) -> bool:
    if COOKIE_SECURE in ("1", "true", "yes"):
        return True
    if COOKIE_SECURE in ("0", "false", "no"):
        return False
    peer = request.client.host if request.client else ""
    proto = request.headers.get("x-forwarded-proto", "") if peer in TRUSTED_PROXIES else ""
    return request.url.scheme == "https" or proto.split(",")[-1].strip() == "https"


def client_ip(request: Request) -> str:
    """Echte Client-IP: X-Forwarded-For zählt nur, wenn die Anfrage von einem vertrauten Proxy kommt."""
    peer = request.client.host if request.client else "?"
    if peer in TRUSTED_PROXIES:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[-1].strip() or peer
    return peer


def current_user(request: Request) -> User:
    tok = request.cookies.get(COOKIE)
    if not tok:
        raise HTTPException(401, "Nicht angemeldet")
    try:
        data = serializer.loads(tok, max_age=SESSION_HOURS * 3600)
    except (BadSignature, SignatureExpired):
        raise HTTPException(401, "Sitzung abgelaufen")
    with db() as c:
        row = c.execute("SELECT id,username,role,pw_hash FROM users WHERE id=?", (data["uid"],)).fetchone()
    if not row:
        raise HTTPException(401, "Benutzer existiert nicht mehr")
    if not hmac.compare_digest(data.get("pv", ""), _pv(row["pw_hash"])):
        raise HTTPException(401, "Sitzung abgelaufen")
    return User(id=row["id"], username=row["username"], role=row["role"])


def require(role: str):
    def dep(request: Request, user: User = Depends(current_user)) -> User:
        if ROLES[user.role] < ROLES[role]:
            raise HTTPException(403, "Dafür fehlt dir die Berechtigung")
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-requested-with") != "r3e-panel":
            raise HTTPException(403, "Ungültige Anfrage")
        return user

    return dep


# --------------------------------------------------------------------- Manager
class ManagerError(Exception):
    pass


_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=MANAGER_URL,
            headers={"Host": MANAGER_HOST_HEADER},
            timeout=httpx.Timeout(20.0, connect=5.0),
        )
    return _client


async def mgr(method: str, path: str, json: Any = None) -> Any:
    try:
        r = await client().request(method, path, json=json)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"RaceRoom-Manager nicht erreichbar ({type(e).__name__})")
    if r.status_code >= 400:
        raise HTTPException(502, f"RaceRoom-Manager meldet Fehler {r.status_code}")
    if not r.content:
        return None
    try:
        data = r.json()
    except Exception:
        raise HTTPException(502, "Ungültige Antwort vom RaceRoom-Manager")
    if isinstance(data, dict) and len(data) == 1 and data.get("Message") and not path.startswith("/motd"):
        raise HTTPException(502, f"RaceRoom-Manager: {data['Message']}")
    return data


_content_cache: dict[str, Any] = {"ts": 0, "data": None}


async def content() -> dict:
    if _content_cache["data"] is None or time.time() - _content_cache["ts"] > 600:
        raw = await mgr("GET", "/content/all")
        _content_cache["data"] = raw
        _content_cache["ts"] = time.time()
    return _content_cache["data"]


# ------------------------------------------------------------------ Datenmodell
SESSION_KEYS = ("Practice", "Qualify", "Race1", "Race2", "Race3")


class SessionCfg(BaseModel):
    duration: int = Field(10, ge=0, le=600, description="Minuten")
    laps: int = Field(0, ge=0, le=500)
    time_of_day: Optional[int] = Field(None, ge=0, le=4)  # 0 Dawn .. 4 Sunset
    rolling_start: Optional[bool] = None


class ServerSettings(BaseModel):
    name: str = Field(..., min_length=1, max_length=40)
    password: str = Field("", max_length=40)
    experience: int
    layout_id: int
    car_ids: list[int] = Field(..., min_length=1, max_length=60)
    max_players: int = Field(12, ge=1, le=64)
    warmup: int = Field(0, ge=0, le=30)
    sessions: dict[str, SessionCfg] = {}
    time_multiplier: int = Field(0, ge=0, le=25)
    drive_assist_preset: int = Field(0, ge=0, le=3)
    assist_transmission: int = Field(-1, ge=-1, le=2)
    assist_esp: int = Field(-1, ge=-1, le=3)
    assist_steer: int = Field(-1, ge=-1, le=1)
    assist_brake: int = Field(-1, ge=-1, le=3)
    assist_traction: int = Field(-1, ge=-1, le=2)
    assist_raceline: int = Field(-1, ge=-1, le=2)
    assist_autopit: int = Field(-1, ge=-1, le=1)
    fuel_usage: int = Field(0, ge=0, le=4)
    tire_wear: int = Field(0, ge=0, le=4)
    tire_temperature: int = Field(0, ge=0, le=1)
    damage: int = Field(0, ge=0, le=2)
    flag_rules: int = Field(0, ge=0, le=3)
    cut_rules: int = Field(0, ge=0, le=3)
    mandatory_pitstop: int = Field(0, ge=0, le=2)
    prevent_false_start: int = Field(0, ge=0, le=1)
    wrecker_prevention: int = Field(0, ge=0, le=1)
    max_ping: int = Field(0, ge=0, le=450)
    state_report_interval: int = Field(500, ge=250, le=3000)
    race_finish_duration: int = Field(30, ge=30, le=600)
    reverse_start_order_places: int = Field(0, ge=0, le=100)
    stay_in_practice_if_empty: int = Field(0, ge=0, le=1)
    allow_join_in_qualifying: int = Field(0, ge=0, le=1)
    starting_grid_from_leaderboards: int = Field(0, ge=0, le=1)
    checkered_after_time: int = Field(0, ge=0, le=600)
    fixed_setup: int = Field(0, ge=0, le=1)
    max_incident_points: int = Field(0, ge=0, le=1000)
    min_rating: int = Field(0, ge=0, le=2100)
    max_rating: int = Field(0, ge=0, le=2100)
    min_reputation: int = Field(0, ge=0, le=100)
    max_reputation: int = Field(0, ge=0, le=100)
    non_ready_pitlane_start_delay: int = Field(0, ge=0, le=120)
    thumbnail: Optional[str] = Field(None, max_length=600_000)  # None = unverändert, "" = entfernen, sonst Base64 (PNG/JPEG)

    @field_validator("thumbnail")
    @classmethod
    def check_thumbnail(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return v
        import base64, binascii
        try:
            raw = base64.b64decode(v, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("Bild ist kein gültiges Base64")
        if len(raw) > 400_000:
            raise ValueError("Bild ist zu groß (max. 400 KB)")
        if not (raw.startswith(b"\x89PNG\r\n\x1a\n") or raw.startswith(b"\xff\xd8\xff")):
            raise ValueError("Nur PNG oder JPEG erlaubt")
        return v

    @field_validator("name")
    @classmethod
    def clean_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Name fehlt")
        return v

    @field_validator("sessions")
    @classmethod
    def known_sessions(cls, v: dict) -> dict:
        for k in v:
            if k not in SESSION_KEYS:
                raise ValueError(f"Unbekannte Session: {k}")
        return v


def liveries_for(cars: list[int], cdata: dict) -> list[int]:
    out: list[int] = []
    wanted = set(cars)
    for entry in cdata["Liveries"]:
        if entry["Car"]["Id"] in wanted:
            out += [l["Id"] for l in entry["Liveries"]]
    return out


def apply_settings(gs: dict, s: ServerSettings, cdata: dict) -> dict:
    """Schreibt die validierten Felder in ein (bestehendes) GameSetting."""
    track_ok = any(l["Id"] == s.layout_id for t in cdata["Tracks"] for l in t["Layouts"])
    if not track_ok:
        raise HTTPException(422, "Unbekanntes Streckenlayout")
    known_cars = {e["Car"]["Id"] for e in cdata["Liveries"]}
    if any(c not in known_cars for c in s.car_ids):
        raise HTTPException(422, "Unbekanntes Auto")
    if s.experience not in {e["Id"] for e in cdata["Experiences"]}:
        raise HTTPException(422, "Unbekannte Experience")

    gs["Name"] = s.name
    gs["Password"] = s.password
    gs["Experience"] = s.experience
    gs["MaxNumOfPlayers"] = s.max_players
    gs["Cars"] = s.car_ids
    gs["Liveries"] = liveries_for(s.car_ids, cdata)
    gs["DriveAssistPreset"] = s.drive_assist_preset
    gs["DriveAssistTransmission"] = s.assist_transmission
    gs["DriveAssistEsp"] = s.assist_esp
    gs["DriveAssistSteer"] = s.assist_steer
    gs["DriveAssistBrake"] = s.assist_brake
    gs["DriveAssistTractionControl"] = s.assist_traction
    gs["DriveAssistRaceline"] = s.assist_raceline
    gs["DriveAssistAutopit"] = s.assist_autopit
    gs["FuelUsage"] = s.fuel_usage
    gs["TireWear"] = s.tire_wear
    gs["TireTemperature"] = s.tire_temperature
    gs["Damage"] = s.damage
    gs["FlagRules"] = s.flag_rules
    gs["CutRules"] = s.cut_rules
    gs["MandatoryPitstop"] = s.mandatory_pitstop
    gs["PreventFalseStart"] = s.prevent_false_start
    gs["WreckerPrevention"] = s.wrecker_prevention
    gs["MaxPing"] = s.max_ping
    gs["StateReportInterval"] = s.state_report_interval
    gs["RaceFinishDuration"] = s.race_finish_duration
    gs["ReverseStartOrderPlaces"] = s.reverse_start_order_places
    gs["StayInPracticeSessionIfEmpty"] = s.stay_in_practice_if_empty
    gs["AllowPlayersJoinInQualifying"] = s.allow_join_in_qualifying
    gs["StartingGridFromLeaderboards"] = s.starting_grid_from_leaderboards
    gs["CheckeredAfterTime"] = s.checkered_after_time
    gs["FixedSetup"] = s.fixed_setup
    gs["MaxIncidentPoints"] = s.max_incident_points
    gs["MinRating"] = s.min_rating
    gs["MaxRating"] = s.max_rating
    gs["MinReputation"] = s.min_reputation
    gs["MaxReputation"] = s.max_reputation
    gs["NonReadyPitlaneStartDelay"] = s.non_ready_pitlane_start_delay
    if s.thumbnail is not None:
        gs["Thumbnail"] = s.thumbnail or None

    tl = gs["TrackLayouts"][0] if gs.get("TrackLayouts") else {"EventPackId": 0, "Sessions": {}, "TimeOfDayMultiplier": 0}
    tl["LayoutId"] = s.layout_id
    tl["TimeOfDayMultiplier"] = s.time_multiplier
    tl.setdefault("Sessions", {})
    tl["Sessions"]["WarmupDuration"] = s.warmup
    for key, cfg in s.sessions.items():
        cur = tl["Sessions"].get(key) or {"SessionType": 1, "Clouds": None}
        cur["Duration"] = cfg.duration
        cur["Laps"] = cfg.laps
        if cfg.time_of_day is not None:
            cur["TimeOfDay"] = cfg.time_of_day
        if cfg.rolling_start is not None:
            # Rennen: 1 = Standing, 0 = Rolling; Training/Quali: 0 = Standing, 1 = Rolling
            is_race = key.startswith("Race")
            cur["SessionType"] = (0 if cfg.rolling_start else 1) if is_race else (1 if cfg.rolling_start else 0)
        tl["Sessions"][key] = cur
    gs["TrackLayouts"] = [tl]
    return gs


def to_view(item: dict, role: str) -> dict:
    gs = item["GameSetting"]
    ps = item["ProcessState"]
    tl = (gs.get("TrackLayouts") or [{}])[0]
    sess = tl.get("Sessions", {})
    th = gs.get("Thumbnail")
    if isinstance(th, str) and th.startswith("https://"):
        thumb = {"kind": "url", "url": th}
    elif isinstance(th, str) and th:
        thumb = {"kind": "data", "v": hashlib.sha1(th.encode()).hexdigest()[:10]}
    else:
        thumb = None
    view = {
        "id": gs["Id"],
        "name": gs["Name"],
        "thumb": thumb,
        "has_password": bool(gs.get("Password")),
        "experience": gs["Experience"],
        "layout_id": tl.get("LayoutId"),
        "car_ids": gs.get("Cars", []),
        "max_players": gs["MaxNumOfPlayers"],
        "warmup": sess.get("WarmupDuration", 0),
        "sessions": {
            k: {
                "duration": sess[k]["Duration"],
                "laps": sess[k]["Laps"],
                "time_of_day": sess[k]["TimeOfDay"],
                "rolling_start": (sess[k].get("SessionType") == 0) if k.startswith("Race") else (sess[k].get("SessionType") == 1),
            }
            for k in SESSION_KEYS
            if k in sess
        },
        "time_multiplier": tl.get("TimeOfDayMultiplier", 0),
        "drive_assist_preset": gs.get("DriveAssistPreset", 0),
        "assist_transmission": gs.get("DriveAssistTransmission", -1),
        "assist_esp": gs.get("DriveAssistEsp", -1),
        "assist_steer": gs.get("DriveAssistSteer", -1),
        "assist_brake": gs.get("DriveAssistBrake", -1),
        "assist_traction": gs.get("DriveAssistTractionControl", -1),
        "assist_raceline": gs.get("DriveAssistRaceline", -1),
        "assist_autopit": gs.get("DriveAssistAutopit", -1),
        "fuel_usage": gs.get("FuelUsage", 0),
        "tire_wear": gs.get("TireWear", 0),
        "tire_temperature": gs.get("TireTemperature", 0),
        "damage": gs.get("Damage", 0),
        "flag_rules": gs.get("FlagRules", 0),
        "cut_rules": gs.get("CutRules", 0),
        "mandatory_pitstop": gs.get("MandatoryPitstop", 0),
        "prevent_false_start": gs.get("PreventFalseStart", 0),
        "wrecker_prevention": gs.get("WreckerPrevention", 0),
        "max_ping": gs.get("MaxPing", 0),
        "state_report_interval": gs.get("StateReportInterval", 3000),
        "race_finish_duration": gs.get("RaceFinishDuration", 30),
        "reverse_start_order_places": gs.get("ReverseStartOrderPlaces", 0),
        "stay_in_practice_if_empty": gs.get("StayInPracticeSessionIfEmpty", 0),
        "allow_join_in_qualifying": gs.get("AllowPlayersJoinInQualifying", 0),
        "starting_grid_from_leaderboards": gs.get("StartingGridFromLeaderboards", 0),
        "checkered_after_time": gs.get("CheckeredAfterTime", 0),
        "fixed_setup": gs.get("FixedSetup", 0),
        "max_incident_points": gs.get("MaxIncidentPoints", 0),
        "min_rating": gs.get("MinRating", 0),
        "max_rating": gs.get("MaxRating", 0),
        "min_reputation": gs.get("MinReputation", 0),
        "max_reputation": gs.get("MaxReputation", 0),
        "non_ready_pitlane_start_delay": gs.get("NonReadyPitlaneStartDelay", 0),
        "state": {
            "running": ps["Running"],
            "port": ps["Port"],
            "time_port": ps["TimePort"],
            "players_online": ps["PlayersOnServer"],
            "session": ps["CurrentSession"],
            "session_time": ps["SessionTime"],
            "session_time_left": ps["SessionTimeLeft"],
            "players": [
                {"name": p.get("Name") or p.get("UserName") or "?", "raw": p}
                for p in (ps.get("Players") or [])
                # Zuschauer (Broadcast-Bot) ohne Auto: Position 0/0, kein Ping
                if not (p.get("X") == 0 and p.get("Z") == 0 and (p.get("LatestPingMilliSec") is None or p["LatestPingMilliSec"] < 0))
            ],
        },
    }
    if ROLES[role] >= ROLES["host"]:
        view["password"] = gs.get("Password", "")
    return view


# -------------------------------------------------------------------------- App
app = FastAPI(title="RaceRoom Panel", docs_url=None, redoc_url=None, openapi_url=None)
init_db()


CSP = (
    "default-src 'self'; img-src 'self' https: data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
    "connect-src 'self'; font-src 'self'; media-src 'self' blob:; worker-src 'self' blob:; frame-src https://player.twitch.tv https://www.youtube-nocookie.com; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if request.url.path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


@app.exception_handler(HTTPException)
async def http_exc(_: Request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


class LoginIn(BaseModel):
    username: str = Field(..., max_length=64)
    password: str = Field(..., max_length=256)


@app.post("/api/login")
def login(body: LoginIn, request: Request, response: Response):
    ip = client_ip(request)
    ukey = "u:" + body.username.strip().lower()
    if _rate_limited("ip:" + ip) or _rate_limited(ukey, limit=15, window=900):
        raise HTTPException(429, "Zu viele Versuche, bitte in einigen Minuten erneut probieren")
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE username=?", (body.username.strip(),)).fetchone()
    ok = bool(row) and verify_pw(body.password, row["pw_hash"])
    if not ok:
        _fails.setdefault("ip:" + ip, []).append(time.time())
        _fails.setdefault(ukey, []).append(time.time())
        if not row:  # Timing angleichen
            verify_pw(body.password, hash_pw("dummy"))
        raise HTTPException(401, "Benutzername oder Passwort falsch")
    tok = serializer.dumps({"uid": row["id"], "pv": _pv(row["pw_hash"])})
    response.set_cookie(COOKIE, tok, max_age=SESSION_HOURS * 3600, httponly=True, secure=_cookie_secure(request), samesite="lax", path="/")
    audit(row["username"], "login")
    return {"username": row["username"], "role": row["role"]}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(user: User = Depends(current_user)):
    return user


@app.get("/api/health")
async def health():
    try:
        r = await client().get("/")
        return {"panel": True, "manager": r.status_code == 200}
    except httpx.HTTPError:
        return {"panel": True, "manager": False}


@app.get("/api/content")
async def get_content(_: User = Depends(require("viewer"))):
    c = await content()
    return {
        "experiences": c["Experiences"],
        "car_classes": c["CarClasses"],
        "tracks": [
            {"id": t["Id"], "name": t["Name"], "layouts": [{"id": l["Id"], "name": l["Name"], "experiences": l["Experiences"], "max": l["MaxVehicles"]} for l in t["Layouts"]]}
            for t in sorted(c["Tracks"], key=lambda t: t["Name"].lower())
        ],
        "cars": [
            {"id": e["Car"]["Id"], "name": e["Car"]["Name"], "class": e["Car"]["Class"], "experiences": e["Car"]["Experiences"], "liveries": len(e["Liveries"])}
            for e in sorted(c["Liveries"], key=lambda e: e["Car"]["Name"].lower())
        ],
    }


def _track_of_layout(cdata: dict, layout_id: int) -> Optional[int]:
    for t in cdata["Tracks"]:
        if any(l["Id"] == layout_id for l in t["Layouts"]):
            return t["Id"]
    return None


@app.get("/api/tracks/{layout_id}/geo")
async def get_geo(layout_id: int, _: User = Depends(require("viewer"))):
    tid = _track_of_layout(await content(), layout_id)
    if tid is None:
        raise HTTPException(404, "Unbekanntes Layout")
    cd = await content()
    lids = next(([l["Id"] for l in t["Layouts"]] for t in cd["Tracks"] if t["Id"] == tid), [])
    with db() as c:
        r = c.execute("SELECT * FROM track_geo WHERE track_id=?", (tid,)).fetchone()
        n = c.execute(f"SELECT COUNT(*) n FROM track_cells WHERE layout_id IN ({','.join(str(int(i)) for i in lids) or '0'})").fetchone()["n"]
        a = c.execute("SELECT ts,ok,note FROM geo_attempts WHERE track_id=?", (tid,)).fetchone()
    auto = {"cells": n, "last": dict(a) if a else None}
    if not r:
        return {"geo": None, "track_id": tid, "auto": auto}
    return {"track_id": tid, "auto": auto, "geo": {"lat0": r["lat0"], "lon0": r["lon0"], "rot_deg": r["rot_deg"], "mirror": bool(r["mirror"]), "source": r["source"], "view": json.loads(r["view"]) if r["view"] else None}}


class GeoIn(BaseModel):
    lat0: float = Field(..., ge=-85, le=85)
    lon0: float = Field(..., ge=-180, le=180)
    rot_deg: float = Field(0, ge=-360, le=360)
    mirror: bool = False


@app.put("/api/tracks/{layout_id}/geo")
async def put_geo(layout_id: int, body: GeoIn, user: User = Depends(require("admin"))):
    tid = _track_of_layout(await content(), layout_id)
    if tid is None:
        raise HTTPException(404, "Unbekanntes Layout")
    with db() as c:
        c.execute("INSERT INTO track_geo(track_id,lat0,lon0,rot_deg,mirror,source) VALUES(?,?,?,?,?,?) ON CONFLICT(track_id) DO UPDATE SET lat0=excluded.lat0, lon0=excluded.lon0, rot_deg=excluded.rot_deg, mirror=excluded.mirror, source=excluded.source",
                  (tid, body.lat0, body.lon0, body.rot_deg, int(body.mirror), f"manuell von {user.username}"))
    audit(user.username, "geo.set", f"track={tid}")
    return {"ok": True}


@app.get("/api/servers")
async def list_servers(user: User = Depends(require("viewer"))):
    data = await mgr("GET", "/dedi/")
    return [to_view(i, user.role) for i in data]


@app.get("/api/servers/{sid}/thumbnail")
async def get_thumbnail(sid: int, _: User = Depends(require("viewer"))):
    import base64
    for i in await mgr("GET", "/dedi/"):
        if i["GameSetting"]["Id"] == sid:
            th = i["GameSetting"].get("Thumbnail")
            if isinstance(th, str) and th and not th.startswith("http"):
                try:
                    raw = base64.b64decode(th)
                except Exception:
                    break
                mt = "image/png" if raw.startswith(b"\x89PNG") else "image/jpeg"
                return Response(raw, media_type=mt, headers={"Cache-Control": "private, max-age=86400"})
            break
    raise HTTPException(404, "Kein Bild")


@app.get("/api/servers/{sid}")
async def get_server(sid: int, user: User = Depends(require("viewer"))):
    for i in await mgr("GET", "/dedi/"):
        if i["GameSetting"]["Id"] == sid:
            return to_view(i, user.role)
    raise HTTPException(404, "Server nicht gefunden")


@app.get("/api/defaults")
async def defaults(_: User = Depends(require("host"))):
    d = await mgr("GET", "/dedi/default")
    c = await content()
    first_car = c["Liveries"][0]["Car"]["Id"] if c["Liveries"] else None
    return {"max_players": d["GameSetting"]["MaxNumOfPlayers"], "experience": d["GameSetting"]["Experience"], "first_car": first_car}


@app.post("/api/servers")
async def create_server(body: ServerSettings, user: User = Depends(require("host"))):
    base = (await mgr("GET", "/dedi/default"))["GameSetting"]
    gs = apply_settings(base, body, await content())
    res = await mgr("POST", "/dedi/", {"GameSettings": gs})
    audit(user.username, "server.create", f"{body.name} id={res.get('ProcessId')}")
    return {"id": res.get("ProcessId")}


@app.put("/api/servers/{sid}")
async def update_server(sid: int, body: ServerSettings, user: User = Depends(require("host"))):
    items = await mgr("GET", "/dedi/")
    cur = next((i for i in items if i["GameSetting"]["Id"] == sid), None)
    if not cur:
        raise HTTPException(404, "Server nicht gefunden")
    gs = apply_settings(cur["GameSetting"], body, await content())
    gs["Id"] = sid
    await mgr("POST", "/dedi/", {"GameSettings": gs})
    audit(user.username, "server.update", f"{body.name} id={sid}")
    return {"id": sid, "restart_needed": bool(cur["ProcessState"]["Running"])}


class Action(BaseModel):
    pass


@app.post("/api/servers/{sid}/start")
async def start_server(sid: int, user: User = Depends(require("host"))):
    res = await mgr("POST", "/dedi/start", {"ProcessId": sid})
    audit(user.username, "server.start", f"id={sid}")
    return res


@app.post("/api/servers/{sid}/stop")
async def stop_server(sid: int, user: User = Depends(require("host"))):
    res = await mgr("POST", "/dedi/stop", {"ProcessId": sid})
    audit(user.username, "server.stop", f"id={sid}")
    return res


@app.delete("/api/servers/{sid}")
async def delete_server(sid: int, user: User = Depends(require("admin"))):
    items = await mgr("GET", "/dedi/")
    cur = next((i for i in items if i["GameSetting"]["Id"] == sid), None)
    if not cur:
        raise HTTPException(404, "Server nicht gefunden")
    if cur["ProcessState"]["Running"]:
        raise HTTPException(409, "Server läuft noch, bitte zuerst stoppen")
    await mgr("DELETE", f"/dedi/{sid}")
    audit(user.username, "server.delete", f"id={sid}")
    return {"ok": True}


# ------------------------------------------------------------------- Benutzer
class UserIn(BaseModel):
    username: str = Field(..., min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(..., min_length=10, max_length=128)
    role: Literal["viewer", "host", "admin"] = "host"


class UserPatch(BaseModel):
    password: Optional[str] = Field(None, min_length=10, max_length=128)
    role: Optional[Literal["viewer", "host", "admin"]] = None


@app.get("/api/users")
def list_users(_: User = Depends(require("admin"))):
    with db() as c:
        return [dict(r) for r in c.execute("SELECT id,username,role,created FROM users ORDER BY username")]


@app.post("/api/users")
def create_user(body: UserIn, admin: User = Depends(require("admin"))):
    with db() as c:
        if c.execute("SELECT 1 FROM users WHERE username=?", (body.username,)).fetchone():
            raise HTTPException(409, "Benutzername schon vergeben")
        c.execute("INSERT INTO users(username,pw_hash,role,created) VALUES(?,?,?,?)", (body.username, hash_pw(body.password), body.role, int(time.time())))
    audit(admin.username, "user.create", f"{body.username} ({body.role})")
    return {"ok": True}


@app.patch("/api/users/{uid}")
def patch_user(uid: int, body: UserPatch, admin: User = Depends(require("admin"))):
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            raise HTTPException(404, "Benutzer nicht gefunden")
        if body.role and row["role"] == "admin" and body.role != "admin":
            if c.execute("SELECT COUNT(*) n FROM users WHERE role='admin'").fetchone()["n"] <= 1:
                raise HTTPException(409, "Der letzte Admin kann nicht herabgestuft werden")
        if body.role:
            c.execute("UPDATE users SET role=? WHERE id=?", (body.role, uid))
        if body.password:
            c.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_pw(body.password), uid))
    audit(admin.username, "user.update", row["username"])
    return {"ok": True}


@app.delete("/api/users/{uid}")
def delete_user(uid: int, admin: User = Depends(require("admin"))):
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            raise HTTPException(404, "Benutzer nicht gefunden")
        if row["id"] == admin.id:
            raise HTTPException(409, "Du kannst dich nicht selbst löschen")
        if row["role"] == "admin" and c.execute("SELECT COUNT(*) n FROM users WHERE role='admin'").fetchone()["n"] <= 1:
            raise HTTPException(409, "Der letzte Admin kann nicht gelöscht werden")
        c.execute("DELETE FROM users WHERE id=?", (uid,))
    audit(admin.username, "user.delete", row["username"])
    return {"ok": True}


@app.get("/api/audit")
def get_audit(_: User = Depends(require("admin"))):
    with db() as c:
        return [dict(r) for r in c.execute("SELECT ts,user,action,detail FROM audit ORDER BY id DESC LIMIT 100")]


# ------------------------------------------------------------------ Live-Modul
from . import live as _live  # noqa: E402

hub = _live.setup(app, {"db": db, "content": content, "mgr": mgr, "require": require, "audit": audit, "to_view": to_view})


# ------------------------------------------------------------------- Frontend
if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, "Nicht gefunden")
        f = (STATIC_DIR / path).resolve()
        if path and f.is_file() and STATIC_DIR.resolve() in f.parents:
            return FileResponse(f)
        return FileResponse(STATIC_DIR / "index.html")

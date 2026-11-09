#!/usr/bin/env python3
"""Wächter für den Zuschauer-Bot: sorgt dafür, dass der Bot auf dem laufenden RaceRoom-Server als Zuschauer sitzt.

* fragt den Manager (Liste + Spieler), ob der Bot (BOT_UID) auf einem laufenden Server steht
* fehlt er: tritt über die Spiel-Menüs wieder bei (Klicks per xdotool), Passwort kommt vom Manager
* hängt das Spiel nicht im Menü oder läuft nicht: RaceRoom über Steam neu starten
Konfiguration: /etc/r3e-watch.conf (KEY=Wert). Protokoll: Journal (journalctl -u r3e-watch).
"""
import json, os, subprocess, sys, time, urllib.request

CONF = {"MANAGER": "http://192.0.2.20:8090", "BOT_UID": "0", "TARGET": "", "INTERVAL": "20", "MISSING_CHECKS": "3"}
try:
    for line in open("/etc/r3e-watch.conf"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1); CONF[k.strip()] = v.strip()
except FileNotFoundError:
    pass
BOT_UID = int(CONF["BOT_UID"]); INTERVAL = int(CONF["INTERVAL"]); NEED = int(CONF["MISSING_CHECKS"])
ENV = {**os.environ, "DISPLAY": ":0", "XDG_RUNTIME_DIR": "/run/user/1000", "LANG": "de_DE.UTF-8", "SDL_VIDEO_X11_XRANDR": "0"}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def api(path):
    r = urllib.request.Request(CONF["MANAGER"] + path, headers={"Host": "localhost:8088"})
    return json.load(urllib.request.urlopen(r, timeout=8))


def sh(cmd, check=False, timeout=60):
    return subprocess.run(["runuser", "-u", "bot", "--", "env", *[f"{k}={v}" for k, v in ENV.items() if k in ("DISPLAY", "XDG_RUNTIME_DIR", "LANG", "SDL_VIDEO_X11_XRANDR")], *cmd],
                          capture_output=True, text=True, timeout=timeout, check=check)


def x(*args):
    return sh(["xdotool", *args]).stdout.strip()


def game_running():
    return subprocess.run(["pgrep", "-f", "RRRE64.exe"], capture_output=True).returncode == 0


def game_window():
    return x("search", "--name", "RaceRoom Racing Experience").split("\n")[0]


def focus():
    w = game_window()
    if w:
        x("windowactivate", "--sync", w); x("windowfocus", "--sync", w)
    return bool(w)


def click(px, py):
    for d in (80, 40, 15, 0):
        x("mousemove", str(px - d), str(py)); time.sleep(0.1)
    time.sleep(0.3); x("click", "1")


def paste(text):
    p = subprocess.Popen(["runuser", "-u", "bot", "--", "env", "DISPLAY=:0", "xclip", "-selection", "clipboard"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p.stdin.write(text.encode()); p.stdin.close(); time.sleep(0.5)
    x("key", "--clearmodifiers", "ctrl+a"); x("key", "--clearmodifiers", "Delete"); x("key", "--clearmodifiers", "ctrl+v"); time.sleep(0.5)
    subprocess.run(["pkill", "-u", "bot", "xclip"], capture_output=True)


def at_menu():
    """Menü erkannt, wenn das weiße RaceRoom-Logo oben links zu sehen ist (viele fast weiße Pixel)."""
    r = subprocess.run(["runuser", "-u", "bot", "--", "env", "DISPLAY=:0", "ffmpeg", "-loglevel", "error", "-f", "x11grab", "-video_size", "1920x1080", "-i", ":0",
                        "-frames:v", "1", "-vf", "crop=350:70:40:50,format=rgb24", "-f", "rawvideo", "-"], capture_output=True, timeout=20)
    b = r.stdout
    if len(b) < 3000:
        return False
    white = sum(1 for i in range(0, len(b) - 2, 3) if b[i] > 235 and b[i + 1] > 235 and b[i + 2] > 235)
    return white / (len(b) / 3) > 0.12


def wait_menu(timeout):
    t = time.time()
    while time.time() - t < timeout:
        if game_window() and at_menu():
            return True
        time.sleep(3)
    return False


def start_game():
    log("starte RaceRoom neu")
    subprocess.run(["pkill", "-f", "RRRE64.exe"], capture_output=True); time.sleep(10)
    sh(["steam", "-applaunch", "211500"], timeout=30)
    for _ in range(40):
        if game_window():
            break
        time.sleep(3)
    else:
        log("kein Fenster nach applaunch, versuche Steam-Bibliothek (Play-Knopf)")
        click(1516, 208); time.sleep(2); click(360, 430)
        for _ in range(60):
            if game_window():
                break
            time.sleep(3)
    ok = wait_menu(150)
    log("Menü erreicht" if ok else "Menü nicht erkannt")
    return ok


def join(name, password):
    log(f"trete bei: {name!r} (Passwort: {'ja' if password else 'nein'})")
    if not focus():
        return False
    click(584, 90); time.sleep(4)          # Start/News (auch aus Unterseiten)
    click(770, 90); time.sleep(5)          # DRIVE
    click(1404, 420); time.sleep(9)        # MULTIPLAYER
    click(1462, 90); time.sleep(9)         # COMMUNITY SERVERS
    click(182, 174); time.sleep(0.5); paste(name.split()[0] if name else ""); x("key", "Return"); time.sleep(5)
    click(160, 322); time.sleep(1.5)       # erster Treffer
    click(1708, 922); time.sleep(4)        # BROADCAST
    if password:
        click(958, 530); time.sleep(0.6); paste(password); time.sleep(0.5)
        click(1098, 600)                   # JOIN
    return True


def target():
    servers = api("/dedi/")
    run = [s for s in servers if s["ProcessState"]["Running"]]
    if CONF["TARGET"]:
        run = [s for s in run if s["GameSetting"]["Name"] == CONF["TARGET"]]
    return run[0] if run else None


def bot_present(sid):
    d = api(f"/dedi/{sid}")
    return any(p.get("UserId") == BOT_UID for p in (d.get("ProcessState", {}).get("Players") or []))


def main():
    if BOT_UID == 0:
        sys.exit("BOT_UID fehlt in /etc/r3e-watch.conf")
    missing, fails, cooldown_until = 0, 0, 0
    log("Wächter gestartet, Bot-ID", BOT_UID)
    while True:
        try:
            t = target()
            if not t:
                missing = 0
            else:
                gs = t["GameSetting"]; sid = gs["Id"]
                if bot_present(sid):
                    if missing or fails:
                        log("Bot ist wieder auf dem Server")
                    missing = fails = 0
                else:
                    missing += 1
                    if missing >= NEED and time.time() >= cooldown_until:
                        fails += 1
                        log(f"Bot fehlt seit {missing} Prüfungen, Versuch {fails}")
                        ok = game_running() and at_menu() and focus()
                        if not ok:
                            ok = start_game() and focus()
                        if ok:
                            join(gs["Name"], gs.get("Password") or "")
                            time.sleep(70)
                        cooldown_until = time.time() + (60 if fails < 3 else 1800)
                        if fails >= 3:
                            log("3 Versuche ohne Erfolg, Pause 30 min")
                            fails = 0
        except Exception as e:  # noqa: BLE001
            log("Fehler:", type(e).__name__, str(e)[:200])
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()

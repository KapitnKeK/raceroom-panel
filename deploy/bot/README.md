# Zuschauer-Bot: RaceRoom auf einer GPU ohne Monitor (experimentell)

Ziel: Ein RaceRoom-Client tritt dem eigenen Server im **Zuschauer-Modus** bei (`-broadcastUrl=…`, kein Fahrerplatz,
keine Inhalte nötig), liefert über die offizielle Overlay-Schnittstelle Daten aller Fahrer und lässt sich als
„Regie" fernsteuern (Kamera/Fahrer). Das Bild wird per NVENC gestreamt. Siehe
[sector3studios/r3e-spectator-overlay](https://github.com/sector3studios/r3e-spectator-overlay).

**Stand:** RaceRoom läuft headless auf einer Tesla P100 in einem Proxmox-LXC (Debian 13, Proton Experimental,
1920×1080, ~2 GB VRAM im Menü). Zuschauer-Beitritt, Datenbrücke und Stream sind in Arbeit.

## Was funktioniert hat
| Baustein | Ergebnis |
|---|---|
| Xorg ohne Monitor auf Tesla P100 (`UseDisplayDevice None`, `xorg.conf`) | OpenGL 4.6 mit direktem Rendering, Vulkan 1.4 |
| NVENC (`ffmpeg -c:v h264_nvenc`, `x11grab`) | funktioniert |
| Steam-Oberfläche | erst mit `SDL_VIDEO_X11_XRANDR=0` (siehe `steam-wrapper.sh`) |
| Xvfb + NVIDIA-Vulkan | **geht nicht** (Absturz beim Präsentieren) |
| RaceRoom unter Proton | läuft, **nach** CPU-Fix (unten) |

## Wichtigster Stolperstein: CPU-Zuweisung im LXC
RaceRoom bricht beim Start mit `Fatal Error … SystemManager::WorkaroundForDifferentProcessors … Error Code: 87`
(davor evtl. `Internal error 0xE200000C`) ab. Ursache: Proxmox gibt dem Container lückenhafte Kerne (z. B.
`Cpus_allowed_list: 7-10,12-13`), lxcfs zeigt aber 6 CPUs mit den Nummern 0–5. Wine legt Threads auf „CPU 0–5“ →
`EINVAL`. Auch `WINE_CPU_TOPOLOGY` scheitert dann („Invalid WINE_CPU_TOPOLOGY string“). `-nothreadaffinity` allein
reicht nicht.

**Lösung:** dem Container zusammenhängende Kerne ab 0 geben:
```
# /etc/pve/lxc/<id>.conf
lxc.cgroup2.cpuset.cpus: 0-5
```
Zusätzlich als Steam-Startoption: `/usr/local/bin/r3e-launch %command% -nothreadaffinity`
(`r3e-launch.sh` setzt `WINE_CPU_TOPOLOGY` passend zu den erlaubten Kernen und schaltet das Proton-Log ein).

## Weitere Hinweise
- Steams Shader-Vorberechnung („Processing Vulkan shaders“) kann sehr lange laufen → „Skip“.
- Beim Start fragt Steam nach der Startvariante („Play RaceRoom“) und zeigt einen Controller-Hinweis.
- Startoptionen ohne Oberfläche setzen: `userdata/<id>/config/localconfig.vdf` → `apps/211500/LaunchOptions` (Steam vorher beenden).

## Einrichtung (Kurzfassung)
1. **Host (Proxmox):** NVIDIA-Treiber mit Kernelmodul; zusätzlich `modprobe nvidia-modeset && nvidia-modprobe -m`
   und dauerhaft machen (`/etc/modules-load.d/`, Gerätknoten bei jedem Boot anlegen).
2. **LXC:** unprivilegiert, `features: nesting=1,keyctl=1`, Geräte `/dev/nvidia0, nvidiactl, nvidia-uvm,
   nvidia-uvm-tools, nvidia-modeset, nvidia-caps/*` durchreichen. ~150 GB Platz (RaceRoom ist ~92 GB).
3. **Im LXC:** `contrib non-free` + `i386`-Architektur; Pakete `xserver-xorg-core x11vnc novnc websockify openbox
   xterm ffmpeg libvulkan1(:i386) libgl1:i386 libegl1:i386 steam-installer`. NVIDIA-Userspace in **exakt** der
   Host-Version: `NVIDIA-Linux-x86_64-<ver>.run -s --no-kernel-modules --install-compat32-libs --no-nvidia-modprobe`.
   Falls die EGL-Warnung kommt: `/usr/share/glvnd/egl_vendor.d/10_nvidia.json` anlegen.
4. Benutzer `bot`, `/run/user/<uid>` per tmpfiles anlegen (sonst „g_mkdtemp: Keine Berechtigung"),
   `xorg.conf`, `systemd/*.service`, `steam-wrapper.sh` → `/usr/local/bin/steam`.
5. Über noVNC (`http://<ip>:6080/vnc.html`) bei Steam anmelden, Proton aktivieren, RaceRoom installieren.

## Datenbrücke und Stream (im Panel eingebaut)
- **Zuschauer-Seite:** `<panel>/broadcast/?t=<token>` (Token unter *Steuerung → Zuschauer-Bot*). Als Steam-Startoption:
  `/usr/local/bin/r3e-launch %command% -nothreadaffinity -broadcastUrl=http://<panel-ip>:8180/broadcast/?t=<token>`.
  Die Seite nutzt die offizielle `r3e.js` (Unlicense) und schickt jede Sekunde Fahrer-, Session- und Event-Daten an
  `/api/bot/ingest`; Kamerabefehle aus dem Panel kommen in der Antwort zurück (`r3e.setCamera.<typ>({slotId})`).
- **Video:** `systemd/r3e-stream.service` nimmt `:0` per `x11grab` auf, kodiert mit `h264_nvenc` (720p, 4 Mbit/s) und
  schreibt HLS nach `/var/www/hls`; `nginx-hls.conf` liefert das nur ins LAN. Das Panel reicht den Stream unter
  `/api/bot/hls/stream.m3u8` nur an angemeldete Benutzer durch (Adresse unter *Steuerung → Zuschauer-Bot* eintragen).
- **Offen:** Beim allerersten Start verlangt RaceRoom ein ausgefülltes Profil (Land) im Spiel; danach Mehrspieler →
  Server wählen → als Zuschauer beitreten. Ob die Sektorzeiten der Overlay-API kumuliert sind, ist noch zu prüfen.

## Ton im Stream (PulseAudio)

Der Bot hat keine Soundkarte. `r3e-audio.service` startet PulseAudio mit einem Null-Sink `r3e`; RaceRoom (Proton) spielt darauf,
`r3e-stream.service` nimmt `r3e.monitor` zusammen mit dem Bild als AAC in den HLS-Stream auf. Im Panel ist der Player zunächst
stumm (Browser-Autoplay), der Knopf „Ton an“ schaltet ihn ein. RaceRoom muss nach dem Einrichten einmal neu gestartet werden.

## Wächter (`r3e-watch.py`)

Dienst `r3e-watch.service`: prüft alle 20 s über den Manager, ob der Bot (`BOT_UID`) auf einem laufenden Server in der Spielerliste steht.
Fehlt er dreimal hintereinander, tritt er über die Spiel-Menüs wieder bei (xdotool-Klicks, Passwort stammt vom Manager) und startet
RaceRoom bei Bedarf über Steam neu. Nach 3 erfolglosen Versuchen 30 Minuten Pause. Die Klickkoordinaten gelten für 1920x1080 und die
Menüs der RaceRoom-Version vom November 2026; sie können nach Spiel-Updates abweichen. Protokoll: `journalctl -u r3e-watch -o cat | grep -v pam_unix`.

"""
F1 25 Discord Rich Presence

Reads F1 25's UDP telemetry and shows your game mode, track, driver (career)
and position on your Discord profile. Runs quietly in the system tray.

Run from source:  python f1_presence.py            (tray icon)
                  python f1_presence.py --no-tray  (console only)
"""

import asyncio
import json
import logging
import os
import socket
import struct
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

APP_NAME = "F1RichPresence"
APP_DISPLAY = "F1 25 Rich Presence"
VERSION = "1.0.0"
IS_WINDOWS = sys.platform == "win32"


DEFAULT_CLIENT_ID = "1568772640536141895"

DEFAULT_CONFIG = {
    "client_id": "",                      # blank = use the built-in ID above
    "udp_port": 20777,
    "large_image": "f1",                  # app icon Art Asset key (shown in menus / unknown tracks)
    "track_images": True,                 # track outline as the big image
    "team_images": True,                  # your team's badge as the small corner image
    "show_driver_outside_career": False,
    "check_game_process": True,           # set false if you play on console
    "game_process_names": ["F1_25.exe"],
    "menu_timeout": 10,                   # seconds without packets before showing "In the menus"
    "debug": False,                       # log unknown mode / track ids
}

MIN_UPDATE_GAP = 5          # Discord rate limits presence updates
NO_PROCESS_CLEAR_TIMEOUT = 600

log = logging.getLogger(APP_NAME)


# paths/config

def app_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    d = Path(base) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def resource_path(rel: str) -> Path:
    base = getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)
    return Path(base) / rel


def load_config():
    """Returns (config, is_first_run)."""
    path = app_dir() / "config.json"
    cfg = dict(DEFAULT_CONFIG)
    first_run = not path.exists()
    if not first_run:
        try:
            cfg.update(json.loads(path.read_text(encoding="utf-8")))
        except Exception as e:
            log.warning("Couldn't read %s (%s), using defaults", path, e)
            return cfg, False   # don't overwrite a file the user is mid-edit on
    try:
        path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError:
        pass
    return cfg, first_run


def setup_logging(debug: bool):
    log.setLevel(logging.DEBUG if debug else logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S")
    fh = RotatingFileHandler(app_dir() / "log.txt", maxBytes=200_000, backupCount=1, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if sys.stdout:   # no stdout when built with --noconsole
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)


def message_box(text: str):
    log.error(text)
    if IS_WINDOWS:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, APP_DISPLAY, 0x10)
    else:
        print(text)


# start with windows

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def startup_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = pyw if pyw.exists() else Path(sys.executable)
    return f'"{exe}" "{Path(__file__).resolve()}"'


def startup_enabled() -> bool:
    if not IS_WINDOWS:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except FileNotFoundError:
        return False


def set_startup(enable: bool):
    if not IS_WINDOWS:
        return
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        if enable:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass
    log.info("Start with Windows: %s", "on" if enable else "off")


# game data

HEADER_FMT = "<HBBBBBQfIIBB"
HEADER_SIZE = struct.calcsize(HEADER_FMT)   # 29 bytes
MAX_CARS = 22
PKT_SESSION, PKT_LAP, PKT_PARTICIPANTS = 1, 2, 4

GAME_MODES = {
    0: "Event Mode", 3: "Grand Prix", 4: "Grand Prix", 5: "Time Trial", 6: "Splitscreen",
    7: "Online Custom", 8: "Online League", 11: "Career Invitational",
    12: "Championship Invitational", 13: "Championship", 14: "Online Championship",
    15: "Online Weekly Event", 17: "Braking Point",
    19: "Career", 20: "Online Career", 21: "Career", 22: "Online Career",
    23: "Driver Career", 24: "Online Career", 25: "My Team", 26: "Curated Career",
    27: "My Team", 28: "Driver Career", 29: "Online Career", 30: "Challenge Career",
    75: "APXGP Story", 78: "Driver Career '26", 79: "My Team '26", 127: "Benchmark",
}
CAREER_MODES = {19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 78, 79}

TRACKS = {
    0: "Melbourne", 1: "Paul Ricard", 2: "Shanghai", 3: "Bahrain", 4: "Barcelona",
    5: "Monaco", 6: "Montreal", 7: "Silverstone", 8: "Hockenheim", 9: "Hungaroring",
    10: "Spa", 11: "Monza", 12: "Singapore", 13: "Suzuka", 14: "Abu Dhabi",
    15: "COTA", 16: "Interlagos", 17: "Red Bull Ring", 18: "Sochi", 19: "Mexico City",
    20: "Baku", 21: "Bahrain Short", 22: "Silverstone Short", 23: "COTA Short",
    24: "Suzuka Short", 25: "Hanoi", 26: "Zandvoort", 27: "Imola", 28: "Portimao",
    29: "Jeddah", 30: "Miami", 31: "Las Vegas", 32: "Losail",
    39: "Silverstone (Reverse)", 40: "Red Bull Ring (Reverse)", 41: "Zandvoort (Reverse)",
}

SESSIONS = {
    0: "", 1: "Practice 1", 2: "Practice 2", 3: "Practice 3", 4: "Short Practice",
    5: "Q1", 6: "Q2", 7: "Q3", 8: "Short Quali", 9: "One-Shot Quali",
    10: "Sprint Shootout 1", 11: "Sprint Shootout 2", 12: "Sprint Shootout 3",
    13: "Short Sprint Shootout", 14: "One-Shot Sprint Shootout",
    15: "Race", 16: "Race 2", 17: "Race 3", 18: "Time Trial",
}
RACE_SESSIONS = {15, 16, 17}

TEAMS = {
    0: "Mercedes", 1: "Ferrari", 2: "Red Bull", 3: "Williams", 4: "Aston Martin",
    5: "Alpine", 6: "Racing Bulls", 7: "Haas", 8: "McLaren", 9: "Sauber",
}

# Art Assets uploaded to the Discord app (see assets/discord). Key = lowercase name, letters only.
TRACK_IMAGES = {
    "melbourne", "shanghai", "suzuka", "bahrain", "jeddah", "miami", "imola", "monaco",
    "barcelona", "montreal", "redbullring", "silverstone", "spa", "hungaroring", "zandvoort",
    "monza", "baku", "singapore", "cota", "mexicocity", "interlagos", "lasvegas", "losail",
    "abudhabi", "madring", "silverstonereverse", "redbullringreverse", "zandvoortreverse",
}


def asset_key(name):
    return "".join(c for c in (name or "").lower() if c.isalnum())


def track_image_key(track_id):
    """'Silverstone (Reverse)' -> 'silverstonereverse', 'Bahrain Short' -> 'bahrain'."""
    key = asset_key((TRACKS.get(track_id) or "").replace(" Short", ""))
    return key if key in TRACK_IMAGES else None


def team_image_key(team_id):
    name = TEAMS.get(team_id)
    return asset_key(name) if name else None


class GameProcess:
    """Checks (at most every few seconds) whether the game is running on this PC."""

    def __init__(self, names):
        self.names = {n.lower() for n in names}
        self.last_check = 0.0
        self.running = None
        self.since = None

    def is_running(self):
        if time.time() - self.last_check < 5:
            return self.running
        self.last_check = time.time()
        try:
            import psutil
            found = any((p.info["name"] or "").lower() in self.names
                        for p in psutil.process_iter(["name"]))
        except Exception:
            found = None    # can't tell, fall back to packet timeouts
        if found and not self.running:
            self.since = int(time.time())
        self.running = found
        return found


class GameState:
    def __init__(self, cfg, process_checker=None):
        self.cfg = cfg
        self.proc = process_checker
        self.session_uid = None
        self.session_start = None
        self.game_mode = None
        self.track_id = None
        self.session_type = 0
        self.total_laps = 0
        self.position = 0
        self.lap = 0
        self.num_cars = 0
        self.driver_name = ""
        self.team_id = None
        self.last_packet = 0.0
        self.warned_format = False

    def handle(self, data: bytes):
        if len(data) < HEADER_SIZE:
            return
        (fmt, _year, _maj, _min, _ver, pkt_id, uid, _stime,
         _frame, _oframe, player_idx, _p2) = struct.unpack_from(HEADER_FMT, data, 0)

        if fmt != 2025:
            if not self.warned_format:
                log.warning("Got packet format %s. Set UDP Format to 2025 in the game's telemetry settings.", fmt)
                self.warned_format = True
            return

        self.last_packet = time.time()
        if uid != self.session_uid:
            self.session_uid = uid
            self.session_start = int(time.time())

        body = HEADER_SIZE
        if pkt_id == PKT_SESSION and len(data) >= body + 667:
            self.total_laps = data[body + 3]
            self.session_type = data[body + 6]
            self.track_id = struct.unpack_from("<b", data, body + 7)[0]
            self.game_mode = data[body + 665]
            if self.game_mode not in GAME_MODES or self.track_id not in TRACKS:
                log.debug("Unknown id: mode=%s track=%s", self.game_mode, self.track_id)

        elif pkt_id == PKT_LAP and player_idx < MAX_CARS:
            stride = (len(data) - body - 2) // MAX_CARS
            off = body + player_idx * stride
            if stride >= 34:
                self.position = data[off + 32]
                self.lap = data[off + 33]

        elif pkt_id == PKT_PARTICIPANTS and player_idx < MAX_CARS:
            self.num_cars = data[body]
            stride = (len(data) - body - 1) // MAX_CARS
            off = body + 1 + player_idx * stride
            if stride >= 40:
                self.team_id = data[off + 3]
                raw = data[off + 7: off + 7 + 32]
                self.driver_name = raw.split(b"\x00", 1)[0].decode("utf-8", "ignore").strip()

    def presence(self):
        """Returns the dict to send to Discord, or None to clear."""
        idle = time.time() - self.last_packet
        in_session = self.last_packet and idle <= self.cfg["menu_timeout"] and self.game_mode is not None

        if not in_session:
            running = self.proc.is_running() if self.proc else None
            if running:
                return self._with_image({"details": "In the menus", "start": self.proc.since})
            if running is None and self.last_packet and idle < NO_PROCESS_CLEAR_TIMEOUT:
                return self._with_image({"details": "In the menus"})
            return None

        mode = GAME_MODES.get(self.game_mode, f"Mode {self.game_mode}")
        track = TRACKS.get(self.track_id, f"Track {self.track_id}")
        session = SESSIONS.get(self.session_type, "")
        is_career = self.game_mode in CAREER_MODES
        is_tt = self.session_type == 18 or self.game_mode == 5

        parts = []
        if (is_career or self.cfg["show_driver_outside_career"]) and self.driver_name:
            team = TEAMS.get(self.team_id)
            parts.append(f"{self.driver_name} ({team})" if team else self.driver_name)
        if session and not is_tt:
            parts.append(session)
        if not is_tt and self.position and self.num_cars:
            parts.append(f"P{self.position}/{self.num_cars}")
        if self.session_type in RACE_SESSIONS and self.lap and self.total_laps:
            parts.append(f"Lap {min(self.lap, self.total_laps)}/{self.total_laps}")
        if is_tt:
            parts.append("Hotlapping")

        return self._with_image({
            "details": f"{mode} · {track}",
            "state": " · ".join(parts) or None,
            "start": self.session_start,
        }, in_session=True)

    def _with_image(self, out, in_session=False):
        icon = self.cfg.get("large_image")
        track_key = track_image_key(self.track_id) if in_session and self.cfg.get("track_images", True) else None
        team_key = team_image_key(self.team_id) if in_session and self.cfg.get("team_images", True) else None

        if track_key:
            out["large_image"] = track_key
            out["large_text"] = TRACKS[self.track_id]
        elif icon:
            out["large_image"] = icon
            out["large_text"] = "F1 25"

        if out.get("large_image"):    # Discord only shows a small image if there's a large one
            if team_key:
                out["small_image"] = team_key
                out["small_text"] = TEAMS[self.team_id]
            elif track_key and icon:
                out["small_image"] = icon
                out["small_text"] = "F1 25"
        return out


# discord

class Discord:
    def __init__(self, client_id):
        self.client_id = client_id
        self.rpc = None
        self.next_retry = 0

    @property
    def connected(self):
        return self.rpc is not None

    def _connect(self):
        if self.rpc or time.time() < self.next_retry:
            return self.rpc is not None
        try:
            from pypresence import Presence
            rpc = Presence(self.client_id)
            rpc.connect()
            self.rpc = rpc
            log.info("Connected to Discord")
        except Exception as e:
            log.debug("Can't reach Discord (%s), retrying in 15s", e)
            self.next_retry = time.time() + 15
        return self.rpc is not None

    def set(self, payload):
        """Returns True if Discord accepted the update."""
        if not self._connect():
            return False
        try:
            if payload is None:
                self.rpc.clear()
            else:
                self.rpc.update(**{k: v for k, v in payload.items() if v is not None})
            return True
        except Exception as e:
            log.info("Lost Discord connection (%s), reconnecting", e)
            self.rpc = None
            return False

    def close(self):
        if self.rpc:
            try:
                self.rpc.clear()
                self.rpc.close()
            except Exception:
                pass
            self.rpc = None


# worker thread

class Worker(threading.Thread):
    def __init__(self, cfg, discord, process_checker=None):
        super().__init__(daemon=True, name="presence-worker")
        self.cfg = cfg
        self.discord = discord
        self.state = GameState(cfg, process_checker)
        self.stop_event = threading.Event()
        self.ready = threading.Event()
        self.error = None
        self.status = "Starting..."
        self.on_status = None

    def _set_status(self, text):
        if text != self.status:
            self.status = text
            if self.on_status:
                try:
                    self.on_status(text)
                except Exception:
                    pass

    def stop(self):
        self.stop_event.set()

    def run(self):
        # pypresence needs an asyncio loop in whatever thread it runs in
        asyncio.set_event_loop(asyncio.new_event_loop())

        port = int(self.cfg["udp_port"])
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            self.error = (f"Port {port} is already in use.\n\n"
                          "Either F1 25 Rich Presence is already running (check your tray), "
                          "or another telemetry app is using that port.")
            self.ready.set()
            return
        sock.settimeout(1.0)
        log.info("Listening for F1 25 telemetry on UDP port %s", port)
        self.ready.set()

        last_sent, last_time = "unset", 0.0
        while not self.stop_event.is_set():
            try:
                data, _ = sock.recvfrom(4096)
                self.state.handle(data)
            except socket.timeout:
                pass
            except OSError as e:
                log.debug("Socket error: %s", e)

            now = time.time()
            payload = self.state.presence()
            if payload != last_sent and now - last_time >= MIN_UPDATE_GAP:
                last_time = now
                if self.discord.set(payload):
                    last_sent = payload
                    if payload:
                        log.info("%s | %s", payload["details"], payload.get("state") or "")
                    else:
                        log.info("Cleared (game not running)")

            if not self.discord.connected:
                self._set_status("Waiting for Discord...")
            elif payload:
                line = payload["details"] + (f" | {payload['state']}" if payload.get("state") else "")
                self._set_status(line)
            else:
                self._set_status("Waiting for F1 25...")

        sock.close()
        self.discord.close()
        log.info("Stopped")


# tray

def make_fallback_icon():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, 63, 63), 12, fill=(225, 6, 0))
    for r in range(4):
        for c in range(4):
            if (r + c) % 2 == 0:
                d.rectangle((12 + c * 10, 12 + r * 10, 21 + c * 10, 21 + r * 10), fill="white")
    return img


def run_tray(worker, first_run):
    import pystray
    from PIL import Image

    try:
        image = Image.open(resource_path("assets/icon.ico"))
    except Exception:
        image = make_fallback_icon()

    def toggle_startup(icon, item):
        set_startup(not startup_enabled())

    def open_folder(icon, item):
        if IS_WINDOWS:
            os.startfile(app_dir())

    def quit_app(icon, item):
        worker.stop()
        worker.join(timeout=3)
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(lambda item: worker.status[:60], None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start with Windows", toggle_startup,
                         checked=lambda item: startup_enabled(), visible=IS_WINDOWS),
        pystray.MenuItem("Open settings folder", open_folder, visible=IS_WINDOWS),
        pystray.MenuItem(f"Quit (v{VERSION})", quit_app),
    )
    icon = pystray.Icon(APP_NAME, image, APP_DISPLAY, menu)

    def on_status(text):
        icon.title = f"{APP_DISPLAY}\n{text}"[:127]
        icon.update_menu()

    worker.on_status = on_status

    def setup(icon):
        icon.visible = True
        if first_run:
            try:
                icon.notify("Running in your system tray. Right-click the icon for options.", APP_DISPLAY)
            except Exception:
                pass

    icon.run(setup=setup)


# main

def main():
    cfg, first_run = load_config()
    setup_logging(bool(cfg.get("debug")))
    log.info("%s v%s starting", APP_DISPLAY, VERSION)

    client_id = str(cfg.get("client_id") or DEFAULT_CLIENT_ID).strip()
    if not client_id:
        message_box("No Discord application ID set.\n\n"
                    f"Add your \"client_id\" to:\n{app_dir() / 'config.json'}")
        return

    # Turn on start-with-Windows the first time, and keep the path fresh if the exe moved
    if IS_WINDOWS and getattr(sys, "frozen", False) and (first_run or startup_enabled()):
        set_startup(True)

    proc = GameProcess(cfg["game_process_names"]) if cfg.get("check_game_process") else None
    worker = Worker(cfg, Discord(client_id), proc)
    worker.start()
    worker.ready.wait(5)
    if worker.error:
        message_box(worker.error)
        return

    if "--no-tray" in sys.argv:
        try:
            while worker.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            worker.stop()
            worker.join(timeout=3)
        return

    try:
        run_tray(worker, first_run)
    except ImportError:
        log.warning("pystray/Pillow not installed, running without a tray icon (Ctrl+C to quit)")
        try:
            while worker.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            worker.stop()


if __name__ == "__main__":
    main()

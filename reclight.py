"""RecLight - indikátor nahrávání: DAW (Studio One, Cubase; Mackie Control přes virtuální MIDI port) -> ESP32 přes USB.

Spuštění:
    reclight.py            okno aplikace + ikona v liště
    reclight.py --hidden   start jen v liště (používá autostart)
    reclight.py --list     vypíše MIDI porty a COM porty (včetně VID/PID)
    reclight.py --monitor  vypisuje příchozí MIDI zprávy do konzole (ladění)
"""
import ctypes
import json
import logging
import os
import plistlib
import socket
import sys
import threading
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

APP_NAME = "RecLight"
APP_VERSION = "2.0"
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
LAUNCH_LABEL = "eu.c2coder.reclight"          # LaunchAgent (macOS)
SINGLE_PORT = 47839                           # jedna instance (macOS / Linux): lokální port
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

DEFAULT_CONFIG = {
    # část názvu MIDI portu (virtuální port RecLight / loopMIDI), do kterého DAW (Studio One, Cubase) posílá Mackie Control
    "midi_port_contains": "RecLight",
    # Mackie Control: tlačítko Record = nota 95 (0x5F), kanál 1 (index 0); null = jakýkoliv kanál
    "record_note": 95,
    "record_channel": None,
    # ESP: "auto" = najde se podle VID/PID, nebo napište konkrétní port např. "COM5"
    "esp_port": "auto",
    "esp_vid": None,       # např. "0x10C4"; null = běžné USB-serial čipy
    "esp_pid": None,       # např. "0xEA60"; null = libovolné PID
    "esp_port_fallback": None,
    "baud": 115200,
    "keepalive_seconds": 1.0,
    # po zastavení nahrávání světlo svítí ještě tolik sekund (0 = zhasne hned)
    "off_delay": 5,
    # když port neexistuje, vytvoří si RecLight vlastní virtuální port (Windows: ovladač virtualMIDI z loopMIDI, macOS: CoreMIDI)
    "create_virtual_port": True,
    "debug_midi": False,
}

KNOWN_SERIAL_VIDS = {0x303A, 0x1A86, 0x10C4, 0x0403}  # Espressif, CH340, CP210x, FTDI
SERIAL_HINTS = ("espressif", "ch340", "ch910", "cp210", "ftdi", "usb serial", "usb-serial", "jtag")


# ---------------------------------------------------------------- konfigurace a logy
def config_dir() -> Path:
    if IS_WIN:
        base = Path(os.environ.get("APPDATA", str(Path.home())))
        return base / APP_NAME
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path.home() / ".reclight"


def config_path() -> Path:
    """Starší konfigurace (jen pro převzetí hodnot)."""
    return config_dir() / "config.json"


def settings_path() -> Path:
    return config_dir() / "settings.json"


def stats_path() -> Path:
    return config_dir() / "stats.json"


def log_path() -> Path:
    return config_dir() / "reclight.log"


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def setup_logging(console: bool = False) -> None:
    config_dir().mkdir(parents=True, exist_ok=True)
    handlers = [RotatingFileHandler(log_path(), maxBytes=500_000, backupCount=2, encoding="utf-8")]
    if console:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:
        logging.error("Konfiguraci %s se nepodařilo načíst: %s", path, exc)
        return {}


def save_config(cfg: dict) -> None:
    config_dir().mkdir(parents=True, exist_ok=True)
    data = {k: cfg.get(k, v) for k, v in DEFAULT_CONFIG.items()}
    settings_path().write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_config() -> dict:
    """Nastavení žije v settings.json. Při prvním spuštění se převezmou hodnoty ze starších
    config.json (AppData i vedle aplikace), aby se neztratila identifikace ESP."""
    config_dir().mkdir(parents=True, exist_ok=True)
    cfg = dict(DEFAULT_CONFIG)
    if settings_path().exists():
        cfg.update(_read_json(settings_path()))
        return cfg
    for p in (config_path(), app_dir() / "config.json", app_dir().parent / "config.json"):
        if p.exists():
            cfg.update(_read_json(p))
    save_config(cfg)
    return cfg


def to_int(value):
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return int(value, 0)
    return int(value)


# ---------------------------------------------------------------- jádro
class Core:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.stop = threading.Event()
        self.wake = threading.Event()
        self._lock = threading.Lock()
        self.generation = 0          # zvýší se při změně nastavení -> smyčky se znovu připojí
        self.recording = False
        self.midi_ok = False
        self.esp_ok = False
        self.midi_name = None
        self.esp_device = None
        self.test_until = 0.0
        self.hold_until = 0.0        # dosvícení po konci nahrávání
        self.rec_started = None
        self.listeners = []
        self.last_midi_at = None     # kdy naposledy přišla jakákoliv MIDI zpráva z DAW
        self.sessions = self._load_sessions()

    # --- stav
    def _notify(self) -> None:
        for cb in self.listeners:
            try:
                cb()
            except Exception:  # noqa: BLE001
                logging.exception("Chyba v listeneru")

    def light_on(self) -> bool:
        now = time.time()
        return self.recording or now < self.test_until or now < self.hold_until

    def testing(self) -> bool:
        return time.time() < self.test_until

    def off_delay(self) -> float:
        try:
            return min(120.0, max(0.0, float(self.cfg.get("off_delay", 5))))
        except (TypeError, ValueError):
            return 5.0

    def holding(self) -> bool:
        """Nahrávání skončilo, ale světlo ještě svítí (zpoždění zhasnutí)."""
        return not self.recording and time.time() < self.hold_until

    def hold_left(self) -> int:
        return max(0, int(self.hold_until - time.time() + 0.999))

    def _schedule_wake(self, seconds: float) -> None:
        """Vzbudí odesílací smyčku a okno ve chvíli, kdy se má světlo změnit."""
        def later():
            time.sleep(seconds + 0.1)
            self.wake.set()
            self._notify()

        threading.Thread(target=later, daemon=True).start()

    def _set(self, name: str, value: bool) -> None:
        with self._lock:
            changed = getattr(self, name) != value
            setattr(self, name, value)
        if changed:
            logging.info("%s -> %s", name, value)
            self.wake.set()
            self._notify()

    def set_recording(self, value: bool) -> None:
        if value and not self.recording:
            self.rec_started = time.time()
        elif not value and self.recording:
            self._finish_session()
            delay = self.off_delay()
            if delay > 0:
                self.hold_until = time.time() + delay
                self._schedule_wake(delay)
        if value:
            self.hold_until = 0.0
        self._set("recording", value)

    def start_test(self, seconds: float = 3.0) -> None:
        self.test_until = time.time() + seconds
        self.wake.set()
        self._notify()
        self._schedule_wake(seconds)

    def set_option(self, key: str, value) -> None:
        """Změní jednu volbu hned a uloží ji (bez opětovného připojování zařízení)."""
        self.cfg[key] = value
        try:
            save_config(self.cfg)
        except OSError:
            logging.exception("Nastavení se nepodařilo uložit")
        self.wake.set()
        self._notify()

    def apply_config(self, new: dict) -> None:
        """Použije nové nastavení hned, bez restartu: smyčky se znovu připojí."""
        self.cfg.update(new)
        self.generation += 1
        self.wake.set()
        logging.info("Nastavení změněno, znovu se připojuji")

    # --- statistika nahrávání
    def elapsed(self) -> int:
        if self.recording and self.rec_started:
            return int(time.time() - self.rec_started)
        return 0

    @staticmethod
    def _load_sessions() -> list:
        try:
            data = json.loads(stats_path().read_text(encoding="utf-8"))
            return data.get("sessions", []) if isinstance(data, dict) else []
        except (OSError, ValueError):
            return []

    def _finish_session(self) -> None:
        if not self.rec_started:
            return
        seconds = int(time.time() - self.rec_started)
        started = datetime.fromtimestamp(self.rec_started)
        self.rec_started = None
        if seconds < 1:
            return
        self.sessions.append({"start": started.isoformat(timespec="seconds"), "seconds": seconds})
        self.sessions = self.sessions[-200:]
        try:
            config_dir().mkdir(parents=True, exist_ok=True)
            stats_path().write_text(json.dumps({"sessions": self.sessions}, ensure_ascii=False), encoding="utf-8")
        except OSError:
            logging.exception("Statistiku se nepodařilo uložit")

    def today_sessions(self) -> list:
        today = datetime.now().date().isoformat()
        out = [s for s in self.sessions if str(s.get("start", "")).startswith(today)]
        if self.recording and self.rec_started:
            out.append({"start": datetime.fromtimestamp(self.rec_started).isoformat(timespec="seconds"),
                        "seconds": self.elapsed()})
        return out

    def today_seconds(self) -> int:
        return sum(int(s.get("seconds", 0)) for s in self.today_sessions())

    # --- MIDI
    def daw_label(self) -> str:
        return "DAW"

    def handle_midi(self, msg) -> None:
        self.last_midi_at = time.time()
        if self.cfg.get("debug_midi"):
            logging.info("MIDI: %s", msg)
        if msg.type not in ("note_on", "note_off"):
            return
        if msg.note != to_int(self.cfg["record_note"]):
            return
        channel = self.cfg.get("record_channel")
        if channel is not None and msg.channel != to_int(channel):
            return
        self.set_recording(msg.type == "note_on" and msg.velocity > 0)

    def _find_midi_port(self):
        import mido
        if IS_MAC and self.cfg.get("create_virtual_port", True):
            return None   # na macOS si RecLight vytvoří vlastní port (IAC bus se použije, jen když je volba vypnutá)
        needle = str(self.cfg["midi_port_contains"]).lower()
        for name in mido.get_input_names():
            if needle in name.lower():
                return name
        return None

    def midi_loop(self) -> None:
        import mido
        mido.set_backend("mido.backends.rtmidi")
        while not self.stop.is_set():
            gen = self.generation
            try:
                name = self._find_midi_port()
                if not name:
                    self.midi_name = None
                    self._set("midi_ok", False)
                    if self.cfg.get("create_virtual_port", True) and self._virtual_available():
                        self._virtual_session(gen)
                        continue
                    self.stop.wait(2.0)
                    continue
                with mido.open_input(name) as port:
                    logging.info("MIDI port otevřen: %s", name)
                    self.midi_name = name
                    self._set("midi_ok", True)
                    last_check = time.time()
                    while not self.stop.is_set() and gen == self.generation:
                        for msg in port.iter_pending():
                            self.handle_midi(msg)
                        time.sleep(0.01)
                        if time.time() - last_check > 3.0:
                            last_check = time.time()
                            if name not in mido.get_input_names():
                                logging.warning("MIDI port zmizel: %s", name)
                                break
            except Exception as exc:  # noqa: BLE001
                logging.error("MIDI chyba: %s", exc)
            self.midi_name = None
            self._set("midi_ok", False)
            if gen == self.generation:
                self.stop.wait(2.0)

    @staticmethod
    def _virtual_available() -> bool:
        try:
            import vmidi
            return vmidi.available()
        except Exception:  # noqa: BLE001
            return False

    def _virtual_session(self, gen: int) -> None:
        """Vlastní virtuální MIDI port RecLight (bez loopMIDI). Blokuje, dokud se nezmění nastavení,
        nepřijde ukončení, nebo se neobjeví port z loopMIDI."""
        import mido
        import vmidi
        port_name = str(self.cfg.get("midi_port_contains") or "RecLight")
        try:
            vp = vmidi.VirtualPort(port_name)
        except Exception as exc:  # noqa: BLE001
            logging.error("Vlastní MIDI port: %s", exc)
            self.stop.wait(3.0)
            return
        logging.info("Vytvořen vlastní virtuální MIDI port: %s", port_name)
        self.midi_name = f"{port_name} (vlastní port)"
        self._set("midi_ok", True)

        def watchdog():
            while not self.stop.is_set() and gen == self.generation and not vp.closed:
                time.sleep(0.2)
            vp.shutdown()

        threading.Thread(target=watchdog, daemon=True).start()
        try:
            while not vp.closed:
                data = vp.read()
                if data is None:
                    break
                self.last_midi_at = time.time()
                try:
                    self.handle_midi(mido.Message.from_bytes(list(data)))
                except (ValueError, IndexError):
                    pass  # sysex a jiné nezajímavé zprávy
        except Exception as exc:  # noqa: BLE001
            logging.error("Vlastní MIDI port selhal: %s", exc)
        finally:
            vp.close()
            logging.info("Vlastní virtuální MIDI port zavřen")
            self.midi_name = None
            self._set("midi_ok", False)

    # --- ESP / USB serial
    def _find_esp_port(self):
        from serial.tools import list_ports
        ports = list(list_ports.comports())
        present = {p.device.upper(): p.device for p in ports}
        explicit = str(self.cfg.get("esp_port", "auto"))
        if explicit.lower() != "auto":
            return present.get(explicit.upper(), explicit)
        vid = to_int(self.cfg.get("esp_vid"))
        pid = to_int(self.cfg.get("esp_pid"))
        # 1) přesná shoda VID/PID z konfigurace
        if vid is not None:
            for p in ports:
                if p.vid == vid and (pid is None or p.pid == pid):
                    return p.device
        # 2) běžné USB-serial čipy / ESP podle VID nebo popisu
        for p in ports:
            text = f"{p.description} {p.manufacturer or ''}".lower()
            if p.vid in KNOWN_SERIAL_VIDS or any(k in text for k in SERIAL_HINTS):
                return p.device
        # 3) záložní port (ten, kde ESP víme, že je), pokud existuje
        fallback = self.cfg.get("esp_port_fallback")
        if fallback and str(fallback).upper() in present:
            return present[str(fallback).upper()]
        return None

    def esp_loop(self) -> None:
        import serial
        while not self.stop.is_set():
            gen = self.generation
            keepalive = float(self.cfg["keepalive_seconds"])
            ser = None
            try:
                device = self._find_esp_port()
                if not device and not getattr(self, "_logged_ports", False):
                    from serial.tools import list_ports
                    self._logged_ports = True
                    for p in list_ports.comports():
                        logging.info("COM: %s VID=%s PID=%s %s", p.device, p.vid, p.pid, p.description)
                if not device:
                    self.esp_device = None
                    self._set("esp_ok", False)
                    self.stop.wait(2.0)
                    continue
                ser = serial.Serial()
                ser.port = device
                ser.baudrate = int(self.cfg["baud"])
                ser.write_timeout = 2
                ser.dtr = False  # aby se ESP nerestartovalo při otevření portu
                ser.rts = False
                ser.open()
                logging.info("ESP připojeno na %s", device)
                self.esp_device = device
                self._set("esp_ok", True)
                while not self.stop.is_set() and gen == self.generation:
                    line = b"REC\n" if self.light_on() else b"IDLE\n"
                    ser.write(line)
                    ser.flush()
                    self.wake.wait(keepalive)
                    self.wake.clear()
            except Exception as exc:  # noqa: BLE001
                logging.error("ESP chyba: %s", exc)
            finally:
                if ser is not None:
                    try:
                        ser.close()
                    except Exception:  # noqa: BLE001
                        pass
            self.esp_device = None
            self._set("esp_ok", False)
            if gen == self.generation:
                self.stop.wait(2.0)

    def start(self) -> None:
        for target in (self.midi_loop, self.esp_loop):
            threading.Thread(target=target, daemon=True).start()

    def status_text(self) -> str:
        state = "NAHRÁVÁ" if self.recording else "Čeká"
        midi = "MIDI ok" if self.midi_ok else "MIDI: hledám port"
        esp = "ESP ok" if self.esp_ok else "ESP: nepřipojeno"
        return f"{state} · {midi} · {esp}"

    def icon_state(self) -> str:
        if not (self.midi_ok and self.esp_ok):
            return "warn"
        return "rec" if self.light_on() else "idle"


# ---------------------------------------------------------------- autostart
def launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --hidden'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{exe}" "{Path(__file__).resolve()}" --hidden'


def _launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_LABEL}.plist"


def _mac_program_args() -> list:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--hidden"]
    return [sys.executable, str(Path(__file__).resolve()), "--hidden"]


def autostart_enabled() -> bool:
    if IS_MAC:
        return _launch_agent_path().exists()
    if not IS_WIN:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except Exception:  # noqa: BLE001
        return False


def set_autostart(enable: bool) -> None:
    if IS_MAC:
        path = _launch_agent_path()
        if enable:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {"Label": LAUNCH_LABEL, "ProgramArguments": _mac_program_args(), "RunAtLoad": True,
                    "ProcessType": "Interactive"}
            with open(path, "wb") as fh:
                plistlib.dump(data, fh)
        else:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        return
    if not IS_WIN:
        raise RuntimeError("Autostart je podporován jen ve Windows a macOS")
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enable:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------- jedna instance
def _kernel32():
    k = ctypes.windll.kernel32
    k.CreateMutexW.restype = ctypes.c_void_p
    k.CreateEventW.restype = ctypes.c_void_p
    k.OpenEventW.restype = ctypes.c_void_p
    k.SetEvent.argtypes = [ctypes.c_void_p]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    return k


_instance_sock = None


def single_instance() -> bool:
    """False = už běží jiná instance (té se pošle signál, ať ukáže okno)."""
    global _instance_sock
    if not IS_WIN:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", SINGLE_PORT))
            sock.listen(2)
            _instance_sock = sock
            return True
        except OSError:
            sock.close()
            try:
                with socket.create_connection(("127.0.0.1", SINGLE_PORT), timeout=1.0) as c:
                    c.sendall(b"show\n")
            except OSError:
                pass
            return False
    k = _kernel32()
    k.CreateMutexW(None, False, "Local\\RecLightMutex")
    if k.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        handle = k.OpenEventW(0x0002, False, "Local\\RecLightShow")  # EVENT_MODIFY_STATE
        if handle:
            k.SetEvent(handle)
            k.CloseHandle(handle)
        return False
    return True


def watch_show_requests(callback) -> None:
    """Na pozadí čeká, až druhé spuštění požádá o zobrazení okna."""
    if not IS_WIN:
        sock = _instance_sock
        if sock is None:
            return

        def accept_loop():
            while True:
                try:
                    conn, _ = sock.accept()
                except OSError:
                    return
                try:
                    conn.close()
                    callback()
                except Exception:  # noqa: BLE001
                    logging.exception("Zobrazení okna selhalo")

        threading.Thread(target=accept_loop, daemon=True).start()
        return
    k = _kernel32()
    handle = k.CreateEventW(None, False, False, "Local\\RecLightShow")

    def loop():
        while True:
            if k.WaitForSingleObject(handle, 1000) == 0:
                try:
                    callback()
                except Exception:  # noqa: BLE001
                    logging.exception("Zobrazení okna selhalo")

    threading.Thread(target=loop, daemon=True).start()


# ---------------------------------------------------------------- CLI nástroje
def cmd_list() -> None:
    import mido
    from serial.tools import list_ports
    mido.set_backend("mido.backends.rtmidi")
    print("MIDI vstupy:")
    for name in mido.get_input_names():
        print("  ", name)
    print("\nMIDI výstupy:")
    for name in mido.get_output_names():
        print("  ", name)
    print("\nSériové porty (COM / tty):")
    for p in list_ports.comports():
        vid = f"0x{p.vid:04X}" if p.vid is not None else "-"
        pid = f"0x{p.pid:04X}" if p.pid is not None else "-"
        print(f"   {p.device}  VID={vid} PID={pid}  {p.description}")


def cmd_monitor() -> None:
    cfg = load_config()
    cfg["debug_midi"] = True
    core = Core(cfg)
    orig = core.handle_midi

    def printing(msg):
        print(msg)
        orig(msg)

    core.handle_midi = printing  # type: ignore[method-assign]
    core.start()
    print("Poslouchám MIDI, Ctrl+C ukončí.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        core.stop.set()


def main() -> None:
    if "--list" in sys.argv:
        setup_logging(console=True)
        cmd_list()
        return
    if "--monitor" in sys.argv:
        setup_logging(console=True)
        cmd_monitor()
        return
    setup_logging()
    if not single_instance():
        return
    cfg = load_config()
    if autostart_enabled():
        try:
            set_autostart(True)  # obnoví příkaz (přidá --hidden, opraví cestu po přesunu aplikace)
        except Exception:  # noqa: BLE001
            logging.exception("Autostart se nepodařilo obnovit")
    core = Core(cfg)
    core.start()
    try:
        from gui import App
        App(core, start_hidden="--hidden" in sys.argv).run()
    except Exception:  # noqa: BLE001
        logging.exception("Rozhraní selhalo")
        raise


if __name__ == "__main__":
    main()

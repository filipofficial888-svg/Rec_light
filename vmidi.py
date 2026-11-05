"""Vlastní virtuální MIDI port.

Windows: přes ovladač virtualMIDI (instaluje ho loopMIDI). macOS: přes CoreMIDI (virtuální port, nic se neinstaluje).
Port existuje jen po dobu běhu RecLight. Studio One i Cubase ho vidí jako běžné MIDI zařízení.
Pokud to není možné, available() vrátí False a aplikace použije existující port (např. z loopMIDI).
"""
import sys
import time
import ctypes
import logging
import os
import struct
import threading

TE_VM_FLAGS_PARSE_RX = 1
MAX_SYSEX = 65535

_lib = None  # None = nenačteno, False = nedostupné


IS_MAC = sys.platform == "darwin"


def _load():
    global _lib
    if _lib is not None:
        return _lib or None
    if os.name != "nt":
        _lib = False
        return None
    names = ["teVirtualMIDI64.dll" if struct.calcsize("P") == 8 else "teVirtualMIDI32.dll", "teVirtualMIDI.dll"]
    for name in names:
        try:
            lib = ctypes.WinDLL(name)
        except OSError:
            continue
        try:
            lib.virtualMIDICreatePortEx2.restype = ctypes.c_void_p
            lib.virtualMIDICreatePortEx2.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_size_t,
                                                     ctypes.c_uint32, ctypes.c_uint32]
            lib.virtualMIDIGetData.restype = ctypes.c_int
            lib.virtualMIDIGetData.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint32)]
            lib.virtualMIDIShutdown.argtypes = [ctypes.c_void_p]
            lib.virtualMIDIClosePort.argtypes = [ctypes.c_void_p]
        except AttributeError:
            logging.warning("%s nemá očekávané funkce", name)
            continue
        _lib = lib
        logging.info("Načten ovladač virtualMIDI: %s", name)
        return lib
    _lib = False
    return None


def available() -> bool:
    if IS_MAC:
        try:
            import mido  # noqa: F401
            import rtmidi  # noqa: F401
            return True
        except Exception:  # noqa: BLE001
            return False
    return _load() is not None


class MacVirtualPort:
    """macOS: virtuální MIDI cíl (DAW do něj posílá) a zdroj (DAW z něj může číst) pojmenované stejně."""

    def __init__(self, name: str):
        import mido
        mido.set_backend("mido.backends.rtmidi")
        self.name = name
        self._closed = False
        self._lock = threading.Lock()
        self._out = None
        self._in = mido.open_input(name, virtual=True)
        try:
            self._out = mido.open_output(name, virtual=True)   # kvůli DAW, které chtějí i vstup; zprávy se neposílají
        except Exception:  # noqa: BLE001
            logging.warning("Virtuální výstup '%s' se nepodařilo vytvořit (jen příjem)", name)

    @property
    def closed(self) -> bool:
        return self._closed

    def read(self):
        while not self._closed:
            msg = self._in.poll()
            if msg is not None:
                return bytes(msg.bytes())
            time.sleep(0.005)
        return None

    def shutdown(self) -> None:
        self._closed = True

    def close(self) -> None:
        self.shutdown()
        with self._lock:
            ports, self._in, self._out = (self._in, self._out), None, None
        for p in ports:
            if p is not None:
                try:
                    p.close()
                except Exception:  # noqa: BLE001
                    logging.exception("Zavření virtuálního portu selhalo")


class VirtualPort:
    """Virtuální MIDI port. read() blokuje, dokud nepřijde zpráva; close() ho ukončí."""

    def __new__(cls, name: str):
        if IS_MAC:
            return MacVirtualPort(name)
        return super().__new__(cls)

    def __init__(self, name: str):
        lib = _load()
        if lib is None:
            raise RuntimeError("Ovladač virtualMIDI není nainstalovaný")
        self._lib = lib
        self.name = name
        self._buf = ctypes.create_string_buffer(MAX_SYSEX)
        self._lock = threading.Lock()
        self._closed = False
        self._port = lib.virtualMIDICreatePortEx2(name, None, 0, MAX_SYSEX, TE_VM_FLAGS_PARSE_RX)
        if not self._port:
            raise RuntimeError(f"Port '{name}' se nepodařilo vytvořit (název už používá jiná aplikace?)")

    @property
    def closed(self) -> bool:
        return self._closed

    def read(self):
        """Vrátí bytes jedné MIDI zprávy, nebo None při ukončení či chybě."""
        if self._closed:
            return None
        length = ctypes.c_uint32(MAX_SYSEX)
        ok = self._lib.virtualMIDIGetData(self._port, self._buf, ctypes.pointer(length))
        if not ok or self._closed:
            return None
        return bytes(self._buf[:length.value])

    def shutdown(self) -> None:
        """Odblokuje čekající read() (lze volat z jiného vlákna)."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._lib.virtualMIDIShutdown(self._port)
            except Exception:  # noqa: BLE001
                logging.exception("virtualMIDIShutdown selhal")

    def close(self) -> None:
        self.shutdown()
        with self._lock:
            port, self._port = self._port, None
        if port:
            try:
                self._lib.virtualMIDIClosePort(port)
            except Exception:  # noqa: BLE001
                logging.exception("virtualMIDIClosePort selhal")

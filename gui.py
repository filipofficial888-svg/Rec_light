"""RecLight - okno aplikace a ikona v liště (customtkinter + pystray)."""
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime

import customtkinter as ctk
from PIL import Image, ImageDraw

import reclight as rl

# ---------------------------------------------------------------- vzhled
BG = "#141518"
CARD = "#1e2024"
CARD2 = "#2a2d33"
BORDER = "#3a3d44"
TEXT = "#ededee"
MUTED = "#9a9da6"
RED = "#E24B4A"
RED_BG = "#D94342"
AMBER = "#E0A030"
AMBER_BG = "#3b2f14"
GREEN = "#2DB58A"
GRAY = "#6d7079"
ACCENT = "#4C8DF6"
ACCENT_HOVER = "#3c78dc"

FAMILY = "Segoe UI" if os.name == "nt" else None
AUTO_LABEL = "Automaticky (najít USB-serial čip)"
ANY_CHANNEL = "Libovolný"


def font(size=13, weight="normal"):
    return ctk.CTkFont(family=FAMILY, size=size, weight=weight)


# ---------------------------------------------------------------- obrázky
def make_logo(size=96) -> Image.Image:
    k = 4
    s = size * k
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((0, 0, s - 1, s - 1), fill="#26282c")
    pad = s * 0.17
    d.ellipse((pad, pad, s - pad, s - pad), outline=(226, 75, 74, 130), width=max(2, int(s * 0.03)))
    c = s * 0.30
    d.ellipse((c, c, s - c, s - c), fill=RED)
    h = s * 0.40
    d.ellipse((h, h, h + s * 0.09, h + s * 0.09), fill="#F5A5A3")
    return img.resize((size, size), Image.LANCZOS)


_TRAY_COLORS = {"rec": RED, "idle": "#7d8088", "warn": "#D9962B"}


def make_tray_icon(state: str) -> Image.Image:
    k = 4
    size = 64
    s = size * k
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((s * 0.03, s * 0.03, s * 0.97, s * 0.97), fill=_TRAY_COLORS[state])
    if state == "rec":
        d.ellipse((s * 0.30, s * 0.30, s * 0.70, s * 0.70), fill="white")
    elif state == "idle":
        d.ellipse((s * 0.30, s * 0.30, s * 0.70, s * 0.70), outline="white", width=int(s * 0.07))
    else:
        d.rounded_rectangle((s * 0.44, s * 0.20, s * 0.56, s * 0.58), radius=s * 0.04, fill="white")
        d.ellipse((s * 0.43, s * 0.67, s * 0.57, s * 0.81), fill="white")
    return img.resize((size, size), Image.LANCZOS)


def write_ico() -> str | None:
    try:
        path = rl.config_dir() / "reclight.ico"
        rl.config_dir().mkdir(parents=True, exist_ok=True)
        make_logo(256).save(path, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        return str(path)
    except Exception:  # noqa: BLE001
        logging.exception("Ikonu okna se nepodařilo vytvořit")
        return None


# ---------------------------------------------------------------- pomocné funkce
def fmt_hms(seconds: int) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def fmt_total(seconds: int) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


def list_midi_inputs() -> list:
    try:
        import mido
        mido.set_backend("mido.backends.rtmidi")
        return list(mido.get_input_names())
    except Exception:  # noqa: BLE001
        return []


def list_serial_devices() -> list:
    """[(label, device, vid, pid)]"""
    out = []
    try:
        from serial.tools import list_ports
        for p in list_ports.comports():
            if p.vid is None:
                continue
            desc = (p.description or "").replace(f"({p.device})", "").strip()
            label = f"{p.device} · {desc} · {p.vid:04X}:{p.pid:04X}"
            out.append((label, p.device, p.vid, p.pid))
    except Exception:  # noqa: BLE001
        pass
    return out


# ---------------------------------------------------------------- aplikace
class App:
    def __init__(self, core: rl.Core, start_hidden: bool = False, tray: bool = True):
        self.core = core
        self.q: queue.Queue = queue.Queue()
        self.tick_n = 0
        self.tray = None
        self.tray_ok = False
        self.sessions_sig = None
        self.esp_devices: list = []
        self.hero_state = None

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.root = ctk.CTk(fg_color=BG)
        self.root.withdraw()
        self.root.title("RecLight")
        self.root.geometry("500x800")
        self.root.minsize(460, 720)

        ico = write_ico()
        if ico and os.name == "nt":
            try:
                self.root.iconbitmap(ico)
                self.root.after(300, lambda: self.root.iconbitmap(ico))
            except Exception:  # noqa: BLE001
                pass

        self._build_header()
        self.tabs = ctk.CTkTabview(
            self.root, fg_color="transparent", corner_radius=14,
            segmented_button_fg_color=CARD, segmented_button_selected_color=ACCENT,
            segmented_button_selected_hover_color=ACCENT_HOVER,
            segmented_button_unselected_color=CARD, segmented_button_unselected_hover_color=CARD2,
            text_color=TEXT,
        )
        self.tabs._segmented_button.configure(font=font(13, "bold"))
        self.tabs.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        for name in ("Přehled", "Nastavení", "DAW", "Log"):
            self.tabs.add(name)
        self._build_overview(self.tabs.tab("Přehled"))
        self._build_settings(self.tabs.tab("Nastavení"))
        self._build_daw(self.tabs.tab("DAW"))
        self._build_log(self.tabs.tab("Log"))

        if tray:
            self._start_tray()
        self.root.protocol("WM_DELETE_WINDOW", self.hide if self.tray_ok else self.quit)
        if not start_hidden or not self.tray_ok:
            self.show()
        rl.watch_show_requests(lambda: self.q.put("show"))
        if sys.platform == "darwin":
            try:   # klik na ikonu v Docku znovu otevře skryté okno
                self.root.createcommand("tk::mac::ReopenApplication", self.show)
            except Exception:  # noqa: BLE001
                pass
        self._tick()

    # ------------------------------------------------------------ okno
    def show(self):
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.attributes("-topmost", True)
            self.root.after(250, lambda: self.root.attributes("-topmost", False))
            self.root.focus_force()
        except Exception:  # noqa: BLE001
            pass

    def hide(self):
        self.root.withdraw()

    def quit(self):
        self.core.stop.set()
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:  # noqa: BLE001
                pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()

    # ------------------------------------------------------------ hlavička
    def _build_header(self):
        bar = ctk.CTkFrame(self.root, fg_color="transparent")
        bar.pack(fill="x", padx=22, pady=(20, 10))
        self.logo_img = ctk.CTkImage(light_image=make_logo(112), dark_image=make_logo(112), size=(52, 52))
        ctk.CTkLabel(bar, image=self.logo_img, text="").pack(side="left")
        titles = ctk.CTkFrame(bar, fg_color="transparent")
        titles.pack(side="left", padx=14)
        ctk.CTkLabel(titles, text="RecLight", font=font(24, "bold"), text_color=TEXT, anchor="w").pack(anchor="w")
        ctk.CTkLabel(titles, text="Indikátor nahrávání pro Studio One a Cubase", font=font(12), text_color=MUTED,
                     anchor="w").pack(anchor="w")
        ctk.CTkLabel(bar, text=f"v{rl.APP_VERSION}", font=font(11), text_color=MUTED).pack(side="right", anchor="n")

    # ------------------------------------------------------------ přehled
    def _build_overview(self, tab):
        self.hero = ctk.CTkFrame(tab, corner_radius=20, fg_color=CARD)
        self.hero.pack(fill="x", pady=(6, 10))
        self.hero_canvas = ctk.CTkCanvas(self.hero, width=96, height=96, highlightthickness=0, bg=CARD)
        self.hero_canvas.pack(pady=(14, 0))
        self.hero_title = ctk.CTkLabel(self.hero, text="", font=font(26, "bold"), text_color=TEXT)
        self.hero_title.pack()
        self.hero_sub = ctk.CTkLabel(self.hero, text="", font=font(14), text_color=MUTED)
        self.hero_sub.pack(pady=(2, 16))
        self.hero_note = ctk.CTkLabel(self.hero, text="", font=font(12), text_color="#ffd9a0")

        status = ctk.CTkFrame(tab, corner_radius=14, fg_color=CARD)
        status.pack(fill="x", pady=(0, 10))
        self.midi_row = self._status_row(status, "MIDI vstup", pad=(10, 3))
        ctk.CTkFrame(status, height=1, fg_color=BORDER).pack(fill="x", padx=14)
        self.esp_row = self._status_row(status, "ESP světlo", pad=(3, 10))

        stats = ctk.CTkFrame(tab, fg_color="transparent")
        stats.pack(fill="x", pady=(0, 10))
        stats.grid_columnconfigure((0, 1), weight=1, uniform="s")
        self.stat_total = self._stat_card(stats, 0, "Dnes nahráno")
        self.stat_count = self._stat_card(stats, 1, "Nahrávání dnes")

        recent = ctk.CTkFrame(tab, corner_radius=14, fg_color=CARD)
        recent.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(recent, text="Poslední nahrávání", font=font(12, "bold"), text_color=MUTED,
                     anchor="w").pack(fill="x", padx=16, pady=(10, 2))
        self.recent_box = ctk.CTkFrame(recent, fg_color="transparent")
        self.recent_box.pack(fill="x", padx=16, pady=(0, 10))

        self.test_btn = ctk.CTkButton(tab, text="Test světla (3 s)", height=42, corner_radius=12, font=font(14, "bold"),
                                      fg_color=CARD2, hover_color=BORDER, text_color=TEXT,
                                      command=lambda: self.core.start_test(3.0))
        self.test_btn.pack(fill="x")

    def _status_row(self, parent, name, pad):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=pad)
        dot = ctk.CTkCanvas(row, width=16, height=16, highlightthickness=0, bg=CARD)
        dot.pack(side="left", padx=(0, 10))
        left = ctk.CTkFrame(row, fg_color="transparent")
        left.pack(side="left")
        ctk.CTkLabel(left, text=name, font=font(14, "bold"), text_color=TEXT, anchor="w").pack(anchor="w")
        value = ctk.CTkLabel(left, text="", font=font(12), text_color=MUTED, anchor="w")
        value.pack(anchor="w")
        badge = ctk.CTkLabel(row, text="", font=font(12, "bold"), corner_radius=8, width=96, height=26)
        badge.pack(side="right")
        return {"dot": dot, "value": value, "badge": badge}

    def _stat_card(self, parent, col, caption):
        card = ctk.CTkFrame(parent, corner_radius=14, fg_color=CARD)
        card.grid(row=0, column=col, sticky="nsew", padx=(0, 5) if col == 0 else (5, 0))
        ctk.CTkLabel(card, text=caption, font=font(12), text_color=MUTED, anchor="w").pack(fill="x", padx=16,
                                                                                         pady=(10, 0))
        value = ctk.CTkLabel(card, text="", font=font(22, "bold"), text_color=TEXT, anchor="w")
        value.pack(fill="x", padx=16, pady=(0, 10))
        return value

    # ------------------------------------------------------------ nastavení
    def _section(self, parent, title, hint=None):
        card = ctk.CTkFrame(parent, corner_radius=14, fg_color=CARD)
        card.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(card, text=title, font=font(14, "bold"), text_color=TEXT, anchor="w").pack(
            fill="x", padx=16, pady=(14, 0))
        if hint:
            ctk.CTkLabel(card, text=hint, font=font(12), text_color=MUTED, anchor="w", justify="left",
                         wraplength=400).pack(fill="x", padx=16, pady=(2, 0))
        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="x", padx=16, pady=(8, 14))
        return body

    def _field(self, parent, label, hint=None):
        ctk.CTkLabel(parent, text=label, font=font(12, "bold"), text_color=MUTED, anchor="w").pack(
            fill="x", pady=(6, 2))
        holder = ctk.CTkFrame(parent, fg_color="transparent")
        holder.pack(fill="x")
        if hint:
            ctk.CTkLabel(parent, text=hint, font=font(11), text_color=MUTED, anchor="w", justify="left",
                         wraplength=400).pack(fill="x", pady=(2, 0))
        return holder

    def _build_settings(self, tab):
        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.pack(side="bottom", fill="x", pady=(8, 0))
        self.save_btn = ctk.CTkButton(bar, text="Uložit a použít", height=42, corner_radius=12,
                                      font=font(14, "bold"), fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                      command=self.save_settings)
        self.save_btn.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ctk.CTkButton(bar, text="Zrušit změny", height=42, corner_radius=12, font=font(14), fg_color=CARD2,
                      hover_color=BORDER, text_color=TEXT, width=120, command=self.load_settings).pack(side="left")
        self.flash_label = ctk.CTkLabel(tab, text="", font=font(12, "bold"), text_color=GREEN, height=18)
        self.flash_label.pack(side="bottom", pady=(6, 0))

        scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent", corner_radius=0)
        scroll.pack(fill="both", expand=True)

        midi = self._section(scroll, "DAW (MIDI)",
                             "Studio One i Cubase posílají stav nahrávání přes Mackie Control do virtuálního portu RecLight. "
                             "Postup nastavení je v záložce DAW.")
        holder = self._field(midi, "MIDI port (část názvu)")
        self.midi_combo = ctk.CTkComboBox(holder, values=[], height=36, font=font(13), dropdown_font=font(13),
                                          fg_color=CARD2, border_color=BORDER, button_color=BORDER,
                                          button_hover_color=GRAY, dropdown_fg_color=CARD2, text_color=TEXT)
        self.midi_combo.pack(side="left", fill="x", expand=True)
        self._refresh_btn(holder)
        row = ctk.CTkFrame(midi, fg_color="transparent")
        row.pack(fill="x")
        row.grid_columnconfigure((0, 1), weight=1, uniform="m")
        left = ctk.CTkFrame(row, fg_color="transparent")
        left.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkLabel(left, text="Tlačítko Record (nota)", font=font(12, "bold"), text_color=MUTED,
                     anchor="w").pack(fill="x", pady=(6, 2))
        self.note_entry = ctk.CTkEntry(left, height=36, font=font(13), fg_color=CARD2, border_color=BORDER)
        self.note_entry.pack(fill="x")
        right = ctk.CTkFrame(row, fg_color="transparent")
        right.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        ctk.CTkLabel(right, text="MIDI kanál", font=font(12, "bold"), text_color=MUTED, anchor="w").pack(
            fill="x", pady=(6, 2))
        self.channel_menu = ctk.CTkOptionMenu(right, values=[ANY_CHANNEL] + [str(i) for i in range(1, 17)],
                                              height=36, font=font(13), dropdown_font=font(13), fg_color=CARD2,
                                              button_color=CARD2, button_hover_color=BORDER,
                                              dropdown_fg_color=CARD2, text_color=TEXT)
        self.channel_menu.pack(fill="x")
        self.vport_var = ctk.BooleanVar(value=True)
        ctk.CTkSwitch(midi, text="Vytvořit port RecLight sám, když chybí", variable=self.vport_var,
                      font=font(12), progress_color=GREEN).pack(anchor="w", pady=(10, 0))
        ctk.CTkLabel(midi, text="Mackie Control: Record = nota 95. Pokud nahrávání nereaguje, zkontrolujte "
                                "zprávy v záložce Log.", font=font(11), text_color=MUTED, anchor="w",
                     justify="left", wraplength=400).pack(fill="x", pady=(8, 0))

        light = self._section(scroll, "Světlo", "Jak dlouho má světlo svítit po zastavení nahrávání.")
        holder = self._field(light, "Zhasnout po ukončení nahrávání")
        self.delay_label = ctk.CTkLabel(holder, text="5 s", font=font(14, "bold"), text_color=TEXT, width=64,
                                        anchor="e")
        self.delay_label.pack(side="right")
        self.delay_slider = ctk.CTkSlider(holder, from_=0, to=30, number_of_steps=30, height=18,
                                          progress_color=ACCENT, button_color=ACCENT,
                                          button_hover_color=ACCENT_HOVER, fg_color=CARD2,
                                          command=self._delay_changed)
        self.delay_slider.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(light, text="0 s = zhasne okamžitě. Dobu lze změnit i v menu ikony v liště.", font=font(11),
                     text_color=MUTED, anchor="w", justify="left", wraplength=400).pack(fill="x", pady=(2, 0))

        esp = self._section(scroll, "ESP světlo (USB)",
                            "Zařízení se hledá podle VID:PID, takže funguje i po změně čísla COM portu.")
        holder = self._field(esp, "Zařízení")
        self.esp_menu = ctk.CTkOptionMenu(holder, values=[AUTO_LABEL], height=36, font=font(13),
                                          dropdown_font=font(13), fg_color=CARD2, button_color=CARD2,
                                          button_hover_color=BORDER, dropdown_fg_color=CARD2, text_color=TEXT,
                                          dynamic_resizing=False)
        self.esp_menu.pack(side="left", fill="x", expand=True)
        self._refresh_btn(holder)
        holder = self._field(esp, "Rychlost (baud)")
        self.baud_entry = ctk.CTkEntry(holder, height=36, font=font(13), fg_color=CARD2, border_color=BORDER,
                                       width=140)
        self.baud_entry.pack(side="left")

        app = self._section(scroll, "Aplikace")
        self.autostart_var = ctk.BooleanVar(value=False)
        sw = ctk.CTkSwitch(app, text="Spustit se systémem", variable=self.autostart_var, font=font(13),
                           progress_color=GREEN, command=self.toggle_autostart)
        sw.pack(anchor="w", pady=(4, 2))
        ctk.CTkLabel(app, text="Zavřením okna se RecLight skryje do lišty u hodin. Ukončíte ho v menu ikony.",
                     font=font(11), text_color=MUTED, anchor="w", justify="left", wraplength=400).pack(
            fill="x", pady=(6, 0))
        btns = ctk.CTkFrame(app, fg_color="transparent")
        btns.pack(fill="x", pady=(10, 0))
        ctk.CTkButton(btns, text="Otevřít složku s nastavením", height=34, corner_radius=10, font=font(12),
                      fg_color=CARD2, hover_color=BORDER, text_color=TEXT,
                      command=lambda: self.open_path(rl.config_dir())).pack(side="left")

        self.load_settings()

    def _delay_changed(self, value):
        self.delay_label.configure(text="ihned" if int(value) == 0 else f"{int(value)} s")

    def _refresh_btn(self, parent):
        ctk.CTkButton(parent, text="↻", width=40, height=36, corner_radius=8, font=font(16), fg_color=CARD2,
                      hover_color=BORDER, text_color=TEXT, command=self.refresh_ports).pack(side="left", padx=(8, 0))

    def refresh_ports(self):
        current_midi = self.midi_combo.get()
        ports = list_midi_inputs()
        self.midi_combo.configure(values=ports or ["(žádný MIDI port nenalezen)"])
        self.midi_combo.set(current_midi)
        self._fill_esp_menu(keep=self.esp_menu.get() if self.esp_devices else None)

    def _fill_esp_menu(self, keep=None):
        cfg = self.core.cfg
        self.esp_devices = list_serial_devices()
        labels = [AUTO_LABEL] + [d[0] for d in self.esp_devices]
        vid, pid = rl.to_int(cfg.get("esp_vid")), rl.to_int(cfg.get("esp_pid"))
        selected = AUTO_LABEL
        if keep and keep in labels:
            selected = keep
        elif vid is not None:
            for label, _dev, dvid, dpid in self.esp_devices:
                if dvid == vid and (pid is None or dpid == pid):
                    selected = label
                    break
            else:
                selected = f"Uložené zařízení {vid:04X}:{(pid or 0):04X} (nepřipojeno)"
                labels.append(selected)
        self.esp_menu.configure(values=labels)
        self.esp_menu.set(selected)

    def load_settings(self):
        cfg = self.core.cfg
        self.midi_combo.set(str(cfg.get("midi_port_contains", "")))
        self.note_entry.delete(0, "end")
        self.note_entry.insert(0, str(cfg.get("record_note", 95)))
        ch = cfg.get("record_channel")
        self.channel_menu.set(ANY_CHANNEL if ch is None else str(rl.to_int(ch) + 1))
        self.baud_entry.delete(0, "end")
        self.baud_entry.insert(0, str(cfg.get("baud", 115200)))
        self.vport_var.set(bool(cfg.get("create_virtual_port", True)))
        delay = int(round(self.core.off_delay()))
        self.delay_slider.set(min(30, delay))
        self._delay_changed(min(30, delay))
        self.autostart_var.set(rl.autostart_enabled())
        self.refresh_ports()
        self.flash("")

    def flash(self, text, error=False):
        self.flash_label.configure(text=text, text_color=RED if error else GREEN)
        if text and not error:
            self.root.after(3000, lambda: self.flash_label.configure(text=""))

    def save_settings(self):
        try:
            note = int(self.note_entry.get().strip(), 0)
            if not 0 <= note <= 127:
                raise ValueError
        except ValueError:
            self.flash("Nota musí být číslo 0 až 127", error=True)
            return
        try:
            baud = int(self.baud_entry.get().strip())
            if baud < 1200:
                raise ValueError
        except ValueError:
            self.flash("Rychlost musí být číslo, např. 115200", error=True)
            return
        midi = self.midi_combo.get().strip()
        if not midi or midi.startswith("("):
            midi = "RecLight"
        ch_text = self.channel_menu.get()
        new = {
            "midi_port_contains": midi,
            "record_note": note,
            "record_channel": None if ch_text == ANY_CHANNEL else int(ch_text) - 1,
            "baud": baud,
            "create_virtual_port": bool(self.vport_var.get()),
            "off_delay": int(self.delay_slider.get()),
            "esp_port": "auto",
        }
        label = self.esp_menu.get()
        if label == AUTO_LABEL:
            new.update(esp_vid=None, esp_pid=None)
        else:
            for dlabel, device, vid, pid in self.esp_devices:
                if dlabel == label:
                    new.update(esp_vid=f"0x{vid:04X}", esp_pid=f"0x{pid:04X}", esp_port_fallback=device)
                    break
            # uložené, ale nepřipojené zařízení se nemění
        try:
            merged = dict(self.core.cfg)
            merged.update(new)
            rl.save_config(merged)
        except OSError as exc:
            self.flash(f"Uložení se nepovedlo: {exc}", error=True)
            return
        self.core.apply_config(new)
        self.flash("Uloženo a použito")

    def toggle_autostart(self):
        try:
            rl.set_autostart(self.autostart_var.get())
        except Exception:  # noqa: BLE001
            logging.exception("Autostart se nepodařilo změnit")
            self.autostart_var.set(rl.autostart_enabled())
            self.flash("Autostart se nepodařilo změnit", error=True)


    # ------------------------------------------------------------ DAW (návod)
    def _build_daw(self, tab):
        scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent", corner_radius=0)
        scroll.pack(fill="both", expand=True)

        status = self._section(scroll, "Stav spojení s DAW",
                               "Po nastavení DAW (jednorázově) se tu objeví, že z něj chodí MIDI zprávy.")
        self.daw_status = ctk.CTkLabel(status, text="", font=font(13, "bold"), text_color=MUTED, anchor="w",
                                       justify="left", wraplength=400)
        self.daw_status.pack(fill="x")
        row = ctk.CTkFrame(status, fg_color="transparent")
        row.pack(fill="x", pady=(8, 0))
        ctk.CTkLabel(row, text="Název portu:", font=font(12), text_color=MUTED).pack(side="left")
        ctk.CTkLabel(row, text=str(self.core.cfg.get("midi_port_contains") or "RecLight"), font=font(13, "bold"),
                     text_color=TEXT).pack(side="left", padx=(6, 0))

        one = self._section(scroll, "Jednorázové nastavení (platí pro všechny projekty)",
                            "RecLight je při běhu vidět jako MIDI port RecLight. DAW se nastaví jen jednou, "
                            "dál už nic. RecLight se spouští se systémem, takže port je k dispozici vždy.")
        steps = [
            ("Studio One",
             "1. Studio One > Možnosti > Externí zařízení > Přidat.\n"
             "2. Vyberte Mackie > Mackie Control.\n"
             "3. Send To (Odeslat do) = RecLight. Receive From (Přijímat z) = RecLight (nebo nic).\n"
             "4. Potvrďte OK. Hotovo, nastavení zůstává pro všechny skladby."),
            ("Cubase / Nuendo",
             "1. Studio > Nastavení studia (Studio Setup) > Přidat zařízení (+) > Mackie Control.\n"
             "2. MIDI Input = RecLight, MIDI Output = RecLight.\n"
             "3. Potvrďte OK. Nastavení je uloženo v předvolbách Cubase, platí pro všechny projekty.\n"
             "4. Pokud nahrávání nereaguje, zkontrolujte, že je Mackie Control zapnuto (zelená tečka)."),
        ]
        for title, text in steps:
            ctk.CTkLabel(one, text=title, font=font(13, "bold"), text_color=TEXT, anchor="w").pack(
                fill="x", pady=(8, 0))
            ctk.CTkLabel(one, text=text, font=font(12), text_color=MUTED, anchor="w", justify="left",
                         wraplength=400).pack(fill="x", pady=(2, 0))

        tips = self._section(scroll, "Když to nefunguje")
        ctk.CTkLabel(tips, text="- Pokud se na stavu výše nic neobjeví, DAW neposílá do správného portu.\n"
                                "- Záložka Log ukáže přijaté MIDI zprávy po zapnutí ladění (debug_midi v settings.json).\n"
                                "- Port RecLight existuje jen při běhu RecLight. Spusťte nejdřív RecLight, pak DAW.\n"
                                "- Jiné DAW (Reaper, Logic, Ableton) fungují stejně, pokud umí Mackie Control.",
                     font=font(12), text_color=MUTED, anchor="w", justify="left", wraplength=400).pack(fill="x")

    def _update_daw_status(self):
        t = self.core.last_midi_at
        if not self.core.midi_ok:
            text, color = "MIDI port RecLight zatím není připraven.", AMBER
        elif t is None:
            text, color = "Port RecLight je připraven. Z DAW zatím nepřišlo nic. Nastavte Mackie Control podle návodu níže.", MUTED
        else:
            ago = int(time.time() - t)
            text, color = f"Z DAW chodí MIDI (naposledy před {ago} s). Nastavení je v pořádku.", GREEN
        self.daw_status.configure(text=text, text_color=color)

    # ------------------------------------------------------------ log
    def _build_log(self, tab):
        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.pack(side="bottom", fill="x", pady=(8, 0))
        ctk.CTkButton(bar, text="Obnovit", height=36, corner_radius=10, font=font(13), fg_color=CARD2,
                      hover_color=BORDER, text_color=TEXT, command=self.refresh_log).pack(side="left")
        ctk.CTkButton(bar, text="Otevřít soubor s logem", height=36, corner_radius=10, font=font(13),
                      fg_color=CARD2, hover_color=BORDER, text_color=TEXT,
                      command=lambda: self.open_path(rl.log_path())).pack(side="left", padx=8)
        self.log_box = ctk.CTkTextbox(tab, font=ctk.CTkFont(family="Consolas" if os.name == "nt" else "monospace",
                                                            size=11),
                                      fg_color=CARD, text_color=TEXT, corner_radius=12, wrap="none")
        self.log_box.pack(fill="both", expand=True)
        self.log_box.configure(state="disabled")
        self.refresh_log()

    def refresh_log(self):
        try:
            lines = rl.log_path().read_text(encoding="utf-8", errors="replace").splitlines()[-150:]
        except OSError:
            lines = ["(log zatím neexistuje)"]
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.insert("end", "\n".join(lines) if any(lines) else "(log je zatím prázdný)")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    @staticmethod
    def open_path(path):
        try:
            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception:  # noqa: BLE001
            logging.exception("Cestu se nepodařilo otevřít")

    # ------------------------------------------------------------ tray
    def _start_tray(self):
        try:
            import pystray
            from pystray import Menu, MenuItem
        except Exception:  # noqa: BLE001
            logging.warning("pystray není k dispozici, ikona v liště se nezobrazí")
            return

        def toggle_autostart(icon, item):
            try:
                rl.set_autostart(not rl.autostart_enabled())
                self.q.put("autostart")
            except Exception:  # noqa: BLE001
                logging.exception("Autostart se nepodařilo změnit")

        def delay_item(seconds):
            label = "Ihned" if seconds == 0 else f"{seconds} s"

            def choose(icon, item):
                self.core.set_option("off_delay", seconds)
                self.q.put("delay")

            def is_current(item):
                return int(round(self.core.off_delay())) == seconds

            # pystray povoluje u akcí jen 0 až 2 parametry, proto bez výchozích hodnot (seconds je z uzávěru)
            return MenuItem(label, choose, radio=True, checked=is_current)

        delay_menu = Menu(*[delay_item(v) for v in (0, 2, 5, 10, 30)])

        menu = Menu(
            MenuItem("Otevřít RecLight", lambda icon, item: self.q.put("show"), default=True),
            Menu.SEPARATOR,
            MenuItem(lambda item: self.core.status_text(), None, enabled=False),
            Menu.SEPARATOR,
            MenuItem("Test světla (3 s)", lambda icon, item: self.core.start_test(3.0)),
            MenuItem(lambda item: "Zhasnout po: " + ("ihned" if self.core.off_delay() == 0
                                                       else f"{int(round(self.core.off_delay()))} s"), delay_menu),
            MenuItem("Spustit se systémem", toggle_autostart, checked=lambda item: rl.autostart_enabled()),
            Menu.SEPARATOR,
            MenuItem("Konec", lambda icon, item: self.q.put("quit")),
        )
        self.tray = pystray.Icon(rl.APP_NAME, make_tray_icon(self.core.icon_state()), rl.APP_NAME, menu)
        self.tray_state = self.core.icon_state()
        self.tray.run_detached()
        self.tray_ok = True

    def _update_tray(self):
        if self.tray is None:
            return
        state = self.core.icon_state()
        if state != self.tray_state:
            self.tray_state = state
            self.tray.icon = make_tray_icon(state)
        self.tray.title = f"{rl.APP_NAME}: {self.core.status_text()}"
        if self.tick_n % 4 == 0:
            try:
                self.tray.update_menu()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ obnovování
    def _tick(self):
        self.tick_n += 1
        try:
            while True:
                cmd = self.q.get_nowait()
                if cmd == "show":
                    self.show()
                elif cmd == "quit":
                    self.quit()
                    return
                elif cmd == "autostart":
                    self.autostart_var.set(rl.autostart_enabled())
                elif cmd == "delay":
                    d = min(30, int(round(self.core.off_delay())))
                    self.delay_slider.set(d)
                    self._delay_changed(d)
        except queue.Empty:
            pass
        try:
            self.refresh_view()
            if self.tick_n % 4 == 0 and self.tabs.get() == "DAW":
                self._update_daw_status()
            self._update_tray()
            if self.tabs.get() == "Log" and self.tick_n % 8 == 0:
                self.refresh_log()
        except Exception:  # noqa: BLE001
            logging.exception("Chyba při obnovení okna")
        self.root.after(250, self._tick)

    def _hero_state(self):
        c = self.core
        if c.recording:
            return "rec"
        if not (c.midi_ok and c.esp_ok):
            return "warn"
        if c.holding():
            return "hold"
        if c.testing():
            return "test"
        return "idle"

    def refresh_view(self):
        c = self.core
        state = self._hero_state()
        pulse = (self.tick_n // 2) % 2 == 0

        if state == "rec":
            bg, title, tcol, sub, scol = RED_BG, "NAHRÁVÁM", "#ffffff", f"{c.daw_label()} · {fmt_hms(c.elapsed())}", "#ffe3e2"
            note = "" if c.esp_ok else "ESP není připojeno, světlo nesvítí"
        elif state == "test":
            bg, title, tcol, sub, scol = RED_BG, "TEST SVĚTLA", "#ffffff", "Světlo by mělo svítit", "#ffe3e2"
            note = ""
        elif state == "hold":
            bg, title, tcol = CARD, "Dosvícení", "#F08A89"
            sub, scol = f"Světlo zhasne za {c.hold_left()} s", MUTED
            note = ""
        elif state == "warn":
            missing = []
            if not c.midi_ok:
                missing.append("MIDI port")
            if not c.esp_ok:
                missing.append("ESP")
            bg, title, tcol = AMBER_BG, "Chybí připojení", AMBER
            sub, scol = "Hledám: " + " a ".join(missing), "#d9c28a"
            note = ""
        else:
            bg, title, tcol, sub, scol = CARD, "Připraveno", TEXT, "Čeká na nahrávání v DAW", MUTED
            note = ""
        if state != self.hero_state:
            self.hero_state = state
            for w in (self.hero,):
                w.configure(fg_color=bg)
            self.hero_canvas.configure(bg=bg)
            self.hero_title.configure(text_color=tcol)
            self.hero_sub.configure(text_color=scol)
        self.hero_title.configure(text=title)
        self.hero_sub.configure(text=sub)
        if note:
            self.hero_note.configure(text=note)
            if not self.hero_note.winfo_ismapped():
                self.hero_note.pack(pady=(0, 12))
        elif self.hero_note.winfo_ismapped():
            self.hero_note.pack_forget()
        self._draw_hero(state, pulse)

        self._set_row(self.midi_row, c.midi_ok, c.midi_name, "Hledám port RecLight…")
        self._set_row(self.esp_row, c.esp_ok, c.esp_device and f"Port {c.esp_device}", "Zařízení nenalezeno")

        today = c.today_sessions()
        self.stat_total.configure(text=fmt_total(c.today_seconds()))
        self.stat_count.configure(text=str(len(today)))
        self._draw_recent(c)

    def _draw_hero(self, state, pulse):
        cv = self.hero_canvas
        cv.delete("all")
        cx = cy = 48
        if state in ("rec", "test"):
            r = 40 if pulse else 36
            cv.create_oval(cx - r, cy - r, cx + r, cy + r, outline="#ffb3b1", width=3)
            cv.create_oval(cx - 24, cy - 24, cx + 24, cy + 24, fill="#ffffff", outline="")
        elif state == "hold":
            cv.create_oval(cx - 36, cy - 36, cx + 36, cy + 36, outline="#8a3a39", width=3)
            r = 20 if pulse else 16
            cv.create_oval(cx - r, cy - r, cx + r, cy + r, fill=RED, outline="")
        elif state == "warn":
            cv.create_oval(cx - 36, cy - 36, cx + 36, cy + 36, outline=AMBER, width=3)
            cv.create_rectangle(cx - 4, cy - 20, cx + 4, cy + 6, fill=AMBER, outline="")
            cv.create_oval(cx - 5, cy + 13, cx + 5, cy + 23, fill=AMBER, outline="")
        else:
            cv.create_oval(cx - 36, cy - 36, cx + 36, cy + 36, outline=GRAY, width=3)
            cv.create_oval(cx - 18, cy - 18, cx + 18, cy + 18, fill=GRAY, outline="")

    def _set_row(self, row, ok, detail, missing_text):
        dot = row["dot"]
        dot.delete("all")
        dot.create_oval(2, 2, 14, 14, fill=GREEN if ok else AMBER, outline="")
        row["value"].configure(text=detail if ok and detail else missing_text)
        if ok:
            row["badge"].configure(text="Připojeno", text_color="#7fe0bf", fg_color="#16392e")
        else:
            row["badge"].configure(text="Nepřipojeno", text_color="#f0c070", fg_color="#3b2f14")

    def _draw_recent(self, core):
        recent = list(reversed(core.today_sessions()))[:3]
        sig = tuple((s["start"], s["seconds"] if not core.recording else core.elapsed()) for s in recent)
        if sig == self.sessions_sig:
            return
        self.sessions_sig = sig
        for w in self.recent_box.winfo_children():
            w.destroy()
        if not recent:
            ctk.CTkLabel(self.recent_box, text="Dnes zatím žádné. Začněte nahrávat v DAW.", font=font(12),
                         text_color=MUTED, anchor="w").pack(fill="x")
            return
        for s in recent:
            row = ctk.CTkFrame(self.recent_box, fg_color="transparent")
            row.pack(fill="x", pady=1)
            try:
                when = datetime.fromisoformat(s["start"]).strftime("%H:%M")
            except ValueError:
                when = "--:--"
            ctk.CTkLabel(row, text=when, font=font(13), text_color=TEXT).pack(side="left")
            ctk.CTkLabel(row, text=fmt_hms(s["seconds"]), font=font(13), text_color=MUTED).pack(side="right")

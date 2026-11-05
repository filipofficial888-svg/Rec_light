# RecLight 2.0 (Windows a macOS)

Aplikace s oknem a ikonou v liště. Hlídá, jestli v DAW (Studio One, Cubase) běží nahrávání, a posílá stav po USB do ESP32, které rozsvítí světlo.

## Instalace
**Windows**: spusťte `RecLight-2.0-Windows-Setup.exe`. Instalátor (bez práv správce):
- nainstaluje RecLight, zástupce v nabídce Start a složku s firmwarem,
- nabídne spuštění po přihlášení do Windows (doporučeno),
- nainstaluje loopMIDI, tedy ovladač virtuálního MIDI portu (přes winget; Windows požádá o souhlas správce),
- po dokončení spustí RecLight. Port `RecLight` si aplikace vytvoří sama, v loopMIDI se nic nenastavuje.

**macOS**: otevřete `RecLight-2.0-macOS-<arm64|x86_64>.dmg` a přetáhněte RecLight do Aplikací (arm64 = Apple Silicon, x86_64 = Intel). Nic dalšího se neinstaluje, virtuální MIDI port vytvoří CoreMIDI. Aplikace není podepsaná u Applu: při prvním spuštění pravé tlačítko > Otevřít. Zapněte v Nastavení "Spustit se systémem".

## Nastavení DAW (jednorázově, jeden společný port RecLight)
RecLight vytváří MIDI port `RecLight`. Každé DAW se nastaví jednou a pak už nic. Stav spojení je v záložce **DAW**.
- **Studio One**: Možnosti > Externí zařízení > Přidat > Mackie > Mackie Control, Send To = `RecLight`.
- **Cubase / Nuendo**: Studio > Nastavení studia > + > Mackie Control, MIDI Input a Output = `RecLight`.
- Ostatní DAW s podporou Mackie Control (Reaper, Logic, Ableton, ...) fungují stejně: tlačítko Record je MIDI nota 95.

## Okno aplikace
- **Přehled**: stav nahrávání (červená = nahrává), MIDI a ESP, dnešní statistika, Test světla.
- **Nastavení**: MIDI port, nota Record, kanál, výběr ESP zařízení (podle VID:PID), baud, zhasnutí po nahrávání, spuštění se systémem.
- **DAW**: návod pro Studio One a Cubase, stav příchozího MIDI.
- **Log**: posledních 150 řádků.
Zavřením okna se aplikace skryje do lišty (Windows) / do menu (macOS). Ukončení: ikona > Konec.

## Sestavení instalátorů
- **Windows**: dvakrát klikněte na `build_installer.bat` (doinstaluje Python a Inno Setup přes winget) > `Output\RecLight-2.0-Windows-Setup.exe`.
- **macOS**: na Macu `bash build_mac.sh` > `dist/RecLight-2.0-macOS-<arch>.dmg`.
- **Obojí najednou**: GitHub Actions (`.github/workflows/release.yml`) sestaví Windows `.exe`, oba `.dmg` a zip s firmwarem; po push značky `v2.0` je vystaví v Releases.

## ESP světlo
- Jednoduše: `firmware/reclight_esp32/reclight_esp32.ino` (GPIO2 = červená při nahrávání).
- S bezdrátovou ceduli: panel (`reclight_panel` pro ESP32 DevKit, `reclight_panel_c3` pro ESP32-C3) a cedule (`reclight_sign`). Viz níže.

## Soubory
- Windows: nastavení `%APPDATA%\RecLight\settings.json`, log `reclight.log`. macOS: `~/Library/Application Support/RecLight/`.
- Ladění MIDI: `py -3 reclight.py --monitor`, seznam portů: `py -3 reclight.py --list`.

## Panel a bezdrátová cedule (volitelné)
- `firmware/reclight_panel/` je ovladač u počítače (ESP32, OLED 0,96", enkodér). Z PC dostává `REC`/`IDLE` přes USB, ukazuje stav a přes ESP-NOW posílá cedulce barvu, jas a efekt. Přes menu na displeji se nastavuje zpoždění, barvy, efekt, jas, test a další. Potřebuje knihovnu U8g2.
- `firmware/reclight_sign/` je firmware cedule (ESP32-C3, LED panel, baterie). Přijímá barvu z panelu a odpovídá stavem (napětí baterie).
- Oba skeče musí mít stejný soubor `reclight_proto.h` (kanál a ID sítě). Zapojení a ovládání jsou v hlavičkách souborů.
- `firmware/reclight_esp32/` je původní jednoduchá verze (světlo přímo na ESP32 u PC).

### Panel na ESP32-C3 (malá verze)
`firmware/reclight_panel_c3/reclight_panel_c3.ino` je stejný panel pro desku ESP32-C3 (původní `reclight_panel` pro ESP32 DevKit zůstává).
Deska "ESP32C3 Dev Module", USB CDC On Boot = Enabled. Zapojení: OLED SDA GPIO7, SCL GPIO6; enkodér CLK GPIO21, DT GPIO20, SW GPIO10, + na GPIO8 (drženo HIGH), GND.
PC vidí C3 jako USB zařízení Espressif (VID 303A), v aplikaci RecLight se vybere nové zařízení.

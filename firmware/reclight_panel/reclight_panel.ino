// RecLight PANEL - ovladač u počítače (ESP32 + OLED 0.96" SSD1306 + enkodér).
// Přijímá stav z PC přes USB ("REC" / "IDLE"), ukazuje ho na displeji a přes ESP-NOW posílá
// bezdrátové cedulce (ESP32-C3, skeč reclight_sign) barvu, jas a efekt. Přes menu se nastavuje vše bez aplikace.
//
// KNIHOVNA: v Arduino IDE (Správce knihoven) nainstalujte "U8g2" (autor olikraus). ESP-NOW je součástí jádra ESP32.
// DESKA: "ESP32 Dev Module". Při nahrávání může být nutné podržet BOOT.
// SOUBORY: reclight_proto.h musí ležet ve stejné složce a být shodný se skečem cedule.
//
// ZAPOJENÍ
//   OLED:    SDA -> GPIO21, SCL -> GPIO22, VCC -> 3V3, GND -> GND   (adresa 0x3C)
//   Enkodér: +   -> 3V3 (NE 5 V), CLK -> GPIO18, DT -> GPIO19, SW -> GPIO23, GND -> GND
//   Žádné LED na panelu: světlo je v cedulce.
//
// OVLÁDÁNÍ
//   Stavová obrazovka:  stisk = otevřít menu, dlouhý stisk = rychlý test (cedule natvrdo svítí nahrávání / zpět na Auto)
//   Menu:               otáčení = pohyb, stisk = vybrat / upravit, dlouhý stisk = zpět na stav
//   Úprava hodnoty:     otáčení = změna, stisk = uložit, dlouhý stisk = zrušit (vrátí původní hodnotu)
//
// PROTOKOL (USB z PC): řádky "REC" a "IDLE" (RecLight je posílá každou sekundu).
//   Když z PC nic nepřijde po dobu "Timeout PC", stav je "BEZ PC".
//
// POZOR: zpoždění zhasnutí se přičítá ke zpoždění v aplikaci RecLight. Chcete-li používat jen zpoždění
//   z panelu, nastavte v aplikaci zpoždění na 0.

#include <Arduino.h>
#include <U8g2lib.h>
#include <Preferences.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include "reclight_proto.h"

#ifndef ESP_ARDUINO_VERSION_MAJOR
#define ESP_ARDUINO_VERSION_MAJOR 2
#endif

#define FW_VERSION "3.2"
#define RL_DEBUG 1   // 1 = jednou za sekundu výpis stavu ESP-NOW na USB sériovou linku (Serial Monitor 115200)

// ---------------- piny a základní volby ----------------
#define PIN_ENC_CLK  18
#define PIN_ENC_DT   19
#define PIN_ENC_SW   23

#define ENC_STEPS_PER_CLICK 4     // 4 pro většinu modulů KY-040, 2 pokud jeden "cvak" přeskakuje dvě položky
#define ENC_REVERSE false         // true = prohodit směr otáčení
#define LONG_PRESS_MS 700
#define MENU_TIMEOUT_MS 30000
#define SIGN_LINK_TIMEOUT_MS 5000 // po této době bez odpovědi cedule je "CED:--"
#define HEARTBEAT_MS 500          // jak často se barva posílá, i když se nic nemění

// ---------------- typy ----------------
enum LightState : uint8_t { ST_NO_PC = 0, ST_IDLE = 1, ST_REC = 2 };
enum Override : uint8_t { OV_AUTO = 0, OV_REC, OV_IDLE, OV_NOPC, OV_ALL, OV_COUNT };
enum Screen : uint8_t { SCR_STATUS, SCR_MENU, SCR_INFO, SCR_CONFIRM };
enum InfoKind : uint8_t { INFO_SIGN, INFO_ABOUT };
enum BtnEvent : uint8_t { BTN_NONE, BTN_SHORT, BTN_LONG };

enum MenuId : uint8_t {
  MI_DELAY, MI_TIMEOUT, MI_COL_REC, MI_COL_IDLE, MI_COL_NOPC, MI_FX_REC, MI_BRIGHT,
  MI_CUST_R, MI_CUST_G, MI_CUST_B, MI_PRESET, MI_TEST, MI_TEST3,
  MI_CONTRAST, MI_SCREENOFF, MI_SIGN, MI_ABOUT, MI_FACTORY, MI_BACK, MI_COUNT
};

const char* const MENU_LABELS[MI_COUNT] = {
  "Zpozdeni vyp", "Timeout PC", "Barva REC", "Barva pripr.", "Barva bez PC", "Efekt REC", "Jas cedule",
  "Vlast. R", "Vlast. G", "Vlast. B", "Predvolba", "Test cedule", "Test 3 s",
  "Jas displeje", "Displej vyp.", "Cedule", "O zarizeni", "Tovarni nast.", "Zpet"
};
const char* const OV_NAMES[OV_COUNT] = { "Auto", "Nahravani", "Pripraven", "Bez PC", "Bila" };
const char* const FX_NAMES[3] = { "Svit", "Blikani", "Dychani" };
const uint16_t SCREEN_OFF_S[] = { 0, 10, 30, 60, 300 };
const int SCREEN_OFF_N = sizeof(SCREEN_OFF_S) / sizeof(SCREEN_OFF_S[0]);

// paleta barev pro cedulu (poslední položka je "Vlastni" = hodnoty R, G, B z menu)
struct PalColor { const char* name; uint8_t r, g, b; };
const PalColor PALETTE[] = {
  { "Vyp", 0, 0, 0 }, { "Cervena", 255, 0, 0 }, { "Zelena", 0, 255, 0 }, { "Modra", 0, 0, 255 },
  { "Oranz.", 255, 90, 0 }, { "Zluta", 255, 200, 0 }, { "Azur", 0, 200, 255 }, { "Fialova", 160, 0, 255 },
  { "Ruzova", 255, 0, 110 }, { "Bila", 255, 255, 255 }, { "Vlastni", 0, 0, 0 }
};
#define PAL_N 11
#define PAL_CUSTOM 10

// ---------------- konfigurace (ukládá se do paměti NVS) ----------------
#define CFG_MAGIC 0xA3
struct Config {
  uint8_t magic;
  uint8_t delayS;        // zpoždění zhasnutí po konci nahrávání, 0..60 s
  uint8_t timeoutS;      // po jaké době bez dat z PC je stav "BEZ PC", 2..60 s
  uint8_t color[3];      // index do PALETTE pro každý stav (index = LightState)
  uint8_t fxRec;         // efekt při nahrávání: RL_FX_*
  uint8_t bright;        // jas cedule 5..100 %
  uint8_t cust[3];       // vlastní barva R, G, B
  uint8_t contrast;      // jas OLED 10..255
  uint8_t screenOffIdx;  // index do SCREEN_OFF_S
};

Config cfg;
Config backupCfg;
Preferences prefs;

void setDefaults(Config& c) {
  c.magic = CFG_MAGIC;
  c.delayS = 0;
  c.timeoutS = 5;
  c.color[ST_REC] = 1;      // nahrává se: červená
  c.color[ST_IDLE] = 0;     // vypnuto
  c.color[ST_NO_PC] = 0;    // vypnuto
  c.fxRec = RL_FX_SOLID;
  c.bright = 100;
  c.cust[0] = 255; c.cust[1] = 120; c.cust[2] = 0;
  c.contrast = 160;
  c.screenOffIdx = 0;
}

void loadConfig() {
  setDefaults(cfg);
  prefs.begin("reclight", false);
  if (prefs.getBytesLength("cfg") == sizeof(Config)) {
    Config t;
    prefs.getBytes("cfg", &t, sizeof(Config));
    if (t.magic == CFG_MAGIC && t.delayS <= 60 && t.timeoutS >= 2 && t.timeoutS <= 60 &&
        t.color[0] < PAL_N && t.color[1] < PAL_N && t.color[2] < PAL_N && t.fxRec <= RL_FX_PULSE &&
        t.bright >= 5 && t.bright <= 100 && t.contrast >= 10 && t.screenOffIdx < SCREEN_OFF_N) {
      cfg = t;
    }
  }
}

void saveConfig() { prefs.putBytes("cfg", &cfg, sizeof(Config)); }

// ---------------- displej ----------------
U8G2_SSD1306_128X64_NONAME_F_HW_I2C oled(U8G2_R0, U8X8_PIN_NONE);   // I2C: SDA 21, SCL 22

// ---------------- stav ----------------
char rxBuf[16];
uint8_t rxLen = 0;
uint32_t lastRx = 0;          // čas posledních platných dat z PC (0 = ještě nic nepřišlo)
bool cmdRec = false;          // poslední příkaz z PC byl REC
uint32_t holdUntil = 0;       // do kdy ještě držet REC po příkazu IDLE (zpoždění zhasnutí)
uint32_t recStart = 0;        // kdy začalo aktuální nahrávání
LightState prevAuto = ST_NO_PC;

Override override_ = OV_AUTO;
Override backupOv = OV_AUTO;
uint32_t testUntil = 0;       // krátký test (3 s)

Screen screen = SCR_STATUS;
InfoKind infoKind = INFO_SIGN;
int sel = 0, topIdx = 0;
bool editing = false;
bool displayAsleep = false;
uint32_t lastActivity = 0;
uint32_t lastDraw = 0;
uint32_t lastDbg = 0;
bool needDraw = true;

// co se právě posílá cedulce
LightState effState = ST_NO_PC;
LightState autoState = ST_NO_PC;
uint8_t effRGB[3] = { 0, 0, 0 };
uint8_t effFx = RL_FX_SOLID;
uint8_t effBright = 100;
const char* effColorName = "Vyp";

// ESP-NOW
bool espOk = false;
volatile uint32_t signLastRx = 0;     // kdy naposledy odpověděla cedule (0 = nikdy)
volatile uint32_t rxAny = 0, rxOk = 0, rxBad = 0;   // diagnostika příjmu: všechny pakety / platné od cedule / odmítnuté
volatile int rxLastLen = 0;
volatile uint16_t signMv = 0;         // napětí baterie cedule v mV (0 = neměří se)
uint8_t txSeq = 0;
uint32_t txOk = 0, txErr = 0;     // výsledky esp_now_send (vidět v menu Cedule)
int lastTxErr = 0;
uint8_t txBurst = 0;
uint32_t lastSend = 0;
uint8_t sentRGB[3] = { 255, 255, 255 }, sentFx = 255, sentBright = 255;   // naposledy odeslané (aby se odeslala i první hodnota)
const uint8_t BCAST[6] = { 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF };

static inline int wrapi(int v, int n) { return ((v % n) + n) % n; }
static inline int clampi(int v, int lo, int hi) { return v < lo ? lo : (v > hi ? hi : v); }

// ---------------- barvy ----------------
void paletteRGB(int idx, uint8_t* rgb) {
  if (idx == PAL_CUSTOM) { rgb[0] = cfg.cust[0]; rgb[1] = cfg.cust[1]; rgb[2] = cfg.cust[2]; }
  else { rgb[0] = PALETTE[idx].r; rgb[1] = PALETTE[idx].g; rgb[2] = PALETTE[idx].b; }
}

// ---------------- ESP-NOW ----------------
#if ESP_ARDUINO_VERSION_MAJOR >= 3
void onRecv(const esp_now_recv_info_t* info, const uint8_t* data, int len) {
#else
void onRecv(const uint8_t* mac, const uint8_t* data, int len) {
#endif
  RLPacket p;
  rxAny++;
  rxLastLen = len;
  if (!rlParse(data, len, &p) || p.type != RL_TYPE_STATUS) { rxBad++; return; }
  rxOk++;
  signMv = p.aux;
  signLastRx = millis() ? millis() : 1;
}

void espnowInit() {
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_ps(WIFI_PS_NONE);              // úsporný režim Wi-Fi by mohl zahazovat příchozí pakety ESP-NOW
  esp_wifi_set_channel(RL_CHANNEL, WIFI_SECOND_CHAN_NONE);
  if (esp_now_init() != ESP_OK) { espOk = false; return; }
  esp_now_peer_info_t peer;
  memset(&peer, 0, sizeof(peer));
  memcpy(peer.peer_addr, BCAST, 6);
  peer.channel = 0;                           // 0 = aktuální kanál
  peer.ifidx = WIFI_IF_STA;
  peer.encrypt = false;
  espOk = esp_now_add_peer(&peer) == ESP_OK;
  esp_now_register_recv_cb(onRecv);
}

void sendCmd() {
  if (!espOk) return;
  RLPacket p;
  memset(&p, 0, sizeof(p));
  p.seq = ++txSeq;
  p.r = effRGB[0]; p.g = effRGB[1]; p.b = effRGB[2];
  p.bright = effBright;
  p.effect = effFx;
  rlSeal(&p, RL_TYPE_CMD);
  esp_err_t e = esp_now_send(BCAST, (const uint8_t*)&p, sizeof(p));
  if (e == ESP_OK) txOk++; else { txErr++; lastTxErr = (int)e; }
}

// změny se odešlou hned (třikrát po sobě, kdyby se paket ztratil), jinak se posílá "heartbeat"
void txService(uint32_t now) {
  bool changed = memcmp(sentRGB, effRGB, 3) != 0 || sentFx != effFx || sentBright != effBright;
  if (changed) {
    memcpy(sentRGB, effRGB, 3);
    sentFx = effFx;
    sentBright = effBright;
    txBurst = 3;
    lastSend = now - 1000;
  }
  if (txBurst > 0) {
    if (now - lastSend >= 40) { sendCmd(); txBurst--; lastSend = now; }
  } else if (now - lastSend >= HEARTBEAT_MS) {
    sendCmd();
    lastSend = now;
  }
}

bool signOnline(uint32_t now) { return signLastRx != 0 && (now - signLastRx) <= SIGN_LINK_TIMEOUT_MS; }

// ---------------- enkodér (přerušení) ----------------
volatile int32_t encPos = 0;
volatile uint8_t encState = 0;
const int8_t ENC_TABLE[16] = { 0, -1, 1, 0, 1, 0, 0, -1, -1, 0, 0, 1, 0, 1, -1, 0 };

void IRAM_ATTR encISR() {
  uint8_t s = ((encState << 2) | (digitalRead(PIN_ENC_CLK) << 1) | digitalRead(PIN_ENC_DT)) & 0x0F;
  encState = s & 0x03;
  encPos += ENC_TABLE[s];
}

int readEncoderSteps() {
  static int32_t last = 0;
  int32_t p = encPos;
  int d = (p - last) / ENC_STEPS_PER_CLICK;
  if (d != 0) last += d * ENC_STEPS_PER_CLICK;
  return ENC_REVERSE ? -d : d;
}

BtnEvent readButton(uint32_t now) {
  static bool down = false, longFired = false;
  static uint32_t downAt = 0, changedAt = 0;
  bool pressed = digitalRead(PIN_ENC_SW) == LOW;
  if (pressed != down && now - changedAt > 25) {
    changedAt = now;
    down = pressed;
    if (down) {
      downAt = now;
      longFired = false;
    } else if (!longFired) {
      return BTN_SHORT;
    }
  }
  if (down && !longFired && now - downAt > LONG_PRESS_MS) {
    longFired = true;
    return BTN_LONG;
  }
  return BTN_NONE;
}

// ---------------- logika stavu ----------------
void parseLine(const char* s, uint32_t now) {
  if (strcmp(s, "REC") == 0) {
    cmdRec = true;
    holdUntil = 0;
    lastRx = now ? now : 1;
  } else if (strcmp(s, "IDLE") == 0) {
    if (cmdRec && cfg.delayS > 0) holdUntil = now + (uint32_t)cfg.delayS * 1000UL;
    cmdRec = false;
    lastRx = now ? now : 1;
  }
}

void readSerial(uint32_t now) {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      rxBuf[rxLen] = 0;
      if (rxLen) parseLine(rxBuf, now);
      rxLen = 0;
    } else if (rxLen < sizeof(rxBuf) - 1) {
      rxBuf[rxLen++] = c;
    }
  }
}

bool pcAlive(uint32_t now) { return lastRx != 0 && (now - lastRx) <= (uint32_t)cfg.timeoutS * 1000UL; }

LightState computeAuto(uint32_t now) {
  if (!pcAlive(now)) { holdUntil = 0; return ST_NO_PC; }
  if (cmdRec) return ST_REC;
  if (holdUntil && (int32_t)(holdUntil - now) > 0) return ST_REC;
  holdUntil = 0;
  return ST_IDLE;
}

bool testActive() { return override_ != OV_AUTO || testUntil != 0; }

void updateState(uint32_t now) {
  autoState = computeAuto(now);
  if (autoState == ST_REC && prevAuto != ST_REC) recStart = now;
  if (autoState != prevAuto) {
    lastActivity = now;
    needDraw = true;
    if (displayAsleep) { displayAsleep = false; oled.setPowerSave(0); }   // změna stavu displej probudí
  }
  prevAuto = autoState;

  effState = autoState;
  bool white = false;
  if (testUntil && (int32_t)(testUntil - now) > 0) {
    effState = ST_REC;
  } else {
    testUntil = 0;
    switch (override_) {
      case OV_REC:  effState = ST_REC;   break;
      case OV_IDLE: effState = ST_IDLE;  break;
      case OV_NOPC: effState = ST_NO_PC; break;
      case OV_ALL:  effState = ST_REC; white = true; break;
      default: break;
    }
  }
  if (white) {
    effRGB[0] = effRGB[1] = effRGB[2] = 255;
    effFx = RL_FX_SOLID;
    effBright = 100;
    effColorName = "Bila";
  } else {
    int idx = cfg.color[effState];
    paletteRGB(idx, effRGB);
    effFx = (effState == ST_REC) ? cfg.fxRec : RL_FX_SOLID;
    effBright = cfg.bright;
    effColorName = PALETTE[idx].name;
  }
  txService(now);
}

// ---------------- pomocné texty ----------------
void fmtTime(char* buf, size_t n, uint32_t sec) {
  if (sec >= 3600) snprintf(buf, n, "%lu:%02lu:%02lu", (unsigned long)(sec / 3600), (unsigned long)((sec / 60) % 60), (unsigned long)(sec % 60));
  else snprintf(buf, n, "%02lu:%02lu", (unsigned long)(sec / 60), (unsigned long)(sec % 60));
}

int presetOf() {
  if (cfg.color[ST_REC] == 1 && cfg.color[ST_IDLE] == 0 && cfg.color[ST_NO_PC] == 0) return 0;
  if (cfg.color[ST_REC] == 1 && cfg.color[ST_IDLE] == 2 && cfg.color[ST_NO_PC] == 3) return 1;
  return -1;
}

void applyPreset(int p) {
  cfg.color[ST_REC] = 1;                 // nahrává se: červená
  cfg.color[ST_IDLE] = (p == 0) ? 0 : 2; // připraven: vypnuto / zelená
  cfg.color[ST_NO_PC] = (p == 0) ? 0 : 3;// bez PC: vypnuto / modrá
}

bool isEditable(int id) { return id <= MI_TEST || (id >= MI_CONTRAST && id <= MI_SCREENOFF); }

void valueText(int id, char* buf, size_t n) {
  buf[0] = 0;
  switch (id) {
    case MI_DELAY:     snprintf(buf, n, "%u s", cfg.delayS); break;
    case MI_TIMEOUT:   snprintf(buf, n, "%u s", cfg.timeoutS); break;
    case MI_COL_REC:   snprintf(buf, n, "%s", PALETTE[cfg.color[ST_REC]].name); break;
    case MI_COL_IDLE:  snprintf(buf, n, "%s", PALETTE[cfg.color[ST_IDLE]].name); break;
    case MI_COL_NOPC:  snprintf(buf, n, "%s", PALETTE[cfg.color[ST_NO_PC]].name); break;
    case MI_FX_REC:    snprintf(buf, n, "%s", FX_NAMES[cfg.fxRec]); break;
    case MI_BRIGHT:    snprintf(buf, n, "%u%%", cfg.bright); break;
    case MI_CUST_R:    snprintf(buf, n, "%u", cfg.cust[0]); break;
    case MI_CUST_G:    snprintf(buf, n, "%u", cfg.cust[1]); break;
    case MI_CUST_B:    snprintf(buf, n, "%u", cfg.cust[2]); break;
    case MI_PRESET: {
      int p = presetOf();
      snprintf(buf, n, "%s", p == 0 ? "1 barva" : (p == 1 ? "Semafor" : "Vlastni"));
      break;
    }
    case MI_TEST:      snprintf(buf, n, "%s", OV_NAMES[override_]); break;
    case MI_CONTRAST:  snprintf(buf, n, "%d%%", (int)cfg.contrast * 100 / 255); break;
    case MI_SCREENOFF: {
      uint16_t s = SCREEN_OFF_S[cfg.screenOffIdx];
      if (s == 0) snprintf(buf, n, "nikdy");
      else if (s < 60) snprintf(buf, n, "%u s", s);
      else snprintf(buf, n, "%u min", s / 60);
      break;
    }
    default: break;
  }
}

// ---------------- úpravy hodnot v menu ----------------
void adjust(int id, int d) {
  switch (id) {
    case MI_DELAY:    cfg.delayS = clampi(cfg.delayS + d, 0, 60); break;
    case MI_TIMEOUT:  cfg.timeoutS = clampi(cfg.timeoutS + d, 2, 60); break;
    case MI_COL_REC:  cfg.color[ST_REC] = wrapi(cfg.color[ST_REC] + d, PAL_N); break;
    case MI_COL_IDLE: cfg.color[ST_IDLE] = wrapi(cfg.color[ST_IDLE] + d, PAL_N); break;
    case MI_COL_NOPC: cfg.color[ST_NO_PC] = wrapi(cfg.color[ST_NO_PC] + d, PAL_N); break;
    case MI_FX_REC:   cfg.fxRec = wrapi(cfg.fxRec + d, 3); break;
    case MI_BRIGHT:   cfg.bright = clampi(cfg.bright + d * 5, 5, 100); break;
    case MI_CUST_R:   cfg.cust[0] = clampi(cfg.cust[0] + d * 5, 0, 255); break;
    case MI_CUST_G:   cfg.cust[1] = clampi(cfg.cust[1] + d * 5, 0, 255); break;
    case MI_CUST_B:   cfg.cust[2] = clampi(cfg.cust[2] + d * 5, 0, 255); break;
    case MI_PRESET: {
      int p = presetOf();
      applyPreset(wrapi((p < 0 ? 0 : p) + d, 2));
      break;
    }
    case MI_TEST:      override_ = (Override)wrapi((int)override_ + d, OV_COUNT); break;
    case MI_CONTRAST:  cfg.contrast = clampi(cfg.contrast + d * 10, 10, 255); oled.setContrast(cfg.contrast); break;
    case MI_SCREENOFF: cfg.screenOffIdx = clampi(cfg.screenOffIdx + d, 0, SCREEN_OFF_N - 1); break;
    default: break;
  }
}

void factoryReset() {
  setDefaults(cfg);
  override_ = OV_AUTO;
  testUntil = 0;
  oled.setContrast(cfg.contrast);
  saveConfig();
}

void activate(int id, uint32_t now) {
  if (isEditable(id)) {
    backupCfg = cfg;
    backupOv = override_;
    editing = true;
    return;
  }
  switch (id) {
    case MI_TEST3:   testUntil = now + 3000; screen = SCR_STATUS; break;
    case MI_SIGN:    infoKind = INFO_SIGN;  screen = SCR_INFO; break;
    case MI_ABOUT:   infoKind = INFO_ABOUT; screen = SCR_INFO; break;
    case MI_FACTORY: screen = SCR_CONFIRM; break;
    case MI_BACK:    screen = SCR_STATUS; break;
    default: break;
  }
}

void handleInput(int steps, BtnEvent b, uint32_t now) {
  switch (screen) {
    case SCR_STATUS:
      if (b == BTN_SHORT) { screen = SCR_MENU; editing = false; }
      else if (b == BTN_LONG) { override_ = (override_ == OV_AUTO) ? OV_REC : OV_AUTO; }
      break;
    case SCR_MENU:
      if (!editing) {
        if (steps) sel = wrapi(sel + steps, MI_COUNT);
        if (b == BTN_SHORT) activate(sel, now);
        else if (b == BTN_LONG) screen = SCR_STATUS;
      } else {
        if (steps) adjust(sel, steps);
        if (b == BTN_SHORT) { saveConfig(); editing = false; }
        else if (b == BTN_LONG) {
          cfg = backupCfg;
          override_ = backupOv;
          oled.setContrast(cfg.contrast);
          editing = false;
        }
      }
      break;
    case SCR_INFO:
      if (b != BTN_NONE) screen = SCR_MENU;
      break;
    case SCR_CONFIRM:
      if (b == BTN_SHORT) { factoryReset(); screen = SCR_MENU; }
      else if (b == BTN_LONG) screen = SCR_MENU;
      break;
  }
  needDraw = true;
}

// ---------------- kreslení ----------------
#define FONT_SMALL u8g2_font_6x10_tf
#define FONT_BIG   u8g2_font_helvB14_tr

void drawCentered(int y, const char* s) {
  int w = oled.getStrWidth(s);
  oled.drawStr((128 - w) / 2, y, s);
}

void drawRightAligned(int y, const char* s, int margin) {
  int w = oled.getStrWidth(s);
  oled.drawStr(128 - w - margin, y, s);
}

void drawStatus(uint32_t now) {
  char buf[40];
  oled.setFont(FONT_SMALL);
  oled.drawStr(0, 9, pcAlive(now) ? "USB:OK" : "USB:--");
  drawRightAligned(9, !espOk ? "CED:ERR" : (signOnline(now) ? "CED:OK" : "CED:--"), 0);
  oled.drawHLine(0, 11, 128);

  const char* big;
  if (override_ == OV_ALL) big = "BILA";
  else if (effState == ST_REC) big = "NAHRAVA";
  else if (effState == ST_IDLE) big = "PRIPRAVEN";
  else big = "BEZ PC";
  oled.setFont(FONT_BIG);
  drawCentered(34, big);

  oled.setFont(FONT_SMALL);
  if (autoState == ST_REC && !testActive()) {
    char t[16];
    fmtTime(t, sizeof(t), (now - recStart) / 1000);
    if (holdUntil && (int32_t)(holdUntil - now) > 0 && !cmdRec) {
      snprintf(buf, sizeof(buf), "%s  zhasne %lus", t, (unsigned long)((holdUntil - now + 999) / 1000));
    } else {
      snprintf(buf, sizeof(buf), "REC %s", t);
    }
  } else if (autoState == ST_IDLE) {
    snprintf(buf, sizeof(buf), "Ceka na nahravani");
  } else if (autoState == ST_NO_PC) {
    if (lastRx == 0) snprintf(buf, sizeof(buf), "Zatim zadna data");
    else snprintf(buf, sizeof(buf), "Posl. data pred %lus", (unsigned long)((now - lastRx) / 1000));
  } else {
    buf[0] = 0;
  }
  drawCentered(47, buf);

  // spodní řádek: vlevo test / baterie cedule, vpravo barva, která se posílá
  if (testActive()) {
    int w = oled.getStrWidth("TEST");
    oled.drawBox(0, 54, w + 4, 10);
    oled.setDrawColor(0);
    oled.drawStr(2, 62, "TEST");
    oled.setDrawColor(1);
  } else if (signOnline(now) && signMv > 0) {
    snprintf(buf, sizeof(buf), "Bat %u.%02uV", signMv / 1000, (signMv % 1000) / 10);
    oled.drawStr(0, 62, buf);
  } else {
    oled.drawStr(0, 62, "RecLight");
  }
  drawRightAligned(62, effColorName, 0);
}

void drawMenu() {
  char val[24], tmp[28], hdr[16];
  oled.setFont(FONT_SMALL);
  oled.drawStr(0, 9, editing ? "UPRAVA" : "MENU");
  snprintf(hdr, sizeof(hdr), "%d/%d", sel + 1, (int)MI_COUNT);
  drawRightAligned(9, hdr, 0);
  oled.drawHLine(0, 11, 128);

  if (sel < topIdx) topIdx = sel;
  if (sel >= topIdx + 5) topIdx = sel - 4;
  for (int r = 0; r < 5; r++) {
    int idx = topIdx + r;
    if (idx >= MI_COUNT) break;
    int y = 21 + 10 * r;
    bool isSel = idx == sel;
    if (isSel) {
      oled.drawBox(0, y - 9, 128, 10);
      oled.setDrawColor(0);
    }
    oled.drawStr(1, y, MENU_LABELS[idx]);
    valueText(idx, val, sizeof(val));
    if (val[0]) {
      if (isSel && editing) { snprintf(tmp, sizeof(tmp), "<%s>", val); drawRightAligned(y, tmp, 1); }
      else drawRightAligned(y, val, 1);
    }
    if (isSel) oled.setDrawColor(1);
  }
}

void drawInfo(uint32_t now) {
  char buf[48], t[16];
  oled.setFont(FONT_SMALL);
  if (infoKind == INFO_SIGN) {
    oled.drawStr(0, 9, "Cedule (ESP-NOW)");
    oled.drawHLine(0, 11, 128);
    if (!espOk) snprintf(buf, sizeof(buf), "Stav: chyba ESP-NOW");
    else snprintf(buf, sizeof(buf), "Stav: %s", signOnline(now) ? "online" : "bez odezvy");
    oled.drawStr(0, 21, buf);
    if (signLastRx == 0) snprintf(buf, sizeof(buf), "Odpoved: zatim zadna");
    else snprintf(buf, sizeof(buf), "Odpoved: pred %lus", (unsigned long)((now - signLastRx) / 1000));
    oled.drawStr(0, 31, buf);
    if (signMv > 0) snprintf(buf, sizeof(buf), "Baterie: %u.%02u V", signMv / 1000, (signMv % 1000) / 10);
    else snprintf(buf, sizeof(buf), "Baterie: nezmerena");
    oled.drawStr(0, 41, buf);
    snprintf(buf, sizeof(buf), "RX%lu ok%lu zle%lu", (unsigned long)(rxAny % 10000), (unsigned long)(rxOk % 10000), (unsigned long)(rxBad % 1000));
    oled.drawStr(0, 51, buf);
    if (txErr) snprintf(buf, sizeof(buf), "TX%lu ERR%lu(%d)", (unsigned long)(txOk % 10000), (unsigned long)(txErr % 1000), lastTxErr);
    else snprintf(buf, sizeof(buf), "TX%lu K%d FW%s", (unsigned long)(txOk % 10000), RL_CHANNEL, FW_VERSION);
    oled.drawStr(0, 61, buf);
  } else {
    snprintf(buf, sizeof(buf), "RecLight FW %s", FW_VERSION);
    oled.drawStr(0, 9, buf);
    oled.drawHLine(0, 11, 128);
    if (lastRx == 0) snprintf(buf, sizeof(buf), "USB: zatim bez dat");
    else snprintf(buf, sizeof(buf), "USB: data pred %lus", (unsigned long)((now - lastRx) / 1000));
    oled.drawStr(0, 23, buf);
    fmtTime(t, sizeof(t), now / 1000);
    snprintf(buf, sizeof(buf), "Beh: %s", t);
    oled.drawStr(0, 34, buf);
    snprintf(buf, sizeof(buf), "ESP-NOW kanal %d", RL_CHANNEL);
    oled.drawStr(0, 45, buf);
    oled.drawStr(0, 62, "Stisk = zpet");
  }
}

void drawConfirm() {
  oled.setFont(FONT_SMALL);
  oled.drawStr(0, 9, "Tovarni nastaveni");
  oled.drawHLine(0, 11, 128);
  oled.drawStr(0, 28, "Smazat vsechna nastaveni?");
  oled.drawStr(0, 46, "Stisk = ANO");
  oled.drawStr(0, 58, "Drzet = NE");
}

void draw(uint32_t now) {
  oled.clearBuffer();
  switch (screen) {
    case SCR_STATUS:  drawStatus(now); break;
    case SCR_MENU:    drawMenu(); break;
    case SCR_INFO:    drawInfo(now); break;
    case SCR_CONFIRM: drawConfirm(); break;
  }
  oled.sendBuffer();
}

// ---------------- setup / loop ----------------
void setup() {
  loadConfig();

  pinMode(PIN_ENC_CLK, INPUT_PULLUP);
  pinMode(PIN_ENC_DT, INPUT_PULLUP);
  pinMode(PIN_ENC_SW, INPUT_PULLUP);
  encState = (digitalRead(PIN_ENC_CLK) << 1) | digitalRead(PIN_ENC_DT);
  attachInterrupt(digitalPinToInterrupt(PIN_ENC_CLK), encISR, CHANGE);
  attachInterrupt(digitalPinToInterrupt(PIN_ENC_DT), encISR, CHANGE);

  Serial.begin(115200);

  oled.begin();
  oled.setBusClock(400000);
  oled.setContrast(cfg.contrast);
  oled.clearBuffer();
  oled.setFont(FONT_BIG);
  drawCentered(30, "RecLight");
  oled.setFont(FONT_SMALL);
  char v[24];
  snprintf(v, sizeof(v), "panel FW %s", FW_VERSION);
  drawCentered(48, v);
  oled.sendBuffer();

  espnowInit();
  delay(600);
  lastActivity = millis();
}

void loop() {
  uint32_t now = millis();

  readSerial(now);
  updateState(now);

  int steps = readEncoderSteps();
  BtnEvent b = readButton(now);
  if (steps || b != BTN_NONE) {
    lastActivity = now;
    if (displayAsleep) {
      // první pohyb jen probudí displej
      displayAsleep = false;
      oled.setPowerSave(0);
      needDraw = true;
    } else {
      handleInput(steps, b, now);
    }
  }

  // automatické vypnutí displeje (ochrana OLED) a návrat z menu
  uint16_t offS = SCREEN_OFF_S[cfg.screenOffIdx];
  if (screen != SCR_STATUS && now - lastActivity > MENU_TIMEOUT_MS) {
    if (editing) {
      cfg = backupCfg;
      override_ = backupOv;
      oled.setContrast(cfg.contrast);
      editing = false;
    }
    screen = SCR_STATUS;
    needDraw = true;
  }
  if (offS && !displayAsleep && screen == SCR_STATUS && now - lastActivity > (uint32_t)offS * 1000UL) {
    displayAsleep = true;
    oled.setPowerSave(1);
  }

#if RL_DEBUG
  if (now - lastDbg >= 1000) {
    lastDbg = now;
    uint8_t mac[6] = { 0 }, ch = 0;
    wifi_second_chan_t sc;
    WiFi.macAddress(mac);
    esp_wifi_get_channel(&ch, &sc);
    Serial.printf("[panel %lus] MAC %02X:%02X:%02X:%02X:%02X:%02X ch=%u espOk=%d | RX vse=%lu ok=%lu zle=%lu delka=%d | TX ok=%lu err=%lu(%d) | USB=%d stav=%d cedule=%d\n",
                  (unsigned long)(now / 1000), mac[0], mac[1], mac[2], mac[3], mac[4], mac[5], ch, espOk ? 1 : 0,
                  (unsigned long)rxAny, (unsigned long)rxOk, (unsigned long)rxBad, (int)rxLastLen,
                  (unsigned long)txOk, (unsigned long)txErr, lastTxErr, pcAlive(now) ? 1 : 0, (int)effState, signOnline(now) ? 1 : 0);
  }
#endif

  if (!displayAsleep && (needDraw || now - lastDraw >= 200)) {
    draw(now);
    lastDraw = now;
    needDraw = false;
  }
}

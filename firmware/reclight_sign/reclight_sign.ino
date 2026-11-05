// RecLight SIGN - bezdrátová cedule "NAHRÁVÁ SE" (ESP32-C3 + LED panel + baterie s nabíjecím modulem).
// Přijímá přes ESP-NOW barvu, jas a efekt z panelu (skeč reclight_panel) a rozsvěcí LED.
// Panel posílá příkazy minimálně 2x za sekundu; cedule na ně odpovídá stavem (napětí baterie), takže
// panel ví, že je cedule v dosahu. Když příkazy přestanou chodit, cedule zhasne a jen občas krátce probliknutí modře.
//
// DESKA: "ESP32C3 Dev Module" (u Super Mini s USB zapnout "USB CDC On Boot: Enabled").
// SOUBORY: reclight_proto.h musí ležet ve stejné složce a být shodný s tím v panelu.
//
// ZAPOJENÍ (výchozí režim LED_MODE_PWM, RGB panel přes tranzistory/MOSFETy):
//   GPIO5 -> brána/báze tranzistoru pro ČERVENOU, GPIO6 -> ZELENOU, GPIO7 -> MODROU
//   (N-MOSFET s logickou úrovní nebo NPN s rezistorem 330 ohm v bázi; zdroj LED se spíná na straně GND;
//    GND baterie, ESP a tranzistorů musí být spojené.)
//   Režim WS2812 (adresovatelné LED): změňte LED_MODE na LED_MODE_WS2812, data na GPIO5 přes 330 ohm,
//   potřebuje knihovnu "Adafruit NeoPixel". Tento režim je zatím jen připravený, nezkoušený.
//   Měření baterie (volitelné): rozdělovač 100k + 100k z BAT+ na GND, střed na GPIO4, pak BATTERY_SENSE_PIN = 4.
//
// PINY: GPIO2, GPIO8, GPIO9 jsou strapping piny (GPIO9 je tlačítko BOOT), GPIO18/19 jsou USB. Těm se firmware vyhýbá.

#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <math.h>
#include "reclight_proto.h"

#ifndef ESP_ARDUINO_VERSION_MAJOR
#define ESP_ARDUINO_VERSION_MAJOR 2
#endif
// od ESP-IDF 5.5 (jádro ESP32 3.3+) má callback po odeslání jiný podpis
#if defined(ESP_IDF_VERSION) && defined(ESP_IDF_VERSION_VAL)
#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 5, 0)
#define RL_SEND_CB_NEW 1
#endif
#endif

#define FW_VERSION "1.0"

// ---------------- nastavení ----------------
#define LED_MODE_PWM     0
#define LED_MODE_WS2812  1
#define LED_MODE         LED_MODE_PWM

#define PIN_LED_R   5
#define PIN_LED_G   6
#define PIN_LED_B   7
#define LED_ACTIVE_LOW 0          // 1 = výstup je invertovaný (např. PNP / P-MOSFET na straně +)
#define PWM_FREQ    5000
#define PWM_BITS    10            // plný rozsah 1023

#define PIN_LED_DATA  5           // jen režim WS2812
#define WS2812_COUNT  64

#define BATTERY_SENSE_PIN 4       // -1 = neměřit, jinak číslo pinu s rozdělovačem (100k + 100k z BAT+ na GND, střed na GPIO4)
#define BATTERY_DIV       2.0f    // poměr rozdělovače (100k + 100k = 2.0)
#define LOW_BATT_MV       3300    // pod touto hodnotou se LED vypnou (ochrana článku); 0 = vypnuto

#define SIGN_TIMEOUT_MS   3000    // po této době bez příkazu z panelu přejde cedule do "offline"
#define OFFLINE_BLINK     1       // 1 = v offline stavu krátce probliknout modře každé 3 s (hodí se pro kontrolu)
#define STATUS_LED_PIN    8       // vestavěná LED desky (Super Mini: GPIO8, svítí při LOW); -1 = nepoužít. Svítí, dokud cedule slyší panel
#define STATUS_LED_ACTIVE_LOW 1
#define CPU_MHZ           80      // nižší takt šetří baterii (WiFi vyžaduje minimálně 80)
#define RL_DEBUG          1       // 1 = každou sekundu výpis stavu přes USB (Serial Monitor 115200); vypisuje se jen když je USB připojené

#if LED_MODE == LED_MODE_WS2812
#include <Adafruit_NeoPixel.h>
Adafruit_NeoPixel strip(WS2812_COUNT, PIN_LED_DATA, NEO_GRB + NEO_KHZ800);
#endif

// ---------------- stav ----------------
volatile uint8_t tgtR = 0, tgtG = 0, tgtB = 0, tgtBright = 100, tgtFx = RL_FX_SOLID;
volatile uint32_t lastCmdAt = 0;      // kdy přišel poslední příkaz (0 = ještě žádný)
volatile uint8_t lastSeq = 0;
volatile bool needReply = false;
uint32_t lastReply = 0;
uint16_t battMv = 0;
bool lowBatt = false;
uint32_t lastBattRead = 0;
uint16_t gammaTab[256];
uint8_t shownR = 255, shownG = 255, shownB = 255;
uint8_t txSeq = 0;
uint32_t txOk = 0, txErr = 0;
int lastTxErr = 0;
bool espOk = false;
volatile uint32_t rxRaw = 0, rxGood = 0, rxBad = 0;   // diagnostika: všechny přijaté / platné příkazy / neplatné
const uint8_t BCAST[6] = { 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF };
uint8_t panelMac[6] = { 0 };          // MAC panelu, zjištěná z jeho příkazů (odpovídáme na ni přímo, unicastem)
volatile bool havePanel = false;
bool panelPeerAdded = false;
volatile uint32_t ackOk = 0, ackFail = 0;   // výsledek odeslání (u unicastu = potvrzení od panelu na úrovni rádia)
int panelPeerErr = 0;

// ---------------- stavová LED na desce ----------------
void statusLed(bool on) {
  if (STATUS_LED_PIN < 0) return;
  digitalWrite(STATUS_LED_PIN, (on != (STATUS_LED_ACTIVE_LOW != 0)) ? HIGH : LOW);
}

// ---------------- LED výstup ----------------
void pwmWrite(int ch, uint32_t duty) {
  uint32_t maxDuty = (1u << PWM_BITS) - 1;
  if (LED_ACTIVE_LOW) duty = maxDuty - duty;
  const int pins[3] = { PIN_LED_R, PIN_LED_G, PIN_LED_B };
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcWrite(pins[ch], duty);
#else
  ledcWrite(ch, duty);
#endif
}

void ledInit() {
  // gamma 2.2, aby nízký jas vypadal přirozeně
  for (int i = 0; i < 256; i++) gammaTab[i] = (uint16_t)(powf(i / 255.0f, 2.2f) * ((1u << PWM_BITS) - 1) + 0.5f);
#if LED_MODE == LED_MODE_PWM
  const int pins[3] = { PIN_LED_R, PIN_LED_G, PIN_LED_B };
  for (int i = 0; i < 3; i++) {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
    ledcAttach(pins[i], PWM_FREQ, PWM_BITS);
#else
    ledcSetup(i, PWM_FREQ, PWM_BITS);
    ledcAttachPin(pins[i], i);
#endif
    pwmWrite(i, 0);
  }
#else
  strip.begin();
  strip.clear();
  strip.show();
#endif
}

void showRGB(uint8_t r, uint8_t g, uint8_t b) {
  if (r == shownR && g == shownG && b == shownB) return;
  shownR = r; shownG = g; shownB = b;
#if LED_MODE == LED_MODE_PWM
  pwmWrite(0, gammaTab[r]);
  pwmWrite(1, gammaTab[g]);
  pwmWrite(2, gammaTab[b]);
#else
  uint32_t c = strip.Color(gammaTab[r] >> (PWM_BITS - 8), gammaTab[g] >> (PWM_BITS - 8), gammaTab[b] >> (PWM_BITS - 8));
  strip.fill(c);
  strip.show();
#endif
}

// ---------------- baterie ----------------
uint16_t readBatteryMv() {
  if (BATTERY_SENSE_PIN < 0) return 0;
  uint32_t sum = 0;
  for (int i = 0; i < 8; i++) sum += analogReadMilliVolts(BATTERY_SENSE_PIN);
  return (uint16_t)((sum / 8.0f) * BATTERY_DIV);
}

// ---------------- ESP-NOW ----------------
#if ESP_ARDUINO_VERSION_MAJOR >= 3
void onRecv(const esp_now_recv_info_t* info, const uint8_t* data, int len) {
#else
void onRecv(const uint8_t* mac, const uint8_t* data, int len) {
#endif
  RLPacket p;
  rxRaw++;
  if (!rlParse(data, len, &p)) { rxBad++; return; }
  if (p.type != RL_TYPE_CMD) return;
  rxGood++;
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  const uint8_t* src = info ? info->src_addr : nullptr;
#else
  const uint8_t* src = mac;
#endif
  if (src) {
    if (!havePanel || memcmp(panelMac, src, 6) != 0) { memcpy(panelMac, src, 6); panelPeerAdded = false; havePanel = true; }
  }
  tgtR = p.r; tgtG = p.g; tgtB = p.b;
  tgtBright = p.bright > 100 ? 100 : p.bright;
  tgtFx = p.effect <= RL_FX_PULSE ? p.effect : RL_FX_SOLID;
  lastSeq = p.seq;
  uint32_t n = millis();
  lastCmdAt = n ? n : 1;
  needReply = true;
}

#ifdef RL_SEND_CB_NEW
void onSent(const esp_now_send_info_t* info, esp_now_send_status_t status) {
#else
void onSent(const uint8_t* mac, esp_now_send_status_t status) {
#endif
  if (status == ESP_NOW_SEND_SUCCESS) ackOk++; else ackFail++;
}

bool espnowInit() {
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_ps(WIFI_PS_NONE);              // úsporný režim Wi-Fi by mohl zahazovat příchozí pakety ESP-NOW
  WiFi.setTxPower(WIFI_POWER_8_5dBm);         // malé C3 desky s plnou silou vysílání často nevysílají spolehlivě (slabá anténa / napájení)
  esp_wifi_set_channel(RL_CHANNEL, WIFI_SECOND_CHAN_NONE);
  esp_err_t e = esp_now_init();
  if (e != ESP_OK) {
    if (RL_DEBUG) Serial.printf("ESP-NOW init CHYBA %d\n", (int)e);
    return false;
  }
  esp_now_peer_info_t peer;
  memset(&peer, 0, sizeof(peer));
  memcpy(peer.peer_addr, BCAST, 6);
  peer.channel = 0;                           // 0 = aktuální kanál
  peer.ifidx = WIFI_IF_STA;
  peer.encrypt = false;
  e = esp_now_add_peer(&peer);
  if (RL_DEBUG) Serial.printf("add_peer: %d\n", (int)e);
  e = esp_now_register_recv_cb(onRecv);
  esp_now_register_send_cb(onSent);
  if (RL_DEBUG) Serial.printf("register_recv_cb: %d\n", (int)e);
  return true;
}

void sendStatus() {
  RLPacket p;
  memset(&p, 0, sizeof(p));
  p.seq = lastSeq;
  p.r = shownR; p.g = shownG; p.b = shownB;
  p.bright = tgtBright;
  p.effect = tgtFx;
  p.aux = battMv;
  rlSeal(&p, RL_TYPE_STATUS);
  // unicast na panel má potvrzení a opakování na úrovni rádia, broadcast ne; dokud MAC panelu neznáme, jde broadcast
  const uint8_t* dest = BCAST;
  if (havePanel) {
    if (!panelPeerAdded) {
      esp_now_peer_info_t peer;
      memset(&peer, 0, sizeof(peer));
      memcpy(peer.peer_addr, panelMac, 6);
      peer.channel = 0;
      peer.ifidx = WIFI_IF_STA;
      peer.encrypt = false;
      esp_err_t pe = esp_now_add_peer(&peer);
      panelPeerErr = (int)pe;
      panelPeerAdded = (pe == ESP_OK || pe == ESP_ERR_ESPNOW_EXIST);
    }
    if (panelPeerAdded) dest = panelMac;
  }
  esp_err_t e = esp_now_send(dest, (const uint8_t*)&p, sizeof(p));
  if (e == ESP_OK) txOk++; else { txErr++; lastTxErr = (int)e; }
}

// ---------------- vykreslení ----------------
bool isOnline(uint32_t now) { return lastCmdAt != 0 && (now - lastCmdAt) <= SIGN_TIMEOUT_MS; }

void render(uint32_t now) {
  uint8_t r = 0, g = 0, b = 0;
  if (lowBatt) {
    // slabá baterie: vše vypnuto
  } else if (isOnline(now)) {
    float f = 1.0f;
    if (tgtFx == RL_FX_BLINK) {
      f = ((now / 500) & 1) ? 0.0f : 1.0f;                       // 1 Hz
    } else if (tgtFx == RL_FX_PULSE) {
      float t = (now % 2000) / 2000.0f;                           // perioda 2 s
      f = 0.12f + 0.88f * (0.5f - 0.5f * cosf(6.2831853f * t));
    }
    float k = (tgtBright / 100.0f) * f;
    r = (uint8_t)(tgtR * k + 0.5f);
    g = (uint8_t)(tgtG * k + 0.5f);
    b = (uint8_t)(tgtB * k + 0.5f);
  } else if (OFFLINE_BLINK && (now % 3000) < 80) {
    b = 40;                                                        // krátké modré probliknutí = cedule žije, ale panel neslyší
  }
  showRGB(r, g, b);
}

// ---------------- setup / loop ----------------
void setup() {
#ifdef CPU_MHZ
  setCpuFrequencyMhz(CPU_MHZ);
#endif
  if (RL_DEBUG) { Serial.begin(115200); delay(300); }
  if (STATUS_LED_PIN >= 0) { pinMode(STATUS_LED_PIN, OUTPUT); statusLed(false); }
  ledInit();
  // po startu krátce červená, zelená, modrá naplno (0,5 s každá) = test zapojení
  const uint8_t seq[3][3] = { { 255, 0, 0 }, { 0, 255, 0 }, { 0, 0, 255 } };
  for (int i = 0; i < 3; i++) {
    showRGB(seq[i][0], seq[i][1], seq[i][2]);
    statusLed(true);
    delay(250);
    statusLed(false);
    delay(250);
  }
  showRGB(0, 0, 0);
  espOk = espnowInit();
  battMv = readBatteryMv();
  if (RL_DEBUG) {
    uint8_t mac[6] = { 0 }, ch = 0;
    wifi_second_chan_t sc;
    WiFi.macAddress(mac);
    esp_wifi_get_channel(&ch, &sc);
    Serial.printf("\nRecLight sign FW %s, ESP-NOW %s, kanal %d (cil %d), net %d, MAC %02X:%02X:%02X:%02X:%02X:%02X\n",
                  FW_VERSION, espOk ? "OK" : "CHYBA", ch, RL_CHANNEL, RL_NET_ID, mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
  }
}

void loop() {
  uint32_t now = millis();

  if (BATTERY_SENSE_PIN >= 0 && (now - lastBattRead >= 5000 || lastBattRead == 0)) {
    lastBattRead = now ? now : 1;
    battMv = readBatteryMv();
    if (LOW_BATT_MV) {
      if (battMv && battMv < LOW_BATT_MV) lowBatt = true;
      else if (battMv > LOW_BATT_MV + 150) lowBatt = false;      // hystereze
    }
  }

#if RL_DEBUG
  static uint32_t lastDbg = 0;
  if (now - lastDbg >= 1000) {
    lastDbg = now;
    if (Serial) {
      uint8_t pri = 0;
      wifi_second_chan_t sec;
      esp_wifi_get_channel(&pri, &sec);
      char line[160];
      snprintf(line, sizeof(line), "[sign %lus] ch=%u espnow=%d rx_raw=%lu good=%lu bad=%lu tx_ok=%lu tx_err=%lu(%d) ack=%lu nack=%lu panel=%d peer=%d online=%d rgb=%u,%u,%u fx=%u br=%u bat=%umV",
               (unsigned long)(now / 1000), pri, espOk ? 1 : 0, (unsigned long)rxRaw, (unsigned long)rxGood, (unsigned long)rxBad,
               (unsigned long)txOk, (unsigned long)txErr, lastTxErr, (unsigned long)ackOk, (unsigned long)ackFail, havePanel ? 1 : 0, panelPeerErr, isOnline(now) ? 1 : 0, tgtR, tgtG, tgtB, tgtFx, tgtBright, battMv);
      Serial.println(line);
    }
  }
#endif

  render(now);
  statusLed(isOnline(now));

  // odpověď panelu (po vykreslení, aby hlásila skutečně zobrazenou barvu); nejvýše 1x za sekundu
  if (needReply && (lastReply == 0 || now - lastReply >= 1000)) {
    needReply = false;
    lastReply = now ? now : 1;
    sendStatus();
  }

  delay(10);   // ~100 Hz, nechává procesoru čas usnout (šetří baterii)
}

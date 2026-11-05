// RecLight - firmware pro ESP32 (ESP32S / DevKit s CP210x)
// Přijímá řádky "REC" / "IDLE" přes USB (Serial).
//
// TEST_RGB_MODE = true  (testovací režim, tři barvy):
//   modrá (GPIO5)   = z PC nic nechodí (RecLight neběží / PC vypnuté), po 5 s bez dat
//   zelená (GPIO4)  = RecLight běží, nenahrává se
//   červená (GPIO2) = nahrává se
//
// TEST_RGB_MODE = false (běžný režim): světlo na GPIO2 svítí jen při nahrávání, jinak je zhasnuté.
//
// Zapojení každé barvy: pin -> rezistor 220 Ohm až 1 kOhm -> LED (anoda) -> katoda na GND.
// Arduino IDE: Board = "ESP32 Dev Module", Port = váš COM. Při nahrávání může být nutné podržet tlačítko BOOT.

#define TEST_RGB_MODE false    // false = vrátit původní chování (jen jedno světlo při nahrávání)

#define PIN_RED   2            // nahrává se (v běžném režimu jediný výstup)
#define PIN_GREEN 4            // nenahrává se
#define PIN_BLUE  5            // PC / RecLight neběží

#define ACTIVE_LOW false       // true, pokud výstup zapíná stav LOW
#define BOOT_BLINK true        // po startu krátce ukázat barvy / bliknout = firmware běží
#define TIMEOUT_MS 5000        // po této době bez dat z PC se přejde do stavu "PC neběží"

enum LightState { ST_NO_PC, ST_IDLE, ST_REC };

String buffer;
unsigned long lastRx = 0;
LightState state = ST_NO_PC;

void writePin(int pin, bool on) {
  digitalWrite(pin, (on != ACTIVE_LOW) ? HIGH : LOW);
}

void applyState(LightState s) {
  state = s;
  writePin(PIN_RED, s == ST_REC);
  if (TEST_RGB_MODE) {
    writePin(PIN_GREEN, s == ST_IDLE);
    writePin(PIN_BLUE, s == ST_NO_PC);
  }
}

void allOff() {
  writePin(PIN_RED, false);
  if (TEST_RGB_MODE) {
    writePin(PIN_GREEN, false);
    writePin(PIN_BLUE, false);
  }
}

void setup() {
  pinMode(PIN_RED, OUTPUT);
  if (TEST_RGB_MODE) {
    pinMode(PIN_GREEN, OUTPUT);
    pinMode(PIN_BLUE, OUTPUT);
  }
  allOff();                 // hned na začátku, ať nic nesvítí náhodně
  Serial.begin(115200);

  if (BOOT_BLINK) {
    if (TEST_RGB_MODE) {
      // projetí barev: červená, zelená, modrá
      writePin(PIN_RED, true);   delay(250); writePin(PIN_RED, false);
      writePin(PIN_GREEN, true); delay(250); writePin(PIN_GREEN, false);
      writePin(PIN_BLUE, true);  delay(250); writePin(PIN_BLUE, false);
    } else {
      for (int i = 0; i < 3; i++) {
        writePin(PIN_RED, true);  delay(120);
        writePin(PIN_RED, false); delay(120);
      }
    }
  }
  applyState(ST_NO_PC);
}

void loop() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      buffer.trim();
      if (buffer == "REC") {
        applyState(ST_REC);
        lastRx = millis();
      } else if (buffer == "IDLE") {
        applyState(ST_IDLE);
        lastRx = millis();
      }
      buffer = "";
    } else if (buffer.length() < 16) {
      buffer += c;
    }
  }

  if (state != ST_NO_PC && millis() - lastRx > TIMEOUT_MS) {
    applyState(ST_NO_PC);
  }
}

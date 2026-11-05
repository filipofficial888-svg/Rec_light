// RecLight - společný protokol ESP-NOW mezi panelem (ESP32 u PC) a ceduli (ESP32-C3).
// POZOR: tento soubor musí být v obou skečích (reclight_panel i reclight_sign) STEJNÝ.
#pragma once
#include <stdint.h>
#include <stddef.h>

#define RL_PROTO_VER 1
#define RL_NET_ID    1      // identifikátor "sítě": změňte v OBOU skečích, pokud by se rušily dva systémy vedle sebe
#define RL_CHANNEL   1      // Wi-Fi kanál ESP-NOW, musí být v obou stejný (1 až 13)

enum { RL_TYPE_CMD = 1, RL_TYPE_STATUS = 2 };
enum { RL_FX_SOLID = 0, RL_FX_BLINK = 1, RL_FX_PULSE = 2 };

// 14 bajtů. CMD (panel -> cedule): barva + jas + efekt. STATUS (cedule -> panel): napětí baterie v mV.
struct __attribute__((packed)) RLPacket {
  uint8_t magic[2];   // 'R','L'
  uint8_t ver;
  uint8_t net;
  uint8_t type;
  uint8_t seq;
  uint8_t r, g, b;    // barva 0..255
  uint8_t bright;     // jas 0..100 %
  uint8_t effect;     // RL_FX_*
  uint16_t aux;       // STATUS: napětí baterie v mV (0 = neměří se)
  uint8_t crc;
};

inline uint8_t rlCrc(const RLPacket* p) {
  const uint8_t* b = (const uint8_t*)p;
  uint8_t c = 0x5A;
  for (size_t i = 0; i + 1 < sizeof(RLPacket); i++) c = (uint8_t)(((c << 1) | (c >> 7)) ^ b[i]);
  return c;
}

inline void rlSeal(RLPacket* p, uint8_t type) {
  p->magic[0] = 'R';
  p->magic[1] = 'L';
  p->ver = RL_PROTO_VER;
  p->net = RL_NET_ID;
  p->type = type;
  p->crc = rlCrc(p);
}

// vrátí true, pokud jde o platný paket naší sítě
inline bool rlParse(const uint8_t* data, int len, RLPacket* out) {
  if (len != (int)sizeof(RLPacket)) return false;
  const RLPacket* p = (const RLPacket*)data;
  if (p->magic[0] != 'R' || p->magic[1] != 'L' || p->ver != RL_PROTO_VER || p->net != RL_NET_ID) return false;
  if (p->crc != rlCrc(p)) return false;
  *out = *p;
  return true;
}

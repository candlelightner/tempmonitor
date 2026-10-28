#pragma once
#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>
#include <cstdint>

// Pure safety/curve functions used by ESPHome lambdas and the native C++ tests.
namespace tempmonitor {
constexpr uint32_t TELEMETRY_TIMEOUT_MS = 22000;
constexpr uint32_t ENVIRONMENT_TIMEOUT_MS = 20000;

inline bool fresh(uint32_t now, uint32_t received, bool seen, uint32_t limit) {
  return seen && uint32_t(now - received) <= limit;
}

inline bool valid_temperature(float value, float low, float high) {
  return std::isfinite(value) && value >= low && value <= high;
}

inline bool valid_curve(const float *temperatures, const int *duties, size_t count) {
  if (count != 4) return false;
  for (size_t i = 0; i < count; ++i) {
    if (!valid_temperature(temperatures[i], 0, 80) || duties[i] < 0 || duties[i] > 100) return false;
    if (i && (temperatures[i] <= temperatures[i - 1] || duties[i] < duties[i - 1])) return false;
  }
  return duties[3] == 100;
}

inline int curve_duty(float temperature, const float *temperatures, const int *duties) {
  if (!std::isfinite(temperature) || !valid_curve(temperatures, duties, 4)) return 100;
  if (temperature <= temperatures[0]) return duties[0];
  for (int i = 1; i < 4; ++i) {
    if (temperature <= temperatures[i]) {
      const float portion = (temperature - temperatures[i - 1]) / (temperatures[i] - temperatures[i - 1]);
      return int(std::ceil(duties[i - 1] + portion * (duties[i] - duties[i - 1])));
    }
  }
  return 100;
}

inline uint32_t override_remaining(uint32_t now, uint32_t until) {
  const int32_t remaining = int32_t(until - now);
  return remaining > 0 ? uint32_t(remaining + 999) / 1000 : 0;
}

inline int target_duty(bool healthy, float temperature, const float *temperatures, const int *duties,
                       int manual_duty, bool manual_active, int minimum_duty) {
  if (!healthy || !std::isfinite(temperature) || temperature >= 80 || minimum_duty < 5 || minimum_duty > 100) return 100;
  const int automatic = curve_duty(temperature, temperatures, duties);
  const int target = manual_active ? std::max(automatic, std::clamp(manual_duty, 0, 100)) : automatic;
  return target > 0 ? std::max(target, minimum_duty) : 0;
}

struct StartupBoost {
  bool running = false;
  uint32_t started = 0;
  int apply(uint32_t now, int target) {
    if (target <= 0) { running = false; return 0; }
    if (!running) { started = now; running = true; }
    return uint32_t(now - started) < 2000 ? 100 : target;
  }
};
}  // namespace tempmonitor

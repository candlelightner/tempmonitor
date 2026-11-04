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
// Data older than the nominal timeouts ramps fans up; older than this forces 100%.
constexpr uint32_t STALE_FAILSAFE_MS = 45000;
constexpr int GRACE_RAMP_PERCENT_PER_S = 4;

enum class Mode : uint8_t { Normal, Grace, Failsafe };

struct Assessment {
  Mode mode;
  uint32_t excess_ms;  // Largest age beyond its nominal timeout; 0 unless Grace.
};

// Callers pass hard_fault for OTA, unseen/invalid data, invalid curves and >=80°C.
inline Assessment assess(uint32_t now, bool hard_fault, uint32_t gpu_received, uint32_t environment_received,
                         bool external_required, uint32_t external_received) {
  if (hard_fault) return {Mode::Failsafe, 0};
  uint32_t excess = 0;
  const auto within = [&](uint32_t received, uint32_t nominal) {
    const uint32_t age = uint32_t(now - received);
    if (age > nominal) excess = std::max(excess, age - nominal);
    return age <= STALE_FAILSAFE_MS;
  };
  if (!within(gpu_received, TELEMETRY_TIMEOUT_MS) || !within(environment_received, ENVIRONMENT_TIMEOUT_MS) ||
      (external_required && !within(external_received, ENVIRONMENT_TIMEOUT_MS))) return {Mode::Failsafe, 0};
  return {excess ? Mode::Grace : Mode::Normal, excess};
}

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

struct FanControl {
  float temperature = NAN;
  int duty = 100;
  bool decreasing = false;
  uint32_t below_since = 0;
  bool grace = false;
  int grace_base = 0;

  int apply(uint32_t now, Mode mode, uint32_t excess_ms, float raw_temperature, const float *temperatures,
            const int *duties, int manual_duty, bool manual_active, int minimum_duty) {
    int target = 100;
    if (mode == Mode::Failsafe || !std::isfinite(raw_temperature) || raw_temperature >= 80) {
      temperature = NAN;
      grace = false;
    } else {
      // A falling reading must clear 5°C before reducing the curve's temperature.
      temperature = std::isfinite(temperature)
        ? std::clamp(temperature, raw_temperature, raw_temperature + 5.0f) : raw_temperature;
      target = target_duty(true, temperature, temperatures, duties, manual_duty, manual_active, minimum_duty);
      if (mode == Mode::Grace) {
        if (!grace) { grace_base = duty; grace = true; }
        // Ramp from data age, not call count, and never cool down while data is late.
        const uint32_t ramp = (std::min(excess_ms, uint32_t(100000)) * GRACE_RAMP_PERCENT_PER_S + 999) / 1000;
        int late = std::min(100, grace_base + int(ramp));
        if (late > 0) late = std::min(100, std::max(late, minimum_duty));
        target = std::max({target, late, duty});
      } else {
        grace = false;
      }
    }
    if (target >= duty) {
      duty = target;
      decreasing = false;
    } else {
      if (!decreasing) { below_since = now; decreasing = true; }
      const uint32_t elapsed = uint32_t(now - below_since);
      if (elapsed >= 61000) {
        const uint32_t steps = (elapsed - 60000) / 1000;
        duty = std::max(target, duty - int(std::min(steps, uint32_t(100))));
        // Never creep below the calibrated running minimum on the way to stopping.
        if (duty > 0 && duty < minimum_duty) duty = target == 0 ? 0 : minimum_duty;
        below_since += steps * 1000;
      }
    }
    return duty;
  }

  // Shorthand without comms grace: healthy is Normal, otherwise Failsafe.
  int apply(uint32_t now, bool healthy, float raw_temperature, const float *temperatures,
            const int *duties, int manual_duty, bool manual_active, int minimum_duty) {
    return apply(now, healthy ? Mode::Normal : Mode::Failsafe, 0, raw_temperature, temperatures, duties,
                 manual_duty, manual_active, minimum_duty);
  }
};

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

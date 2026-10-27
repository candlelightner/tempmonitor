#include "../esphome/control.h"
#include <cassert>
#include <limits>

int main() {
  using namespace tempmonitor;
  const float t[] = {35, 45, 60, 75};
  const int d[] = {0, 35, 70, 100};
  assert(valid_curve(t, d, 4));
  assert(!valid_curve(t, d, 3));
  assert(curve_duty(20, t, d) == 0);
  assert(curve_duty(35, t, d) == 0);
  assert(curve_duty(40, t, d) == 18);
  assert(curve_duty(45, t, d) == 35);
  assert(curve_duty(75, t, d) == 100);
  assert(curve_duty(90, t, d) == 100);
  assert(curve_duty(std::numeric_limits<float>::quiet_NaN(), t, d) == 100);
  assert(target_duty(false, 20, t, d, 0, true, 25) == 100);
  assert(target_duty(true, 80, t, d, 0, true, 25) == 100);
  assert(target_duty(true, 45, t, d, 0, true, 25) == 35);
  assert(target_duty(true, 30, t, d, 70, true, 25) == 70);
  assert(target_duty(true, 30, t, d, 70, false, 25) == 0);
  assert(target_duty(true, 36, t, d, 0, false, 35) == 35);
  assert(target_duty(true, 36, t, d, 0, false, 0) == 100);
  StartupBoost boost;
  assert(boost.apply(1000, 35) == 100);
  assert(boost.apply(2999, 35) == 100);
  assert(boost.apply(3000, 35) == 35);
  assert(boost.apply(3500, 0) == 0);
  assert(boost.apply(4000, 25) == 100);
  StartupBoost rollover;
  assert(rollover.apply(UINT32_MAX - 1000, 25) == 100);
  assert(rollover.apply(999, 25) == 25);
  assert(!fresh(0, 0, false, TELEMETRY_TIMEOUT_MS));
  assert(fresh(22000, 0, true, TELEMETRY_TIMEOUT_MS));
  assert(!fresh(22001, 0, true, TELEMETRY_TIMEOUT_MS));
  assert(fresh(10, UINT32_MAX - 10, true, 22));
  assert(!fresh(10, UINT32_MAX - 10, true, 20));
  assert(override_remaining(1000, 6000) == 5);
  assert(override_remaining(6000, 6000) == 0);
  assert(override_remaining(7000, 6000) == 0);
  assert(override_remaining(UINT32_MAX - 1000, 999) == 2);
  const float invalid[] = {35, 35, 60, 75};
  assert(!valid_curve(invalid, d, 4));
  assert(curve_duty(20, invalid, d) == 100);
  assert(!valid_temperature(std::numeric_limits<float>::infinity(), -20, 85));
  assert(!valid_temperature(-21, -20, 85));
}

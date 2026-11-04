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

  FanControl control;
  assert(control.apply(0, true, 36, t, d, 0, false, 35) == 100);
  assert(control.apply(60000, true, 35, t, d, 0, false, 35) == 100);
  assert(control.apply(61000, true, 36, t, d, 0, false, 35) == 99);
  assert(control.apply(61000, true, 36, t, d, 0, false, 35) == 99); // Extra sensor callbacks cannot accelerate cooling.
  assert(control.apply(125000, true, 35, t, d, 0, false, 35) == 35);
  // Replay the deployed 35/36°C threshold oscillation for ten minutes.
  for (uint32_t now = 126000; now < 726000; now += 1000) {
    assert(control.apply(now, true, (now / 7000) % 2 ? 35 : 36, t, d, 0, false, 35) == 35);
  }
  assert(control.temperature == 36);
  FanControl idle = control;
  // The installed P40 reaches 32°C at minimum speed; keep running through that equilibrium.
  for (uint32_t now = 726000; now < 1026000; now += 1000) {
    assert(idle.apply(now, true, 32, t, d, 0, false, 35) == 35);
  }
  assert(control.apply(726000, true, 33, t, d, 0, false, 35) == 35);
  assert(control.apply(727000, true, 30, t, d, 0, false, 35) == 35);
  assert(control.apply(787000, true, 30, t, d, 0, false, 35) == 35);
  assert(control.apply(788000, true, 30, t, d, 0, false, 35) == 0);
  assert(control.apply(788000, true, 60, t, d, 0, false, 35) == 70);
  assert(control.apply(788000, true, 60, t, d, 90, true, 35) == 90);
  assert(control.apply(789000, true, 60, t, d, 0, false, 35) == 90);
  assert(control.apply(849000, true, 60, t, d, 0, false, 35) == 90);
  assert(control.apply(850000, true, 60, t, d, 0, false, 35) == 89);
  assert(control.apply(850000, false, 30, t, d, 0, false, 35) == 100);
  assert(control.apply(851000, true, 30, t, d, 0, false, 35) == 100);
  assert(control.apply(912000, true, 30, t, d, 0, false, 35) == 99);
  assert(control.apply(912000, true, 80, t, d, 0, true, 35) == 100);
  assert(control.apply(913000, true, std::numeric_limits<float>::quiet_NaN(), t, d, 0, false, 35) == 100);
  assert(control.apply(914000, true, 20, invalid, d, 0, false, 35) == 100);
  assert(control.apply(915000, true, 20, t, d, 0, false, 0) == 100);

  FanControl clock_wrap;
  const uint32_t start = UINT32_MAX - 30000;
  assert(clock_wrap.apply(start, true, 36, t, d, 0, false, 35) == 100);
  assert(clock_wrap.apply(start + 61000, true, 36, t, d, 0, false, 35) == 99);
  assert(clock_wrap.apply(start + 125000, true, 36, t, d, 0, false, 35) == 35);
  // A higher calibrated minimum takes effect immediately, including during a cooldown.
  assert(clock_wrap.apply(start + 126000, true, 36, t, d, 0, false, 50) == 50);

  // assess(): nominal timeouts give grace, 45 s or a hard fault gives failsafe.
  const uint32_t rx = 100000;
  auto a = assess(rx + 5000, false, rx, rx, false, 0);
  assert(a.mode == Mode::Normal && a.excess_ms == 0);
  a = assess(rx + TELEMETRY_TIMEOUT_MS, false, rx, rx + 10000, false, 0);
  assert(a.mode == Mode::Normal && a.excess_ms == 0);
  a = assess(rx + 23000, false, rx, rx + 10000, false, 0);
  assert(a.mode == Mode::Grace && a.excess_ms == 1000);
  a = assess(rx + STALE_FAILSAFE_MS, false, rx, rx + 40000, false, 0);
  assert(a.mode == Mode::Grace && a.excess_ms == 23000);
  assert(assess(rx + 45001, false, rx, rx + 40000, false, 0).mode == Mode::Failsafe);
  a = assess(rx + 21000, false, rx + 10000, rx, false, 0);
  assert(a.mode == Mode::Grace && a.excess_ms == 1000);
  assert(assess(rx + 45001, false, rx + 40000, rx, false, 0).mode == Mode::Failsafe);
  // The external reading only counts when its package requires it.
  assert(assess(rx + 30000, false, rx + 30000, rx + 30000, false, rx).mode == Mode::Normal);
  a = assess(rx + 30000, false, rx + 30000, rx + 30000, true, rx);
  assert(a.mode == Mode::Grace && a.excess_ms == 10000);
  assert(assess(rx + 45001, false, rx + 45000, rx + 45000, true, rx).mode == Mode::Failsafe);
  // The largest excess wins.
  a = assess(rx + 30000, false, rx, rx + 5000, true, rx + 2000);
  assert(a.mode == Mode::Grace && a.excess_ms == 8000);
  a = assess(rx, true, rx, rx, false, 0);
  assert(a.mode == Mode::Failsafe && a.excess_ms == 0);
  // Ages are wrap-safe across the millis() rollover.
  const uint32_t late = UINT32_MAX - 2000;
  assert(assess(late + 5000, false, late, late, true, late).mode == Mode::Normal);
  a = assess(late + 23000, false, late, late + 10000, false, 0);
  assert(a.mode == Mode::Grace && a.excess_ms == 1000);
  assert(assess(late + 45001, false, late, late + 40000, false, 0).mode == Mode::Failsafe);

  // Grace ramps 4%/s of excess from the duty on entry, idempotent per excess value.
  FanControl grace;
  for (uint32_t now = 0; now < 126000; now += 1000) grace.apply(now, Mode::Normal, 0, 36, t, d, 0, false, 35);
  assert(grace.duty == 35);
  assert(grace.apply(126000, Mode::Grace, 1, 36, t, d, 0, false, 35) == 36);
  assert(grace.apply(126000, Mode::Grace, 1000, 36, t, d, 0, false, 35) == 39);
  assert(grace.apply(126500, Mode::Grace, 1000, 36, t, d, 0, false, 35) == 39);
  assert(grace.apply(127000, Mode::Grace, 1000, 36, t, d, 0, false, 35) == 39);
  assert(grace.apply(127000, Mode::Grace, 2500, 36, t, d, 0, false, 35) == 45);
  // Lower excess (one source caught up) never lowers duty during grace.
  assert(grace.apply(128000, Mode::Grace, 500, 36, t, d, 0, false, 35) == 45);
  assert(grace.apply(129000, Mode::Grace, 16000, 36, t, d, 0, false, 35) == 99);
  assert(grace.apply(129000, Mode::Grace, 16250, 36, t, d, 0, false, 35) == 100);
  assert(grace.apply(130000, Mode::Grace, 23000, 36, t, d, 0, false, 35) == 100);
  // A hotter curve demand or manual override still wins during grace.
  FanControl hot;
  for (uint32_t now = 0; now < 126000; now += 1000) hot.apply(now, Mode::Normal, 0, 36, t, d, 0, false, 35);
  assert(hot.apply(126000, Mode::Grace, 1000, 60, t, d, 0, false, 35) == 70);
  assert(hot.apply(127000, Mode::Grace, 2000, 60, t, d, 90, true, 35) == 90);
  // Raw >=80 or NaN during grace, and Failsafe, force 100 immediately.
  FanControl raw;
  for (uint32_t now = 0; now < 126000; now += 1000) raw.apply(now, Mode::Normal, 0, 36, t, d, 0, false, 35);
  assert(raw.apply(126000, Mode::Grace, 1000, 80, t, d, 0, false, 35) == 100);
  assert(!raw.grace && std::isnan(raw.temperature));
  FanControl nan_grace = hot;
  assert(nan_grace.apply(128000, Mode::Grace, 1000, std::numeric_limits<float>::quiet_NaN(), t, d, 0, false, 35) == 100);
  FanControl failsafe;
  for (uint32_t now = 0; now < 126000; now += 1000) failsafe.apply(now, Mode::Normal, 0, 36, t, d, 0, false, 35);
  assert(failsafe.apply(126000, Mode::Failsafe, 0, 36, t, d, 0, false, 35) == 100);
  // A stopped channel ramps to its running minimum, never an uncalibrated duty.
  FanControl stopped;
  for (uint32_t now = 0; now < 200000; now += 1000) stopped.apply(now, Mode::Normal, 0, 20, t, d, 0, false, 35);
  assert(stopped.duty == 0);
  assert(stopped.apply(200000, Mode::Grace, 1000, 20, t, d, 0, false, 35) == 35);
  assert(stopped.apply(201000, Mode::Grace, 10000, 20, t, d, 0, false, 35) == 40);

  // Grace freezes a cooldown in progress; leaving it restarts the 60 s hold.
  FanControl frozen;
  assert(frozen.apply(0, Mode::Normal, 0, 36, t, d, 0, false, 35) == 100);
  assert(frozen.apply(65000, Mode::Normal, 0, 36, t, d, 0, false, 35) == 95);
  assert(frozen.apply(66000, Mode::Grace, 1, 36, t, d, 0, false, 35) == 96);
  for (uint32_t now = 67000; now < 90000; now += 1000) {
    assert(frozen.apply(now, Mode::Grace, 1, 36, t, d, 0, false, 35) == 96);
  }
  assert(frozen.apply(90000, Mode::Normal, 0, 36, t, d, 0, false, 35) == 96);
  assert(frozen.apply(150000, Mode::Normal, 0, 36, t, d, 0, false, 35) == 96);
  assert(frozen.apply(151000, Mode::Normal, 0, 36, t, d, 0, false, 35) == 95);

  // End to end: a 30 s GPU gap from 35% idle stays well below 100, then cools normally.
  FanControl gap;
  uint32_t gpu_rx = 0, env_rx = 0;
  for (uint32_t now = 0; now < 200000; now += 1000) {
    if (now % 5000 == 0) gpu_rx = env_rx = now;
    const auto s = assess(now, false, gpu_rx, env_rx, false, 0);
    assert(s.mode == Mode::Normal);
    gap.apply(now, s.mode, s.excess_ms, 36, t, d, 0, false, 35);
  }
  assert(gap.duty == 35);
  const uint32_t last_gpu = gpu_rx;
  int peak = 0;
  for (uint32_t now = 200000; now <= last_gpu + 30000; now += 1000) {
    if (now % 5000 == 0) env_rx = now;
    const auto s = assess(now, false, last_gpu, env_rx, false, 0);
    assert(s.mode != Mode::Failsafe);
    peak = gap.apply(now, s.mode, s.excess_ms, 36, t, d, 0, false, 35);
    assert(peak < 100 && peak <= 35 + 32);
  }
  assert(peak == 35 + 32);
  // Fresh GPU data: 60 s hold at the grace peak, then 1%/s back to idle.
  uint32_t now = last_gpu + 31000;
  gpu_rx = now;
  for (; now <= last_gpu + 31000 + 60000; now += 1000) {
    if (now % 5000 == 0) gpu_rx = env_rx = now;
    const auto s = assess(now, false, gpu_rx, env_rx, false, 0);
    assert(s.mode == Mode::Normal);
    assert(gap.apply(now, s.mode, s.excess_ms, 36, t, d, 0, false, 35) == 67);
  }
  for (int expected = 66; expected >= 35; --expected, now += 1000) {
    if (now % 5000 == 0) gpu_rx = env_rx = now;
    const auto s = assess(now, false, gpu_rx, env_rx, false, 0);
    assert(gap.apply(now, s.mode, s.excess_ms, 36, t, d, 0, false, 35) == expected);
  }
  assert(gap.apply(now + 10000, Mode::Normal, 0, 36, t, d, 0, false, 35) == 35);
}

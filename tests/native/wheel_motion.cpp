#include "WheelMotion.h"
#include <cassert>

using inputbridge::WheelMotion;
using inputbridge::WheelOptions;

int main()
{
  using inputbridge::physicalWheel;
  assert(physicalWheel(false, false, false, false));
  assert(!physicalWheel(true, false, false, false));
  assert(!physicalWheel(false, true, false, false));
  assert(!physicalWheel(false, false, true, false));
  assert(!physicalWheel(false, false, false, true));
  const WheelOptions away{WheelOptions::AwayY | (32 << 6) | (170 << 13) | (18 << 22)};
  assert(away.away() && !away.local());
  assert(away.pixels(1) == 54 && away.pixels(-1) == -54);
  assert(inputbridge::directed(120, away.has(WheelOptions::AwayY)) == -120);
  assert(inputbridge::directed(120, away.has(WheelOptions::AwayX)) == 120);

  // A mouse press disables smoothing, not the configured direction.
  using inputbridge::WheelHandling;
  using inputbridge::wheelHandling;
  assert(wheelHandling(true, false, false) == WheelHandling::AllowSmoothing);
  assert(wheelHandling(true, false, true) == WheelHandling::DirectionOnly);
  assert(wheelHandling(true, true, true) == WheelHandling::Native);
  assert(wheelHandling(false, false, true) == WheelHandling::Native);

  // Smooth without changing total distance or sign on either axis.
  WheelMotion wheel;
  const double start = 1;
  wheel.push(32, -64, start, away.duration());
  int sumX = 0, sumY = 0, frames = 0;
  for (int i = 0; i < 20; ++i) {
    const auto frame = wheel.frame(start + i/60.0);
    assert(frame.x >= 0 && frame.y <= 0);
    sumX += frame.x;
    sumY += frame.y;
    frames += frame.x || frame.y;
  }
  assert(sumX == 32 && sumY == -64 && frames > 2 && !wheel.active());

  // A reversal discards the old tail on that axis immediately.
  wheel.push(40, 60, 2, 0.18);
  (void)wheel.frame(2.01);
  wheel.push(-30, 0, 2.03, 0.18);
  const auto reversed = wheel.frame(2.04);
  assert(reversed.x < 0 && reversed.y > 0);
  const auto stale = wheel.frame(4);
  assert(!stale.x && !stale.y && !wheel.active());
}

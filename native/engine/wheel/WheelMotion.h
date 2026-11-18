// SPDX-License-Identifier: MIT
// InputBridge wheel policy and distance-based easing. No platform dependencies.
#pragma once
#include <algorithm>
#include <cmath>
#include <cstdint>

namespace inputbridge {
struct WheelOptions {
  enum Flag : uint32_t {
    SmoothY = 1, SmoothX = 2, ReverseY = 4, ReverseX = 8, AwayY = 16, AwayX = 32
  };
  uint32_t bits;
  bool has(Flag flag) const { return (bits & flag) != 0; }
  bool local() const { return (bits & 15) != 0; }
  bool away() const { return (bits & 48) != 0; }
  int step() const { return std::clamp(static_cast<int>((bits >> 6) & 127), 8, 100); }
  int speed() const { return std::clamp(static_cast<int>((bits >> 13) & 511), 25, 300); }
  double duration() const { return std::clamp(static_cast<int>((bits >> 22) & 63), 6, 50) / 100.0; }
  int pixels(int lines) const { return std::clamp(lines, -24, 24) * step() * speed() / 100; }
};

inline bool physicalWheel(bool continuous, bool phase, bool momentum, bool synthesized)
{
  return !continuous && !phase && !momentum && !synthesized;
}

inline int directed(int value, bool flip)
{
  return flip ? -std::max(value, INT32_MIN + 1) : value;
}

class WheelMotion {
public:
  void push(int x, int y, double now, double duration)
  {
    // A new gesture after a pause must not replay a preceding gesture's tail.
    if (now - m_last > 0.15)
      reset();
    add(m_x, x, now + duration);
    add(m_y, y, now + duration);
    m_last = now;
  }

  struct Frame { int x, y; };
  Frame frame(double now)
  {
    if (now - m_last > 0.65) {
      reset();
      return {};
    }
    return {take(m_x, now), take(m_y, now)};
  }
  bool active() const { return m_x.pending || m_y.pending; }
  void reset() { m_x = {}; m_y = {}; }

private:
  struct Axis { int pending = 0; double until = 0; };
  static void add(Axis &axis, int delta, double until)
  {
    if (!delta) return;
    if ((axis.pending > 0 && delta < 0) || (axis.pending < 0 && delta > 0))
      axis.pending = 0;
    axis.pending = static_cast<int>(std::clamp(int64_t(axis.pending) + delta, int64_t(-960), int64_t(960)));
    axis.until = until;
  }
  static int take(Axis &axis, double now)
  {
    if (!axis.pending) return 0;
    const double remaining = axis.until - now;
    const int magnitude = std::abs(axis.pending);
    const int eased = remaining <= 0 ? magnitude :
        std::clamp(static_cast<int>(std::ceil(magnitude * std::min(1.0, 0.048 / remaining))), 1, magnitude);
    const int value = axis.pending > 0 ? eased : -eased;
    axis.pending -= value;
    return value;
  }
  Axis m_x, m_y;
  double m_last = 0;
};
} // namespace inputbridge

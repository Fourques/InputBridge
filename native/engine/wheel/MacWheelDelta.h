// SPDX-License-Identifier: MIT
#pragma once
#include <ApplicationServices/ApplicationServices.h>

namespace inputbridge {
inline void reverseWheelAxis(CGEventRef event, CGEventField line, CGEventField pixels, CGEventField fixed)
{
  // Quartz updates the pixel and fixed-point values when the line value changes.
  const auto lines = CGEventGetIntegerValueField(event, line);
  const auto points = CGEventGetIntegerValueField(event, pixels);
  const auto fraction = CGEventGetDoubleValueField(event, fixed);
  CGEventSetIntegerValueField(event, line, -lines);
  CGEventSetIntegerValueField(event, pixels, -points);
  CGEventSetDoubleValueField(event, fixed, -fraction);
}
} // namespace inputbridge

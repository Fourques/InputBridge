#include "MacWheelDelta.h"
#include <cassert>

void checkAxis(CGEventRef event, CGEventField line, CGEventField point, CGEventField fixed)
{
  const auto lines = CGEventGetIntegerValueField(event, line);
  const auto points = CGEventGetIntegerValueField(event, point);
  const auto fraction = CGEventGetDoubleValueField(event, fixed);
  assert(lines && points && fraction);
  inputbridge::reverseWheelAxis(event, line, point, fixed);
  assert(CGEventGetIntegerValueField(event, line) == -lines);
  assert(CGEventGetIntegerValueField(event, point) == -points);
  assert(CGEventGetDoubleValueField(event, fixed) == -fraction);
}

void checkEvent(CGScrollEventUnit unit)
{
  auto event = CGEventCreateScrollWheelEvent2(nullptr, unit, 2, 5, -7, 0);
  assert(event);
  checkAxis(event, kCGScrollWheelEventDeltaAxis1, kCGScrollWheelEventPointDeltaAxis1,
            kCGScrollWheelEventFixedPtDeltaAxis1);
  checkAxis(event, kCGScrollWheelEventDeltaAxis2, kCGScrollWheelEventPointDeltaAxis2,
            kCGScrollWheelEventFixedPtDeltaAxis2);
  CFRelease(event);
}

int main()
{
  checkEvent(kCGScrollEventUnitLine);
  checkEvent(kCGScrollEventUnitPixel);
}

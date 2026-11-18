// SPDX-License-Identifier: MIT
// Runs on InputBridge's existing Quartz event-tap thread. No second input tap.
#pragma once
#include "InputBridgeWheelMotion.h"
#include "deskflow/ipc/CoreIpcServer.h"
#include <ApplicationServices/ApplicationServices.h>
#include <atomic>
#include <chrono>

namespace inputbridge {
class MacWheel {
public:
  static constexpr int64_t tag = 0x4942574cLL << 32;
  static bool generated(CGEventRef event)
  {
    return (value(event, kCGEventSourceUserData) >> 32) == (tag >> 32);
  }
  static bool sameFocus(CGEventRef event, unsigned int generation)
  {
    return value(event, kCGEventSourceUserData) == (tag | generation);
  }

  static bool eligible(CGEventRef event)
  {
    return event && physicalWheel(
        value(event, kCGScrollWheelEventIsContinuous) != 0,
        value(event, kCGScrollWheelEventScrollPhase) != 0,
        value(event, kCGScrollWheelEventMomentumPhase) != 0,
        value(event, kCGEventSourceUnixProcessID) != 0 || generated(event));
  }

  static bool gestureInProgress(CGEventRef event)
  {
    const auto modifiers = kCGEventFlagMaskControl | kCGEventFlagMaskCommand |
                           kCGEventFlagMaskAlternate | kCGEventFlagMaskShift;
    return (CGEventGetFlags(event) & modifiers) ||
           CGEventSourceButtonState(kCGEventSourceStateHIDSystemState, kCGMouseButtonLeft) ||
           CGEventSourceButtonState(kCGEventSourceStateHIDSystemState, kCGMouseButtonRight);
  }

  // Returns true if the raw event has been replaced by scheduled pixel events.
  bool local(CGEventRef event, WheelOptions options, const std::atomic_bool &onMac,
             const std::atomic_uint &epoch)
  {
    const auto x = static_cast<int32_t>(value(event, kCGScrollWheelEventDeltaAxis2));
    const auto y = static_cast<int32_t>(value(event, kCGScrollWheelEventDeltaAxis1));
    const bool smoothX = x && options.has(WheelOptions::SmoothX);
    const bool smoothY = y && options.has(WheelOptions::SmoothY);
    const auto generation = epoch.load();
    // Mixed-axis events with an unsmoothed axis stay in their original units.
    if ((smoothX || smoothY) && (!x || smoothX) && (!y || smoothY) &&
        !value(event, kCGScrollWheelEventDeltaAxis3)) {
      if (m_bits != options.bits || m_epoch != generation)
        cancel();
      if (start()) {
        m_onMac = &onMac;
        m_focus = &epoch;
        m_epoch = generation;
        m_bits = options.bits;
        m_location = CGEventGetLocation(event);
        m_flags = CGEventGetFlags(event);
        m_motion.push(options.pixels(directed(x, options.has(WheelOptions::ReverseX))),
                      options.pixels(directed(y, options.has(WheelOptions::ReverseY))),
                      seconds(), options.duration());
        return true;
      }
    }
    cancel();
    if (options.has(WheelOptions::ReverseY))
      reverse(event, kCGScrollWheelEventDeltaAxis1, kCGScrollWheelEventPointDeltaAxis1,
              kCGScrollWheelEventFixedPtDeltaAxis1);
    if (options.has(WheelOptions::ReverseX))
      reverse(event, kCGScrollWheelEventDeltaAxis2, kCGScrollWheelEventPointDeltaAxis2,
              kCGScrollWheelEventFixedPtDeltaAxis2);
    return false;
  }

  void cancel()
  {
    m_motion.reset();
    if (m_timer) {
      CFRunLoopTimerInvalidate(m_timer);
      CFRelease(m_timer);
      m_timer = nullptr;
    }
  }

private:
  static int64_t value(CGEventRef event, CGEventField field)
  {
    return CGEventGetIntegerValueField(event, field);
  }
  static void reverse(CGEventRef event, CGEventField line, CGEventField pixels, CGEventField fixed)
  {
    CGEventSetIntegerValueField(event, line, -value(event, line));
    CGEventSetIntegerValueField(event, pixels, -value(event, pixels));
    CGEventSetDoubleValueField(event, fixed, -CGEventGetDoubleValueField(event, fixed));
  }
  static double seconds()
  {
    using namespace std::chrono;
    return duration<double>(steady_clock::now().time_since_epoch()).count();
  }
  bool start()
  {
    if (m_timer) return true;
    CFRunLoopTimerContext context = {0, this, nullptr, nullptr, nullptr};
    m_timer = CFRunLoopTimerCreate(kCFAllocatorDefault, CFAbsoluteTimeGetCurrent() + 1.0/120,
                                 1.0/60, 0, 0, [](CFRunLoopTimerRef, void *ptr) {
      static_cast<MacWheel *>(ptr)->tick();
    }, &context);
    if (!m_timer) return false;
    CFRunLoopAddTimer(CFRunLoopGetCurrent(), m_timer, kCFRunLoopDefaultMode);
    return true;
  }
  void tick()
  {
    if (!m_onMac->load() || m_focus->load() != m_epoch ||
        deskflow::core::ipc::CoreIpcServer::instance().wheelOptions() != m_bits) {
      cancel();
      return;
    }
    const auto frame = m_motion.frame(seconds());
    if (frame.x || frame.y) {
      auto event = CGEventCreateScrollWheelEvent2(nullptr, kCGScrollEventUnitPixel, 2, frame.y, frame.x, 0);
      if (event) {
        CGEventSetLocation(event, m_location);
        CGEventSetFlags(event, m_flags);
        CGEventSetIntegerValueField(event, kCGEventSourceUserData, tag | m_epoch);
        CGEventPost(kCGHIDEventTap, event);
        CFRelease(event);
      }
    }
    if (!m_motion.active())
      cancel();
  }

  WheelMotion m_motion;
  CFRunLoopTimerRef m_timer = nullptr;
  const std::atomic_bool *m_onMac = nullptr;
  const std::atomic_uint *m_focus = nullptr;
  uint32_t m_bits = 0;
  unsigned int m_epoch = 0;
  CGPoint m_location{};
  CGEventFlags m_flags{};
};
} // namespace inputbridge

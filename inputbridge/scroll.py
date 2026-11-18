"""Mac wheel settings: local feel and outgoing direction are independent."""
import json

from .security import atomic_json

FLAGS = ('smooth_vertical', 'smooth_horizontal', 'reverse_vertical',
         'reverse_horizontal', 'remote_reverse_vertical', 'remote_reverse_horizontal')
LIMITS = {'step': (8, 100), 'speed': (25, 300), 'duration_ms': (60, 500)}
DEFAULTS = dict.fromkeys(FLAGS, False)
DEFAULTS.update(step=32, speed=100, duration_ms=180)


def sanitize(value):
    result = DEFAULTS.copy()
    if not isinstance(value, dict):
        return result
    for key in FLAGS:
        if type(value.get(key)) is bool:
            result[key] = value[key]
    for key, (low, high) in LIMITS.items():
        v = value.get(key)
        if type(v) is int and low <= v <= high:
            result[key] = v
    result['duration_ms'] = 10 * round(result['duration_ms'] / 10)
    return result


def load(profile):
    try:
        return sanitize(json.loads((profile / 'scroll.json').read_text()))
    except (OSError, ValueError):
        return DEFAULTS.copy()


def save(profile, value):
    result = sanitize(value)
    atomic_json(profile / 'scroll.json', result)
    return result


def encode(value, local=True):
    s = sanitize(value)
    flags = sum(1 << index for index, name in enumerate(FLAGS) if s[name])
    if not local:
        flags &= ~15
    return flags | s['step'] << 6 | s['speed'] << 13 | (s['duration_ms'] // 10) << 22

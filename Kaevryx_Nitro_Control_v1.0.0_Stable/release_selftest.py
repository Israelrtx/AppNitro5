#!/usr/bin/env python3
from knc_common import percent_to_pwm

tests = [
    (30, 77),
    (50, 128),
    (100, 255),
]

for percent, expected_min in tests:
    value = percent_to_pwm(percent, 77)
    assert 0 <= value <= 255
    if percent == 30:
        assert value >= expected_min

print("SELFTEST OK")

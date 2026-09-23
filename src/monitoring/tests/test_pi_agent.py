"""Pi sensor agent: SGP40 CRC/compensation, null-on-failure, init backoff, exit codes.

Imports the agent without smbus (it is absent on the control PC) and drives
the sensor classes with fake buses; no I2C bus or network is touched.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pi"))

import pi_sensor_agent as agent  # noqa: E402


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)


# --- SGP40 -------------------------------------------------------------------

def test_crc8_datasheet_example():
    assert agent.sgp40_crc8([0xBE, 0xEF]) == 0x92


def test_default_compensation_matches_datasheet_constants():
    # 50 %RH -> 0x8000 (CRC 0xA2), 25 degC -> 0x6666 (CRC 0x93)
    assert agent.sgp40_measure_args() == [0x0F, 0x80, 0x00, 0xA2, 0x66, 0x66, 0x93]
    assert agent.sgp40_measure_args(None, float("nan")) == agent.sgp40_measure_args()


def test_measured_compensation_is_encoded_with_crcs():
    args = agent.sgp40_measure_args(humidity=25.0, temperature=40.0)
    rh = round(25.0 * 65535 / 100)
    t = round((40.0 + 45.0) * 65535 / 175)
    assert args[1:3] == [rh >> 8, rh & 0xFF] and args[3] == agent.sgp40_crc8(args[1:3])
    assert args[4:6] == [t >> 8, t & 0xFF] and args[6] == agent.sgp40_crc8(args[4:6])


def test_compensation_is_clamped():
    assert agent.sgp40_measure_args(150.0, 500.0)[1:3] == [0xFF, 0xFF]


def test_reply_crc_is_checked():
    good = [0x75, 0x30, agent.sgp40_crc8([0x75, 0x30])]
    assert agent.sgp40_parse_reply(good) == 0x7530
    assert agent.sgp40_parse_reply([0x75, 0x30, good[2] ^ 0x01]) is None
    assert agent.sgp40_parse_reply([0x75]) is None


class _SgpBus:
    def __init__(self, reply):
        self.reply = reply
        self.written = []

    def write_i2c_block_data(self, addr, reg, data):
        self.written.append((reg, list(data)))

    def read_i2c_block_data(self, addr, reg, n):
        return list(self.reply)


def test_sgp40_uses_measured_humidity_and_drops_bad_crc():
    bus = _SgpBus([0x75, 0x30, agent.sgp40_crc8([0x75, 0x30])])
    sgp = agent.SGP40(bus)
    assert sgp.read_voc(humidity=30.0, temperature=22.0) == 0x7530
    assert bus.written[-1] == (0x26, agent.sgp40_measure_args(30.0, 22.0))
    bus.reply = [0x75, 0x30, 0x00]
    assert sgp.read_voc() is None      # corrupted: not the previous value, not 0


# --- null on failure, ok flags ----------------------------------------------

class _DeadBus:
    def __getattr__(self, name):
        def fail(*a, **k):
            raise OSError(121, "Remote I/O error")
        return fail


def test_failed_reads_are_none_not_zero():
    bus = _DeadBus()
    assert agent.BME280(bus).read_data() == (None, None)
    assert agent.TSL25911(bus).read_raw_ch0() is None
    assert agent.ICM20948(bus).read_accel_gyro() == (None, None)
    assert agent.LTR390(bus).read_uv() is None
    assert agent.SGP40(bus).read_voc() is None


def test_build_message_nulls_and_ok_flags():
    msg = agent.build_message("pi-a", "BME280 (T/H)", None, None, None, None, 812, None, 30000)
    assert json.loads(json.dumps(msg)) == msg
    assert msg["temp"] is None and msg["accel"] is None and msg["lux_raw"] == 812
    assert "lux" not in msg
    assert msg["ok"] == {"bme280": False, "tsl25911": True, "icm20948": False,
                         "ltr390": False, "sgp40": True}


# --- init retry with backoff -------------------------------------------------

class _FlakyBus:
    """Fails the first ``fail_times`` writes, then works."""

    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.calls = 0

    def write_byte_data(self, addr, reg, value):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise OSError("not there yet")

    def read_byte_data(self, addr, reg):
        return 0x12

    def read_i2c_block_data(self, addr, reg, n):
        return [0] * n


def test_init_retries_with_exponential_backoff():
    now = [0.0]
    bus = _FlakyBus(fail_times=3)
    s = agent.TSL25911(bus, clock=lambda: now[0])
    assert not s.ready and bus.calls == 1
    assert s.read_raw_ch0() is None and bus.calls == 1      # inside the 1 s backoff
    now[0] = 1.0
    assert s.read_raw_ch0() is None and bus.calls == 2      # retry 2 fails -> 2 s
    now[0] = 2.5
    s.ensure_ready()
    assert bus.calls == 2                                   # still backing off
    now[0] = 3.0
    s.ensure_ready()
    assert bus.calls == 3                                   # retry 3 fails -> 4 s
    now[0] = 7.0
    assert s.read_raw_ch0() == 0x1212 and s.ready           # retry 4 succeeds


def test_backoff_is_capped():
    now = [0.0]
    s = agent.LTR390(_DeadBus(), clock=lambda: now[0])
    for _ in range(20):
        now[0] = s._next_try
        s.ensure_ready()
    assert s._backoff == agent._Sensor.BACKOFF_MAX


# --- start-up failures exit non-zero -----------------------------------------

def test_start_agent_without_smbus_returns_1(monkeypatch):
    monkeypatch.setattr(agent, "smbus", None)
    assert agent.start_agent("192.0.2.1", 50001, 0.1) == 1


def test_start_agent_bus_open_failure_returns_1(monkeypatch):
    class _Smbus:
        @staticmethod
        def SMBus(n):
            raise FileNotFoundError("/dev/i2c-1")
    monkeypatch.setattr(agent, "smbus", _Smbus)
    assert agent.start_agent("192.0.2.1", 50001, 0.1) == 1


def test_start_agent_no_sensor_answers_returns_1(monkeypatch):
    class _Smbus:
        @staticmethod
        def SMBus(n):
            return _DeadBus()
    monkeypatch.setattr(agent, "smbus", _Smbus)
    assert agent.start_agent("192.0.2.1", 50001, 0.1) == 1


def test_no_bare_except_left():
    with open(agent.__file__, encoding="utf-8") as f:
        src = f.read()
    assert "except:" not in src


def test_auth_line():
    assert json.loads(agent.auth_line("abc")) == {"type": "auth", "token": "abc"}

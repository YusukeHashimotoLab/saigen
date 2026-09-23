"""BME280 compensation math of the Raspberry Pi sensor agent.

The agent imports ``smbus`` at module level but only prints a message when it is
missing, so it can be imported on the control PC. The compensation formulas are
pure functions and are exercised here without any I2C bus.

Reference values
----------------
* Temperature: the Bosch BMP280 datasheet (section 3.12, the same integer
  temperature formula as the BME280) gives dig_T1=27504, dig_T2=26435,
  dig_T3=-1000 and adc_T=519888, which yields t_fine=128422 and T=25.08 degC.
* Humidity: Bosch publishes no worked humidity example, so the calibration
  below is a typical set read from a real BME280 (dig_H1=75, dig_H2=362,
  dig_H3=0, dig_H4=313, dig_H5=50, dig_H6=30). With t_fine=128422 and
  adc_H=30000 the datasheet's floating-point formula (section 8.1,
  ``bme280_compensate_H_double``) gives 55.0007 %RH; the integer formula gives
  (v_x1 >> 12) = 56317, i.e. 56317 / 1024 = 54.9971 %RH. The test checks the
  integer result against an independent implementation of the double formula.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pi"))

import pi_sensor_agent as agent  # noqa: E402

T_CAL = (27504, 26435, -1000)
ADC_T = 519888
H_CAL = (75, 362, 0, 313, 50, 30)


def _humidity_double(adc_h, t_fine, h1, h2, h3, h4, h5, h6):
    """Bosch floating-point reference (datasheet section 8.1), independent of the agent."""
    var_h = t_fine - 76800.0
    var_h = (adc_h - (h4 * 64.0 + h5 / 16384.0 * var_h)) * (
        h2 / 65536.0 * (1.0 + h6 / 67108864.0 * var_h * (1.0 + h3 / 67108864.0 * var_h))
    )
    var_h = var_h * (1.0 - h1 * var_h / 524288.0)
    return min(100.0, max(0.0, var_h))


def test_temperature_matches_datasheet_example():
    t_fine, temp = agent.compensate_temperature(ADC_T, *T_CAL)
    assert t_fine == 128422
    assert temp == 2508  # 25.08 degC in 0.01 degC


def test_humidity_datasheet_style_example_is_percent_rh():
    t_fine, _ = agent.compensate_temperature(ADC_T, *T_CAL)
    humi = agent.compensate_humidity(30000, t_fine, *H_CAL)
    assert humi == pytest.approx(56317 / 1024.0)  # 54.997 %RH
    assert 40.0 < humi < 80.0
    assert humi == pytest.approx(_humidity_double(30000, t_fine, *H_CAL), abs=0.05)


@pytest.mark.parametrize("adc_h", [22000, 26000, 28000, 32000, 34000])
def test_humidity_agrees_with_float_reference(adc_h):
    t_fine, _ = agent.compensate_temperature(ADC_T, *T_CAL)
    assert agent.compensate_humidity(adc_h, t_fine, *H_CAL) == pytest.approx(
        _humidity_double(adc_h, t_fine, *H_CAL), abs=0.05
    )


def test_humidity_clamps_to_0_and_100():
    t_fine, _ = agent.compensate_temperature(ADC_T, *T_CAL)
    assert agent.compensate_humidity(0, t_fine, *H_CAL) == 0.0
    assert agent.compensate_humidity(65535, t_fine, *H_CAL) == 100.0


class _FakeBus:
    """Minimal SMBus stand-in returning fixed calibration and measurement bytes."""

    def __init__(self, calib_88, calib_a1, calib_e1, data_f7):
        self.blocks = {0x88: calib_88, 0xE1: calib_e1, 0xF7: data_f7}
        self.bytes = {0xD0: 0x60, 0xA1: calib_a1, 0xF3: 0x00}

    def write_byte_data(self, addr, reg, value):
        pass

    def read_byte_data(self, addr, reg):
        return self.bytes[reg]

    def read_i2c_block_data(self, addr, reg, length):
        return list(self.blocks[reg][:length])


def _le16(v):
    v &= 0xFFFF
    return [v & 0xFF, v >> 8]


def test_read_data_end_to_end_reports_percent_rh(monkeypatch):
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    h1, h2, h3, h4, h5, h6 = H_CAL
    calib_88 = _le16(T_CAL[0]) + _le16(T_CAL[1]) + _le16(T_CAL[2]) + [0] * 18
    calib_e1 = _le16(h2) + [h3, h4 >> 4, ((h5 & 0x0F) << 4) | (h4 & 0x0F), h5 >> 4, h6 & 0xFF]
    adc_h = 30000
    data_f7 = [0, 0, 0, (ADC_T >> 12) & 0xFF, (ADC_T >> 4) & 0xFF, (ADC_T & 0x0F) << 4,
               adc_h >> 8, adc_h & 0xFF]
    sensor = agent.BME280(_FakeBus(calib_88, h1, calib_e1, data_f7))
    temp, humi = sensor.read_data()
    assert temp == pytest.approx(25.08)
    assert humi == pytest.approx(56317 / 1024.0)

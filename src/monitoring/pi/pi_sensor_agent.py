"""Sensor agent for the Raspberry Pi Zero WH + Waveshare Environment Sensor HAT.

Reads the on-board I2C sensors and pushes a JSON line per sample over TCP to the
control PC running the dashboard (see ``src/monitoring/dashboard/launch_sensor_dashboard.py``).

Sensors read (I2C addresses on the default bus, ``/dev/i2c-1``):

    - BME280   0x76  temperature, humidity
    - TSL25911 0x29  illuminance (lux)
    - ICM20948 0x68  3-axis acceleration, 3-axis angular velocity
    - LTR390   0x53  UV index (UVI)
    - SGP40    0x59  VOC (raw signal, not converted to an index)

Note: the ICM20948 also contains an AK09916 magnetometer accessible through its
auxiliary I2C bus, but this agent does NOT read it. Only the accelerometer and
gyroscope of the ICM20948 are used.

Note on the UV channel: recordings made before 2026-09-08 were taken with the
LTR390 left in ALS mode, in which the UVS data registers never update, so their
``uvi`` column is 0 throughout. From this version the sensor is initialised in
UVS mode and ``uvi`` carries a UV index (float). The CSV column name is
unchanged, so old files stay parseable — only the semantics changed.

Note on humidity: before 2026-09-23 the BME280 humidity was divided by 1024
twice, so older recordings hold 0-0.098 instead of 0-100 %RH (multiply by 1024
to recover it, to a resolution of 1 %RH). See ``compensate_humidity``.

Dependencies are kept minimal on purpose (the Pi Zero has limited resources and
this script should run with nothing beyond the Python standard library plus
``smbus``/``smbus2``): no PyYAML, no third-party sensor drivers.

Configuration (environment variables, all optional except the host):

    SENSOR_AGENT_PC_HOST      Required. Hostname or IP address of the control PC.
                               There is no default — an unset value is treated as
                               a configuration error (see below), so a stale
                               address is never silently used.
    SENSOR_AGENT_PC_PORT      TCP port of the dashboard's sensor listener. Default: 50001.
    SENSOR_AGENT_INTERVAL_S   Delay between samples, in seconds. Default: 0.1.

Command-line arguments ``--host``, ``--port`` and ``--interval`` override the
corresponding environment variables. If neither an env var nor ``--host`` is
given, the agent prints an error and exits with status 1 instead of guessing an
address.

Usage:

    SENSOR_AGENT_PC_HOST=192.0.2.10 python3 pi_sensor_agent.py
    python3 pi_sensor_agent.py --host 192.0.2.10 --port 50001

Runs as a systemd service in normal operation; see ``sensor-agent.service`` and
``sensor-agent.env.example`` in this directory.
"""
import argparse
import json
import os
import socket
import struct
import sys
import time

try:
    import smbus
except ImportError:
    try:
        import smbus2 as smbus
    except ImportError:
        print("[!] Error: smbus or smbus2 library not found. Install with: sudo apt install python3-smbus")


def compensate_temperature(adc_t, dig_T1, dig_T2, dig_T3):
    """Bosch BME280 integer temperature compensation (datasheet 4.2.3).

    Returns ``(t_fine, temp)`` where ``temp`` is in 0.01 degC (5123 = 51.23 degC)
    and ``t_fine`` is the fine-resolution value the humidity formula needs.
    Pure integer math, no I/O, so it can be unit-tested without ``smbus``.
    """
    v1 = ((((adc_t >> 3) - (dig_T1 << 1))) * (dig_T2)) >> 11
    v2 = (((((adc_t >> 4) - (dig_T1)) * ((adc_t >> 4) - (dig_T1))) >> 12) * (dig_T3)) >> 14
    t_fine = v1 + v2
    temp = (t_fine * 5 + 128) >> 8
    return t_fine, temp


def compensate_humidity(adc_h, t_fine, dig_H1, dig_H2, dig_H3, dig_H4, dig_H5, dig_H6):
    """Bosch BME280 integer humidity compensation (datasheet 4.2.3), in %RH.

    The reference ``bme280_compensate_H_int32`` returns ``v_x1_u32r >> 12``, an
    unsigned Q22.10 value (47445 = 47445/1024 = 46.333 %RH), so %RH is that
    value divided by 1024. The clamp 419430400 = 100 * 2**22 is 100 %RH before
    the final shift. Before 2026-09-23 this code shifted by 22 (already whole
    %RH) and then also divided by 1024, recording 0-0.098 instead of 0-100.
    """
    vh = t_fine - 76800
    vh = (((((adc_h << 14) - (dig_H4 << 20) - (dig_H5 * vh)) + 16384) >> 15) *
          (((((((vh * dig_H6) >> 10) * (((vh * dig_H3) >> 11) + 32768)) >> 10) + 2097152) * dig_H2 + 8192) >> 14))
    vh = vh - (((((vh >> 15) * (vh >> 15)) >> 7) * dig_H1) >> 4)
    vh = 0 if vh < 0 else vh
    vh = 419430400 if vh > 419430400 else vh
    return (vh >> 12) / 1024.0


class BME280:
    """Bosch BME280 temperature/humidity sensor."""

    def __init__(self, bus, address=0x76):
        self.bus = bus
        self.address = address
        self.calib = []
        self.chip_id = 0
        self.model = "Unknown"
        self.load_calibration()

    def load_calibration(self):
        try:
            # 1. Reset the sensor
            self.bus.write_byte_data(self.address, 0xE0, 0xB6)
            time.sleep(0.1)

            # 2. Check the chip ID
            self.chip_id = self.bus.read_byte_data(self.address, 0xD0)
            if self.chip_id == 0x60: self.model = "BME280 (T/H)"
            elif self.chip_id == 0x58: self.model = "BMP280 (T only)"

            # 3. Read the calibration data
            c1 = self.bus.read_i2c_block_data(self.address, 0x88, 24)
            c2 = [self.bus.read_byte_data(self.address, 0xA1)]
            c3 = self.bus.read_i2c_block_data(self.address, 0xE1, 7)
            self.calib = c1 + c2 + c3
        except: pass

    def read_data(self):
        try:
            if not self.calib: return 0.0, 0.0

            # Write order matters for enabling humidity: 0xF2 must be written before 0xF4
            self.bus.write_byte_data(self.address, 0xF2, 0x01) # Hum x1
            self.bus.write_byte_data(self.address, 0xF4, 0x25) # Temp x1, Press x1, Forced mode

            # Wait for the measurement to finish (up to 50ms)
            for _ in range(10):
                status = self.bus.read_byte_data(self.address, 0xF3)
                if not (status & 0x08): break
                time.sleep(0.01)

            data = self.bus.read_i2c_block_data(self.address, 0xF7, 8)

            # --- Temperature ---
            adc_t = (data[3] << 12) | (data[4] << 4) | (data[5] >> 4)
            dig_T1 = (self.calib[1] << 8) | self.calib[0]
            dig_T2 = struct.unpack("<h", bytes(self.calib[2:4]))[0]
            dig_T3 = struct.unpack("<h", bytes(self.calib[4:6]))[0]

            t_fine, temp = compensate_temperature(adc_t, dig_T1, dig_T2, dig_T3)

            # --- Humidity ---
            humi = 0.0
            if self.chip_id == 0x60:
                adc_h = (data[6] << 8) | data[7]
                if adc_h != 0x8000: # 0x8000 means "not measured"
                    dig_H1 = self.calib[24]
                    dig_H2 = struct.unpack("<h", bytes(self.calib[25:27]))[0]
                    dig_H3 = self.calib[27]
                    dig_H4 = (self.calib[28] << 4) | (self.calib[29] & 0x0F)
                    dig_H5 = (self.calib[30] << 4) | (self.calib[29] >> 4)
                    dig_H6 = struct.unpack("<b", bytes([self.calib[31]]))[0]

                    humi = compensate_humidity(
                        adc_h, t_fine, dig_H1, dig_H2, dig_H3, dig_H4, dig_H5, dig_H6
                    )

            return temp / 100.0, humi
        except:
            return 0.0, 0.0


class TSL25911:
    """AMS TSL25911 ambient light sensor (illuminance)."""

    def __init__(self, bus, address=0x29):
        self.bus = bus; self.address = address
        try: self.bus.write_byte_data(self.address, 0xA0, 0x03)
        except: pass
    def read_lux(self):
        try:
            l, h = self.bus.read_byte_data(self.address, 0xB4), self.bus.read_byte_data(self.address, 0xB5)
            return (h << 8) | l
        except: return 0


class ICM20948:
    """TDK InvenSense ICM20948 IMU. Only accelerometer + gyroscope are read here
    (the embedded AK09916 magnetometer is NOT read by this agent)."""

    def __init__(self, bus, address=0x68):
        self.bus = bus; self.address = address
        try: self.bus.write_byte_data(self.address, 0x06, 0x01)
        except: pass
    def read_accel_gyro(self):
        try:
            d = self.bus.read_i2c_block_data(self.address, 0x2D, 12)
            ax, ay, az = struct.unpack(">hhh", bytes(d[0:6]))
            gx, gy, gz = struct.unpack(">hhh", bytes(d[6:12]))
            return (ax/16384.0, ay/16384.0, az/16384.0), (gx/131.0, gy/131.0, gz/131.0)
        except: return (0,0,0), (0,0,0)


class LTR390:
    """Lite-On LTR390 UV sensor, configured in UVS mode and reporting a UV index.

    Register setup (see the LTR390-UV-01 datasheet):

        MAIN_CTRL (0x00) = 0x0A   sensor enabled + UVS mode
                                  (0x02 would be enabled + ALS mode, in which
                                  the UVS data registers stay at 0)
        MEAS_RATE (0x04) = 0x05   20-bit resolution (400 ms conversion) with a
                                  1000 ms measurement rate
        GAIN      (0x05) = 0x01   gain 3

    UVS data is a 20-bit little-endian value in 0x10..0x12. The datasheet's
    sensitivity is 2300 counts per UVI at gain 18 / 20-bit, so at gain 3 the
    divisor is 2300 * 3/18.

    The conversion time (400 ms at 20-bit, with a 1000 ms measurement rate) is
    much longer than this agent's 100 ms sampling loop, so consecutive reads
    return the same conversion result; that is the sensor's real update rate,
    not a stall. Read errors return the previously reported value rather than
    dropping to 0, so a single I2C hiccup does not look like darkness.
    """

    _SENSITIVITY = 2300.0 * (3.0 / 18.0)   # counts per UVI at gain 3, 20-bit

    def __init__(self, bus, address=0x53):
        self.bus = bus; self.address = address
        self.last_uvi = 0.0
        try:
            self.bus.write_byte_data(self.address, 0x00, 0x0A)  # enable + UVS mode
            self.bus.write_byte_data(self.address, 0x04, 0x05)  # 20-bit, 1000 ms rate
            self.bus.write_byte_data(self.address, 0x05, 0x01)  # gain 3
            time.sleep(0.5)                                     # first conversion
        except: pass

    def read_uv(self):
        """Return the UV index as a float (0.0 in darkness)."""
        try:
            d = self.bus.read_i2c_block_data(self.address, 0x10, 3)
            raw = d[0] | (d[1] << 8) | (d[2] << 16)
            self.last_uvi = round(raw / self._SENSITIVITY, 3)
        except: pass
        return self.last_uvi


class SGP40:
    """Sensirion SGP40 VOC sensor. Reports the raw signal (not the VOC index)."""

    def __init__(self, bus, address=0x59):
        self.bus = bus; self.address = address
        self.last_voc = 0
    def read_voc(self):
        try:
            # SGP40 "Measure Raw Signal" command (0x260F).
            # The command accepts relative-humidity/temperature compensation
            # parameters; this agent sends the sensor's documented default values.
            self.bus.write_i2c_block_data(self.address, 0x26, [0x0F, 0x80, 0x00, 0xA2, 0x66, 0x66, 0x93])
            time.sleep(0.03) # measurement delay

            # Read 3 bytes (Data H, Data L, CRC)
            d = self.bus.read_i2c_block_data(self.address, 0x59, 3)
            raw_value = (d[0] << 8) | d[1]

            # Simple filter: on an implausible reading (zero or out of range),
            # keep the previous value instead of pushing a spike.
            if raw_value == 0 or raw_value > 65535:
                return self.last_voc

            self.last_voc = raw_value
            return raw_value
        except:
            return self.last_voc


def parse_args():
    parser = argparse.ArgumentParser(description="Push I2C environment-sensor readings to the dashboard PC over TCP.")
    parser.add_argument("--host", default=None, help="Dashboard PC hostname or IP (overrides SENSOR_AGENT_PC_HOST)")
    parser.add_argument("--port", type=int, default=None, help="Dashboard TCP port (overrides SENSOR_AGENT_PC_PORT, default 50001)")
    parser.add_argument("--interval", type=float, default=None, help="Seconds between samples (overrides SENSOR_AGENT_INTERVAL_S, default 0.1)")
    return parser.parse_args()


def resolve_config(args):
    host = args.host or os.environ.get("SENSOR_AGENT_PC_HOST")
    if not host:
        print("[!] Error: no dashboard PC host configured. Set SENSOR_AGENT_PC_HOST or pass --host.")
        sys.exit(1)
    port = args.port if args.port is not None else int(os.environ.get("SENSOR_AGENT_PC_PORT", "50001"))
    interval = args.interval if args.interval is not None else float(os.environ.get("SENSOR_AGENT_INTERVAL_S", "0.1"))
    return host, port, interval


def start_agent(host, port, interval):
    import platform
    my_name = platform.node()  # local hostname, sent with each sample
    try: bus = smbus.SMBus(1)
    except: return

    light = TSL25911(bus)
    bme = BME280(bus)
    imu = ICM20948(bus)
    uv = LTR390(bus)
    voc = SGP40(bus)

    while True:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(5)
                s.connect((host, port))
                s.settimeout(None)
                while True:
                    t, h = bme.read_data()
                    acc, gyr = imu.read_accel_gyro()
                    lx = light.read_lux()
                    uvv = uv.read_uv()
                    v = voc.read_voc()

                    data = {
                        "type": "sensor_data", "temp": t, "humi": h,
                        "accel": acc, "gyro": gyr, "lux": lx, "uv": uvv, "voc": v,
                        "model": bme.model,
                        "hostname": my_name,
                    }
                    s.sendall((json.dumps(data) + "\n").encode())
                    time.sleep(interval)

        except: time.sleep(5)


if __name__ == "__main__":
    _host, _port, _interval = resolve_config(parse_args())
    start_agent(_host, _port, _interval)

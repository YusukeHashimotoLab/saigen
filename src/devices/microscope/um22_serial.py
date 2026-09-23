"""
UM22 系 USB デジタル顕微鏡（サンワサプライ 400-CAM106 など）のシリアル制御

400-CAM106 の中身は Vitiny UM22。1 本の USB ケーブルの先に内部ハブがあり、
UVC カメラ（映像）と CP210x USB シリアル（制御 MCU）がぶら下がっている。
LED 照明の ON/OFF・明るさ、ズーム、オートフォーカスはこのシリアル経由で
制御される（メーカー製 UM Viewer も同じ経路）。映像は
``MicroscopeController``（OpenCV）が担当し、このモジュールは制御側だけを扱う。

プロトコル（UM Viewer 1.000.096 を解析し、2026-09-22 に実機で検証）:

- 115200 bps、8 ビット、パリティなし、**ストップビット 2**
- **RTS を立てると MCU がリセットされ、カメラも電源が入れ直される**（約 7 秒）。
  pyserial は既定で open 時に RTS を立てるので、必ず ``rts=False`` にしてから開く
- コマンドは ASCII、**CR（``\\r``）終端**。応答は ``@`` + 16 進 2 桁 + CR
- 読み出し  ``R00`` + アドレス(16 進 2 桁)     例: ``R0006`` → ``@0C`` (LED レベル 12)
- 文字列    ``S00`` + アドレス                 型番はアドレス 0x2C→0x23 の 10 文字
- 書き込み  ``W`` + ((アドレス<<8) | キー | 値) を 16 進 4 桁
- 状態レジスタ 0x00: 0x01 = LED 点灯, 0x03 = LED 消灯（ビット 1 が消灯）
- LED ON/OFF は**トグル** ``W0159``（アドレス 1, キー 64, 値 25）。
  ``set_led(on)`` は状態を読んでから必要なときだけトグルする
- LED レベルはアドレス 0x06（``W06xx`` で書き込み、出荷時 12）。
  LED モードはアドレス 0x10（0 = 全灯、読み出しのみ対応）
- フォーカス（レンズ）モーター: 位置 = (アドレス 0x09 << 8) | アドレス 0x0A
  （実機で 346〜1766 を観測、出荷時 1568）。状態レジスタのビット 0x04 = モーター動作中、
  アドレス 0x04 = モーターモード（0 待機、2 AF 探索中）。
  AF モード書き込み: ``W0154`` 手動 / ``W0155`` ワンショット AF / ``W0156`` 連続 AF。
  位置指定: ``W18hh`` ``W19ll`` ``W016E``（上位・下位バイトを書いてから移動コマンド）。
  ステップ移動: ``W0152``(in) / ``W0153``(out) を押し、``W0192`` / ``W0193`` で離す。
  AF は視野にテクスチャが無いと収束せず探索し続ける（白紙で 60 秒以上）。

使用例:
    from src.devices.microscope import UM22SerialController

    with UM22SerialController("COM10") as scope:
        print(scope.get_model(), scope.get_firmware())   # 'UM22TW0100', '01.03.00'
        scope.set_led(False)                              # 消灯
        scope.set_led(True, level=8)                      # 点灯して明るさ 8
"""

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# レジスタ / コマンド定数（UM Viewer の NewUMControl と同じ値）
ADDR_STATUS = 0x00
ADDR_LED_LEVEL = 0x06
ADDR_LED_MODE = 0x10
ADDR_FW = (0x2F, 0x2E, 0x2D)          # major, minor, patch
ADDR_MODEL = tuple(range(0x2C, 0x22, -1))  # 0x2C .. 0x23 の 10 文字
STATUS_LED_OFF_BIT = 0x02
KEY_BUTTON = 64
CMD_LED_TOGGLE = 25                   # WriteValue(1, 64, 25) -> "W0159"
CMD_LED_STEP_UP = 26
CMD_LED_STEP_DOWN = 27
LED_LEVEL_DEFAULT = 12

# フォーカス（レンズ）モーター
ADDR_MOTOR_MODE = 0x04
ADDR_MOTOR_POS_HI = 0x09
ADDR_MOTOR_POS_LO = 0x0A
ADDR_TARGET_POS_HI = 0x18
ADDR_TARGET_POS_LO = 0x19
STATUS_MOTOR_BUSY_BIT = 0x04
KEY_RELEASE = 128                     # "ボタンを離す"（連続動作の停止）
CMD_ZOOM_IN = 16
CMD_ZOOM_OUT = 17
CMD_ZOOM_IN_STEP = 18
CMD_ZOOM_OUT_STEP = 19
CMD_AF_MANUAL = 20
CMD_AF_SINGLE = 21
CMD_AF_CONTINUOUS = 22
CMD_MOTOR_RESET = 30
CMD_GOTO_POSITION = 46
FOCUS_MODES = {"manual": CMD_AF_MANUAL, "auto": CMD_AF_SINGLE, "continuous": CMD_AF_CONTINUOUS}
MOTOR_POS_MAX = 0xFFFF


class UM22SerialController:
    """UM22 系顕微鏡の制御 MCU とシリアルで通信するクラス（LED 制御・状態読み出し）"""

    def __init__(self, port: str = "COM10", baudrate: int = 115200,
                 timeout: float = 0.05, reply_wait: float = 0.35):
        """
        Args:
            port: CP210x の COM ポート（プレースホルダ既定値。config.yaml の
                shared_devices.microscope_port で指定する）
            baudrate: 通信速度（115200 固定でよい）
            timeout: 1 回の read のタイムアウト（秒）
            reply_wait: コマンド送信後に応答を待つ時間（秒）
        """
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.reply_wait = reply_wait
        self.ser = None

    # ------------------------------------------------------------------
    # 接続
    # ------------------------------------------------------------------
    def connect(self) -> bool:
        """ポートを開く。RTS/DTR は立てない（RTS = MCU リセット）。"""
        try:
            import serial
            s = serial.Serial()
            s.port = self.port
            s.baudrate = self.baudrate
            s.bytesize = 8
            s.parity = "N"
            s.stopbits = 2
            s.timeout = self.timeout
            s.write_timeout = 0.5
            s.rts = False
            s.dtr = False
            s.open()
            self.ser = s
            time.sleep(0.2)
            status = self.get_status()
            if status is None:
                logger.error(f"顕微鏡 MCU ({self.port}) から応答がありません")
                self.disconnect()
                return False
            logger.info(f"✓ 顕微鏡 MCU 接続 ({self.port}): status=0x{status:02X}, "
                        f"LED={'ON' if not status & STATUS_LED_OFF_BIT else 'OFF'}")
            return True
        except Exception as e:
            logger.error(f"顕微鏡 MCU 接続エラー ({self.port}): {e}")
            self.ser = None
            return False

    def disconnect(self):
        if self.ser is not None:
            try:
                self.ser.close()
            finally:
                self.ser = None

    @property
    def is_connected(self) -> bool:
        return self.ser is not None and self.ser.is_open

    def __enter__(self):
        if not self.connect():
            raise ConnectionError(f"顕微鏡 MCU に接続できません (port={self.port})")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.disconnect()

    # ------------------------------------------------------------------
    # 低レベル
    # ------------------------------------------------------------------
    @staticmethod
    def frame_read(addr: int) -> str:
        return f"R00{addr & 0xFF:02X}"

    @staticmethod
    def frame_read_string(addr: int) -> str:
        return f"S00{addr & 0xFF:02X}"

    @staticmethod
    def frame_write(addr: int, key: int, value: int) -> str:
        return f"W{((addr << 8) | key | value) & 0xFFFF:04X}"

    @staticmethod
    def parse_value(reply: str) -> Optional[int]:
        """``@0C\\r`` -> 12。応答が無い・形式外なら None"""
        if reply.startswith("@") and len(reply) >= 3:
            try:
                return int(reply[1:3], 16)
            except ValueError:
                return None
        return None

    def _require(self):
        if not self.is_connected:
            raise ConnectionError(f"顕微鏡 MCU が接続されていません (port={self.port})")
        return self.ser

    def transact(self, command: str) -> str:
        """CR 終端でコマンドを送り、reply_wait 秒の間に届いた応答を返す"""
        ser = self._require()
        ser.reset_input_buffer()
        ser.write((command + "\r").encode("ascii"))
        deadline = time.monotonic() + self.reply_wait
        buf = b""
        while time.monotonic() < deadline:
            chunk = ser.read(64)
            if chunk:
                buf += chunk
                if buf.endswith(b"\r"):
                    break
        return buf.decode("ascii", "replace")

    def read_value(self, addr: int) -> Optional[int]:
        return self.parse_value(self.transact(self.frame_read(addr)))

    def read_string(self, addrs) -> str:
        """各アドレスの応答 ``@X`` の 1 文字目を連結して文字列を返す"""
        out = ""
        for a in addrs:
            r = self.transact(self.frame_read_string(a))
            out += r[1:2] if r.startswith("@") and len(r) >= 2 else "?"
        return out

    def write(self, addr: int, key: int, value: int) -> None:
        self.transact(self.frame_write(addr, key, value))

    # ------------------------------------------------------------------
    # 高レベル
    # ------------------------------------------------------------------
    def get_status(self) -> Optional[int]:
        return self.read_value(ADDR_STATUS)

    def get_model(self) -> str:
        """型番文字列（例: 'UM22TW0100'）"""
        return self.read_string(ADDR_MODEL)

    def get_firmware(self) -> Optional[str]:
        parts = [self.read_value(a) for a in ADDR_FW]
        if any(p is None for p in parts):
            return None
        return ".".join(f"{p:02d}" for p in parts)

    def get_led_level(self) -> Optional[int]:
        return self.read_value(ADDR_LED_LEVEL)

    def get_led_mode(self) -> Optional[int]:
        return self.read_value(ADDR_LED_MODE)

    def led_is_on(self) -> Optional[bool]:
        status = self.get_status()
        if status is None:
            return None
        return not (status & STATUS_LED_OFF_BIT)

    def toggle_led(self) -> None:
        """UM Viewer の LED ボタンと同じトグルコマンドを送る"""
        self.write(1, KEY_BUTTON, CMD_LED_TOGGLE)
        time.sleep(0.3)

    def set_led(self, on: bool, level: Optional[int] = None) -> bool:
        """LED を指定状態にする。状態を読み、違うときだけトグルする。

        Args:
            on: True = 点灯, False = 消灯
            level: 併せて設定する明るさ（0-255。出荷時 12）。None なら変更しない

        Returns:
            bool: 最終的な LED 状態（True = 点灯）
        """
        if level is not None:
            self.set_led_level(level)
        current = self.led_is_on()
        if current is None:
            raise ConnectionError("顕微鏡 MCU の状態を読み取れません")
        if current != on:
            self.toggle_led()
            current = self.led_is_on()
            if current != on:
                raise RuntimeError(f"LED を {'ON' if on else 'OFF'} にできませんでした (status bit unchanged)")
        logger.info(f"✓ 顕微鏡 LED {'ON' if on else 'OFF'}" + (f" (level {level})" if level is not None else ""))
        return current

    def set_led_level(self, level: int) -> int:
        """LED の明るさを書き込み、読み返した値を返す（0-255）。

        注意: レベルを書き込むと LED は点灯する（実機で確認）。消灯したまま明るさだけを
        変えたいときは ``set_led(False, level=...)`` を使う（書き込み後に消灯し直す）。
        """
        level = int(level)
        if not 0 <= level <= 255:
            raise ValueError(f"LED level out of range: {level}")
        self.write(ADDR_LED_LEVEL, 0, level)
        time.sleep(0.2)
        back = self.get_led_level()
        if back != level:
            logger.warning(f"顕微鏡 LED レベル書き込み: 要求 {level}, 読み返し {back}")
        return back if back is not None else level

    # ------------------------------------------------------------------
    # フォーカス（レンズ）モーター
    # ------------------------------------------------------------------
    def get_motor_position(self) -> Optional[int]:
        """レンズモーターの現在位置（0-65535）。読めなければ None"""
        hi = self.read_value(ADDR_MOTOR_POS_HI)
        lo = self.read_value(ADDR_MOTOR_POS_LO)
        if hi is None or lo is None:
            return None
        return (hi << 8) | lo

    def get_motor_mode(self) -> Optional[int]:
        return self.read_value(ADDR_MOTOR_MODE)

    def motor_is_busy(self) -> Optional[bool]:
        status = self.get_status()
        if status is None:
            return None
        return bool(status & STATUS_MOTOR_BUSY_BIT)

    def wait_motor(self, timeout_s: float = 60.0, interval_s: float = 0.5,
                   settle_polls: int = 3) -> tuple:
        """モーターが止まるまで待つ。

        「動作中ビットが立っていない」かつ「位置が settle_polls 回連続で同じ」を
        停止とみなす。

        Returns:
            (position, stopped): 最後に読んだ位置と、timeout 内に止まったかどうか
        """
        deadline = time.monotonic() + timeout_s
        last = self.get_motor_position()
        same = 0
        while time.monotonic() < deadline:
            time.sleep(interval_s)
            pos = self.get_motor_position()
            busy = self.motor_is_busy()
            same = same + 1 if (pos == last and not busy) else 0
            last = pos
            if same >= settle_polls:
                return last, True
        return last, False

    def set_focus_mode(self, mode: str) -> None:
        """AF モードを設定する: "manual" / "auto"（ワンショット）/ "continuous" """
        if mode not in FOCUS_MODES:
            raise ValueError(f"unknown focus mode: {mode!r} (choose from {sorted(FOCUS_MODES)})")
        self.write(1, KEY_BUTTON, FOCUS_MODES[mode])

    def autofocus(self, timeout_s: float = 60.0) -> tuple:
        """ワンショット AF を実行し、モーターが止まるまで待つ。

        視野にテクスチャが無いと収束せず探索し続ける。timeout 内に止まらなければ
        手動モードに切り替えて探索を止め、(位置, False) を返す。

        Returns:
            (position, converged)
        """
        self.set_focus_mode("auto")
        time.sleep(0.5)
        pos, stopped = self.wait_motor(timeout_s)
        if not stopped:
            logger.warning(f"顕微鏡 AF が {timeout_s:.0f}s 以内に収束しません（位置 {pos}）。手動モードに戻します")
            self.set_focus_mode("manual")
            time.sleep(0.5)
            pos, _ = self.wait_motor(10.0)
            return pos, False
        logger.info(f"✓ 顕微鏡 AF 完了: レンズ位置 {pos}")
        return pos, True

    def goto_position(self, position: int, timeout_s: float = 40.0) -> tuple:
        """レンズモーターを指定位置へ動かして止まるまで待つ。

        Returns:
            (position, reached): 最終位置と、要求位置に一致したか
        """
        position = int(position)
        if not 0 <= position <= MOTOR_POS_MAX:
            raise ValueError(f"motor position out of range: {position}")
        self.write(ADDR_TARGET_POS_HI, 0, (position >> 8) & 0xFF)
        time.sleep(0.02)
        self.write(ADDR_TARGET_POS_LO, 0, position & 0xFF)
        time.sleep(0.02)
        self.write(1, KEY_BUTTON, CMD_GOTO_POSITION)
        time.sleep(0.3)
        pos, stopped = self.wait_motor(timeout_s)
        reached = stopped and pos == position
        if reached:
            logger.info(f"✓ 顕微鏡レンズ位置 {pos}")
        else:
            logger.warning(f"顕微鏡レンズ位置: 要求 {position}, 到達 {pos} (stopped={stopped})")
        return pos, reached

    def step_focus(self, direction: str = "in", steps: int = 1, hold_s: float = 0.3) -> Optional[int]:
        """ステップ移動を steps 回行う（ボタンを hold_s 秒押して離す）。最終位置を返す。

        実機観測: "out" で位置の値が増え、"in" で減る（hold_s=0.3 で 1 押し ≈ 6〜14）。
        """
        if direction not in ("in", "out"):
            raise ValueError(f"direction must be 'in' or 'out', got {direction!r}")
        press = CMD_ZOOM_IN_STEP if direction == "in" else CMD_ZOOM_OUT_STEP
        for _ in range(max(1, int(steps))):
            self.write(1, KEY_BUTTON, press)
            time.sleep(hold_s)
            self.write(1, KEY_RELEASE, press)
            time.sleep(0.2)
        pos, _ = self.wait_motor(10.0)
        logger.info(f"✓ 顕微鏡フォーカス ステップ {direction} x{steps}: レンズ位置 {pos}")
        return pos

    def reset_mcu(self, wait_s: float = 8.0) -> str:
        """RTS パルスで MCU をリセットする。**カメラも電源が入れ直される**ので、
        映像取得中には呼ばないこと。起動ログ文字列を返す。"""
        ser = self._require()
        ser.reset_input_buffer()
        ser.rts = True
        time.sleep(0.1)
        ser.rts = False
        deadline = time.monotonic() + wait_s
        buf = b""
        while time.monotonic() < deadline:
            buf += ser.read(64)
            if b"boot done" in buf:
                break
        log = buf.decode("ascii", "replace")
        logger.info(f"顕微鏡 MCU リセット: {log.strip()!r}")
        return log


# 動作確認用: python -m src.devices.microscope.um22_serial COM10 [on|off]
if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    port = sys.argv[1] if len(sys.argv) > 1 else "COM10"
    with UM22SerialController(port) as scope:
        print("model:", scope.get_model(), " firmware:", scope.get_firmware())
        print("status:", scope.get_status(), " LED on:", scope.led_is_on(),
              " level:", scope.get_led_level(), " mode:", scope.get_led_mode())
        print("motor position:", scope.get_motor_position(), " motor mode:", scope.get_motor_mode())
        if len(sys.argv) > 2 and sys.argv[2] in ("on", "off"):
            scope.set_led(sys.argv[2] == "on")
            print("LED on:", scope.led_is_on())
        elif len(sys.argv) > 2 and sys.argv[2] == "af":
            print("autofocus ->", scope.autofocus(timeout_s=60))
        elif len(sys.argv) > 3 and sys.argv[2] == "goto":
            print("goto ->", scope.goto_position(int(sys.argv[3])))

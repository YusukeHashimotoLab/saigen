"""
Picus2電動ピペットを制御するためのモジュール

このモジュールはPicus2電子ピペットをUSBまたはBluetooth経由で操作するためのインターフェースを提供します。
液体の吸引、吐出、チップの排出、混合など、実験自動化に必要な機能をサポートしています。

使用方法:
    import asyncio
    from picus2.picus2_controller import Picus2Controller, Buttons, ConnectionType

    async def run_demo():
        # USB接続の場合（デフォルト）
        picus = Picus2Controller("COM4")  # または Picus2Controller("COM4", connection_type=ConnectionType.USB)

        # Bluetooth接続の場合
        # picus = Picus2Controller("XX:XX:XX:XX:XX:XX", connection_type=ConnectionType.BLUETOOTH)

        # 接続
        await picus.connect()

        # モーターモードを有効化
        await picus.set_motor_mode(True)

        # 5mLを吸引
        await picus.aspirate(5.0, speed=5)

        # 5mLを吐出
        await picus.dispense(5.0, speed=5)

        # 切断
        await picus.disconnect()

    # 非同期関数を実行
    asyncio.run(run_demo())

"""

import asyncio
import json
import logging
import time
from enum import Enum
from typing import Optional

try:  # ハードウェア依存ライブラリは未インストール環境でも import を通す
    import serial
except ImportError:  # pragma: no cover - 実機環境では常に成功する
    serial = None

try:
    from bleak import BleakClient
except ImportError:  # pragma: no cover
    BleakClient = None

logger = logging.getLogger(__name__)

# 速度ごとの 10000μL 当たりの所要時間(秒)。Picus2 マニュアル 63 ページの表。
SECONDS_PER_10000UL = {
    1: 10.2,
    2: 7.4,
    3: 5.4,
    4: 3.8,
    5: 2.9,
    6: 2.2,
    7: 1.7,
    8: 1.3,
    9: 0.9,
}


def nominal_operation_time(amount: float, speed: int) -> float:
    """吸引/吐出の公称所要時間(秒)を返す（マニュアル p.63 の表より）。

    Args:
        amount (float): 量 (mL)
        speed (int): 速度 (1-9)

    Returns:
        float: 公称所要時間 (s)。例: 5.0 mL / speed 5 → 1.45 s
    """
    if speed not in SECONDS_PER_10000UL:
        raise ValueError(f"speedは1-9の範囲で指定してください（指定値: {speed}）")
    return amount * SECONDS_PER_10000UL[speed] / 10


class Picus2CommandError(ConnectionError):
    """Picus2 へのコマンド送信（ボタン/トリガー含む）に失敗したことを表す例外。

    ``ConnectionError`` の派生なので、既存の ``except ConnectionError`` でも捕捉できる。
    送信失敗を握りつぶして正常終了すると、呼び出し側（LabRobot）が液体が動いた
    ものとして保持量を更新してしまうため、失敗は必ずこの例外で伝播させる。
    """


class ConnectionType(Enum):
    """接続タイプを表す列挙型"""
    USB = "usb"
    BLUETOOTH = "bluetooth"


class Buttons:
    """ピペットのボタン種類を表す定数クラス"""

    # 電源ボタン
    TRIGGER_BUTTON_POWER = "TRIGGER_BUTTON_POWER"
    # 左ボタン
    TRIGGER_BUTTON_LEFT = "TRIGGER_BUTTON_LEFT"
    # 中央ボタン
    TRIGGER_BUTTON_MIDDLE = "TRIGGER_BUTTON_MIDDLE"
    # 右ボタン
    TRIGGER_BUTTON_RIGHT = "TRIGGER_BUTTON_RIGHT"
    # 上部トリガーボタン
    TRIGGER_BUTTON_TOP = "TRIGGER_BUTTON_TOP"
    # プリセットボタン
    TRIGGER_BUTTON_PRESET = "TRIGGER_BUTTON_PRESET"
    # チップ排出ボタン
    TRIGGER_BUTTON_TIPEJECT = "TRIGGER_BUTTON_TIPEJECT"
    # 上方向ボタン
    UP = "UP"
    # 下方向ボタン
    DOWN = "DOWN"


class Picus2Controller:
    """Picus2電動ピペットを操作するクラス"""

    def __init__(self, address: str, connection_type: ConnectionType = ConnectionType.USB):
        """
        Picus2コントローラーを初期化する

        Args:
            address (str): USB接続の場合はCOMポート（例: "COM4"）、
                          Bluetooth接続の場合はMACアドレス（例: "XX:XX:XX:XX:XX:XX"）
            connection_type (ConnectionType): 接続タイプ。デフォルトはUSB
        """
        self.address = address
        self.connection_type = connection_type
        self.motor_mode = False
        self.end_flag = False
        self.DEBUG = False

        # Bluetooth用
        self.client: Optional["BleakClient"] = None
        self.COMMAND_CHARACTERISTIC_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
        self.NOTIFY_CHARACTERISTIC_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

        # USB用
        self.serial: Optional["serial.Serial"] = None

    @property
    def is_connected(self) -> bool:
        """接続中かどうか（USB/Bluetooth 共通）"""
        if self.connection_type == ConnectionType.USB:
            return self.serial is not None and bool(getattr(self.serial, "is_open", False))
        return self.client is not None and bool(getattr(self.client, "is_connected", False))

    def _require_connection(self):
        """未接続の場合は例外を送出する。

        以前は未接続でもコマンド送信が黙って無視され、実際には何も動いていない
        まま処理が進む危険があった（Dobot ドライバの ``_require_device()`` と同じ方針）。
        """
        if not self.is_connected:
            raise ConnectionError(
                f"Picus2 が接続されていません (address={self.address}, "
                f"type={self.connection_type.value})。操作を中止します。"
            )

    async def connect(self) -> bool:
        """
        Picus2に接続する

        Returns:
            bool: 接続に成功した場合 True

        Raises:
            ConnectionError: 接続に失敗した場合（ポート名と元例外を含む）
        """
        if self.connection_type == ConnectionType.USB:
            return await self._connect_usb()
        else:
            return await self._connect_bluetooth()

    async def _connect_usb(self) -> bool:
        """USB経由でPicus2に接続する（失敗時は ConnectionError）"""
        if serial is None:
            raise ConnectionError(
                f"pyserial がインストールされていないため Picus2 に接続できません "
                f"(port={self.address})"
            )
        try:
            self.serial = serial.Serial(
                port=self.address,
                baudrate=230400,
                bytesize=8,
                parity="N",
                stopbits=1,
                timeout=1.0,
            )
        except Exception as e:
            self.serial = None
            self.debug_print(f"USB接続エラー: {e}")
            raise ConnectionError(
                f"Picus2 のUSB接続に失敗しました (port={self.address}): {e}"
            ) from e

        try:
            if not getattr(self.serial, "is_open", False):
                raise ConnectionError(
                    f"Picus2 のUSBポートを開けませんでした (port={self.address})"
                )
        except BaseException:
            # ポートオブジェクト生成後の失敗ではハンドルを閉じてから伝播する（リーク防止）
            self._close_serial_quietly()
            raise

        self.debug_print(f"USB接続状態: True")
        return True

    def _close_serial_quietly(self):
        """接続処理の途中失敗時にシリアルポートを閉じて破棄する（元の例外を優先）。"""
        port, self.serial = self.serial, None
        if port is None:
            return
        try:
            port.close()
        except Exception as close_error:  # 元の例外を隠さないためここでは記録のみ
            logger.warning(
                f"Picus2 のUSBポートを閉じられませんでした (port={self.address}): {close_error}"
            )

    async def _connect_bluetooth(self) -> bool:
        """Bluetooth経由でPicus2に接続する（失敗時は ConnectionError）"""
        if BleakClient is None:
            raise ConnectionError(
                f"bleak がインストールされていないため Picus2 に接続できません "
                f"(address={self.address})"
            )
        try:
            self.client = BleakClient(self.address)
            await self.client.connect()
            is_connected = self.client.is_connected
            await asyncio.sleep(1)  # 早すぎるとエラーになるため少し待機

            if not is_connected:
                raise ConnectionError("BleakClient.is_connected が False です")

            # 通知の設定
            await self.client.start_notify(
                self.NOTIFY_CHARACTERISTIC_UUID, self.__notification_handler
            )
        except ConnectionError:
            self.client = None
            raise
        except Exception as e:
            self.client = None
            self.debug_print(f"Bluetooth接続エラー: {e}")
            raise ConnectionError(
                f"Picus2 のBluetooth接続に失敗しました (address={self.address}): {e}"
            ) from e

        self.debug_print(f"Bluetooth接続状態: True")
        return True

    async def disconnect(self):
        """
        Picus2から切断する

        Returns:
            bool: 切断が成功したかどうか
        """
        if self.connection_type == ConnectionType.USB:
            return await self._disconnect_usb()
        else:
            return await self._disconnect_bluetooth()

    async def _disconnect_usb(self):
        """USB接続を切断する"""
        if self.serial and self.serial.is_open:
            self.serial.close()
        is_disconnected = self.serial is None or not self.serial.is_open
        self.debug_print(f"USB切断状態: {is_disconnected}")
        return is_disconnected

    async def _disconnect_bluetooth(self):
        """Bluetooth接続を切断する"""
        if self.client:
            await self.client.disconnect()
            is_connected = self.client.is_connected
            self.debug_print(f"Bluetooth切断状態: {not is_connected}")
            return not is_connected
        return True

    async def send_command(self, command: str):
        """
        コマンドを送信する

        Args:
            command (str): 送信するコマンド

        Raises:
            Picus2CommandError: 未接続、またはシリアル/Bluetooth の書き込みに
                失敗した場合。``serial.SerialException`` や Bleak の例外も
                すべてこの例外（ConnectionError 派生）に包んで伝播する。
        """
        self.debug_print(f"送信: {command}")
        try:
            if self.connection_type == ConnectionType.USB:
                await self._send_command_usb(command)
            else:
                await self._send_command_bluetooth(command)
        except Picus2CommandError:
            raise
        except Exception as e:
            raise Picus2CommandError(
                f"Picus2 へのコマンド送信に失敗しました (address={self.address}, "
                f"type={self.connection_type.value}, command={command}): {e}"
            ) from e

    async def _send_command_usb(self, command: str):
        """USB経由でコマンドを送信する（ポートが閉じていれば例外。以前は黙って無視）"""
        if not (self.serial and self.serial.is_open):
            raise Picus2CommandError(
                f"Picus2 のUSBポートが開いていないためコマンドを送信できません "
                f"(port={self.address})"
            )
        self.serial.flush()
        self.serial.write(f"{command}\r\n".encode("utf-8"))

    async def _send_command_bluetooth(self, command: str):
        """Bluetooth経由でコマンドを送信する（クライアントが無ければ例外。以前は黙って無視）"""
        if not self.client:
            raise Picus2CommandError(
                f"Picus2 の Bluetooth クライアントが無いためコマンドを送信できません "
                f"(address={self.address})"
            )
        write_value = bytearray(f"{command}\r\n", "utf-8")
        await self.client.write_gatt_char(self.COMMAND_CHARACTERISTIC_UUID, write_value)

    async def __notification_handler(self, sender, data: bytearray):
        """
        Bluetooth通知を受信したときの処理

        Args:
            sender: 送信元
            data (bytearray): 受信したデータ
        """
        if self.DEBUG:
            self.debug_print(f"受信: {data}")
        data_str = data.decode("utf-8")
        if data_str.startswith("END"):
            self.end_flag = True
            self.debug_print(f"self.end_flag = True!")
        self.debug_print(self.end_flag)

    async def wait_until_END_received(self, timeout: float = 10):
        """ENDが受信されるまで待機する"""
        if self.connection_type == ConnectionType.USB:
            await self._wait_for_response_usb("END", timeout)
        else:
            await self._wait_for_response_bluetooth()

    async def _wait_for_response_usb(self, target_prefix: str, timeout: float = 10):
        """USB経由でレスポンスを待機する"""
        start_time = time.time()
        while True:
            if self.serial and self.serial.is_open:
                try:
                    raw = self.serial.readline()
                except Exception as e:
                    raise Picus2CommandError(
                        f"Picus2 からの応答待ち中に読み取りに失敗しました "
                        f"(port={self.address}, waiting for {target_prefix}): {e}"
                    ) from e
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    self.debug_print(f"USB受信: {line}")
                if line.startswith(target_prefix):
                    self.debug_print(f"目標レスポンス受信: {line}")
                    break
            if time.time() - start_time > timeout:
                raise TimeoutError(f"Timeout waiting for response: {target_prefix}")
            await asyncio.sleep(0.01)

    async def _wait_for_response_bluetooth(self):
        """Bluetooth経由でENDレスポンスを待機する"""
        while not self.end_flag:
            self.debug_print(f"待機中...: {self.end_flag}")
            await asyncio.sleep(0.1)
        self.end_flag = False
        self.debug_print(f"END受信完了! {self.end_flag}")

    def calculate_operation_time(self, amount: float, speed: int) -> float:
        """
        ピペット操作にかかる時間を計算する
        
        Args:
            amount (float): 量 (mL)
            speed (int): 速度 (1-9)
            
        Returns:
            float: 予想される操作時間 (s)
        """
        estimated_time = nominal_operation_time(amount, speed)
        self.debug_print(f'予想時間: {estimated_time}秒')
        return estimated_time

    async def button(self, button: str, interval: float = 1):
        """
        指定のボタンを押す
        
        Args:
            button (str): 押すボタンの種類（Buttonsクラス定数を使用）
            interval (float, optional): ボタンを押したあとの待機時間(秒). デフォルト: 1

        Raises:
            ConnectionError: 未接続の場合
            Picus2CommandError: ボタン/トリガーコマンドの送信に失敗した場合。
                以前はここで ``serial.SerialException`` や Bleak の例外を握りつぶして
                正常終了していたため、吸引/吐出トリガーの失敗が「完了」と記録され、
                LabRobot の保持量が実際と食い違う原因になっていた。
        """
        self._require_connection()
        self.end_flag = False  # END待ちのフラグをリセット
        # send_command は失敗をすべて Picus2CommandError として送出する
        await self.send_command(f'{{"button": "{button}"}}')
        # await self.wait_until_END_received() # ENDが受信されるまで待機
        await asyncio.sleep(interval)  # 操作が終わる前にENDが帰ってくる場合があるため一定時間待機

    async def set_motor_mode(self, mode: bool):
        """
        モーターモードを設定する
        
        Args:
            mode (bool): モーターモードを有効にするかどうか
        """
        self._require_connection()
        self.motor_mode = mode
        await self.send_command(
            f'{{"no": 0, "data": "ENABLE_MOTOR_CONTROL {int(mode)}"}}'
        )
        await asyncio.sleep(0.2)
        await self.button(Buttons.TRIGGER_BUTTON_RIGHT)
        await asyncio.sleep(0.2)

    async def aspirate(self, amount: float, speed: int = 5):
        """
        液体を吸引する
        
        Args:
            amount (float): 吸引する量 (mL)。0.5-10mLなど、使用可能な範囲に合わせる
            speed (int, optional): 吸引速度。1-9の範囲。デフォルト: 5
            
        Raises:
            Exception: モーターモードが有効でない場合
        """
        self._require_connection()
        if not self.motor_mode:
            raise Exception("モーターモードを有効にしてください")
        await self.send_command(
            f'{{"no": 0, "data": "RUN_ASPIRATE {amount} {speed}", "gui_pre": "RUN_ASPIRATE"}}'
        )
        # aspirateコマンドを送ってすぐにトリガーを押すと失敗する場合があるため少し待機
        await asyncio.sleep(0.2)
        await self.button(Buttons.TRIGGER_BUTTON_TOP)

        # 操作完了まで待機
        await asyncio.sleep(self.calculate_operation_time(amount, speed))

        # 終了後の待機時間
        await asyncio.sleep(0.2)
        self.debug_print("吸引完了!")

    async def dispense(self, amount: float, speed: int = 5):
        """
        液体を吐出する
        
        Args:
            amount (float): 吐出する量 (mL)。0.5-10mLなど、使用可能な範囲に合わせる
            speed (int, optional): 吐出速度。1-9の範囲。デフォルト: 5
            
        Raises:
            Exception: モーターモードが有効でない場合
        """
        self._require_connection()
        if not self.motor_mode:
            raise Exception("モーターモードを有効にしてください")
        await self.send_command(
            f'{{"no": 0, "data": "RUN_DISPENSE {amount} {speed}", "gui_pre": "RUN_DISPENSE"}}'
        )
        await asyncio.sleep(0.2)
        await self.button(Buttons.TRIGGER_BUTTON_TOP)

        # 操作完了まで待機
        await asyncio.sleep(self.calculate_operation_time(amount, speed))

        # 終了後の待機時間
        await asyncio.sleep(0.2)
        self.debug_print("吐出完了!")

    async def eject_tip(self):
        """
        チップを排出する
        
        Raises:
            Exception: モーターモードが有効でない場合
        """
        self._require_connection()
        if not self.motor_mode:
            raise Exception("モーターモードを有効にしてください")
        await self.send_command(
            f'{{"no": 0, "data": "TIP_EJECT", "gui_pre": "TIP_EJECT"}}'
        )
        await asyncio.sleep(0.2)
        await self.button(Buttons.TRIGGER_BUTTON_TOP)
        
        # 終了待ち
        self.end_flag = False
        await self.wait_until_END_received()

        # 終了後の待機時間
        await asyncio.sleep(0.2)
        self.debug_print("チップ排出完了!")

    async def blow_out(self, go_home: bool = True, speed: int = 1, delay_ms: int = 3000):
        """
        液体をすべて排出する
        
        Args:
            go_home (bool, optional): 終了後にピペット内のピストンがホームポジションに戻るかどうか。デフォルト: True
            speed (int, optional): 排出速度。1-9の範囲。デフォルト: 1
            delay_ms (int, optional): 排出所要時間 (ms)。デフォルト: 3000
            
        Raises:
            Exception: モーターモードが有効でない場合
        """
        self._require_connection()
        if not self.motor_mode:
            raise Exception("モーターモードを有効にしてください")
        await self.send_command(
            f'{{"no": 0, "data": "BLOW_OUT {int(go_home)} {speed} {delay_ms}", "gui_pre": "BLOW_OUT"}}'
        )
        await asyncio.sleep(0.2)
        await self.button(Buttons.TRIGGER_BUTTON_TOP)

        # 操作完了まで待機
        await asyncio.sleep(delay_ms / 1000)

        # 終了後の待機時間
        await asyncio.sleep(0.2)
        self.debug_print("液体排出完了!")

    async def mix(self, cycles: int, speed: int, amount: float):
        """
        液体を混合する
        
        Args:
            cycles (int): 混合サイクル数
            speed (int): 混合速度。1-9の範囲
            amount (float): 混合量 (mL)
            
        Raises:
            Exception: モーターモードが有効でない場合
        """
        self._require_connection()
        if not self.motor_mode:
            raise Exception("モーターモードを有効にしてください")
        await self.send_command(
            f'{{"no": 0, "data": "RUN_MIX {cycles} {speed} {amount}", "gui_pre": "RUN_MIX"}}'
        )
        await asyncio.sleep(0.2)
        await self.button(Buttons.TRIGGER_BUTTON_TOP)

        # 混合操作は途中で停止することがあるため、ENDメッセージを待機
        self.end_flag = False
        await self.wait_until_END_received()

        # 終了後の待機時間
        await asyncio.sleep(0.2)
        self.debug_print("混合完了!")

    def debug_print(self, message: str):
        """
        デバッグメッセージを出力する
        
        Args:
            message (str): 出力するメッセージ
        """
        if self.DEBUG:
            logger.info(message)


async def run_demo():
    """
    Picus2Controllerの接続テスト（USB接続をデフォルトで使用）
    """
    print("=== Picus2 Electronic Pipette Connection Test ===")

    # USB接続の場合（デフォルト）
    port = "COM4"
    connection_type = ConnectionType.USB

    # Bluetooth接続を使う場合はこちらをコメントイン
    # port = "XX:XX:XX:XX:XX:XX"
    # connection_type = ConnectionType.BLUETOOTH

    try:
        print(f"Connecting to Picus2 via {connection_type.value}: {port}")

        # コントローラーの初期化
        picus2 = Picus2Controller(port, connection_type=connection_type)
        picus2.DEBUG = True

        # 接続
        connected = await picus2.connect()

        if connected:
            print("Successfully connected to Picus2!")

            # モーターモード有効化
            print("Enabling motor mode...")
            await picus2.set_motor_mode(True)

            # 簡単な操作テスト
            print("Testing basic operations...")
            await picus2.aspirate(amount=2.0, speed=5)
            await asyncio.sleep(1)
            await picus2.dispense(amount=2.0, speed=5)

            print("Basic operation test completed!")

            # モーターモードから抜ける
            print("Disabling motor mode...")
            await picus2.button(Buttons.TRIGGER_BUTTON_LEFT)
            await picus2.button(Buttons.TRIGGER_BUTTON_RIGHT)

            # 切断
            print("Disconnecting...")
            await picus2.disconnect()
            print("Picus2 test completed successfully!")

        else:
            print("Failed to connect to Picus2")

    except Exception as e:
        print(f"Error occurred: {str(e)}")
        print("Please check:")
        if connection_type == ConnectionType.USB:
            print(f"- Picus2 is connected via USB to {port}")
            print("- COM port is correct (check Device Manager)")
            print("- USB cable is properly connected")
        else:
            print("- Picus2 is powered on and Bluetooth is enabled")
            print(f"- MAC address is correct ({port})")
            print("- Device is not connected to another application")
            print("- Bluetooth adapter is working properly")


if __name__ == "__main__":
    """
    メインエントリーポイント
    """
    # asyncioのイベントループを取得して実行
    loop = asyncio.get_event_loop()
    loop.run_until_complete(run_demo())
    loop.close()
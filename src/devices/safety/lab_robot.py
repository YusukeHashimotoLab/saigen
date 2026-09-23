"""
L1層: 安全ラッパ層（LabRobot）

このモジュールはL0層のデバイスドライバを安全にラップし、
例外処理、待機時間、ログ記録を標準化します。

設計思想:
- AIが書くのは手順(What)だけ
- 安全性や制御の詳細(How)は人間が設計したライブラリ側に閉じ込める
- エラー発生時の安全復帰を自動化

"""

import asyncio
import logging
import math
import time
from typing import Optional, List, Dict, Any

from src.devices.safety.validators import (
    ValidationError,
    WorkspaceValidator,
    WorkspaceViolationError,
)

# ロギング設定
logger = logging.getLogger(__name__)


class LabRobot:
    """
    実験用ロボット安全ラッパクラス

    必要なデバイスのみを選択的に初期化し、
    安全な操作インターフェースを提供します。
    """

    def __init__(self,
                 use_dobot: bool = False,
                 use_picus2: bool = False,
                 use_ika: bool = False,
                 use_powder_dispenser: bool = False,
                 workspace_validator: Optional[WorkspaceValidator] = None,
                 **device_configs):
        """
        LabRobotを初期化

        Args:
            use_dobot: Dobotロボットアームを使用するか
            use_picus2: Picus2電動ピペットを使用するか
            use_ika: IKA撹拌・加熱装置を使用するか
            use_powder_dispenser: 粉体排出装置を使用するか
            workspace_validator: 可動域バリデータ（Noneの場合はバリデーションなし）
            **device_configs: デバイス固有の設定
                - dobot_port: str (デフォルト: 'COM8') - DobotのCOMポート
                - dobot_homing: bool (デフォルト: False)
                - picus2_address: str (デフォルト: 'COM4') - USBの場合はCOMポート、Bluetoothの場合はMACアドレス
                - picus2_connection_type: str (デフォルト: 'usb') - 'usb' または 'bluetooth'
                - ika_port: str (デフォルト: 'COM5')
                - powder_dispenser_port: str (デフォルト: 'COM6')

        Note:
            カメラと電子天秤はSharedDevicesクラスで管理されます。
        """
        # デバイス使用フラグ
        self.use_dobot = use_dobot
        self.use_picus2 = use_picus2
        self.use_ika = use_ika
        self.use_powder_dispenser = use_powder_dispenser

        # デバイスインスタンス（初期化前はNone）
        self.dobot = None
        self.picus2 = None
        self.ika = None
        self.powder_dispenser = None

        # デバイス設定
        self.device_configs = device_configs

        # 実験設定
        self.max_pipette_volume = 10.0  # mL（ピペットの最大容量）
        self.min_pipette_volume = 0.5  # mL（ピペットの最小操作量）
        self.picus2_max_retries = 5    # Picus2再接続最大試行回数
        self.picus2_retry_delays = [2, 3, 4, 5, 6]  # 各試行の待機時間（秒）

        # ピペットボリュームトラッキング（安全機能）
        self._current_pipette_volume = 0.0  # 現在ピペット内に保持している液体量（mL）

        # 位置設定（X, Y, Z, Joint1）
        # 注意: 初期値はNone。set_current_position_as_home()で設定してください
        self.home_position = device_configs.get('home_position', None)

        # 角度制御パラメータ
        self.angle_positions = {
            "aspirate_angle": device_configs.get('aspirate_angle', -60),
            "dispense_angle": device_configs.get('dispense_angle', 0),
        }

        # 垂直移動パラメータ
        self.vertical_movement_aspirate = device_configs.get('vertical_movement_aspirate', 150)
        self.vertical_movement_dispense = device_configs.get('vertical_movement_dispense', 50)

        # 待機時間設定
        self.wait_after_angle_move = device_configs.get('wait_after_angle_move', 1.0)
        self.wait_after_z_move = device_configs.get('wait_after_z_move', 0.5)
        self.wait_after_xy_move = device_configs.get('wait_after_xy_move', 1.0)
        self.wait_after_pipette = device_configs.get('wait_after_pipette', 1.0)
        self.wait_after_slider_move = device_configs.get('wait_after_slider_move', 2.0)
        self.wait_after_conveyer = device_configs.get('wait_after_conveyer', 1.0)

        # スライダー設定
        self.slider_max_position = device_configs.get('slider_max_position', 1000.0)

        # 初期化フラグ
        self._initialized = False

        # 可動域バリデータ（Noneの場合はバリデーションなし）
        self.workspace_validator = workspace_validator

    async def initialize(self) -> bool:
        """
        必要なデバイスを初期化

        Returns:
            bool: 初期化が成功したらTrue（失敗時は例外を送出する）

        Raises:
            RuntimeError: いずれかのデバイスの初期化に失敗した場合

        Note:
            以前は失敗時に False を返していたが、呼び出し側が戻り値を無視すると
            未初期化のまま処理が進み、ロボットが無音で空動作する危険があった。
            現在は失敗時に例外を送出し、部分的に接続済みのデバイスは切断してから送出する。
        """
        try:
            logger.info("=== LabRobot デバイス初期化開始 ===")

            # Dobot（ロボットアーム）の初期化
            if self.use_dobot and not await self._initialize_dobot():
                raise RuntimeError("Dobotの初期化に失敗しました")

            # Picus2（電動ピペット）の初期化
            if self.use_picus2 and not await self._initialize_picus2():
                raise RuntimeError("Picus2電動ピペットの初期化に失敗しました")

            # IKA（撹拌・加熱装置）の初期化
            if self.use_ika and not await self._initialize_ika():
                raise RuntimeError("IKA撹拌・加熱装置の初期化に失敗しました")

            # Powder Dispenser（粉体排出装置）の初期化
            if self.use_powder_dispenser and not await self._initialize_powder_dispenser():
                raise RuntimeError("粉体排出装置の初期化に失敗しました")

            self._initialized = True
            logger.info("=== LabRobot 全デバイス初期化完了 ===\n")
            return True

        except Exception as e:
            logger.error(f"LabRobot初期化エラー: {e}")
            # 部分的に接続済みのデバイスをリークさせないよう安全に切断してから送出
            await self.cleanup()
            raise

    async def _initialize_dobot(self) -> bool:
        """Dobotロボットアームを初期化（pydobotライブラリ使用）"""
        try:
            logger.info("Dobotロボットアームを初期化中...")
            from src.devices.dobot import PyDobotController

            dobot_port = self.device_configs.get('dobot_port', 'COM8')
            dobot_homing = self.device_configs.get('dobot_homing', False)
            dobot_verbose = self.device_configs.get('dobot_verbose', False)

            logger.info(f"Dobotを初期化中 (Port: {dobot_port}, verbose: {dobot_verbose})...")
            self.dobot = PyDobotController(port_name=dobot_port, homing=dobot_homing, verbose=dobot_verbose)

            if self.dobot.api is None:
                logger.error("Dobotの接続に失敗しました")
                return False

            current_pos = self.dobot.get_current_position()
            logger.info(f"✓ Dobot接続成功: 現在位置 X={current_pos[0]:.1f}, Y={current_pos[1]:.1f}, Z={current_pos[2]:.1f}")

            return True

        except Exception as e:
            logger.error(f"Dobotロボットアーム初期化エラー: {e}")
            return False

    async def _initialize_picus2(self) -> bool:
        """Picus2電動ピペットを初期化（再接続機能付き）"""
        try:
            logger.info("Picus2電動ピペットを初期化中...")
            from src.devices.picus2 import Picus2Controller, ConnectionType

            # 接続設定を取得（USB接続がデフォルト）
            picus2_address = self.device_configs.get('picus2_address', 'COM4')
            picus2_connection_type = self.device_configs.get('picus2_connection_type', 'usb')

            # 接続タイプを決定
            if picus2_connection_type.lower() == 'bluetooth':
                connection_type = ConnectionType.BLUETOOTH
                logger.info(f"  接続モード: Bluetooth (MAC: {picus2_address})")
            else:
                connection_type = ConnectionType.USB
                logger.info(f"  接続モード: USB (Port: {picus2_address})")

            # 再接続機能付きで接続
            for attempt in range(self.picus2_max_retries):
                try:
                    logger.info(f"  接続試行 {attempt + 1}/{self.picus2_max_retries}...")
                    self.picus2 = Picus2Controller(picus2_address, connection_type=connection_type)
                    # connect() は失敗時に ConnectionError を送出するが、
                    # 旧実装（bool を返すだけ）でも成功扱いにしないよう戻り値も検査する
                    connected = await self.picus2.connect()
                    if connected is False or connected is None:
                        raise ConnectionError(
                            f"Picus2 への接続に失敗しました (address={picus2_address})"
                        )
                    logger.info("  ✅ Picus2接続成功！")

                    # モーターモードを有効化
                    await self.picus2.set_motor_mode(True)
                    logger.info("✓ Picus2電動ピペット初期化完了（モーターモード有効）")
                    return True

                except Exception as e:
                    logger.warning(f"  ❌ 接続失敗: {str(e)}")
                    if attempt < self.picus2_max_retries - 1:
                        wait_time = self.picus2_retry_delays[attempt]
                        logger.info(f"  ⏳ {wait_time}秒待機してから再試行します...")
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error("  ❌ 最大試行回数に達しました")

            return False

        except Exception as e:
            logger.error(f"Picus2電動ピペット初期化エラー: {e}")
            return False

    async def _initialize_ika(self) -> bool:
        """IKA撹拌・加熱装置を初期化"""
        try:
            logger.info("IKA撹拌・加熱装置を初期化中...")
            from src.devices.ika import IKAController

            ika_port = self.device_configs.get('ika_port', 'COM5')
            self.ika = IKAController(port=ika_port)

            if not self.ika.connect():
                logger.error("IKA撹拌・加熱装置の接続に失敗しました")
                return False

            logger.info("✓ IKA撹拌・加熱装置初期化完了")
            return True

        except Exception as e:
            logger.error(f"IKA撹拌・加熱装置初期化エラー: {e}")
            return False

    async def _initialize_powder_dispenser(self) -> bool:
        """粉体排出装置を初期化"""
        try:
            logger.info("粉体排出装置を初期化中...")
            from src.devices.powder_dispenser import KonaRollerController

            powder_dispenser_port = self.device_configs.get('powder_dispenser_port', 'COM6')
            self.powder_dispenser = KonaRollerController(port=powder_dispenser_port)

            if not self.powder_dispenser.connect():
                logger.error("粉体排出装置の接続に失敗しました")
                return False

            logger.info("✓ 粉体排出装置初期化完了")
            return True

        except Exception as e:
            logger.error(f"粉体排出装置初期化エラー: {e}")
            return False

    # ========================================
    # 高レベル操作メソッド（L2層から呼ばれる）
    # ========================================

    async def rotate(self, angle: float):
        """
        安全に角度を変更（Joint1を回転させてエンドエフェクタを回転）- 絶対角度指定

        Args:
            angle: 目標角度（度）- Joint1角度（ベース回転）の絶対位置

        Raises:
            WorkspaceViolationError: 目標角度が可動域外の場合
            RuntimeError: Dobotが初期化されていない場合
        """
        if self.dobot is None:
            raise RuntimeError("Dobotが初期化されていません")

        # 可動域チェック
        if self.workspace_validator:
            self.workspace_validator.validate_joint1(angle)

        try:
            logger.info(f"Dobot: {angle}度に回転中...")

            # PyDobotControllerのmove_angleを使用
            self.dobot.move_angle(angle)

            await asyncio.sleep(self.wait_after_angle_move)
            logger.info(f"✓ Dobot: {angle}度回転完了")

        except ValidationError:
            # 可動域違反: 移動コマンドは送信されておらずアームは動いていない。
            # 復帰動作は不要なので、未検証の go_home をさせずにそのまま送出する
            raise
        except Exception as e:
            logger.error(f"回転エラー: {e}")
            await self._recover_home()
            raise

    # 回転速度マッピング（low/normal/high → DobotConfigプリセット名）
    ROTATION_SPEED_MAP = {"low": "低速", "normal": "中速", "high": "高速"}

    async def rotate_relative(self, delta_angle: float, speed: str = None):
        """
        現在位置から相対的に回転（Joint1を回転させてエンドエフェクタを回転）

        Args:
            delta_angle: 回転角度（度）- 正=反時計回り（上から見て、+Y方向）、負=時計回り。
                         Dobot Magician の J1（ベース）角度に加算される
            speed: 回転速度 ("low", "normal", "high")。Noneの場合は現在のプリセットを維持

        Raises:
            WorkspaceViolationError: 回転後の角度が可動域外の場合
            RuntimeError: Dobotが初期化されていません
        """
        if self.dobot is None:
            raise RuntimeError("Dobotが初期化されていません")

        # speed指定時はプリセットを一時的に切り替え
        original_preset = None
        if speed and speed in self.ROTATION_SPEED_MAP:
            original_preset = self.dobot.speed_preset
            self.dobot.set_speed_preset(self.ROTATION_SPEED_MAP[speed])

        try:
            # 現在のジョイント角度を取得
            current_pose = self.dobot.get_current_position()
            current_angle = current_pose[4]  # J1角度（ベース回転）
            target_angle = current_angle + delta_angle

            # 可動域チェック
            if self.workspace_validator:
                self.workspace_validator.validate_joint1_relative(current_angle, delta_angle)

            logger.info(f"Dobot: 現在{current_angle:.1f}°から{delta_angle:+.1f}°回転（目標: {target_angle:.1f}°, 速度: {speed or 'default'}）...")

            # PyDobotControllerのmove_angleを使用
            self.dobot.move_angle(target_angle)

            await asyncio.sleep(self.wait_after_angle_move)
            logger.info(f"✓ Dobot: {delta_angle:+.1f}°回転完了（現在: {target_angle:.1f}°）")

        except ValidationError:
            # 可動域違反: 移動コマンドは送信されておらずアームは動いていない。
            # 復帰動作は不要なので、未検証の go_home をさせずにそのまま送出する
            raise
        except Exception as e:
            logger.error(f"相対回転エラー: {e}")
            await self._recover_home()
            raise
        finally:
            # プリセットを元に戻す
            if original_preset and self.dobot:
                self.dobot.set_speed_preset(original_preset)

    async def move_z(self, distance: float):
        """
        安全にZ軸移動

        Args:
            distance: 移動距離（mm、正=上昇、負=下降）

        Raises:
            WorkspaceViolationError: 移動後のZ座標が可動域外の場合
            RuntimeError: Dobotが初期化されていない場合
        """
        if self.dobot is None:
            raise RuntimeError("Dobotが初期化されていません")

        try:
            # 現在位置を取得
            current_pose = self.dobot.get_current_position()

            # 可動域チェック
            if self.workspace_validator:
                self.workspace_validator.validate_z_relative(current_pose[2], distance)

            direction = "上昇" if distance > 0 else "下降"
            logger.info(f"Dobot: Z軸 {abs(distance):.1f}mm {direction}中...")

            # PyDobotControllerのmove_Zを使用
            self.dobot.move_Z(distance)

            await asyncio.sleep(self.wait_after_z_move)
            logger.info(f"✓ Dobot: Z軸移動完了")

        except ValidationError:
            # 可動域違反: 移動コマンドは送信されておらずアームは動いていない。
            # 復帰動作は不要なので、未検証の go_home をさせずにそのまま送出する
            raise
        except Exception as e:
            logger.error(f"Z軸移動エラー: {e}")
            await self._recover_home()
            raise

    async def move_xyz(self, x: float, y: float, z: float):
        """
        安全にXYZ絶対位置へ移動

        Args:
            x, y, z: 目標座標（mm）

        Raises:
            WorkspaceViolationError: 目標座標が可動域外の場合
            RuntimeError: Dobotが初期化されていない場合
        """
        if self.dobot is None:
            raise RuntimeError("Dobotが初期化されていません")

        # 可動域チェック
        if self.workspace_validator:
            self.workspace_validator.validate_xyz(x, y, z)

        try:
            logger.info(f"Dobot: XYZ({x:.1f}, {y:.1f}, {z:.1f})へ移動中...")

            # PyDobotControllerのmove_XYZ_absを使用
            self.dobot.move_XYZ_abs(x, y, z)

            await asyncio.sleep(self.wait_after_xy_move)
            logger.info(f"✓ Dobot: XYZ移動完了")

        except ValidationError:
            # 可動域違反: 移動コマンドは送信されておらずアームは動いていない。
            # 復帰動作は不要なので、未検証の go_home をさせずにそのまま送出する
            raise
        except Exception as e:
            logger.error(f"XYZ移動エラー: {e}")
            await self._recover_home()
            raise

    async def move_radial(self, distance: float):
        """
        半径方向への相対移動（円柱座標系）

        現在のアーム方向を維持したまま、基部からの距離を変更します。
        正の値で外向き（基部から離れる）、負の値で内向き（基部に近づく）。

        Args:
            distance: 半径方向の移動距離（mm）。正=外向き、負=内向き

        Raises:
            WorkspaceViolationError: 移動後の座標が可動域外の場合
            RuntimeError: Dobotが初期化されていない場合
            ValueError: 現在の半径距離が0に近すぎて方向が定義できない場合
        """
        if self.dobot is None:
            raise RuntimeError("Dobotが初期化されていません")

        try:
            current_pose = self.dobot.get_current_position()
            x, y = current_pose[0], current_pose[1]
            r = math.sqrt(x**2 + y**2)

            if r < 1.0:
                raise ValueError(
                    f"現在の半径距離が小さすぎます（r={r:.1f}mm）。"
                    "基部直上では半径方向が定義できません"
                )

            scale = (r + distance) / r
            new_x = x * scale
            new_y = y * scale

            # 可動域チェック
            if self.workspace_validator:
                self.workspace_validator.validate_xyz(new_x, new_y, current_pose[2])

            direction = "外向き" if distance > 0 else "内向き"
            logger.info(f"Dobot: 半径方向 {abs(distance):.1f}mm {direction}移動中...")

            self.dobot.move_XYZ_abs(new_x, new_y, current_pose[2])

            await asyncio.sleep(self.wait_after_xy_move)
            logger.info(f"✓ Dobot: 半径方向移動完了（r: {r:.1f} -> {r + distance:.1f}mm）")

        except ValidationError:
            # 可動域違反: 移動コマンドは送信されておらずアームは動いていない。
            # 復帰動作は不要なので、未検証の go_home をさせずにそのまま送出する
            raise
        except Exception as e:
            logger.error(f"半径方向移動エラー: {e}")
            await self._recover_home()
            raise

    async def move_slider(self, position: float):
        """
        スライダー（リニアレール）を絶対位置に安全に移動

        Args:
            position: スライダー目標位置（mm）。0〜slider_max_position

        Raises:
            ValueError: 位置が範囲外の場合
            RuntimeError: Dobotが初期化されていない場合
        """
        if self.dobot is None:
            raise RuntimeError("Dobotが初期化されていません")

        if position < 0:
            raise ValueError(f"スライダー位置は0以上で指定してください（指定値: {position}）")
        if position > self.slider_max_position:
            raise ValueError(
                f"スライダー位置が最大値を超えています"
                f"（指定値: {position}, 最大: {self.slider_max_position}）"
            )

        try:
            logger.info(f"Dobot: スライダーを位置 {position:.1f} に移動中...")

            self.dobot.move_slider(position)

            await asyncio.sleep(self.wait_after_slider_move)
            logger.info(f"✓ Dobot: スライダー移動完了（位置: {position:.1f}）")

        except Exception as e:
            logger.error(f"スライダー移動エラー: {e}")
            await self._recover_home()
            raise

    async def move_conveyer(self, index: int, speed: float, duration: float):
        """
        コンベアベルトを安全に動作させる

        Args:
            index: コンベアベルトのインデックス（0 or 1）
            speed: 速度（mm/s）
            duration: 動作時間（秒）

        Raises:
            ValueError: パラメータが範囲外の場合
            RuntimeError: Dobotが初期化されていない場合
        """
        if self.dobot is None:
            raise RuntimeError("Dobotが初期化されていません")

        if not 0 <= index <= 1:
            raise ValueError(f"コンベアインデックスは0-1の範囲で指定してください（指定値: {index}）")
        if speed <= 0 or speed > 200:
            raise ValueError(f"コンベア速度は0より大きく200以下で指定してください（指定値: {speed}）")
        if duration <= 0 or duration > 300:
            raise ValueError(f"コンベア動作時間は0より大きく300秒以下で指定してください（指定値: {duration}）")

        try:
            logger.info(
                f"Dobot: コンベアベルト動作開始"
                f"（インデックス: {index}, 速度: {speed}, 時間: {duration}秒）..."
            )

            self.dobot.move_conveyer(index, speed, duration)

            await asyncio.sleep(self.wait_after_conveyer)
            logger.info(f"✓ Dobot: コンベアベルト動作完了")

        except Exception as e:
            logger.error(f"コンベアベルトエラー: {e}")
            raise

    @staticmethod
    def _nominal_pipette_time(volume: float, speed: int) -> float:
        """Picus2 マニュアル p.63 の表に基づく公称所要時間(秒)。

        実測との比較用にログ／実験記録へ残す。
        """
        from src.devices.picus2.picus2_controller import nominal_operation_time

        return nominal_operation_time(volume, speed)

    async def aspirate(self, volume: float, speed: int = 5):
        """
        安全に液体を吸引

        Args:
            volume: 吸引量（mL）
            speed: 速度（1-9）

        Raises:
            RuntimeError: Picus2が初期化されていない場合
            ValueError: パラメータが範囲外の場合、または容量オーバーの場合
        """
        # デバイス初期化チェック
        if not self.use_picus2 or self.picus2 is None:
            raise RuntimeError("Picus2が初期化されていません")

        # パラメータ検証
        if volume < self.min_pipette_volume:
            raise ValueError(f"吸引量が最小値未満です（指定: {volume}mL, 最小: {self.min_pipette_volume}mL）")

        if not 1 <= speed <= 9:
            raise ValueError(f"speedは1-9の範囲で指定してください（指定値: {speed}）")

        # 容量オーバーチェック
        new_volume = self._current_pipette_volume + volume
        if new_volume > self.max_pipette_volume:
            raise ValueError(
                f"容量オーバー: 現在 {self._current_pipette_volume:.2f}mL + "
                f"吸引 {volume:.2f}mL = {new_volume:.2f}mL > "
                f"最大容量 {self.max_pipette_volume}mL"
            )

        try:
            logger.info(
                f"電動ピペット {volume:.2f}mL 吸引中（速度: {speed}）... "
                f"[現在: {self._current_pipette_volume:.2f}mL → {new_volume:.2f}mL]"
            )
            nominal_time = self._nominal_pipette_time(volume, speed)
            logger.info(
                f"  nominal ≈{nominal_time:.2f} s for {volume:.1f} mL at speed {speed}"
            )
            await self.picus2.set_motor_mode(True)
            await self.picus2.aspirate(volume, speed=speed)
            await asyncio.sleep(self.wait_after_pipette)

            # ボリュームトラッキング更新
            self._current_pipette_volume = new_volume
            logger.info(f"✓ {volume:.2f}mL 吸引完了（保持量: {self._current_pipette_volume:.2f}mL）")
            return {"nominal_time_s": nominal_time}

        except Exception as e:
            logger.error(f"吸引エラー: {e}")
            raise

    async def dispense(self, volume: float, speed: int = 5):
        """
        安全に液体を分注

        Args:
            volume: 分注量（mL）
            speed: 速度（1-9）

        Raises:
            RuntimeError: Picus2が初期化されていない場合
            ValueError: パラメータが範囲外の場合、または保持量を超える分注の場合
        """
        # デバイス初期化チェック
        if not self.use_picus2 or self.picus2 is None:
            raise RuntimeError("Picus2が初期化されていません")

        # パラメータ検証
        if volume < self.min_pipette_volume:
            raise ValueError(f"分注量が最小値未満です（指定: {volume}mL, 最小: {self.min_pipette_volume}mL）")

        if not 1 <= speed <= 9:
            raise ValueError(f"speedは1-9の範囲で指定してください（指定値: {speed}）")

        # 保持量チェック
        if self._current_pipette_volume <= 0:
            logger.warning("警告: 吸引せずに分注しようとしています（保持量: 0mL）")

        if volume > self._current_pipette_volume:
            raise ValueError(
                f"分注量が保持量を超えています: "
                f"分注 {volume:.2f}mL > 保持量 {self._current_pipette_volume:.2f}mL"
            )

        new_volume = self._current_pipette_volume - volume

        try:
            logger.info(
                f"電動ピペット {volume:.2f}mL 分注中（速度: {speed}）... "
                f"[現在: {self._current_pipette_volume:.2f}mL → {new_volume:.2f}mL]"
            )
            nominal_time = self._nominal_pipette_time(volume, speed)
            logger.info(
                f"  nominal ≈{nominal_time:.2f} s for {volume:.1f} mL at speed {speed}"
            )
            await self.picus2.set_motor_mode(True)
            await self.picus2.dispense(volume, speed=speed)
            await asyncio.sleep(self.wait_after_pipette)

            # ボリュームトラッキング更新
            self._current_pipette_volume = new_volume
            logger.info(f"✓ {volume:.2f}mL 分注完了（残量: {self._current_pipette_volume:.2f}mL）")
            return {"nominal_time_s": nominal_time}

        except Exception as e:
            logger.error(f"分注エラー: {e}")
            raise

    async def blow_out(self, go_home: bool = True, speed: int = 1, delay_ms: int = 3000):
        """
        安全に液体をすべて排出する（ブローアウト）

        チップ内に残った液体を完全に排出します。
        分注後の残液除去や、粘性の高い液体の完全吐出に使用します。

        Args:
            go_home: 終了後にピストンをホームポジションに戻すか（デフォルト: True）
            speed: 排出速度（1-9、デフォルト: 1 = 最も遅い）
            delay_ms: 排出所要時間（ミリ秒、デフォルト: 3000）

        Raises:
            RuntimeError: Picus2が初期化されていない場合
            ValueError: speedが1-9の範囲外の場合
        """
        # デバイス初期化チェック
        if not self.use_picus2 or self.picus2 is None:
            raise RuntimeError("Picus2が初期化されていません")

        # パラメータ検証
        if not 1 <= speed <= 9:
            raise ValueError(f"speedは1-9の範囲で指定してください（指定値: {speed}）")

        if delay_ms < 0:
            raise ValueError(f"delay_msは0以上で指定してください（指定値: {delay_ms}）")

        previous_volume = self._current_pipette_volume

        try:
            logger.info(
                f"電動ピペット ブローアウト中（速度: {speed}, 待機: {delay_ms}ms）... "
                f"[排出量: {previous_volume:.2f}mL]"
            )

            # モーターモードを確実に有効化
            await self.picus2.set_motor_mode(True)

            # L0層のblow_out呼び出し
            await self.picus2.blow_out(go_home=go_home, speed=speed, delay_ms=delay_ms)

            # 追加の安全待機時間
            await asyncio.sleep(self.wait_after_pipette)

            # ボリュームトラッキングをリセット
            self._current_pipette_volume = 0.0
            logger.info(f"✓ ブローアウト完了（{previous_volume:.2f}mL 排出、保持量: 0.00mL）")

        except Exception as e:
            logger.error(f"ブローアウトエラー: {e}")
            raise

    # ===== ピペットボリューム管理 =====

    @property
    def pipette_volume(self) -> float:
        """
        現在のピペット保持量を取得（mL）

        Returns:
            float: 現在ピペット内に保持している液体量（mL）
        """
        return self._current_pipette_volume

    @property
    def pipette_remaining_capacity(self) -> float:
        """
        ピペットの残り容量を取得（mL）

        Returns:
            float: 追加で吸引可能な液体量（mL）
        """
        return self.max_pipette_volume - self._current_pipette_volume

    def reset_pipette_volume(self, volume: float = 0.0):
        """
        ピペットのボリュームトラッキングを手動リセット

        チップ交換後や、外部でピペット操作を行った場合に使用します。

        Args:
            volume: 設定する保持量（mL、デフォルト: 0.0）

        Raises:
            ValueError: volumeが0未満または最大容量を超える場合
        """
        if volume < 0:
            raise ValueError(f"volumeは0以上で指定してください（指定値: {volume}）")
        if volume > self.max_pipette_volume:
            raise ValueError(
                f"volumeが最大容量を超えています（指定: {volume}mL, 最大: {self.max_pipette_volume}mL）"
            )

        old_volume = self._current_pipette_volume
        self._current_pipette_volume = volume
        logger.info(f"ピペットボリュームをリセット: {old_volume:.2f}mL → {volume:.2f}mL")

    async def _recover_home(self):
        """移動失敗後のベストエフォートなホーム復帰（元の例外を隠さない）

        go_home() 自体の失敗（可動域違反による拒否を含む）はログに残して握りつぶし、
        呼び出し側で元の例外を再送出させる。中断（CancelledError /
        KeyboardInterrupt）はそのまま伝播させる。
        """
        try:
            await self.go_home()
        except Exception as e:
            logger.error(f"エラー後のホーム復帰を実行できませんでした: {e}")

    def _validate_home_legs(self, current_pose):
        """go_home() が送る各移動（Z上昇・Joint1回転・XYZ移動）を事前検証する

        いずれかが可動域外なら WorkspaceViolationError を送出する。
        全区間を先に検証するため、途中まで動いてから拒否されることはない。

        Returns:
            float or None: Z上昇量（上昇不要なら None）
        """
        home = self.home_position
        z_diff = None
        if current_pose[2] < home[2] - 0.1:
            z_diff = home[2] - current_pose[2]
        if self.workspace_validator:
            if z_diff is not None:
                self.workspace_validator.validate_z_relative(current_pose[2], z_diff)
            if len(home) >= 4:
                self.workspace_validator.validate_joint1(home[3])
            self.workspace_validator.validate_xyz(home[0], home[1], home[2])
        return z_diff

    async def go_home(self):
        """
        ホームポジションへ安全に復帰（Z軸 → Joint1角度 → XY座標の順）

        各区間（Z上昇・Joint1回転・XYZ移動）は、最初のコマンドを送る前に
        すべて可動域バリデータで検証する。

        Raises:
            WorkspaceViolationError: いずれかの区間が可動域外の場合（アームは動かない）
        """
        if self.dobot is None:
            logger.warning("Dobotが初期化されていないため、ホーム復帰をスキップします")
            return

        if self.home_position is None:
            logger.warning("ホームポジションが設定されていません。set_current_position_as_home()を先に呼び出してください")
            return

        try:
            logger.info("Dobot: ホームポジションへ復帰中...")

            # 現在位置を取得
            current_pose = self.dobot.get_current_position()

            # 全区間を事前検証（可動域外なら何も動かさずに送出）
            try:
                z_diff = self._validate_home_legs(current_pose)
            except ValidationError as e:
                logger.error(f"ホーム復帰を拒否しました（可動域外）: {e}")
                raise

            # ステップ1: Z軸がホーム位置より低い場合のみ上昇（衝突回避）
            # 現在Zがホームより高い場合は下降させない（障害物回避のため、ステップ3で調整）
            if z_diff is not None:
                logger.info(f"Z軸をホーム位置 {self.home_position[2]:.1f} まで上昇中...")
                self.dobot.move_Z(z_diff)
                await asyncio.sleep(self.wait_after_z_move)

            # ステップ2: Z軸が安全な位置になってからJoint1角度を回転
            if len(self.home_position) >= 4:
                logger.info(f"Joint1角度をホーム角度 {self.home_position[3]:.1f}° に回転中...")
                self.dobot.move_angle(self.home_position[3])
                await asyncio.sleep(self.wait_after_angle_move)

            # ステップ3: 最後にXY座標に移動
            logger.info(f"XY座標 ({self.home_position[0]:.1f}, {self.home_position[1]:.1f}) に移動中...")
            self.dobot.move_XYZ_abs(
                self.home_position[0],
                self.home_position[1],
                self.home_position[2]
            )
            await asyncio.sleep(self.wait_after_xy_move)

            logger.info("✓ Dobot: ホームポジション復帰完了")

        except ValidationError:
            raise
        except Exception as e:
            logger.error(f"ホーム復帰エラー: {e}")
            raise

    def get_current_position(self):
        """
        Dobotの現在位置を取得

        Returns:
            list: [X, Y, Z, R]の現在位置（mm）

        Raises:
            RuntimeError: Dobotが初期化されていない場合
        """
        if self.dobot is None:
            raise RuntimeError("Dobotが初期化されていません")

        try:
            position = self.dobot.get_current_position()
            logger.debug(f"Dobot: 現在位置: X={position[0]:.1f}, Y={position[1]:.1f}, Z={position[2]:.1f}, R={position[3]:.1f}°")
            return position

        except Exception as e:
            logger.error(f"位置取得エラー: {e}")
            raise

    def set_current_position_as_home(self):
        """
        現在位置をホームポジションとして設定

        現在のDobotの位置（X, Y, Z, Joint1）を取得し、それをホームポジションとして保存します。
        これにより、go_home()メソッドはこの位置（座標とJoint1角度）に戻るようになります。

        Returns:
            list: 新しいホームポジション [X, Y, Z, Joint1]

        Raises:
            RuntimeError: Dobotが初期化されていない場合

        Example:
            # 現在位置をホームとして記憶
            robot.set_current_position_as_home()

            # 他の作業を実行
            await robot.move_xyz(300, 100, 50)

            # 記憶したホーム位置に戻る
            await robot.go_home()
        """
        if self.dobot is None:
            raise RuntimeError("Dobotが初期化されていません")

        try:
            # 現在位置を取得（X, Y, Z, R, joint1-4を含む）
            current_pos = self.dobot.get_current_position()

            # ホームポジションを更新（X, Y, Z, J1角度）
            self.home_position = [current_pos[0], current_pos[1], current_pos[2], current_pos[4]]

            # 取得したホームを可動域で検証する。可動域外なら黙って受け入れず警告し、
            # go_home() はこのホームへの移動を拒否する（_validate_home_legs）。
            # 可動域外の位置はファームウェアのホーム（set_home_params）にも書き込まない。
            if not self.home_is_within_limits():
                return self.home_position

            # PyDobotControllerのset_home_paramsを呼び出し
            self.dobot.set_home_params(
                self.home_position[0],
                self.home_position[1],
                self.home_position[2],
                current_pos[4]
            )

            logger.info(f"✓ Dobot: ホームポジション設定完了: X={self.home_position[0]:.1f}, Y={self.home_position[1]:.1f}, Z={self.home_position[2]:.1f}, J1={self.home_position[3]:.1f}°")

            return self.home_position

        except Exception as e:
            logger.error(f"ホームポジション設定エラー: {e}")
            raise

    def home_is_within_limits(self) -> bool:
        """現在のホームポジションが可動域内か検証する（可動域外なら警告を出す）

        バリデータ未設定・ホーム未設定の場合は True を返す。
        """
        if self.workspace_validator is None or self.home_position is None:
            return True
        home = self.home_position
        try:
            self.workspace_validator.validate_xyz(home[0], home[1], home[2])
            if len(home) >= 4:
                self.workspace_validator.validate_joint1(home[3])
        except ValidationError as e:
            logger.warning(
                "警告: ホームとして取得した現在位置が config.yaml の workspace 可動域外です。"
                "go_home() はこのホームへの移動を拒否します。アームを可動域内へ手動で"
                f"移動してから再初期化してください。\n{e}"
            )
            return False
        return True

    def emergency_stop(self):
        """全デバイスを可能な範囲で即時停止する（緊急停止）。

        Ctrl+C（KeyboardInterrupt / CancelledError）などの中断時に呼び出される。
        Dobot のコマンドキュー強制停止（実行中の移動も即時停止）と、
        IKA の撹拌・加熱停止を行う。

        緊急停止経路であるため例外は送出しない（ベストエフォート）。
        切断は行わないので、この後に cleanup() を呼び出すこと。
        """
        logger.warning("=== 緊急停止を実行します ===")

        # Dobot: キュー強制停止＋残キュー破棄＋コンベア停止
        if self.dobot is not None:
            try:
                if self.dobot.force_stop():
                    logger.warning("✓ Dobot: コマンドキューを強制停止しました")
                else:
                    logger.error("Dobot の強制停止に失敗しました（未接続または通信エラー）")
            except Exception as e:
                logger.error(f"Dobot 強制停止エラー: {e}")

        # IKA: 撹拌・温度制御を停止（加熱の継続は危険）
        if self.ika is not None:
            try:
                self.ika.stop_stirring()
                logger.warning("✓ IKA: 撹拌・温度制御を停止しました")
            except Exception as e:
                logger.error(f"IKA 停止エラー: {e}")

        logger.warning("=== 緊急停止処理完了 ===")

    async def cleanup(self):
        """全デバイスを安全に切断

        1台の切断失敗や切断中の中断（CancelledError / KeyboardInterrupt）で
        残りのデバイスの切断が飛ばされないよう、デバイスごとに BaseException を
        捕捉して続行する。中断はすべての切断を試みた後に再送出する。
        """
        logger.info("=== LabRobot デバイス切断開始 ===")
        interrupted = None

        for name, device, is_async in (
            ("Dobot", self.dobot, False),
            ("Picus2", self.picus2, True),
            ("IKA", self.ika, False),
            ("粉体排出装置", self.powder_dispenser, False),
        ):
            if not device:
                continue
            try:
                if is_async:
                    await device.disconnect()
                else:
                    device.disconnect()
                logger.info(f"✓ {name}切断完了")
            except Exception as e:
                logger.error(f"{name}切断エラー: {e}")
            except BaseException as e:  # 中断されても残りの切断は続ける
                logger.error(f"{name}切断中に中断されました: {e!r}")
                if interrupted is None:
                    interrupted = e

        logger.info("=== LabRobot 全デバイス切断完了 ===")
        if interrupted is not None:
            raise interrupted

    async def __aenter__(self):
        """非同期コンテキストマネージャ: 入口"""
        if not await self.initialize():
            raise RuntimeError("LabRobotの初期化に失敗しました")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """非同期コンテキストマネージャ: 出口"""
        await self.cleanup()

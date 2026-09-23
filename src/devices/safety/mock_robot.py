import asyncio
import logging
import math
from typing import Optional

from src.devices.safety.validators import ValidationError, WorkspaceValidator

logger = logging.getLogger(__name__)

class MockLabRobot:
    """
    GUIテスト用モッククラス
    LabRobotと同じメソッドを持つが、実機には接続しない。
    """
    def __init__(
        self,
        use_dobot: bool = False,
        use_picus2: bool = False,
        use_scale: bool = False,
        use_camera: bool = False,
        workspace_validator: Optional[WorkspaceValidator] = None,
        **device_configs
    ):
        self.device_configs = device_configs
        # [X, Y, Z, R]
        self._pos = [250.0, 0.0, 150.0, 0.0]
        self._slider_pos = 0.0
        self.home_position = [250.0, 0.0, 150.0, 0.0]

        # 可動域バリデータ（Noneの場合はバリデーションなし）
        self.workspace_validator = workspace_validator

        # 使用フラグ
        self.use_dobot = use_dobot
        self.use_picus2 = use_picus2
        self.use_scale = use_scale
        self.use_camera = use_camera

    async def initialize(self) -> bool:
        logger.info("=== [MOCK] 仮想デバイス初期化 (成功) ===")
        await asyncio.sleep(0.5)
        return True

    async def rotate(self, angle: float):
        """絶対角度で回転"""
        # 可動域チェック
        if self.workspace_validator:
            self.workspace_validator.validate_joint1(angle)

        logger.info(f"[MOCK Dobot] 絶対回転: {self._pos[3]:.1f}° -> {angle:.1f}°")
        await asyncio.sleep(1.0)
        self._pos[3] = angle
        logger.info(f"✓ [MOCK Dobot] 絶対回転完了")

    async def rotate_relative(self, delta_angle: float, speed: str = None):
        """現在位置から相対的に回転"""
        current = self._pos[3]
        target = current + delta_angle

        # 可動域チェック
        if self.workspace_validator:
            self.workspace_validator.validate_joint1_relative(current, delta_angle)

        speed_info = f", 速度: {speed}" if speed else ""
        logger.info(f"[MOCK Dobot] 相対回転: {current:.1f}°から{delta_angle:+.1f}°（目標: {target:.1f}°{speed_info}）")
        await asyncio.sleep(1.0)
        self._pos[3] = target
        logger.info(f"✓ [MOCK Dobot] 相対回転完了（現在: {target:.1f}°）")

    async def move_z(self, distance: float):
        current_z = self._pos[2]

        # 可動域チェック
        if self.workspace_validator:
            self.workspace_validator.validate_z_relative(current_z, distance)

        target_z = current_z + distance
        logger.info(f"[MOCK Dobot] Z軸移動: {current_z} -> {target_z}")
        await asyncio.sleep(0.5)
        self._pos[2] = target_z
        logger.info(f"✓ [MOCK Dobot] Z軸移動完了")

    async def move_xyz(self, x: float, y: float, z: float):
        # 可動域チェック
        if self.workspace_validator:
            self.workspace_validator.validate_xyz(x, y, z)

        logger.info(f"[MOCK Dobot] XYZ移動: ({x}, {y}, {z})")
        await asyncio.sleep(1.0)
        self._pos[0], self._pos[1], self._pos[2] = x, y, z
        logger.info(f"✓ [MOCK Dobot] XYZ移動完了")

    async def move_radial(self, distance: float):
        """[MOCK] 半径方向移動のシミュレーション"""
        x, y = self._pos[0], self._pos[1]
        r = math.sqrt(x**2 + y**2)

        if self.workspace_validator and r >= 1.0:
            scale = (r + distance) / r
            new_x, new_y = x * scale, y * scale
            self.workspace_validator.validate_xyz(new_x, new_y, self._pos[2])
            self._pos[0], self._pos[1] = new_x, new_y

        direction = "外向き" if distance > 0 else "内向き"
        logger.info(f"[MOCK Dobot] 半径方向移動: {abs(distance):.1f}mm {direction}（r: {r:.1f} -> {r + distance:.1f}mm）")
        await asyncio.sleep(0.5)
        logger.info(f"✓ [MOCK Dobot] 半径方向移動完了")

    async def move_slider(self, position: float):
        """[MOCK] スライダー移動のシミュレーション"""
        logger.info(f"[MOCK Dobot] スライダー移動: {self._slider_pos:.1f} -> {position:.1f}")
        await asyncio.sleep(1.0)
        self._slider_pos = position
        logger.info(f"✓ [MOCK Dobot] スライダー移動完了（位置: {position:.1f}）")

    async def move_conveyer(self, index: int, speed: float, duration: float):
        """[MOCK] コンベアベルト動作のシミュレーション"""
        logger.info(
            f"[MOCK Dobot] コンベアベルト動作"
            f"（インデックス: {index}, 速度: {speed}, 時間: {duration}秒）"
        )
        await asyncio.sleep(0.5)
        logger.info(f"✓ [MOCK Dobot] コンベアベルト動作完了")

    async def go_home(self):
        """LabRobot.go_home と同じく、全区間を事前検証してから復帰する"""
        home = self.home_position
        if self.workspace_validator:
            if self._pos[2] < home[2] - 0.1:
                self.workspace_validator.validate_z_relative(self._pos[2], home[2] - self._pos[2])
            if len(home) >= 4:
                self.workspace_validator.validate_joint1(home[3])
            self.workspace_validator.validate_xyz(home[0], home[1], home[2])
        logger.info("[MOCK Dobot] ホームポジションへ復帰中...")
        await asyncio.sleep(1.0)
        self._pos[:] = list(self.home_position)
        logger.info("✓ [MOCK Dobot] ホーム復帰完了")

    def set_current_position_as_home(self):
        self.home_position = list(self._pos)
        logger.info(f"✓ [MOCK Dobot] 現在位置をホームに設定: {self.home_position}")
        self.home_is_within_limits()
        return self.home_position

    def home_is_within_limits(self) -> bool:
        """LabRobot.home_is_within_limits と同じ検証（可動域外なら警告）"""
        if self.workspace_validator is None or self.home_position is None:
            return True
        home = self.home_position
        try:
            self.workspace_validator.validate_xyz(home[0], home[1], home[2])
            if len(home) >= 4:
                self.workspace_validator.validate_joint1(home[3])
        except ValidationError as e:
            logger.warning(
                "警告: [MOCK] ホームとして取得した現在位置が workspace 可動域外です。"
                f"go_home() はこのホームへの移動を拒否します。\n{e}"
            )
            return False
        return True

    # ===== 電動ピペット操作 =====

    @staticmethod
    def _nominal_pipette_time(volume: float, speed: int) -> float:
        """Picus2 マニュアル p.63 の表に基づく公称所要時間(秒)"""
        from src.devices.picus2.picus2_controller import nominal_operation_time

        return nominal_operation_time(volume, speed)

    async def aspirate(self, volume: float, speed: int = 5):
        """[MOCK] 液体吸引のシミュレーション"""
        nominal_time = self._nominal_pipette_time(volume, speed)
        logger.info(f"[MOCK] 吸引中: {volume:.2f}mL（速度: {speed}）")
        logger.info(f"  nominal ≈{nominal_time:.2f} s for {volume:.1f} mL at speed {speed}")
        await asyncio.sleep(0.5)
        logger.info(f"✓ [MOCK] 吸引完了: {volume:.2f}mL")
        return {"nominal_time_s": nominal_time}

    async def dispense(self, volume: float, speed: int = 5):
        """[MOCK] 液体分注のシミュレーション"""
        nominal_time = self._nominal_pipette_time(volume, speed)
        logger.info(f"[MOCK] 分注中: {volume:.2f}mL（速度: {speed}）")
        logger.info(f"  nominal ≈{nominal_time:.2f} s for {volume:.1f} mL at speed {speed}")
        await asyncio.sleep(0.5)
        logger.info(f"✓ [MOCK] 分注完了: {volume:.2f}mL")
        return {"nominal_time_s": nominal_time}

    async def blow_out(self, go_home: bool = True, speed: int = 1, delay_ms: int = 3000):
        """[MOCK] ブローアウトのシミュレーション"""
        logger.info(f"[MOCK] ブローアウト中（速度: {speed}, 待機: {delay_ms}ms）")
        await asyncio.sleep(0.5)
        if go_home:
            logger.info("[MOCK] ピストンをホームに戻す")
        logger.info("✓ [MOCK] ブローアウト完了")

    # ===== 電子天秤操作（BCE8221） =====

    async def measure_weight(self, stabilization_count: int = 3) -> float:
        """
        [MOCK] BCE8221で安定した重量測定を行うシミュレーション

        Args:
            stabilization_count: 測定回数（中央値を返す）

        Returns:
            float: 測定重量（g）- モックでは固定値を返す
        """
        logger.info(f"[MOCK] BCE8221重量測定中（{stabilization_count}回測定）...")
        await asyncio.sleep(0.5)
        mock_weight = 12.345  # モック値
        logger.info(f"✓ [MOCK] 重量測定完了: {mock_weight:.3f}g")
        return mock_weight

    async def tare_scale(self, delay: float = 1.0):
        """
        [MOCK] BCE8221電子天秤を風袋引き（ゼロ点リセット）するシミュレーション

        Args:
            delay: 風袋引き後の待機時間（秒）
        """
        logger.info("[MOCK] BCE8221電子天秤を風袋引き中...")
        await asyncio.sleep(delay)
        logger.info("✓ [MOCK] 風袋引き完了")

    # ===== カメラ操作 =====

    async def capture_and_save(self, file_path: Optional[str] = None) -> Optional[str]:
        """
        [MOCK] Webcamで画像をキャプチャして保存するシミュレーション

        Args:
            file_path: 保存先のファイル名（省略時は自動生成）

        Returns:
            Optional[str]: 保存したファイルパス
        """
        mock_path = file_path or "captured_images/mock_capture.jpg"
        logger.info(f"[MOCK] Webcamで画像キャプチャ・保存中...")
        logger.info(f"  保存先: {mock_path}")
        await asyncio.sleep(0.5)
        logger.info(f"✓ [MOCK] 画像キャプチャ・保存完了: {mock_path}")
        return mock_path

    def emergency_stop(self):
        """[MOCK] 緊急停止のシミュレーション（LabRobotとのインターフェース互換用）"""
        logger.warning("[MOCK] 緊急停止を実行しました")

    async def cleanup(self):
        logger.info("✓ [MOCK] 仮想接続を切断")

    async def __aenter__(self):
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.cleanup()


class MockSharedDevices:
    """SharedDevices のモック（天秤・カメラの共有デバイス）

    MockLabRobot と対になる共有デバイス側のモック。実機を持たない環境でも
    measurements.csv / summary.md が実行時と同じ形で埋まるように、
    ``measure_weight`` は float を、``capture_and_save`` / ``capture_microscope``
    は保存先パス文字列を返す（None を返すと記録が空になり、記録経路のバグに
    気付けない）。
    """

    #: measure_weight が返すモック測定値 (g)
    MOCK_WEIGHT = 4.98

    def __init__(self, use_scale: bool = False, use_camera: bool = False,
                 use_microscope: bool = False, use_microscope_serial: bool = False, **_ignored):
        self.use_scale = use_scale
        self.use_camera = use_camera
        self.use_microscope = use_microscope
        self.use_microscope_serial = use_microscope_serial
        self._captures = 0
        self._microscope_captures = 0
        self.microscope_led_on = True
        self.microscope_led_level = 12
        self.microscope_focus_position = 1568   # 実機の出荷時レンズ位置

    async def initialize(self) -> bool:
        logger.info("=== [MOCK] 共有デバイス初期化 (成功) ===")
        return True

    async def measure_weight(self, stabilization_count: int = 3) -> float:
        logger.info(f"[MOCK] 重量測定中（{stabilization_count}回測定）...")
        await asyncio.sleep(0.1)
        logger.info(f"✓ [MOCK] 重量測定完了: {self.MOCK_WEIGHT:.3f}g")
        return self.MOCK_WEIGHT

    async def tare_scale(self, delay: float = 1.0):
        logger.info("[MOCK] 電子天秤を風袋引き中...")
        await asyncio.sleep(min(delay, 0.1))
        logger.info("✓ [MOCK] 風袋引き完了")

    async def capture_and_save(self, file_path: Optional[str] = None) -> str:
        self._captures += 1
        path = file_path or f"captured_images/mock_capture_{self._captures:03d}.jpg"
        logger.info(f"[MOCK] 画像キャプチャ・保存中: {path}")
        await asyncio.sleep(0.1)
        logger.info(f"✓ [MOCK] 画像キャプチャ・保存完了: {path}")
        return path

    async def capture_microscope(self, file_path: Optional[str] = None) -> str:
        self._microscope_captures += 1
        path = file_path or f"microscope_images/mock_microscope_{self._microscope_captures:03d}.jpg"
        logger.info(f"[MOCK] 顕微鏡画像キャプチャ・保存中: {path}")
        await asyncio.sleep(0.1)
        logger.info(f"✓ [MOCK] 顕微鏡画像キャプチャ・保存完了: {path}")
        return path

    async def set_microscope_led(self, on: bool = True, level: Optional[int] = None) -> bool:
        if level is not None:
            self.microscope_led_level = int(level)
        self.microscope_led_on = bool(on)
        await asyncio.sleep(0.05)
        logger.info(f"✓ [MOCK] 顕微鏡 LED {'ON' if on else 'OFF'}"
                    + (f" (level {level})" if level is not None else ""))
        return self.microscope_led_on

    async def focus_microscope(self, mode: str = "auto", position: Optional[int] = None,
                               direction: str = "in", steps: int = 1,
                               timeout: float = 60.0) -> dict:
        if mode == "auto":
            self.microscope_focus_position = 1400
        elif mode == "position":
            if position is None:
                raise ValueError("mode='position' には position が必要です")
            self.microscope_focus_position = int(position)
        elif mode == "step":
            # 実機観測: "out" で位置の値が増え、"in" で減る（1 押し ≈ 14）
            self.microscope_focus_position += (-14 if direction == "in" else 14) * int(steps)
        else:
            raise ValueError(f"unknown focus mode: {mode!r}")
        await asyncio.sleep(0.05)
        logger.info(f"✓ [MOCK] 顕微鏡フォーカス {mode}: レンズ位置 {self.microscope_focus_position}")
        return {"focus_position": self.microscope_focus_position, "focus_converged": True}

    async def cleanup(self):
        logger.info("✓ [MOCK] 共有デバイスの仮想接続を切断")

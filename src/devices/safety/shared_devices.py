"""
L1層: 共有デバイス管理クラス（SharedDevices）

このモジュールは複数のロボットアーム間で共有される
デバイス（カメラ、電子天秤など）を管理します。

設計思想:
- ロボットアーム（LabRobot）とは独立したライフサイクルで管理
- 複数のロボットから同じデバイスにアクセス可能
- robot_idに依存しない操作インターフェース
"""

import asyncio
import logging
import threading
from typing import Optional

# ロギング設定
logger = logging.getLogger(__name__)


def parse_resolution(value) -> Optional[tuple]:
    """'3840x2160' / (3840, 2160) / None -> (w, h) または None（ドライバ既定）"""
    if value is None or value == "":
        return None
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return int(value[0]), int(value[1])
    text = str(value).lower().replace("\u00d7", "x").replace("*", "x")
    try:
        w, h = text.split("x")
        return int(w), int(h)
    except ValueError:
        raise ValueError(f"microscope_resolution must be 'WIDTHxHEIGHT', got {value!r}")


class SharedDevices:
    """
    共有デバイス管理クラス

    カメラや電子天秤など、特定のロボットアームに属さない
    共有リソースを管理します。
    """

    def __init__(self,
                 use_scale: bool = False,
                 use_camera: bool = False,
                 use_microscope: bool = False,
                 **device_configs):
        """
        SharedDevicesを初期化

        Args:
            use_scale: 電子天秤（BCE8221）を使用するか
            use_camera: Webcamを使用するか
            use_microscope: USB デジタル顕微鏡（UVC）を使用するか
            **device_configs: デバイス固有の設定
                - scale_port: str (デフォルト: 'COM4') - BCE8221天秤のCOMポート
                - camera_index: int (デフォルト: 1) - Webcamのデバイスインデックス
                - microscope_index: int (デフォルト: 2) - 顕微鏡のデバイスインデックス
                - microscope_port: str (デフォルト: '') - 顕微鏡の制御用シリアルポート
                  (UM22 系の CP210x。空なら LED 制御なし、撮影のみ)
                - microscope_resolution: str (デフォルト: '3840x2160') - 顕微鏡の撮影解像度
                  ("幅x高さ"。データ量を抑えるなら "1280x720" など)
        """
        # デバイス使用フラグ
        self.use_scale = use_scale
        self.use_camera = use_camera
        self.use_microscope = use_microscope

        # デバイスインスタンス（初期化前はNone）
        self.scale = None
        self.camera = None
        self.microscope = None
        self.microscope_serial = None

        # デバイス設定
        self.device_configs = device_configs

        # 待機時間設定
        self.wait_after_scale = device_configs.get('wait_after_scale', 2.0)

        # 初期化フラグ
        self._initialized = False

    async def initialize(self) -> bool:
        """
        共有デバイスを初期化

        Returns:
            bool: 初期化が成功したらTrue
        """
        try:
            logger.info("=== SharedDevices 初期化開始 ===")

            # Scale（電子天秤）の初期化
            if self.use_scale:
                if not await self._initialize_scale():
                    return False

            # Camera（カメラ）の初期化
            if self.use_camera:
                if not await self._initialize_camera():
                    return False

            # Microscope（USB デジタル顕微鏡）の初期化
            if self.use_microscope:
                if not await self._initialize_microscope():
                    return False

            self._initialized = True
            logger.info("=== SharedDevices 全デバイス初期化完了 ===\n")
            return True

        except Exception as e:
            logger.error(f"SharedDevices初期化エラー: {e}")
            return False

    async def _initialize_scale(self) -> bool:
        """BCE8221電子天秤を初期化"""
        try:
            logger.info("BCE8221電子天秤を初期化中...")
            from src.devices.scale.BCE8221 import SerialBalance

            scale_port = self.device_configs.get('scale_port', 'COM4')
            self.scale = SerialBalance(port=scale_port)

            # 接続テスト（重量を取得してみる）
            weight = self.scale.get_weight()
            if weight is None:
                logger.error("BCE8221電子天秤から重量を取得できませんでした")
                return False

            logger.info(f"✓ BCE8221電子天秤初期化完了: {weight:.3f}g")
            return True

        except Exception as e:
            logger.error(f"BCE8221電子天秤初期化エラー: {e}")
            return False

    async def _initialize_camera(self) -> bool:
        """Webcamを初期化"""
        try:
            logger.info("Webcamを初期化中...")
            from src.devices.webcam import WebcamController

            camera_index = self.device_configs.get('camera_index', 1)
            self.camera = WebcamController(camera_index=camera_index)

            # カメラ接続
            if not self.camera.connect():
                logger.error("Webcamの接続に失敗しました")
                return False

            logger.info("✓ Webcam初期化完了")
            return True

        except Exception as e:
            logger.error(f"Webcam初期化エラー: {e}")
            return False

    async def _initialize_microscope(self) -> bool:
        """USB デジタル顕微鏡（UVC）を初期化"""
        try:
            logger.info("デジタル顕微鏡を初期化中...")
            from src.devices.microscope import MicroscopeController

            microscope_index = self.device_configs.get('microscope_index', 2)
            resolution = parse_resolution(self.device_configs.get('microscope_resolution'))
            self.microscope = MicroscopeController(camera_index=microscope_index, resolution=resolution)

            if not self.microscope.connect():
                logger.error("デジタル顕微鏡の接続に失敗しました")
                return False

            # 制御用シリアル（LED 等）は port が設定されているときだけ開く
            microscope_port = self.device_configs.get('microscope_port') or ""
            if microscope_port:
                from src.devices.microscope import UM22SerialController
                self.microscope_serial = UM22SerialController(port=microscope_port)
                if not self.microscope_serial.connect():
                    logger.error(f"デジタル顕微鏡の制御ポート {microscope_port} に接続できません")
                    self.microscope_serial = None
                    return False
                logger.info(f"✓ デジタル顕微鏡 制御ポート接続完了 ({microscope_port}, "
                            f"{self.microscope_serial.get_model()})")

            logger.info("✓ デジタル顕微鏡初期化完了")
            return True

        except Exception as e:
            logger.error(f"デジタル顕微鏡初期化エラー: {e}")
            return False

    # ========================================
    # 電子天秤操作
    # ========================================

    async def measure_weight(self, stabilization_count: int = 3) -> float:
        """
        BCE8221で安定した重量測定を行う

        Args:
            stabilization_count: 測定回数（中央値を返す）

        Returns:
            float: 測定重量（g）

        Raises:
            RuntimeError: 電子天秤が初期化されていない場合
            ValueError: 有効な重量が取得できなかった場合
        """
        if not self.use_scale or self.scale is None:
            raise RuntimeError("BCE8221電子天秤が初期化されていません")

        try:
            weights = []
            for i in range(stabilization_count):
                weight = self.scale.get_weight()
                if weight is not None:
                    weights.append(weight)
                await asyncio.sleep(0.5)

            if not weights:
                raise ValueError("有効な重量データを取得できませんでした")

            # 中央値を返す（外れ値の影響を減らす）
            weights.sort()
            median_weight = weights[len(weights) // 2]

            logger.info(f"✓ 重量測定: {median_weight:.3f}g")
            await asyncio.sleep(self.wait_after_scale)
            return median_weight

        except Exception as e:
            logger.error(f"重量測定エラー: {e}")
            raise

    async def tare_scale(self, delay: float = 1.0):
        """
        BCE8221電子天秤を風袋引き（ゼロ点リセット）

        Args:
            delay: 風袋引き後の待機時間（秒）

        Raises:
            RuntimeError: 電子天秤が初期化されていない場合
        """
        if not self.use_scale or self.scale is None:
            raise RuntimeError("BCE8221電子天秤が初期化されていません")

        try:
            logger.info("BCE8221電子天秤を風袋引き中...")
            self.scale.tare()
            await asyncio.sleep(delay)
            logger.info("✓ 風袋引き完了")

        except Exception as e:
            logger.error(f"風袋引きエラー: {e}")
            raise

    # ========================================
    # カメラ操作
    # ========================================

    async def capture_and_save(self, file_path: Optional[str] = None) -> Optional[str]:
        """
        Webcamで画像をキャプチャして保存

        Args:
            file_path: 保存先のファイル名（省略時は自動生成）

        Returns:
            Optional[str]: 保存したファイルパス。失敗時はNone

        Raises:
            RuntimeError: カメラが初期化されていません
        """
        if not self.use_camera or self.camera is None:
            raise RuntimeError("カメラが初期化されていません")

        try:
            logger.info("Webcamで画像キャプチャ・保存中...")
            saved_path = self.camera.capture_and_save(file_path)

            if saved_path:
                logger.info(f"✓ 画像キャプチャ・保存完了: {saved_path}")
            else:
                logger.warning("画像の取得・保存に失敗しました")

            return saved_path

        except Exception as e:
            logger.error(f"画像キャプチャ・保存エラー: {e}")
            raise

    # ========================================
    # デジタル顕微鏡操作
    # ========================================

    async def capture_microscope(self, file_path: Optional[str] = None) -> Optional[str]:
        """
        USB デジタル顕微鏡で画像をキャプチャして保存

        Args:
            file_path: 保存先のファイル名（省略時は自動生成、microscope_images/ 配下）

        Returns:
            Optional[str]: 保存したファイルパス。失敗時はNone

        Raises:
            RuntimeError: 顕微鏡が初期化されていない場合
        """
        if not self.use_microscope or self.microscope is None:
            raise RuntimeError("デジタル顕微鏡が初期化されていません")

        try:
            logger.info("デジタル顕微鏡で画像キャプチャ・保存中...")
            saved_path = self.microscope.capture_and_save(file_path)

            if saved_path:
                logger.info(f"✓ 顕微鏡画像キャプチャ・保存完了: {saved_path}")
            else:
                logger.warning("顕微鏡画像の取得・保存に失敗しました")

            return saved_path

        except Exception as e:
            logger.error(f"顕微鏡画像キャプチャ・保存エラー: {e}")
            raise

    async def set_microscope_led(self, on: bool = True, level: Optional[int] = None) -> bool:
        """
        USB デジタル顕微鏡の LED 照明を点灯/消灯し、必要なら明るさも設定する

        Args:
            on: True = 点灯, False = 消灯
            level: 明るさ (0-255、出荷時 12)。None なら変更しない

        Returns:
            bool: 実行後の LED 状態

        Raises:
            RuntimeError: 顕微鏡の制御ポート (microscope_port) が設定・接続されていない場合
        """
        if not self.use_microscope or self.microscope_serial is None:
            raise RuntimeError(
                "顕微鏡の制御ポートが初期化されていません "
                "(config.yaml の shared_devices.microscope_port を設定してください)"
            )
        try:
            state = self.microscope_serial.set_led(on, level)
            await asyncio.sleep(0.2)
            return state
        except Exception as e:
            logger.error(f"顕微鏡 LED 制御エラー: {e}")
            raise

    async def focus_microscope(self, mode: str = "auto", position: Optional[int] = None,
                               direction: str = "in", steps: int = 1,
                               timeout: float = 60.0) -> dict:
        """
        USB デジタル顕微鏡（UM22 系）の焦点合わせ

        Args:
            mode: "auto" = ワンショット AF / "position" = レンズ位置を直接指定 /
                  "step" = ステップ移動
            position: mode="position" のときの目標位置 (0-65535)
            direction: mode="step" のときの方向 ("in" / "out")
            steps: mode="step" のときの回数
            timeout: モーター停止を待つ上限秒

        Returns:
            dict: {"focus_position": 最終レンズ位置, "focus_converged": bool}

        Raises:
            RuntimeError: 顕微鏡の制御ポートが初期化されていない場合
        """
        if not self.use_microscope or self.microscope_serial is None:
            raise RuntimeError(
                "顕微鏡の制御ポートが初期化されていません "
                "(config.yaml の shared_devices.microscope_port を設定してください)"
            )
        if mode not in ("auto", "position", "step"):
            raise ValueError(f"unknown focus mode: {mode!r}")
        if mode == "position" and position is None:
            raise ValueError("mode='position' には position が必要です")
        scope = self.microscope_serial

        def _run():
            if mode == "auto":
                return scope.autofocus(timeout_s=timeout)
            if mode == "position":
                return scope.goto_position(position, timeout_s=timeout)
            pos = scope.step_focus(direction, steps)
            return pos, pos is not None

        try:
            # AF の評価値は映像パイプラインが動いているときだけ更新されるので、
            # モーターが動いている間はカメラからフレームを読み続ける
            with self._pump_microscope_frames():
                pos, ok = await asyncio.to_thread(_run)
            await asyncio.sleep(0.2)
            return {"focus_position": pos, "focus_converged": bool(ok)}
        except Exception as e:
            logger.error(f"顕微鏡フォーカス制御エラー: {e}")
            raise

    def _pump_microscope_frames(self, interval_s: float = 0.1):
        """顕微鏡カメラのフレームを背景スレッドで読み続けるコンテキストマネージャ"""
        shared = self

        class _Pump:
            def __enter__(self_):
                self_.stop = threading.Event()
                self_.frames = 0
                cam = getattr(shared.microscope, "camera", None)
                self_.thread = None
                if cam is None or not hasattr(cam, "read"):
                    return self_

                def loop():
                    while not self_.stop.is_set():
                        try:
                            ok, _ = cam.read()
                            if ok:
                                self_.frames += 1
                        except Exception:
                            pass
                        self_.stop.wait(interval_s)

                self_.thread = threading.Thread(target=loop, name="microscope-frame-pump", daemon=True)
                self_.thread.start()
                return self_

            def __exit__(self_, *exc):
                self_.stop.set()
                if self_.thread is not None:
                    self_.thread.join(timeout=2.0)
                logger.debug(f"顕微鏡フレームポンプ終了: {self_.frames} frames")
                return False

        return _Pump()

    # ========================================
    # ライフサイクル管理
    # ========================================

    async def cleanup(self):
        """全共有デバイスを安全に切断"""
        logger.info("=== SharedDevices 切断開始 ===")

        try:
            if self.scale:
                self.scale.close()
                logger.info("✓ BCE8221電子天秤切断完了")
        except Exception as e:
            logger.error(f"BCE8221電子天秤切断エラー: {e}")

        try:
            if self.camera:
                self.camera.disconnect()
                logger.info("✓ Webcam切断完了")
        except Exception as e:
            logger.error(f"Webcam切断エラー: {e}")

        try:
            if self.microscope:
                self.microscope.disconnect()
                logger.info("✓ デジタル顕微鏡切断完了")
        except Exception as e:
            logger.error(f"デジタル顕微鏡切断エラー: {e}")

        try:
            if self.microscope_serial:
                self.microscope_serial.disconnect()
                logger.info("✓ デジタル顕微鏡 制御ポート切断完了")
        except Exception as e:
            logger.error(f"デジタル顕微鏡 制御ポート切断エラー: {e}")

        logger.info("=== SharedDevices 全デバイス切断完了 ===")

    async def __aenter__(self):
        """非同期コンテキストマネージャ: 入口"""
        if not await self.initialize():
            raise RuntimeError("SharedDevicesの初期化に失敗しました")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """非同期コンテキストマネージャ: 出口"""
        await self.cleanup()

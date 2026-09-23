"""
USB デジタル顕微鏡（UVC カメラ）制御クラス

サンワサプライ 400-CAM106 のような USB 接続のデジタル顕微鏡は、OS からは
UVC（USB Video Class）のカメラとして見える。このモジュールは
``WebcamController`` を継承し、顕微鏡向けの既定値だけを変える:

- 保存先は ``microscope_images/``（ウェブカメラの ``captured_images/`` と分ける）
- 既定解像度は 4K（3840x2160、JPEG 1 枚 0.4〜2 MB）。データ量を抑えたいときは下げる
- 露出が安定するまでの読み捨てフレーム数を多めにとる（UVC 顕微鏡は
  接続直後の数フレームが真っ暗・単色になりやすい）
- 必要なら手動露出を固定できる（``exposure``。None で自動露出）
- 接続時に UVC のホワイトバランスを一度書き込む（``white_balance``、既定 5000 K）。
  400-CAM106 は電源投入後にホストが WB コントロールを書くまで WB ゲインが
  適用されず、白い対象が強い緑かぶりで写る（2026-09-22/23 に実機で確認。
  メーカー製ビューアも接続時に書いている）。Windows では WB を書けるように
  DirectShow バックエンドで開く（失敗時は Media Foundation に戻す）

顕微鏡は共有デバイスとして ``SharedDevices.capture_microscope`` から使う。
デバイス index は環境ごとに違うので config.yaml の
``shared_devices.microscope_index`` に書く（既定値はプレースホルダ）。

使用例:
    from src.devices.microscope import MicroscopeController

    with MicroscopeController(camera_index=2) as scope:
        scope.capture_and_save("sample_001.jpg")   # -> microscope_images/sample_001.jpg
"""

import logging
import platform
from typing import Optional, Tuple

import cv2
import numpy as np

from src.devices.webcam.webcam_controller import WebcamController

logger = logging.getLogger(__name__)


class MicroscopeController(WebcamController):
    """
    USB デジタル顕微鏡（UVC）を制御して静止画を取得するクラス

    OpenCV の VideoCapture でフレームを取得する点はウェブカメラと同じ。
    """

    #: 撮影前に読み捨てるフレーム数（自動露出・ホワイトバランスの安定待ち）
    DEFAULT_WARMUP_FRAMES = 10
    #: 接続時に書き込むホワイトバランス（K）。この機種の既定値と同じ
    DEFAULT_WHITE_BALANCE = 5000
    #: 既定の撮影解像度。400-CAM106 は 4K が最も精細で、しかも最も速い
    #: （実測 3840x2160 ≈ 14 fps、1280x720 ≈ 6 fps、1920x1080 ≈ 2 fps）。
    #: JPEG 1 枚 0.4〜2 MB。データ量を抑えたいときは config.yaml の
    #: shared_devices.microscope_resolution で 1280x720 などに下げる
    DEFAULT_RESOLUTION = (3840, 2160)

    def __init__(
        self,
        camera_index: int = 2,
        resolution: Optional[Tuple[int, int]] = None,
        warmup_frames: int = DEFAULT_WARMUP_FRAMES,
        exposure: Optional[float] = None,
        white_balance: Optional[int] = DEFAULT_WHITE_BALANCE,
    ):
        """
        Args:
            camera_index: 顕微鏡のカメラデバイス index（プレースホルダ既定値: 2）。
                実際の値は config.yaml の shared_devices.microscope_index で指定する
            resolution: (width, height)。None なら DEFAULT_RESOLUTION (3840x2160)
            warmup_frames: 撮影前に読み捨てるフレーム数
            exposure: 手動露出値（OpenCV の CAP_PROP_EXPOSURE。Windows では
                -13..-1 程度の対数値）。None なら自動露出のまま
            white_balance: 接続時に書き込む WB（K、手動）。None なら書かない。
                書かないと電源投入後の緑かぶりが残る（モジュール docstring 参照）
        """
        super().__init__(camera_index=camera_index, resolution=resolution or self.DEFAULT_RESOLUTION)
        self.warmup_frames = max(0, int(warmup_frames))
        self.exposure = exposure
        self.white_balance = white_balance
        self.backend_name = None
        # 親クラスは __init__ 時点で captured_images/ を作るので、保存先を差し替えてから作り直す
        self.save_directory = "microscope_images"
        self._ensure_save_directory()

    def _open(self, backend) -> bool:
        cam = cv2.VideoCapture(self.camera_index, backend)
        if not cam.isOpened():
            cam.release()
            return False
        cam.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        cam.set(cv2.CAP_PROP_FRAME_WIDTH, self.resolution[0])
        cam.set(cv2.CAP_PROP_FRAME_HEIGHT, self.resolution[1])
        self.camera = cam
        return True

    def connect(self) -> bool:
        """顕微鏡に接続し、WB（と指定があれば手動露出）を適用する。

        Windows では WB を書き込める DirectShow で開き、開けなければ
        Media Foundation にフォールバックする。他 OS は親クラスと同じ選択。
        """
        try:
            if platform.system() == "Windows":
                if self._open(cv2.CAP_DSHOW):
                    self.backend_name = "DSHOW"
                elif self._open(cv2.CAP_MSMF):
                    self.backend_name = "MSMF"
                else:
                    logger.error(f"顕微鏡カメラ index {self.camera_index} を開けません")
                    return False
                self.is_connected = True
                logger.info(f"顕微鏡カメラ接続成功 - index {self.camera_index} ({self.backend_name}), "
                            f"解像度 {int(self.camera.get(cv2.CAP_PROP_FRAME_WIDTH))}x"
                            f"{int(self.camera.get(cv2.CAP_PROP_FRAME_HEIGHT))}")
            else:
                if not super().connect():
                    return False
                self.backend_name = "default"
        except Exception as e:
            logger.error(f"顕微鏡カメラ接続エラー: {e}")
            self.is_connected = False
            return False
        if self.white_balance is not None:
            self.apply_white_balance(self.white_balance)
        if self.exposure is not None:
            self._apply_exposure(self.exposure)
        return True

    def apply_white_balance(self, kelvin: int) -> bool:
        """WB を手動・指定色温度で書き込む。

        400-CAM106 は電源投入後にこの書き込みがあるまで WB ゲインが効かず緑かぶりする。
        Media Foundation では書き込めない（False が返る）ので Windows では DirectShow で開く。
        """
        try:
            self.camera.set(cv2.CAP_PROP_AUTO_WB, 0)
            ok = bool(self.camera.set(cv2.CAP_PROP_WHITE_BALANCE_BLUE_U, float(kelvin)))
            if ok:
                logger.info(f"顕微鏡カメラの WB を設定: {kelvin} K (manual)")
            else:
                logger.warning(f"顕微鏡カメラの WB を設定できません ({self.backend_name}); 緑かぶりが残る場合は "
                               "メーカー製ビューアで一度接続するか USB を挿し直す")
            return ok
        except Exception as e:
            logger.warning(f"顕微鏡カメラの WB 設定に失敗: {e}")
            return False

    def _apply_exposure(self, exposure: float) -> bool:
        """手動露出に切り替えて値を設定する（対応していない機種では無視される）"""
        try:
            # 0.25 = manual, 0.75 = auto（DirectShow/V4L2 の慣例）
            self.camera.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)
            ok = self.camera.set(cv2.CAP_PROP_EXPOSURE, float(exposure))
            logger.info(f"顕微鏡の露出を設定: {exposure} ({'OK' if ok else '未対応'})")
            return bool(ok)
        except Exception as e:
            logger.warning(f"顕微鏡の露出設定に失敗: {e}")
            return False

    def capture_image(self) -> Optional[np.ndarray]:
        """
        画像を1枚キャプチャ（warmup_frames 枚読み捨ててから取得）

        Returns:
            キャプチャした画像（numpy 配列）。失敗時は None
        """
        if not self.is_connected:
            logger.error("顕微鏡が接続されていません")
            return None
        try:
            for _ in range(self.warmup_frames):
                self.camera.read()
            ret, frame = self.camera.read()
            if not ret or frame is None:
                logger.error("顕微鏡の画像キャプチャ失敗")
                return None
            if self.is_uniform(frame):
                # 単色フレームはレンズキャップ・対象物の密着・映像未到達のいずれか。
                # 画像は返すが警告して気付けるようにする
                logger.warning("顕微鏡の画像がほぼ単色です（レンズキャップ、対象物との密着、照明を確認）")
            logger.info("顕微鏡の画像キャプチャ成功")
            return frame
        except Exception as e:
            logger.error(f"顕微鏡の画像キャプチャエラー: {e}")
            return None

    @staticmethod
    def is_uniform(frame: np.ndarray, threshold: float = 4.0) -> bool:
        """フレームがほぼ単色か。

        チャンネルごとの空間的な標準偏差の最大値が threshold 未満なら単色とみなす。
        （全体の std だと、緑一色のように B/G/R の値が違うだけの平坦なフレームを
        「模様あり」と誤判定する。）
        """
        if frame is None or frame.size == 0:
            return True
        a = np.asarray(frame)
        if a.ndim == 2:
            return float(a.std()) < threshold
        per_channel = a.reshape(-1, a.shape[-1]).std(axis=0)
        return float(per_channel.max()) < threshold


# 動作確認用
if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    index = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    try:
        from pygrabber.dshow_graph import FilterGraph  # Windows: DirectShow の列挙順を表示
        for i, name in enumerate(FilterGraph().get_input_devices()):
            print(f"  camera index {i}: {name}")
    except Exception:
        pass
    scope = MicroscopeController(camera_index=index)
    if scope.connect():
        print("camera info:", scope.get_camera_info())
        path = scope.capture_and_save()
        print("saved:", path)
        scope.disconnect()
    else:
        print(f"顕微鏡に接続できません (index={index})")

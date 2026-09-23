"""Dobot Magician のホーミング（原点復帰）を 1 台ずつ実行する CLI。

使い方::

    python -m src.devices.dobot.home_dobot --robot 1            # config.yaml のポートで Robot 1 をホーミング
    python -m src.devices.dobot.home_dobot --robot 2 --target zero
    python -m src.devices.dobot.home_dobot --robot 1 --mock     # ハードウェア無しで手順だけ確認
    python -m src.devices.dobot.home_dobot --robot 1 --port COM7  # ポートを直接指定

電源投入後に一度実行すると、エンコーダ基準の関節角が実機と一致し、
XYZ 直線補間（MOVL）が正しい鉛直・水平になる。実験フローを流す前の
準備手順であり、フローの中からは呼ばない。

ホーミング完了後の戻り先は ``--target`` で選ぶ::

    current (既定) … 実行開始時にアームがいた位置
    zero            … ベース角 J1=0°（正面）。半径と高さは開始時のまま
    config          … DobotConfig.HOME_SETTINGS の位置 (X=250, Y=0, Z=50)

安全について:

* ホーミング中はアームが J1 の限界まで大きく振れる。その軌道は
  ファームウェアが決めるため WorkspaceValidator では検証できない。
  周囲を空けてから実行すること。
* 戻り先 (x, y, z, joint1) は実行前に config.yaml の ``workspace`` 限界で
  検証し、範囲外なら何も送らずに終了する。
* 実機では Enter を押すまでコマンドを送らない（``--yes`` で省略可）。
  Ctrl+C は緊急停止で、キューを強制停止してその場で止める。

ホーミング中に例外（タイムアウト・応答なし・Ctrl+C など）が起きたら、切断する
前に必ず force_stop() を送る。停止を確認できなければ警告を表示する。

終了コード: 0 = 完了、1 = 接続失敗 / ホーミング中の通信エラー、
2 = 戻り先が可動域外 / タイムアウト、130 = Ctrl+C。
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import sys
from typing import List, Optional, Sequence

from src import config as lab_config
from src.devices.safety.validators import (
    WorkspaceViolationError,
    default_workspace_validator,
)

from .dobot_config import DobotConfig

logger = logging.getLogger(__name__)

TARGET_CHOICES = ("current", "zero", "config")


# ---------------------------------------------------------------------------
# 純粋関数（テスト対象）
# ---------------------------------------------------------------------------
def resolve_dobot_port(robot_id: int, cli_port: Optional[str] = None) -> str:
    """ポートを「引数 > 環境変数 > config.yaml」の優先順で解決する。

    run_flow.resolve_ports と同じ順序。config.yaml にその robot_id が無ければ
    KeyError。
    """
    if cli_port:
        return cli_port
    env_port = os.getenv(f"ROBOT{robot_id}_DOBOT_PORT")
    if env_port:
        return env_port
    ports = lab_config.get_robot_ports()
    if robot_id not in ports or "dobot_port" not in ports[robot_id]:
        raise KeyError(f"config.yaml に robot {robot_id} の dobot_port がありません")
    return ports[robot_id]["dobot_port"]


def compute_return_target(pose: Sequence[float], target: str) -> dict:
    """ホーミング後の戻り先を決める。

    Args:
        pose: 開始時の [x, y, z, r, j1, j2, j3, j4]
        target: "current" / "zero" / "config"

    Returns:
        dict: x, y, z, r（SetHOMEParams に送る値）と joint1（検証用）
    """
    x, y, z, r, j1 = pose[0], pose[1], pose[2], pose[3], pose[4]
    if target == "current":
        return {"x": x, "y": y, "z": z, "r": r, "joint1": j1}
    if target == "zero":
        radius = math.hypot(x, y)
        return {"x": radius, "y": 0.0, "z": z, "r": 0.0, "joint1": 0.0}
    if target == "config":
        h = DobotConfig.HOME_SETTINGS
        hx, hy = float(h["home_x"]), float(h["home_y"])
        return {
            "x": hx,
            "y": hy,
            "z": float(h["home_z"]),
            "r": float(h["home_r"]),
            "joint1": math.degrees(math.atan2(hy, hx)),
        }
    raise ValueError(f"unknown target: {target!r} (choose from {TARGET_CHOICES})")


def validate_return_target(target: dict, validator=None) -> None:
    """戻り先を可動域限界で検証する。範囲外なら WorkspaceViolationError。

    ホーミングの軌道自体は検証できないが、戻り先はフローの move_xyz と同じ
    基準で事前に弾く。``validator`` 省略時は config.yaml の workspace を使う。
    """
    v = validator or default_workspace_validator()
    v.validate_xyz(target["x"], target["y"], target["z"])
    v.validate_joint1(target["joint1"])


# ---------------------------------------------------------------------------
# ハードウェア無しの代替（--mock）
# ---------------------------------------------------------------------------
class MockHomingController:
    """--mock 用。PyDobotController のうち home_dobot が使う部分だけを模擬する。

    開始姿勢は Dobot Magician の典型的な起動姿勢。home() は戻り先を
    そのまま返し、コマンドは何も送らない。
    """

    START_POSE = [193.1, 0.0, 115.1, 0.0, 0.0, 20.2, 4.5, 0.0]

    def __init__(self, port_name: str):
        self.port_name = port_name
        self.pose = list(self.START_POSE)
        self.homed_with: Optional[dict] = None

    @property
    def api(self):
        return self

    def get_current_position(self) -> List[float]:
        return list(self.pose)

    def home(self, x=None, y=None, z=None, r=None, timeout_s=120.0, poll_interval=0.5):
        self.homed_with = {"x": x, "y": y, "z": z, "r": r}
        self.pose = [
            self.pose[0] if x is None else x,
            self.pose[1] if y is None else y,
            self.pose[2] if z is None else z,
            self.pose[3] if r is None else r,
            math.degrees(math.atan2(
                self.pose[1] if y is None else y,
                self.pose[0] if x is None else x,
            )),
            self.pose[5], self.pose[6], self.pose[7],
        ]
        logger.info(f"[MOCK] homing on {self.port_name}: no command sent, pose set to target")
        return list(self.pose)

    def force_stop(self) -> bool:
        return True

    def disconnect(self) -> None:
        pass


def _stop_after_failure(ctrl) -> bool:
    """home() が例外で抜けたら必ず強制停止する。

    SetHOMECmd が送られた後は、CLI 側が切断してもファームウェアのホーミング
    （大きなスイープ）は続く。停止を確認できなければ警告する。
    """
    print("強制停止を送ります（キュー強制停止・キュー破棄）...")
    try:
        ok = bool(ctrl.force_stop())
    except Exception as e:
        print(f"強制停止の送信に失敗: {e}")
        ok = False
    if ok:
        print("強制停止を送信しました")
    else:
        print("警告: 停止を確認できませんでした。アームが動き続けている場合は"
              "電源スイッチ（または USB を抜く）で止めてください。")
    return ok


def _connect(port: str, mock: bool):
    if mock:
        return MockHomingController(port)
    from .pydobot_controller import PyDobotController
    return PyDobotController(port_name=port, homing=False)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m src.devices.dobot.home_dobot",
        description="Dobot Magician のホーミング（原点復帰）。アームが大きく動く。",
    )
    ap.add_argument("--robot", type=int, choices=[1, 2, 3], required=True, help="robot_id")
    ap.add_argument("--port", help="Dobot のポート。省略時は .env / config.yaml")
    ap.add_argument("--target", choices=TARGET_CHOICES, default="current",
                    help="ホーミング後の戻り先 (current=開始位置 / zero=ベース角0° / config=HOME_SETTINGS)")
    ap.add_argument("--timeout", type=float, default=120.0, help="完了待ちの上限秒")
    ap.add_argument("--mock", action="store_true", help="ハードウェア無しで手順だけ実行する")
    ap.add_argument("--yes", action="store_true", help="実行前の Enter 待ちを省略する")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    try:
        port = resolve_dobot_port(args.robot, args.port)
    except KeyError as e:
        print(f"エラー: {e}")
        return 1

    mode = "MOCK" if args.mock else "REAL"
    print(f"[{mode}] Robot {args.robot} (Dobot {port}) に接続中...")
    try:
        ctrl = _connect(port, args.mock)
    except Exception as e:  # ConnectionError など
        print(f"接続失敗: {e}")
        return 1
    if ctrl.api is None:
        print("接続失敗")
        return 1

    try:
        pose = ctrl.get_current_position()
        print(f"ホーミング前: X={pose[0]:.1f} Y={pose[1]:.1f} Z={pose[2]:.1f} R={pose[3]:.1f}  "
              f"J1={pose[4]:.1f} J2={pose[5]:.1f} J3={pose[6]:.1f} J4={pose[7]:.1f}")

        target = compute_return_target(pose, args.target)
        try:
            validate_return_target(target)
        except WorkspaceViolationError as e:
            print("戻り先が config.yaml の workspace 限界の外です。何も送らずに終了します。")
            print(str(e))
            return 2
        print(f"ホーミング後の戻り先 ({args.target}): X={target['x']:.1f} Y={target['y']:.1f} "
              f"Z={target['z']:.1f} R={target['r']:.1f} (J1={target['joint1']:.1f}) … 可動域内")

        if not args.mock and not args.yes:
            print("ホーミング中はアームが J1 の限界まで大きく振れます。周囲を空けてください。")
            try:
                input("Enter で開始、Ctrl+C で中止: ")
            except (KeyboardInterrupt, EOFError):
                print("\n中止しました（コマンドは送っていません）")
                return 130

        print("ホーミング開始。アームが大きく動きます...")
        try:
            final = ctrl.home(target["x"], target["y"], target["z"], target["r"],
                              timeout_s=args.timeout)
        except KeyboardInterrupt:
            print("\nCtrl+C: 緊急停止します（キューを強制停止、その場で停止）")
            _stop_after_failure(ctrl)
            return 130
        except TimeoutError as e:
            print(f"タイムアウト: {e}")
            _stop_after_failure(ctrl)
            return 2
        except Exception as e:  # ConnectionError（応答なし）など
            print(f"ホーミング中にエラー: {e}")
            _stop_after_failure(ctrl)
            return 1

        print(f"ホーミング完了: X={final[0]:.1f} Y={final[1]:.1f} Z={final[2]:.1f} R={final[3]:.1f}  "
              f"J1={final[4]:.1f} J2={final[5]:.1f} J3={final[6]:.1f} J4={final[7]:.1f}")
        return 0
    finally:
        ctrl.disconnect()
        print("切断しました")


if __name__ == "__main__":
    sys.exit(main())

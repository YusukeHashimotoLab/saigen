import asyncio
import json
import logging
import sys
import os

# パス解決: リポジトリルートを sys.path に追加（`python src/flow/executor.py` 直接実行用）
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from src.devices.safety.lab_robot import LabRobot
from src.devices.safety.shared_devices import SharedDevices
from src.devices.safety.mock_robot import MockLabRobot
from src.devices.safety.validators import default_workspace_validator
from src.flow.schema import ExperimentWorkflow

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
_default_logger = logging.getLogger(__name__)

# 共有デバイス用アクション（robot_idに依存しない）
SHARED_DEVICE_ACTIONS = {"measure_weight", "tare_scale", "capture_and_save",
                         "capture_microscope", "microscope_led", "microscope_focus"}
MICROSCOPE_ACTIONS = {"capture_microscope", "microscope_led", "microscope_focus"}

# ループ制御アクション（実行時にスキップ）
LOOP_CONTROL_ACTIONS = {"loop_start", "loop_end"}


def expand_loops(steps: list) -> list:
    """
    loop_start/loop_endマーカーを展開してフラットなリストに変換

    Args:
        steps: ステップリスト（loop_start/loop_endを含む可能性あり）

    Returns:
        展開後のフラットなステップリスト

    Raises:
        ValueError: ループ構造が不正な場合
    """
    expanded = []
    i = 0

    while i < len(steps):
        step = steps[i]
        action = step.get("action") if isinstance(step, dict) else step.action

        if action == "loop_start":
            loop_id = step.get("loop_id") if isinstance(step, dict) else step.loop_id
            count = step.get("count") if isinstance(step, dict) else step.count

            # 対応するloop_endを探す
            loop_body = []
            j = i + 1
            found_end = False

            while j < len(steps):
                inner = steps[j]
                inner_action = inner.get("action") if isinstance(inner, dict) else inner.action
                inner_id = inner.get("loop_id") if isinstance(inner, dict) else getattr(inner, "loop_id", None)

                if inner_action == "loop_end" and inner_id == loop_id:
                    found_end = True
                    break
                elif inner_action == "loop_start":
                    # ネストループは現在非対応
                    raise ValueError(f"ネストされたループは現在サポートされていません（loop_id: {loop_id}）")
                else:
                    loop_body.append(inner)
                j += 1

            if not found_end:
                raise ValueError(f"loop_start (id={loop_id}) に対応する loop_end が見つかりません")

            # count回展開
            for iteration in range(count):
                for body_step in loop_body:
                    if isinstance(body_step, dict):
                        copied = body_step.copy()
                        copied["_iteration"] = iteration + 1
                    else:
                        copied = body_step
                    expanded.append(copied)

            i = j + 1  # loop_endの次へ

        elif action == "loop_end":
            # 対応するloop_startなしのloop_end
            loop_id = step.get("loop_id") if isinstance(step, dict) else step.loop_id
            raise ValueError(f"loop_end (id={loop_id}) に対応する loop_start が見つかりません")

        else:
            expanded.append(step)
            i += 1

    return expanded


async def execute_shared_device_step(step, shared_devices, logger=None):
    """
    共有デバイス用のステップを実行

    Args:
        step: アクション情報（Pydanticモデル or 辞書）
        shared_devices: SharedDevicesインスタンス
        logger: ロガー（省略時はデフォルト）
    """
    if logger is None:
        logger = _default_logger

    # 辞書とPydanticモデルの両方に対応
    action = step.get("action") if isinstance(step, dict) else step.action

    if shared_devices is None:
        raise RuntimeError("SharedDevicesが初期化されていません")

    # ===== カメラ操作 =====
    if action == "capture_and_save":
        file_path = step.get("file_path") if isinstance(step, dict) else step.file_path
        # file_pathが空文字列の場合はNoneに変換（自動生成）
        if not file_path:
            file_path = None
        saved_path = await shared_devices.capture_and_save(file_path)
        return {"image_path": saved_path}

    # ===== デジタル顕微鏡操作 =====
    elif action == "capture_microscope":
        file_path = step.get("file_path") if isinstance(step, dict) else step.file_path
        if not file_path:
            file_path = None
        saved_path = await shared_devices.capture_microscope(file_path)
        return {"image_path": saved_path}

    elif action == "microscope_led":
        on = step.get("on", True) if isinstance(step, dict) else step.on
        level = step.get("level") if isinstance(step, dict) else step.level
        state = await shared_devices.set_microscope_led(on, level)
        logger.info(f"  顕微鏡 LED: {'ON' if state else 'OFF'}")
        return None

    elif action == "microscope_focus":
        if isinstance(step, dict):
            kwargs = {k: step.get(k) for k in ("mode", "position", "direction", "steps", "timeout")
                      if step.get(k) is not None}
        else:
            kwargs = {"mode": step.mode, "position": step.position, "direction": step.direction,
                      "steps": step.steps, "timeout": step.timeout}
        result = await shared_devices.focus_microscope(**kwargs)
        logger.info(f"  顕微鏡フォーカス: 位置 {result.get('focus_position')} "
                    f"({'収束' if result.get('focus_converged') else '未収束'})")
        return result

    # ===== 電子天秤操作（BCE8221） =====
    elif action == "measure_weight":
        stabilization_count = step.get("stabilization_count", 3) if isinstance(step, dict) else step.stabilization_count
        weight = await shared_devices.measure_weight(stabilization_count)
        logger.info(f"  測定結果: {weight:.3f}g")
        return {"weight": weight}

    elif action == "tare_scale":
        delay = step.get("delay", 1.0) if isinstance(step, dict) else step.delay
        await shared_devices.tare_scale(delay)
        return None

    else:
        logger.warning(f"不明な共有デバイスアクション: {action}")
        return None


async def execute_robot_step(step, robots, logger=None):
    """
    ロボットアーム用のステップを実行

    Args:
        step: アクション情報（Pydanticモデル or 辞書）
        robots: {1: LabRobot, 2: LabRobot} または単一のLabRobotインスタンス（後方互換性）
        logger: ロガー（省略時はデフォルト）
    """
    if logger is None:
        logger = _default_logger

    # 辞書とPydanticモデルの両方に対応
    action = step.get("action") if isinstance(step, dict) else step.action

    # robot_idでルーティング（デフォルト: 1）
    robot_id = step.get("robot_id", 1) if isinstance(step, dict) else getattr(step, "robot_id", 1)

    # 後方互換性: 単一robotの場合はそのまま使用
    if isinstance(robots, dict):
        robot = robots.get(robot_id)
        if robot is None:
            raise RuntimeError(f"Robot {robot_id}が初期化されていません")
    else:
        robot = robots

    if action == "move_xyz":
        x = step.get("x") if isinstance(step, dict) else step.x
        y = step.get("y") if isinstance(step, dict) else step.y
        z = step.get("z") if isinstance(step, dict) else step.z
        await robot.move_xyz(x, y, z)

    elif action == "move_z":
        distance = step.get("distance") if isinstance(step, dict) else step.distance
        await robot.move_z(distance)

    elif action == "move_radial":
        distance = step.get("distance") if isinstance(step, dict) else step.distance
        await robot.move_radial(distance)

    elif action == "rotate":
        angle = step.get("angle") if isinstance(step, dict) else step.angle
        await robot.rotate(angle)

    elif action == "rotate_relative":
        angle = step.get("angle") if isinstance(step, dict) else step.angle
        speed = step.get("speed") if isinstance(step, dict) else getattr(step, "speed", None)
        await robot.rotate_relative(angle, speed=speed)

    elif action == "go_home":
        await robot.go_home()

    elif action == "wait":
        seconds = step.get("seconds", 1) if isinstance(step, dict) else step.seconds
        logger.info(f"  {seconds}秒 待機中...")
        await asyncio.sleep(seconds)

    # ===== Picus2（電動ピペット）操作 =====
    elif action == "aspirate":
        volume = step.get("volume") if isinstance(step, dict) else step.volume
        speed = step.get("speed", 5) if isinstance(step, dict) else step.speed
        # 公称所要時間などの記録用データを伝播する
        return await robot.aspirate(volume, speed)

    elif action == "dispense":
        volume = step.get("volume") if isinstance(step, dict) else step.volume
        speed = step.get("speed", 5) if isinstance(step, dict) else step.speed
        return await robot.dispense(volume, speed)

    elif action == "blow_out":
        go_home = step.get("go_home", True) if isinstance(step, dict) else step.go_home
        speed = step.get("speed", 1) if isinstance(step, dict) else step.speed
        delay_ms = step.get("delay_ms", 3000) if isinstance(step, dict) else step.delay_ms
        await robot.blow_out(go_home, speed, delay_ms)

    # ===== スライダー・コンベア操作 =====
    elif action == "move_slider":
        position = step.get("position") if isinstance(step, dict) else step.position
        await robot.move_slider(position)

    elif action == "move_conveyer":
        index = step.get("index", 0) if isinstance(step, dict) else step.index
        speed = step.get("speed") if isinstance(step, dict) else step.speed
        duration = step.get("duration") if isinstance(step, dict) else step.duration
        await robot.move_conveyer(index, speed, duration)

    else:
        logger.warning(f"不明なアクション: {action}")


async def execute_step(step, robots, shared_devices=None, logger=None):
    """
    1つのステップを実行（共通処理）

    Args:
        step: アクション情報（Pydanticモデル or 辞書）
        robots: {1: LabRobot, 2: LabRobot} または単一のLabRobotインスタンス（後方互換性）
        shared_devices: SharedDevicesインスタンス（共有デバイス操作時に必要）
        logger: ロガー（省略時はデフォルト）

    新しいアクションを追加する場合:
    - ロボットアーム操作: execute_robot_step に elif を追加
    - 共有デバイス操作: execute_shared_device_step に elif を追加し、
      SHARED_DEVICE_ACTIONS にアクション名を追加
    """
    if logger is None:
        logger = _default_logger

    # 辞書とPydanticモデルの両方に対応
    action = step.get("action") if isinstance(step, dict) else step.action

    # 共有デバイスアクションかロボットアクションかを判定
    # 戻り値: 測定結果などの記録用データ（{"weight":..} / {"image_path":..}）または None を伝播する
    if action in SHARED_DEVICE_ACTIONS:
        return await execute_shared_device_step(step, shared_devices, logger)
    else:
        return await execute_robot_step(step, robots, logger)


async def execute_workflow(json_path: str, robot_settings: dict = None, shared_settings: dict = None):
    """
    JSONワークフローを読み込んで実行する

    Args:
        json_path: 実行するJSONファイルのパス
        robot_settings: LabRobotに渡す設定辞書 (portなど)
        shared_settings: SharedDevicesに渡す設定辞書 (scale_port, camera_indexなど)
    """
    logger = _default_logger

    if robot_settings is None:
        robot_settings = {}
    if shared_settings is None:
        shared_settings = {}

    # 可動域バリデータを配線（呼び出し側が明示指定していればそれを尊重）
    robot_settings.setdefault("workspace_validator", default_workspace_validator())

    # 1. JSON読み込み
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            json_data = json.load(f)
        logger.info(f"JSONファイル読み込み完了: {json_path}")
    except FileNotFoundError:
        logger.error(f"ファイルが見つかりません: {json_path}")
        return
    except json.JSONDecodeError:
        logger.error(f"JSONフォーマットエラー: {json_path}")
        return

    # 2. スキーマバリデーション & パース
    # ここで schema.py の定義と照らし合わせ、不整合があれば即エラーになります
    try:
        workflow = ExperimentWorkflow(**json_data)  # 型検証しworkflow変数に格納
        logger.info(f"ワークフロー解析成功: {workflow.name}")
        logger.info(f"説明: {workflow.description}")
        logger.info(f"ステップ数: {len(workflow.steps)}")
    except Exception as e:
        logger.error(f"ワークフロー定義エラー (Schema Validation Failed):\n{e}")
        return

    # 3. ワークフローから必要なデバイスを判定
    actions_in_workflow = {step.action for step in workflow.steps}
    needs_shared_devices = bool(actions_in_workflow & SHARED_DEVICE_ACTIONS)
    needs_scale = "measure_weight" in actions_in_workflow or "tare_scale" in actions_in_workflow
    needs_camera = "capture_and_save" in actions_in_workflow
    needs_microscope = bool(actions_in_workflow & MICROSCOPE_ACTIONS)

    # 4. LabRobot初期化 & 実行ループ
    async with LabRobot(use_dobot=True, **robot_settings) as robot:
        # 共有デバイスの初期化（必要な場合のみ）
        shared_devices = None
        if needs_shared_devices:
            shared_devices = SharedDevices(
                use_scale=needs_scale,
                use_camera=needs_camera,
                use_microscope=needs_microscope,
                **shared_settings
            )
            if not await shared_devices.initialize():
                logger.error("SharedDevicesの初期化に失敗しました")
                return

        try:
            # ホームポジションの設定（今回は現在位置をホームとする）
            # ※実運用ではJSON内で指定するか、固定値を使うことを推奨
            logger.info("安全のため、現在の位置をホームポジションとして設定します...")
            robot.set_current_position_as_home()

            logger.info("=== 実験ワークフロー開始 ===")

            for i, step in enumerate(workflow.steps, 1):
                logger.info(f"Step {i}/{len(workflow.steps)}: {step.action}")
                await execute_step(step, robot, shared_devices, logger)

            logger.info("=== 実験ワークフロー完了 ===")

        except (KeyboardInterrupt, asyncio.CancelledError):
            # Ctrl+C / キャンセル時は緊急停止（その場で止め、追加動作はさせない）
            logger.warning("中断要求を受信しました。緊急停止します...")
            robot.emergency_stop()
            raise

        finally:
            # 共有デバイスのクリーンアップ
            if shared_devices is not None:
                await shared_devices.cleanup()


# ==========================================
# 動作確認用メインルーチン
# ==========================================
if __name__ == "__main__":
    # 通常は src/flow/run_flow.py を使用してください。ここは最小限の動作確認用です。
    json_file = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        _REPO_ROOT, "examples", "zif8", "zif8_two_solution_mixing_speed5.json")

    robot_settings = {
        "dobot_port": os.getenv("ROBOT1_DOBOT_PORT", "COM3"),
    }
    shared_settings = {
        "scale_port": os.getenv("SCALE_PORT", "COM8"),
        "camera_index": int(os.getenv("CAMERA_INDEX", "0")),
    }

    asyncio.run(execute_workflow(json_file, robot_settings, shared_settings))

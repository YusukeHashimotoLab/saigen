"""
run_flow.py - JSON 実験フローをコマンドラインから実行する

使い方:
    # 実機なしで動作確認（Mock モード。録画は常に無効）
    python -m src.flow.run_flow examples/zif8/zif8_two_solution_mixing_speed5.json --mock

    # スキーマ検証とループ展開だけ行う
    python -m src.flow.run_flow examples/zif8/zif8_two_solution_mixing_speed5.json --validate-only

    # 実機で実行（ポートは config.yaml / .env / 引数で指定）
    python -m src.flow.run_flow examples/zif8/zif8_two_solution_mixing_speed5.json \
        --robot1-dobot COM3 --robot1-picus2 COM4 \
        --robot2-dobot COM5 --robot2-picus2 COM6 \
        --scale-port COM8

処理の流れ:
    1. JSON を読み込み、Pydantic スキーマ（schema.py）で検証する
    2. loop_start / loop_end を展開してフラットなステップ列にする
    3. 実行ごとのログフォルダを作る（logs/<日付>/<フロー名>_<時刻>/）
    4. ExperimentSession の安全枠（初期化 → 実行 → 中断時は緊急停止 /
       エラー時はホーム復帰 → 切断）の中でステップを順に実行する
    5. 各ステップの結果（重量・画像パス）を measurements.csv / summary.md に残し、
       dispense と直後の measure_weight を対応させた分注精度 CSV も出力する

接続ポートの優先順位:
    コマンドライン引数 > 環境変数（.env / ROBOT1_DOBOT_PORT など）
    > config.yaml（無ければ config.example.yaml）> 組み込みデフォルト
"""
import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from typing import Optional, Tuple
from urllib import request as urlrequest
from urllib.error import URLError

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from dotenv import load_dotenv
from pydantic import ValidationError

from src import config as lab_config
from src.devices.safety.validators import default_workspace_validator
from src.flow.accuracy_logger import DispenseAccuracyLogger
from src.flow.executor import execute_step, expand_loops, SHARED_DEVICE_ACTIONS, MICROSCOPE_CAMERA_ACTIONS, MICROSCOPE_SERIAL_ACTIONS
from src.flow.experiment_logger import ExperimentLogger
from src.flow.experiment_session import ExperimentSession
from src.flow.schema import ExperimentWorkflow

load_dotenv()
logger = logging.getLogger("run_flow")

PICUS2_ACTIONS = {"aspirate", "dispense", "blow_out"}
LOOP_ACTIONS = {"loop_start", "loop_end"}
LOGS_DIR = os.path.join(_REPO_ROOT, "logs")
DASHBOARD_SCRIPT = os.path.join(
    _REPO_ROOT, "src", "monitoring", "dashboard", "launch_sensor_dashboard.py"
)


# ======================================================================
# センサーダッシュボード（CSV 録画）との連携
# ======================================================================
def _sensor_server_url() -> str:
    """ダッシュボードの URL。SENSOR_SERVER_URL > config の web_port の順。"""
    env_url = os.environ.get("SENSOR_SERVER_URL")
    if env_url:
        return env_url.rstrip("/")
    return f"http://localhost:{lab_config.get_dashboard_ports()['web_port']}"


def _auth_headers() -> dict:
    token = os.environ.get("SENSOR_DASHBOARD_TOKEN")
    return {"X-Auth-Token": token} if token else {}


def _http_get_json(url: str, timeout: float = 2.0):
    req = urlrequest.Request(url, headers=_auth_headers(), method="GET")
    with urlrequest.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_post_json(url: str, body: dict, timeout: float = 5.0) -> Tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urlrequest.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", **_auth_headers()},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except URLError as e:
        # サーバはあるがステータスが 2xx 以外（409 など）の場合もここに来る
        status = getattr(e, "code", 0)
        try:
            body_text = e.read().decode("utf-8")  # type: ignore[attr-defined]
        except Exception:
            body_text = ""
        return status, {"detail": body_text}


def ensure_dashboard_running(timeout: float = 15.0) -> bool:
    """センサーダッシュボードが起動していなければ headless で立ち上げる。"""
    base = _sensor_server_url()
    try:
        _http_get_json(f"{base}/api/status", timeout=1.5)
        logger.info("センサーダッシュボードは既に起動しています")
        return True
    except (URLError, ConnectionError, TimeoutError, OSError):
        pass

    if not os.path.exists(DASHBOARD_SCRIPT):
        logger.warning(f"ダッシュボードスクリプトが見つかりません: {DASHBOARD_SCRIPT}")
        return False

    logger.info("センサーダッシュボードを headless で起動します")
    env = os.environ.copy()
    env["SENSOR_DASHBOARD_HEADLESS"] = "1"
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(
        [sys.executable, DASHBOARD_SCRIPT],
        cwd=os.path.dirname(DASHBOARD_SCRIPT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            _http_get_json(f"{base}/api/status", timeout=1.0)
            logger.info("センサーダッシュボード起動完了")
            return True
        except Exception:
            time.sleep(0.5)
    logger.warning("センサーダッシュボードの起動確認がタイムアウトしました")
    return False


def start_csv_recording(experiment_name: str) -> Optional[str]:
    """/api/start を叩いて CSV 録画を開始し、保存ディレクトリを返す。"""
    base = _sensor_server_url()
    status, body = _http_post_json(f"{base}/api/start", {"experiment_name": experiment_name})
    if status == 200:
        save_dir = body.get("save_dir")
        logger.info(f"CSV録画開始 (save_dir={save_dir})")
        return save_dir
    if status == 409:
        logger.info("CSV録画開始スキップ: 既に録画中")
        try:
            return _http_get_json(f"{base}/api/status").get("save_dir")
        except Exception:
            return None
    logger.warning(f"CSV録画開始失敗: {status} {body}")
    return None


def stop_csv_recording():
    status, body = _http_post_json(f"{_sensor_server_url()}/api/end", {})
    if status == 200:
        logger.info(f"CSV録画終了 (saved_path={body.get('saved_path')})")
    elif status == 409:
        logger.info("CSV録画終了スキップ: 録画していません")
    else:
        logger.warning(f"CSV録画終了失敗: {status} {body}")


# ======================================================================
# CLI
# ======================================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="JSON 実験フローを実行する")
    p.add_argument("flow", help="実行する JSON フローのパス")
    p.add_argument("--mock", action="store_true",
                   help="実機を使わずログ出力のみで実行する（録画は常に無効）")
    p.add_argument("--validate-only", action="store_true",
                   help="スキーマ検証とループ展開のみ行い実行しない")
    # ポートは None を既定にして、環境変数 → config.yaml の順で解決する
    for rid in (1, 2, 3):
        p.add_argument(f"--robot{rid}-dobot", default=None,
                       help=f"Robot {rid} の Dobot ポート"
                            f"（既定: 環境変数 ROBOT{rid}_DOBOT_PORT / config.yaml）")
        p.add_argument(f"--robot{rid}-picus2", default=None,
                       help=f"Robot {rid} の Picus2 ポート。空なら Picus2 なし"
                            f"（既定: ROBOT{rid}_PICUS2_PORT / config.yaml）")
    p.add_argument("--scale-port", default=None,
                   help="電子天秤のポート（既定: SCALE_PORT / config.yaml）")
    p.add_argument("--camera-index", type=int, default=None,
                   help="capture_and_save 用カメラ index（既定: CAMERA_INDEX / config.yaml）")
    p.add_argument("--microscope-index", type=int, default=None,
                   help="capture_microscope 用の顕微鏡カメラ index（既定: MICROSCOPE_INDEX / config.yaml）")
    p.add_argument("--microscope-port", default=None,
                   help="顕微鏡 LED 制御用シリアルポート（既定: MICROSCOPE_PORT / config.yaml。空なら LED 制御なし）")
    record = p.add_mutually_exclusive_group()
    record.add_argument("--record", dest="record", action="store_true", default=None,
                        help="センサーCSV・動画の録画を行う（実機実行時の既定）")
    record.add_argument("--no-record", dest="record", action="store_false",
                        help="録画を行わない")
    p.add_argument("--liquid-density", type=float, default=1.0,
                   help="分注精度計算に使う液体密度 g/mL（既定: 1.0 = 水）")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def resolve_ports(args) -> Tuple[dict, dict]:
    """接続設定を「引数 > 環境変数 > config.yaml」の優先順で解決する。

    Returns:
        (robot_ports, shared_config)
    """
    robot_ports = lab_config.get_robot_ports()
    shared_config = lab_config.get_shared_devices()

    for rid in (1, 2, 3):
        ports = dict(robot_ports.get(rid, {}))
        dobot = getattr(args, f"robot{rid}_dobot") or os.getenv(f"ROBOT{rid}_DOBOT_PORT")
        picus2 = getattr(args, f"robot{rid}_picus2") or os.getenv(f"ROBOT{rid}_PICUS2_PORT")
        if dobot:
            ports["dobot_port"] = dobot
        if picus2:
            ports["picus2_address"] = picus2
        robot_ports[rid] = ports

    scale = args.scale_port or os.getenv("SCALE_PORT")
    if scale:
        shared_config["scale_port"] = scale
    camera = args.camera_index if args.camera_index is not None else os.getenv("CAMERA_INDEX")
    if camera not in (None, ""):
        shared_config["camera_index"] = int(camera)
    microscope = (getattr(args, "microscope_index", None)
                  if getattr(args, "microscope_index", None) is not None
                  else os.getenv("MICROSCOPE_INDEX"))
    if microscope not in (None, ""):
        shared_config["microscope_index"] = int(microscope)
    cli_port = getattr(args, "microscope_port", None)
    if cli_port is not None:                       # "" は「制御ポートを使わない」の明示
        shared_config["microscope_port"] = cli_port
    elif os.getenv("MICROSCOPE_PORT"):
        shared_config["microscope_port"] = os.getenv("MICROSCOPE_PORT")

    return robot_ports, shared_config


def load_and_validate(path: str) -> Tuple[ExperimentWorkflow, list]:
    """JSON を読み込み、スキーマ検証とループ展開を行う。

    Returns:
        (workflow, 展開後のステップ dict のリスト)
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    workflow = ExperimentWorkflow(**data)  # 不正なフローはここで ValidationError
    logger.info(f"フロー: {workflow.name} / {workflow.description}")
    steps = [s.model_dump() for s in workflow.steps]
    expanded = expand_loops(steps)
    logger.info(f"ステップ数: {len(steps)}（ループ展開後 {len(expanded)}）")
    return workflow, expanded


def plan_resources(steps: list) -> Tuple[list, set, bool, bool]:
    """フローが必要とするロボット・共有デバイスを洗い出す。"""
    actions = {s.get("action") for s in steps}
    robot_ids = set()
    picus2_robots = set()
    for s in steps:
        action = s.get("action")
        if action in SHARED_DEVICE_ACTIONS or action in LOOP_ACTIONS:
            continue
        rid = s.get("robot_id", 1)
        robot_ids.add(rid)
        if action in PICUS2_ACTIONS:
            picus2_robots.add(rid)
    needs_scale = bool(actions & {"measure_weight", "tare_scale"})
    needs_camera = "capture_and_save" in actions
    needs_microscope = bool(actions & MICROSCOPE_CAMERA_ACTIONS)          # 顕微鏡カメラ
    needs_microscope_serial = bool(actions & MICROSCOPE_SERIAL_ACTIONS)   # 顕微鏡の制御ポート
    return (sorted(robot_ids), picus2_robots, needs_scale, needs_camera,
            needs_microscope, needs_microscope_serial)


def _fmt_params(step: dict) -> str:
    return " ".join(f"{k}={v}" for k, v in step.items() if k not in ("action", "_iteration"))


async def run(args) -> int:
    workflow, steps = load_and_validate(args.flow)
    if args.validate_only:
        logger.info("検証のみ完了（--validate-only）")
        return 0
    if not steps:
        logger.error("ステップが空です")
        return 2

    (robot_ids, picus2_robots, needs_scale, needs_camera,
     needs_microscope, needs_microscope_serial) = plan_resources(steps)
    needs_shared = needs_scale or needs_camera or needs_microscope or needs_microscope_serial
    robot_ports, shared_config = resolve_ports(args)

    # 実機実行では既定で録画する。Mock モードでは常に録画しない。
    record = (not args.mock) and (True if args.record is None else args.record)

    # 実行ごとのログフォルダ（run.log / measurements.csv / summary.md /
    # metadata.json / images/ と入力 JSON のコピー）
    exp_logger = ExperimentLogger(
        workflow.name, workflow.description, args.flow, base_dir=LOGS_DIR
    )
    exp_logger.record_resources(robot_ids, picus2_robots)
    logger.info(f"実験ログ保存先: {exp_logger.dir}")

    accuracy_logger = DispenseAccuracyLogger(
        os.path.join(exp_logger.dir, "dispense_accuracy.csv"), density=args.liquid_density
    )
    logger.info(f"分注精度CSV: {accuracy_logger.csv_path} (密度={args.liquid_density}g/mL)")

    logger.info(f"使用ロボット: {robot_ids}")
    if picus2_robots:
        logger.info(f"ピペット使用: Robot {sorted(picus2_robots)}")
    if needs_shared:
        parts = [n for n, need in (("天秤", needs_scale), ("カメラ", needs_camera),
                                   ("顕微鏡カメラ", needs_microscope),
                                   ("顕微鏡制御ポート", needs_microscope_serial)) if need]
        logger.info(f"共有デバイス: {', '.join(parts)}")

    # 録画セッション開始（CSV: ダッシュボード経由 / 動画: ローカルスレッド）
    video_recorder = None
    if record:
        if ensure_dashboard_running():
            save_dir = start_csv_recording(workflow.name) or exp_logger.dir
            try:
                from src.monitoring.camera.video_recorder import VideoRecorder
                video_recorder = VideoRecorder(
                    output_path=os.path.join(
                        save_dir, f"{os.path.basename(exp_logger.dir)}.mp4"),
                    camera_index=lab_config.get_video_camera_index(),
                )
                if not video_recorder.start():
                    logger.warning("動画録画の開始に失敗しました（CSV録画は継続します）")
                    video_recorder = None
            except ImportError as e:
                logger.warning(f"動画録画を利用できません（OpenCV 未導入?）: {e}")
                video_recorder = None
        else:
            logger.warning("ダッシュボード未起動のため録画をスキップします")

    session = ExperimentSession(
        mock=args.mock,
        robot_ports=robot_ports,
        shared_config=shared_config,
        workspace_validator=default_workspace_validator(),
    )

    async def body():
        for rid in robot_ids:
            await session.add_robot(rid, use_picus2=(rid in picus2_robots))
        if needs_shared:
            await session.add_shared(use_scale=needs_scale, use_camera=needs_camera,
                                     use_microscope=needs_microscope,
                                     use_microscope_serial=needs_microscope_serial)

        logger.info("=== フロー実行開始 ===")
        total = len(steps)
        for i, step in enumerate(steps, 1):
            action = step.get("action")
            rid = None
            if action in SHARED_DEVICE_ACTIONS:
                logger.info(f"[{i}/{total}] 共有: {action} {_fmt_params(step)}")
            else:
                rid = step.get("robot_id", 1)
                logger.info(f"[{i}/{total}] Robot {rid}: {action} {_fmt_params(step)}")

            # 撮影画像は実験フォルダ内に束ねる（file_path 未指定の場合のみ）
            if action == "capture_and_save" and not step.get("file_path"):
                step = {**step, "file_path": exp_logger.next_image_path(i)}
            elif action == "capture_microscope" and not step.get("file_path"):
                step = {**step, "file_path": exp_logger.next_image_path(i, tag="microscope")}

            iteration = step.get("_iteration")
            t_start = time.monotonic()
            try:
                result = await execute_step(step, session.robots, session.shared, logger)
                exp_logger.record_step(i, total, action, rid, iteration,
                                       "ok", time.monotonic() - t_start, result=result)
            except Exception as step_err:
                exp_logger.record_step(i, total, action, rid, iteration,
                                       "error", time.monotonic() - t_start,
                                       error=str(step_err))
                raise

            # 分注精度: dispense の指示量と直後の measure_weight を対応付ける
            if action == "dispense":
                accuracy_logger.record_dispense(
                    volume=step.get("volume"), speed=step.get("speed"),
                    iteration=iteration, step_index=i,
                )
            elif action == "measure_weight":
                weight = result.get("weight") if isinstance(result, dict) else result
                err = accuracy_logger.record_weight(weight)
                if err is not None:
                    logger.info(f"  分注誤差: {err:+.4f}g")

        logger.info("=== フロー実行完了 ===")

    exit_code = 0
    try:
        await session.run(body)
    except (KeyboardInterrupt, asyncio.CancelledError):
        exit_code = 130
    except Exception as e:  # noqa: BLE001 - 実行時エラーは終了コードで伝える
        logger.error(f"フロー実行に失敗しました: {e}")
        exit_code = 1
    finally:
        if video_recorder is not None:
            try:
                video_recorder.stop()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"動画録画の停止でエラー: {e}")
        if record:
            try:
                stop_csv_recording()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"CSV録画の停止でエラー: {e}")
        try:
            accuracy_logger.finalize(logger)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"分注精度CSVの確定でエラー: {e}")
        exp_logger.finalize(session.status, session.error)
        logger.info(f"実験ログを保存しました: {exp_logger.dir}")

    return exit_code


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if not os.path.exists(args.flow):
        logger.error(f"ファイルが見つかりません: {args.flow}")
        return 2
    try:
        return asyncio.run(run(args))
    except ValidationError as e:
        logger.error(f"JSON スキーマ検証に失敗しました:\n{e}")
        return 2
    except json.JSONDecodeError as e:
        logger.error(f"JSON を読み込めません: {e}")
        return 2
    except KeyboardInterrupt:
        # 緊急停止とクリーンアップは run() 内の ExperimentSession が実施済み
        logger.warning("中断されました（緊急停止・クリーンアップ実施済み）")
        return 130


if __name__ == "__main__":
    sys.exit(main())

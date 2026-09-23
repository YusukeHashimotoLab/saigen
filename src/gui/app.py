import streamlit as st
import json
import logging
import sys
import os
import time
from html import escape

import glob as glob_module

import requests

# ==========================================
# 1. パス設定 & モジュールインポート
# ==========================================
current_dir = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(current_dir))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

try:
    from src.agent.llm_service import generate_workflow_from_prompt, LLM_AVAILABLE
except ImportError:
    LLM_AVAILABLE = False
    def generate_workflow_from_prompt(x, template=None): return {}

try:
    from src.voice.speech_service import transcribe_audio, WHISPER_AVAILABLE
    from streamlit_mic_recorder import mic_recorder
    VOICE_INPUT_AVAILABLE = True
except ImportError:
    VOICE_INPUT_AVAILABLE = False

from pydantic import ValidationError

from src.flow.executor import SHARED_DEVICE_ACTIONS

# 実行ロジックは Streamlit 非依存の runner.py に集約している（CLI と同じ経路:
# スキーマ検証 → ループ展開 → ExperimentSession → ExperimentLogger）。
from src.gui import runner as flow_runner

SENSOR_SERVER_URL = os.getenv("SENSOR_SERVER_URL", "http://localhost:8000")


def _load_presets():
    """presets/ディレクトリからプリセットを読み込む"""
    presets = {}
    preset_dir = os.path.join(REPO_ROOT, "src", "agent", "presets")
    if not os.path.isdir(preset_dir):
        return presets
    for path in sorted(glob_module.glob(os.path.join(preset_dir, "*.json"))):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("hidden"):
            continue
        key = os.path.splitext(os.path.basename(path))[0]
        presets[key] = data
    return presets


PRESET_TEMPLATES = _load_presets()

# ==========================================
# 2. 定数・マッピング定義（日本語化）
# ==========================================
ACTION_CONFIG = {
    "move_xyz": {
        "icon": "🚚", "label": "座標移動", "category": "robot",
        "defaults": {"x": 200.0, "y": 0.0, "z": 50.0}
    },
    "move_z": {
        "icon": "⬆️", "label": "Z軸移動", "category": "robot",
        "defaults": {"distance": 10.0}
    },
    "move_radial": {
        "icon": "↔️", "label": "半径移動", "category": "robot",
        "defaults": {"distance": 20.0}
    },
    "rotate": {
        "icon": "🔄", "label": "絶対回転", "category": "robot",
        "defaults": {"angle": 90.0}
    },
    "rotate_relative": {
        "icon": "🔄", "label": "回転", "category": "robot",
        "defaults": {"angle": 90.0, "speed": "low"}
    },
    "go_home": {
        "icon": "🏠", "label": "ホーム", "category": "robot",
        "defaults": {}
    },
    "move_slider": {
        "icon": "🛤️", "label": "スライダー", "category": "peripheral",
        "defaults": {"position": 500.0}
    },
    "move_conveyer": {
        "icon": "🔃", "label": "コンベア", "category": "peripheral",
        "defaults": {"index": 0, "speed": 10.0, "duration": 5.0}
    },
    "wait": {
        "icon": "⏳", "label": "待機", "category": "utility",
        "defaults": {"seconds": 2.0}
    },
    "aspirate": {
        "icon": "💧", "label": "吸引", "category": "pipette",
        "defaults": {"volume": 5.0, "speed": 5}
    },
    "dispense": {
        "icon": "💦", "label": "吐出", "category": "pipette",
        "defaults": {"volume": 5.0, "speed": 5}
    },
    "blow_out": {
        "icon": "💨", "label": "排出", "category": "pipette",
        "defaults": {"go_home": True, "speed": 1, "delay_ms": 3000}
    },
    "capture_and_save": {
        "icon": "📷", "label": "撮影", "category": "sensor",
        "defaults": {"file_path": ""}
    },
    "capture_microscope": {
        "icon": "🔬", "label": "顕微鏡撮影", "category": "sensor",
        "defaults": {"file_path": ""}
    },
    "microscope_led": {
        "icon": "💡", "label": "顕微鏡LED", "category": "sensor",
        "defaults": {"on": True, "level": None}
    },
    "microscope_focus": {
        "icon": "🎯", "label": "顕微鏡フォーカス", "category": "sensor",
        "defaults": {"mode": "auto", "position": None, "direction": "in", "steps": 1, "timeout": 60.0}
    },
    "measure_weight": {
        "icon": "⚖️", "label": "計測", "category": "sensor",
        "defaults": {"stabilization_count": 3}
    },
    "tare_scale": {
        "icon": "🔄", "label": "風袋引き", "category": "sensor",
        "defaults": {"delay": 1.0}
    },
    # ループ制御
    "loop_start": {
        "icon": "🔁", "label": "ループ開始", "category": "loop",
        "defaults": {"loop_id": "", "count": 10}
    },
    "loop_end": {
        "icon": "🔚", "label": "ループ終了", "category": "loop",
        "defaults": {"loop_id": ""}
    },
}

# Robot色定義
ROBOT_COLORS = {
    1: {"bg": "#e3f2fd", "border": "#1976d2", "label": "🔵", "name": "Robot 1"},
    2: {"bg": "#e8f5e9", "border": "#388e3c", "label": "🟢", "name": "Robot 2"},
    3: {"bg": "#fff3e0", "border": "#e65100", "label": "🟠", "name": "Robot 3 (Slider)"},
}

SHARED_COLOR = {"bg": "#f5f5f5", "border": "#9e9e9e"}

# カテゴリ定義（日本語）
CATEGORIES = {
    "robot": ("🤖 ロボット", ["move_xyz", "move_z", "move_radial", "rotate_relative", "go_home"]),
    "peripheral": ("🔩 周辺機器", ["move_slider", "move_conveyer"]),
    "pipette": ("💉 ピペット", ["aspirate", "dispense", "blow_out"]),
    "sensor": ("📡 センサー", ["capture_and_save", "capture_microscope", "microscope_led", "microscope_focus", "measure_weight", "tare_scale"]),
    "utility": ("⚙️ その他", ["wait"]),
    "loop": ("🔄 ループ", ["loop_start"]),  # loop_endはloop_start追加時に自動追加
}

# ループ色定義
LOOP_COLORS = {
    "bg": "#fff3e0",
    "border": "#ff9800",
    "bracket": "#e65100"
}

# ==========================================
# 3. ページ設定
# ==========================================
st.set_page_config(
    page_title="Visual Automation Controller",
    page_icon="🧩",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ==========================================
# 4. 状態管理 (Session State)
# ==========================================
DEFAULT_WORKFLOW = {
    "name": "New Experiment",
    "description": "Created with Visual Editor",
    "steps": []
}

if "workflow_data" not in st.session_state:
    st.session_state.workflow_data = DEFAULT_WORKFLOW

# 接続ポートの既定値は config.yaml（無ければ config.example.yaml）から取る。
# サイドバーで編集した値はそのセッション内だけ有効（恒久的な変更は config.yaml へ）。
if "robot_configs" not in st.session_state or "shared_config" not in st.session_state:
    _device_defaults = flow_runner.default_device_settings()
    st.session_state.robot_configs = _device_defaults["robots"]
    st.session_state.shared_config = _device_defaults["shared"]

if "use_mock" not in st.session_state:
    st.session_state.use_mock = False  # Mockはデフォルトオフ


def generate_loop_id():
    """一意なループIDを生成"""
    import time
    return f"loop_{int(time.time() * 1000) % 100000}"


def add_step(action_type):
    """新しいブロックを追加"""
    config = ACTION_CONFIG.get(action_type, {})
    new_step = {"action": action_type, **config.get("defaults", {})}

    # ループ開始: loop_endも同時に追加
    if action_type == "loop_start":
        loop_id = generate_loop_id()
        new_step["loop_id"] = loop_id
        st.session_state.workflow_data["steps"].append(new_step)
        st.session_state.workflow_data["steps"].append({
            "action": "loop_end",
            "loop_id": loop_id
        })
        return

    # ループ終了: 単独追加は禁止
    if action_type == "loop_end":
        st.warning("ループ終了は「ループ開始」追加時に自動で追加されます")
        return

    if action_type not in SHARED_DEVICE_ACTIONS:
        new_step["robot_id"] = 1
    st.session_state.workflow_data["steps"].append(new_step)


def remove_step(index):
    """ステップを削除（ループの場合はペアで削除）"""
    steps = st.session_state.workflow_data["steps"]
    step = steps[index]
    action = step.get("action")

    # ループの場合はペアで削除
    if action in ["loop_start", "loop_end"]:
        loop_id = step.get("loop_id")
        # 同じloop_idを持つステップのインデックスを収集
        indices_to_remove = []
        for i, s in enumerate(steps):
            if s.get("loop_id") == loop_id and s.get("action") in ["loop_start", "loop_end"]:
                indices_to_remove.append(i)
        # 後ろから削除（インデックスずれ防止）
        for i in sorted(indices_to_remove, reverse=True):
            steps.pop(i)
    else:
        steps.pop(index)


def move_step(index, direction):
    steps = st.session_state.workflow_data["steps"]
    new_index = index + direction
    if 0 <= new_index < len(steps):
        steps[index], steps[new_index] = steps[new_index], steps[index]


def _update_robot_id(idx, key):
    """ラジオボタンのon_changeコールバック用"""
    selected = st.session_state[key]
    # ラベルからrobot_idを逆引き
    for rid, color in ROBOT_COLORS.items():
        if selected == f"{color['label']} {color['name']}":
            st.session_state.workflow_data["steps"][idx]["robot_id"] = rid
            return
    st.session_state.workflow_data["steps"][idx]["robot_id"] = 1


def _update_step_param(idx, param_name, widget_key):
    """ウィジェットの値変更時にstepデータを即時更新するコールバック"""
    st.session_state.workflow_data["steps"][idx][param_name] = st.session_state[widget_key]


# ==========================================
# 5. 実行状態 (Session State)
# ==========================================
if "runner" not in st.session_state:
    st.session_state.runner = None       # 実行中/直近の FlowRunner
if "log_messages" not in st.session_state:
    st.session_state.log_messages = []
if "error_message" not in st.session_state:
    st.session_state.error_message = None
if "run_status" not in st.session_state:
    st.session_state.run_status = None   # completed / failed / aborted
if "run_log_dir" not in st.session_state:
    st.session_state.run_log_dir = None
if "run_progress" not in st.session_state:
    st.session_state.run_progress = (0, 0)  # (完了ステップ数, 総ステップ数)
if "experiment_recording" not in st.session_state:
    st.session_state.experiment_recording = False


def add_log(message: str):
    """ログメッセージをセッションに追加"""
    from datetime import datetime
    timestamp = datetime.now().strftime('%H:%M:%S')
    st.session_state.log_messages.append(f"{timestamp} - {message}")


def is_running() -> bool:
    runner = st.session_state.runner
    return runner is not None and runner.is_running()


# ==========================================
# 6. ワークフロー実行（runner.py に委譲）
# ==========================================
def check_is_safe(log_fn=None) -> bool:
    """各ステップ実行前の安全確認 (sensor_web_dashboard /api/is_safe)

    将来 Pi のセンサー値による異常検知をサーバ側で実装予定。
    成功時は無音、NG・HTTP エラーのみ log_fn で警告。
    センサーサーバ未起動時はゲートを通す（オプション扱い）。
    """
    if log_fn is None:
        log_fn = add_log
    try:
        resp = requests.get(f"{SENSOR_SERVER_URL}/api/is_safe", timeout=3)
        if resp.status_code == 200:
            safe = bool(resp.json().get("safe", False))
            if not safe:
                log_fn("⚠️ 安全確認 NG: 異常検知のため中断します")
            return safe
        log_fn(f"安全確認失敗: {resp.status_code} {resp.text}")
        return False
    except requests.RequestException:
        return True


def start_experiment_session(name: str, description: str = None):
    """センサーサーバーに実験開始を通知 (sensor_web_dashboard /api/start)"""
    try:
        resp = requests.post(
            f"{SENSOR_SERVER_URL}/api/start",
            json={"experiment_name": name},
            timeout=3,
        )
        if resp.status_code == 200:
            data = resp.json()
            st.session_state.experiment_recording = True
            add_log(f"センサー録画開始 (save_dir={data.get('save_dir')})")
        elif resp.status_code == 409:
            add_log("センサー録画開始スキップ: 既に録画中")
        else:
            add_log(f"センサー録画開始失敗: {resp.status_code} {resp.text}")
    except requests.RequestException:
        pass


def stop_experiment_session():
    """センサーサーバーに実験終了を通知 (sensor_web_dashboard /api/end)"""
    if not st.session_state.get("experiment_recording"):
        return
    try:
        resp = requests.post(
            f"{SENSOR_SERVER_URL}/api/end",
            timeout=3,
        )
        if resp.status_code == 200:
            saved_path = resp.json().get("saved_path")
            add_log(f"センサー録画終了 (saved_path={saved_path})")
        elif resp.status_code == 409:
            add_log("センサー録画終了スキップ: 録画していません")
        else:
            add_log(f"センサー録画終了失敗: {resp.status_code} {resp.text}")
    except requests.RequestException:
        pass
    finally:
        st.session_state.experiment_recording = False


def build_run_settings() -> dict:
    """サイドバーの設定を runner が扱う形にまとめる"""
    return {
        "robots": st.session_state.robot_configs,
        "shared": st.session_state.shared_config,
    }


def _safety_gate(step: dict):
    """各ステップ実行前の安全確認。NG なら例外を投げて実行を止める。

    runner のバックグラウンドスレッドから呼ばれるため、Streamlit の API は
    触らない（ログは logging 経由で UI とrun.log の両方に流れる）。
    """
    if not check_is_safe(log_fn=logging.getLogger("gui.safety").warning):
        raise RuntimeError("安全確認 NG（センサーダッシュボードが異常を検知）により中断")


def set_workflow(data: dict, source: str = "生成結果"):
    """LLM・プリセット・JSON読込のフローをキャンバスに載せる（同時に検証する）

    キャンバスは未指定パラメータを既定値で埋めてしまうため、外から来た
    フローは *埋める前に* 検証して、問題があればその場で知らせる。
    実行前の検証（start_run）と合わせて二段構えにしている。
    """
    st.session_state.workflow_data = data
    try:
        flow_runner.validate_workflow(data)
    except ValidationError as e:
        st.session_state.error_message = (
            f"{source}のスキーマ検証に失敗しました。実行前に修正してください:\n\n"
            + flow_runner.format_validation_error(e, st.session_state.workflow_data)
        )
    except (ValueError, TypeError) as e:
        st.session_state.error_message = f"{source}のフロー定義エラー: {e}"
    else:
        st.session_state.error_message = None


def validate_current_workflow():
    """実行せずにスキーマ検証とループ展開だけ行う"""
    try:
        _, steps = flow_runner.validate_workflow(st.session_state.workflow_data)
    except ValidationError as e:
        st.session_state.error_message = (
            "JSON スキーマ検証に失敗しました:\n\n"
            + flow_runner.format_validation_error(e, st.session_state.workflow_data)
        )
    except (ValueError, TypeError) as e:
        st.session_state.error_message = f"フロー定義エラー: {e}"
    else:
        st.session_state.error_message = None
        st.toast(f"検証OK: {len(steps)} ステップ（ループ展開後）", icon="✅")


def start_run():
    """検証してから FlowRunner を起動する（実行はバックグラウンドスレッド1本）

    検証に失敗したフローはここで弾かれ、ロボットには一切触れない。
    """
    st.session_state.log_messages = []
    st.session_state.error_message = None
    st.session_state.run_status = None
    st.session_state.run_log_dir = None
    st.session_state.run_progress = (0, 0)

    use_mock = st.session_state.use_mock
    robot_ports, shared_config = flow_runner.settings_to_session_args(build_run_settings())

    try:
        runner = flow_runner.FlowRunner(
            st.session_state.workflow_data,
            mock=use_mock,
            robot_ports=robot_ports,
            shared_config=shared_config,
            # Mock 実行では録画も安全確認ゲートも使わない（CLI の --mock と同じ）
            pre_step=None if use_mock else _safety_gate,
        )
    except ValidationError as e:
        st.session_state.error_message = (
            "JSON スキーマ検証に失敗しました。実行は行いません:\n\n"
            + flow_runner.format_validation_error(e, st.session_state.workflow_data)
        )
        return
    except (ValueError, TypeError) as e:
        st.session_state.error_message = f"フロー定義エラー。実行は行いません: {e}"
        return

    add_log(f"実験開始: {runner.workflow.name}")
    add_log(f"モード: {'🛠 MOCK' if use_mock else '⚠️ REAL'}")
    add_log(f"✓ 検証OK: {len(runner.steps)} ステップ（ループ展開後）")

    if not use_mock:
        start_experiment_session(
            name=runner.workflow.name, description=runner.workflow.description
        )

    st.session_state.run_progress = (0, len(runner.steps))
    st.session_state.runner = runner
    runner.start()


def drain_runner_events():
    """runner が積んだ進捗イベントをログ・進捗・完了状態に反映する"""
    runner = st.session_state.runner
    if runner is None:
        return
    done, total = st.session_state.run_progress

    for event in runner.drain_events():
        if event.kind == "log":
            st.session_state.log_messages.append(event.message)

        elif event.kind == "start":
            total = event.total or total
            st.session_state.run_log_dir = event.log_dir
            add_log(f"📁 実験ログ: {event.log_dir}")

        elif event.kind == "step":
            done = event.index or done
            total = event.total or total
            if event.status == "error":
                add_log(f"❌ [{event.index}/{total}] {event.action}: {event.error}")

        elif event.kind == "finish":
            st.session_state.run_status = event.status
            st.session_state.run_log_dir = event.log_dir or st.session_state.run_log_dir
            stop_experiment_session()
            if event.status == "completed":
                add_log("✅ 全ステップ完了")
            elif event.status == "aborted":
                add_log("⛔ 停止しました（緊急停止・切断まで実施済み）")
            else:
                add_log(f"❌ 実行エラー: {event.error}")
                st.session_state.error_message = f"実行エラー: {event.error}"
            add_log(f"📁 実験ログを保存しました: {event.log_dir}")

    st.session_state.run_progress = (done, total)


def render_execution_panel():
    """実行パネル（停止 / 検証 / 実行）"""
    running = is_running()
    st.markdown('<div class="execution-panel">', unsafe_allow_html=True)

    col_stop, col_validate, col_run = st.columns([1, 1, 2])
    with col_stop:
        stop_clicked = st.button(
            "■ 停止", type="secondary", use_container_width=True,
            disabled=not running, key="emergency_stop_btn",
            help="実行中のフローを中断し、ロボットを緊急停止して切断します",
        )
    with col_validate:
        validate_clicked = st.button(
            "✔ 検証", use_container_width=True, disabled=running,
            help="実行せずにスキーマ検証とループ展開だけ行います",
        )
    with col_run:
        run_clicked = st.button(
            "▶ 実行", type="primary", use_container_width=True, disabled=running
        )

    st.markdown('</div>', unsafe_allow_html=True)

    # 進捗バーと保存先
    done, total = st.session_state.run_progress
    if total:
        label = f"{done}/{total} ステップ"
        if st.session_state.run_status:
            label = f"{label}（{st.session_state.run_status}）"
        st.progress(min(done / total, 1.0), text=label)
    if st.session_state.run_log_dir:
        st.caption(f"📁 {st.session_state.run_log_dir}")

    if validate_clicked:
        validate_current_workflow()
        st.rerun()

    if run_clicked:
        start_run()
        st.rerun()

    if stop_clicked and st.session_state.runner is not None:
        st.session_state.runner.request_stop()
        add_log("🛑 停止を要求しました（緊急停止 → 後始末）")
        st.rerun()


def render_toolbox():
    """ツールボックス（カテゴリ別、日本語ボタン）"""
    st.markdown("##### 🧰 ツール")

    for cat_key, (cat_label, actions) in CATEGORIES.items():
        with st.expander(cat_label, expanded=False):
            for action in actions:
                config = ACTION_CONFIG[action]
                btn_label = f"{config['icon']} {config['label']}"
                if st.button(btn_label, key=f"add_{action}", use_container_width=True):
                    add_step(action)
                    st.rerun()


def _apply_voice_result(key_suffix: str, text_area_key: str):
    """text_area描画前に呼び出し、音声入力の結果があればsession_stateに転記"""
    voice_key = f"_voice_result_{key_suffix}"
    if voice_key in st.session_state:
        st.session_state[text_area_key] = st.session_state.pop(voice_key)


def _apply_voice_result_append(key_suffix: str, text_area_key: str):
    """音声入力の結果を既存テキストに追記（プリセット用）"""
    voice_key = f"_voice_result_{key_suffix}"
    if voice_key in st.session_state:
        voice_text = st.session_state.pop(voice_key)
        existing = st.session_state.get(text_area_key, "")
        if existing.strip():
            st.session_state[text_area_key] = existing.rstrip() + "\n\n変更指示: " + voice_text
        else:
            st.session_state[text_area_key] = voice_text


def render_voice_input(key_suffix: str):
    """音声入力ボタンを描画し、文字起こし結果を一時キーに保存"""
    if not VOICE_INPUT_AVAILABLE:
        return
    audio = mic_recorder(
        start_prompt="🎤",
        stop_prompt="⏹",
        just_once=True,
        use_container_width=True,
        key=f"recorder_{key_suffix}"
    )
    if audio is not None:
        with st.spinner("文字起こし中..."):
            try:
                text = transcribe_audio(audio["bytes"])
                if text:
                    st.session_state[f"_voice_result_{key_suffix}"] = text
                    st.session_state["_reopen_dialog"] = (key_suffix == "dialog")
                    st.rerun()
            except Exception as e:
                st.error(f"文字起こしエラー: {e}")


@st.dialog("AIでワークフロー生成")
def ai_workflow_dialog():
    """AIによるワークフロー生成ダイアログ"""
    if not LLM_AVAILABLE:
        st.warning("AI生成を使うには .env に GEMINI_API_KEY（または LLM_MODE=openai_compatible と LLM_BASE_URL）を設定してください")
        return

    _apply_voice_result("dialog", "dialog_prompt")

    ai_prompt = st.text_area(
        "やりたいことを入力してください",
        placeholder="例: 90度回転して2秒待つ",
        height=150,
        key="dialog_prompt"
    )

    btn_col, mic_col = st.columns([5, 1], vertical_alignment="bottom")
    generate_clicked = btn_col.button("生成実行", type="primary", use_container_width=True)
    with mic_col:
        render_voice_input("dialog")

    if generate_clicked:
        if ai_prompt.strip():
            with st.spinner("生成中..."):
                try:
                    result = generate_workflow_from_prompt(ai_prompt)
                    set_workflow(result, "AI生成")
                    st.rerun()
                except Exception as e:
                    st.error(f"エラー: {e}")
        else:
            st.warning("指示を入力してください")


def get_param_summary(step):
    """ステップのパラメータサマリーを取得"""
    action = step["action"]

    if action == "move_xyz":
        return f"X: {step.get('x', 0)} Y: {step.get('y', 0)} Z: {step.get('z', 0)}"
    elif action == "move_z":
        return f"距離: {step.get('distance', 0)}mm"
    elif action == "move_radial":
        d = step.get('distance', 0)
        direction = "外向き" if d >= 0 else "内向き"
        return f"{abs(d)}mm {direction}"
    elif action in ["rotate", "rotate_relative"]:
        speed_labels = {"low": "低速", "normal": "普通", "high": "高速"}
        speed = speed_labels.get(step.get("speed", ""), "")
        speed_str = f"　｜　回転速度: {speed}" if speed else ""
        return f"角度: {step.get('angle', 0)}°{speed_str}"
    elif action == "move_slider":
        return f"位置: {step.get('position', 0)}mm"
    elif action == "move_conveyer":
        return f"速度: {step.get('speed', 0)}mm/s {step.get('duration', 0)}秒"
    elif action == "wait":
        return f"{step.get('seconds', 0)}秒"
    elif action == "aspirate":
        return f"{step.get('volume', 0)}mL 速度: {step.get('speed', 5)}"
    elif action == "dispense":
        return f"{step.get('volume', 0)}mL 速度: {step.get('speed', 5)}"
    elif action == "blow_out":
        return f"速度: {step.get('speed', 1)} 遅延: {step.get('delay_ms', 0)}ms"
    elif action == "capture_and_save":
        path = step.get('file_path', '')
        return escape(path) if path else "(自動)"
    elif action == "capture_microscope":
        path = step.get('file_path', '')
        return escape(path) if path else "(自動)"
    elif action == "microscope_led":
        lv = step.get('level')
        return ("点灯" if step.get('on', True) else "消灯") + (f" 明るさ: {lv}" if lv is not None else "")
    elif action == "microscope_focus":
        mode = step.get('mode', 'auto')
        if mode == "position":
            return f"位置指定: {step.get('position')}"
        if mode == "step":
            return f"ステップ {step.get('direction', 'in')} x{step.get('steps', 1)}"
        return f"AF (最大 {step.get('timeout', 60.0)}秒)"
    elif action == "measure_weight":
        return f"測定: {step.get('stabilization_count', 3)}回"
    elif action == "tare_scale":
        return f"待機: {step.get('delay', 1.0)}秒"
    elif action == "go_home":
        return ""
    elif action == "loop_start":
        return f"x{step.get('count', 10)}回"
    elif action == "loop_end":
        return ""
    return ""


def render_step_params(step, idx):
    """ステップのパラメータ入力UI（popover内用）"""
    action = step["action"]

    if action == "move_xyz":
        c1, c2, c3 = st.columns(3)
        step["x"] = c1.number_input("X", value=float(step.get("x", 0)), key=f"x_{idx}", label_visibility="collapsed",
                                    on_change=_update_step_param, args=(idx, "x", f"x_{idx}"))
        step["y"] = c2.number_input("Y", value=float(step.get("y", 0)), key=f"y_{idx}", label_visibility="collapsed",
                                    on_change=_update_step_param, args=(idx, "y", f"y_{idx}"))
        step["z"] = c3.number_input("Z", value=float(step.get("z", 0)), key=f"z_{idx}", label_visibility="collapsed",
                                    on_change=_update_step_param, args=(idx, "z", f"z_{idx}"))
        st.caption("X / Y / Z (mm)")

    elif action == "move_z":
        step["distance"] = st.number_input("距離 (mm)", value=float(step.get("distance", 0)), key=f"dist_{idx}",
                                           on_change=_update_step_param, args=(idx, "distance", f"dist_{idx}"))

    elif action == "move_radial":
        step["distance"] = st.number_input("半径距離 (mm)  正=外向き / 負=内向き", value=float(step.get("distance", 20.0)),
                                           key=f"rad_{idx}",
                                           on_change=_update_step_param, args=(idx, "distance", f"rad_{idx}"))

    elif action in ["rotate", "rotate_relative"]:
        label = "角度 (deg)" + (" - 相対" if action == "rotate_relative" else " - 絶対")
        c1, c2 = st.columns(2)
        step["angle"] = c1.number_input(label, value=float(step.get("angle", 0)), key=f"ang_{idx}",
                                        on_change=_update_step_param, args=(idx, "angle", f"ang_{idx}"))
        speed_options = ["low", "normal", "high"]
        speed_labels = {"low": "低速", "normal": "普通", "high": "高速"}
        current_speed = step.get("speed", "low")
        current_index = speed_options.index(current_speed) if current_speed in speed_options else 0
        step["speed"] = c2.selectbox("速度", options=speed_options, index=current_index,
                                     format_func=lambda x: speed_labels[x], key=f"rot_spd_{idx}",
                                     on_change=_update_step_param, args=(idx, "speed", f"rot_spd_{idx}"))

    elif action == "move_slider":
        step["position"] = st.number_input("位置 (mm)", value=float(step.get("position", 500.0)),
                                           min_value=0.0, max_value=1000.0, step=10.0, key=f"slider_pos_{idx}",
                                           on_change=_update_step_param, args=(idx, "position", f"slider_pos_{idx}"))

    elif action == "move_conveyer":
        c1, c2, c3 = st.columns(3)
        step["index"] = c1.number_input("Index", value=int(step.get("index", 0)),
                                        min_value=0, max_value=1, key=f"conv_idx_{idx}",
                                        on_change=_update_step_param, args=(idx, "index", f"conv_idx_{idx}"))
        step["speed"] = c2.number_input("速度 (mm/s)", value=float(step.get("speed", 10.0)),
                                        min_value=0.1, max_value=200.0, step=1.0, key=f"conv_spd_{idx}",
                                        on_change=_update_step_param, args=(idx, "speed", f"conv_spd_{idx}"))
        step["duration"] = c3.number_input("時間 (秒)", value=float(step.get("duration", 5.0)),
                                           min_value=0.1, max_value=300.0, step=1.0, key=f"conv_dur_{idx}",
                                           on_change=_update_step_param, args=(idx, "duration", f"conv_dur_{idx}"))

    elif action == "wait":
        step["seconds"] = st.number_input("秒数", value=float(step.get("seconds", 1)), min_value=0.1, key=f"sec_{idx}",
                                          on_change=_update_step_param, args=(idx, "seconds", f"sec_{idx}"))

    elif action == "aspirate":
        c1, c2 = st.columns(2)
        step["volume"] = c1.number_input("容量 (mL)", value=float(step.get("volume", 5.0)),
                                         min_value=0.1, max_value=10.0, step=0.1, key=f"asp_vol_{idx}",
                                         on_change=_update_step_param, args=(idx, "volume", f"asp_vol_{idx}"))
        step["speed"] = c2.number_input("速度", value=int(step.get("speed", 5)),
                                        min_value=1, max_value=9, key=f"asp_spd_{idx}",
                                        on_change=_update_step_param, args=(idx, "speed", f"asp_spd_{idx}"))

    elif action == "dispense":
        c1, c2 = st.columns(2)
        step["volume"] = c1.number_input("容量 (mL)", value=float(step.get("volume", 5.0)),
                                         min_value=0.1, max_value=10.0, step=0.1, key=f"dis_vol_{idx}",
                                         on_change=_update_step_param, args=(idx, "volume", f"dis_vol_{idx}"))
        step["speed"] = c2.number_input("速度", value=int(step.get("speed", 5)),
                                        min_value=1, max_value=9, key=f"dis_spd_{idx}",
                                        on_change=_update_step_param, args=(idx, "speed", f"dis_spd_{idx}"))

    elif action == "blow_out":
        c1, c2, c3 = st.columns(3)
        step["go_home"] = c1.checkbox("ホーム", value=bool(step.get("go_home", True)), key=f"bo_home_{idx}",
                                      on_change=_update_step_param, args=(idx, "go_home", f"bo_home_{idx}"))
        step["speed"] = c2.number_input("速度", value=int(step.get("speed", 1)),
                                        min_value=1, max_value=9, key=f"bo_spd_{idx}",
                                        on_change=_update_step_param, args=(idx, "speed", f"bo_spd_{idx}"))
        step["delay_ms"] = c3.number_input("遅延(ms)", value=int(step.get("delay_ms", 3000)),
                                           min_value=0, max_value=10000, step=100, key=f"bo_delay_{idx}",
                                           on_change=_update_step_param, args=(idx, "delay_ms", f"bo_delay_{idx}"))

    elif action == "capture_and_save":
        step["file_path"] = st.text_input("ファイルパス", value=str(step.get("file_path", "")), key=f"cap_path_{idx}",
                                          on_change=_update_step_param, args=(idx, "file_path", f"cap_path_{idx}"))

    elif action == "capture_microscope":
        step["file_path"] = st.text_input("ファイルパス", value=str(step.get("file_path", "")), key=f"micro_path_{idx}",
                                          on_change=_update_step_param, args=(idx, "file_path", f"micro_path_{idx}"))

    elif action == "microscope_led":
        step["on"] = st.checkbox("点灯する", value=bool(step.get("on", True)), key=f"mled_on_{idx}",
                                 on_change=_update_step_param, args=(idx, "on", f"mled_on_{idx}"))
        set_level = st.checkbox("明るさも設定する", value=step.get("level") is not None, key=f"mled_setlv_{idx}")
        if set_level:
            step["level"] = st.number_input("明るさ (0-255、出荷時 12)", value=int(step.get("level") or 12),
                                            min_value=0, max_value=255, key=f"mled_lv_{idx}",
                                            on_change=_update_step_param, args=(idx, "level", f"mled_lv_{idx}"))
        else:
            step["level"] = None

    elif action == "microscope_focus":
        modes = ["auto", "position", "step"]
        step["mode"] = st.selectbox("モード", modes, index=modes.index(step.get("mode", "auto")), key=f"mf_mode_{idx}",
                                    on_change=_update_step_param, args=(idx, "mode", f"mf_mode_{idx}"))
        if step["mode"] == "position":
            step["position"] = st.number_input("レンズ位置 (0-65535)", value=int(step.get("position") or 1568),
                                               min_value=0, max_value=65535, key=f"mf_pos_{idx}",
                                               on_change=_update_step_param, args=(idx, "position", f"mf_pos_{idx}"))
        elif step["mode"] == "step":
            dirs = ["in", "out"]
            step["direction"] = st.selectbox("方向", dirs, index=dirs.index(step.get("direction", "in")), key=f"mf_dir_{idx}",
                                             on_change=_update_step_param, args=(idx, "direction", f"mf_dir_{idx}"))
            step["steps"] = st.number_input("回数", value=int(step.get("steps", 1)), min_value=1, max_value=100, key=f"mf_steps_{idx}",
                                            on_change=_update_step_param, args=(idx, "steps", f"mf_steps_{idx}"))
        step["timeout"] = st.number_input("待機上限(秒)", value=float(step.get("timeout", 60.0)), min_value=1.0, max_value=300.0,
                                          step=5.0, key=f"mf_to_{idx}",
                                          on_change=_update_step_param, args=(idx, "timeout", f"mf_to_{idx}"))

    elif action == "measure_weight":
        step["stabilization_count"] = st.number_input("測定回数", value=int(step.get("stabilization_count", 3)),
                                                      min_value=1, max_value=10, key=f"mw_count_{idx}",
                                                      on_change=_update_step_param, args=(idx, "stabilization_count", f"mw_count_{idx}"))

    elif action == "tare_scale":
        step["delay"] = st.number_input("待機時間(秒)", value=float(step.get("delay", 1.0)),
                                        min_value=0.1, max_value=10.0, step=0.1, key=f"tare_delay_{idx}",
                                        on_change=_update_step_param, args=(idx, "delay", f"tare_delay_{idx}"))

    elif action == "loop_start":
        step["count"] = st.number_input(
            "繰り返し回数",
            value=int(step.get("count", 10)),
            min_value=1,
            max_value=1000,
            key=f"loop_count_{idx}",
            on_change=_update_step_param, args=(idx, "count", f"loop_count_{idx}")
        )
        st.caption(f"ループID: {step.get('loop_id', '')}")

    elif action == "loop_end":
        st.caption(f"ループID: {step.get('loop_id', '')}")
        st.info("対応する「ループ開始」と連動します")


def render_step_card(step, idx, total):
    """ステップカードを描画（シンプル版：カード + メニューpopover）"""
    action = step["action"]
    config = ACTION_CONFIG.get(action, {"icon": "❓", "label": action})
    is_shared = action in SHARED_DEVICE_ACTIONS
    is_loop = action in ["loop_start", "loop_end"]

    # スタイル決定
    if is_loop:
        bg_color = LOOP_COLORS["bg"]
        border_color = LOOP_COLORS["border"]
        bracket_color = LOOP_COLORS["bracket"]
        bracket = "┌" if action == "loop_start" else "└"
        robot_badge = ""
    elif is_shared:
        bg_color = SHARED_COLOR["bg"]
        border_color = SHARED_COLOR["border"]
        robot_badge = '<span style="float:right; font-size:0.9em; color:#666;">📡 共有</span>'
    else:
        r_id = step.get("robot_id", 1)
        robot_color = ROBOT_COLORS.get(r_id, ROBOT_COLORS[1])
        bg_color = robot_color["bg"]
        border_color = robot_color["border"]
        robot_badge = f'<span style="float:right; font-size:0.9em;">{robot_color["label"]} R{r_id}</span>'

    # パラメータサマリー取得
    param_summary = get_param_summary(step)

    # カード本体（ループ用とそれ以外で分岐）
    if is_loop:
        st.markdown(f"""
            <div style="background-color: {bg_color};
                        border-left: 6px solid {border_color};
                        padding: 8px 12px;
                        border-radius: 6px;
                        margin-bottom: 4px;
                        display: flex;
                        align-items: center;">
                <span style="font-size: 1.5em; font-weight: bold; color: {bracket_color}; margin-right: 8px;">{bracket}</span>
                <div>
                    <div style="font-weight: bold;">
                        {config['icon']} {config['label']}
                        <span style="color:#888; font-size:0.8em; margin-left:8px;">#{idx+1}</span>
                    </div>
                    {f'<div style="color:#000; font-size:1.05em;">{param_summary}</div>' if param_summary else ''}
                </div>
            </div>
        """, unsafe_allow_html=True)
    else:
        st.markdown(f"""
            <div style="background-color: {bg_color};
                        border-left: 4px solid {border_color};
                        padding: 10px 12px;
                        border-radius: 6px;
                        margin-bottom: 4px;">
                <div style="font-weight: bold; font-size: 1.1em;">
                    {config['icon']} {config['label']}
                    <span style="color:#888; font-size:0.8em; margin-left:8px;">#{idx+1}</span>
                    {robot_badge}
                </div>
                {f'<div style="color:#000; font-size:1.05em; margin-top:4px;">{param_summary}</div>' if param_summary else ''}
            </div>
        """, unsafe_allow_html=True)

    # メニューpopover（全操作を収納）
    with st.popover("⋮ メニュー", use_container_width=True):
        st.markdown(f"**{config['icon']} {config['label']} #{idx+1}**")

        # 詳細設定（一番上に配置）
        st.markdown("**詳細設定**")
        render_step_params(step, idx)

        # 順序変更
        st.divider()
        st.markdown("**順序変更**")
        col_up, col_down = st.columns(2)
        with col_up:
            st.button("↑ 上へ", key=f"up_{idx}", disabled=(idx == 0), use_container_width=True,
                      on_click=move_step, args=(idx, -1))
        with col_down:
            st.button("↓ 下へ", key=f"down_{idx}", disabled=(idx == total - 1), use_container_width=True,
                      on_click=move_step, args=(idx, 1))

        # ロボット選択（共有デバイス・ループ以外）
        if not is_shared and not is_loop:
            st.divider()
            st.markdown("**ロボット割当**")
            current_robot = step.get("robot_id", 1)
            robot_options = [f"{c['label']} {c['name']}" for c in ROBOT_COLORS.values()]
            radio_key = f"robot_select_{idx}"
            st.radio(
                "ロボット選択",
                robot_options,
                index=current_robot - 1,
                horizontal=True,
                key=radio_key,
                label_visibility="collapsed",
                on_change=_update_robot_id,
                args=(idx, radio_key)
            )

        # 削除ボタン（赤色）
        st.divider()
        st.markdown('<div class="delete-btn">', unsafe_allow_html=True)
        st.button("🗑 削除", key=f"del_{idx}", use_container_width=True,
                  on_click=remove_step, args=(idx,))
        st.markdown('</div>', unsafe_allow_html=True)


def render_empty_state():
    """空の状態のUI - AI生成を促す"""
    st.markdown("---")

    # 中央寄せのコンテナ
    col1, col2, col3 = st.columns([1, 6, 1])
    with col2:
        st.markdown(
            """
            <div style="text-align: center; padding: 20px 0;">
                <span style="font-size: 3em;">🤖</span>
            </div>
            """,
            unsafe_allow_html=True
        )
        st.markdown("### ワークフローを作成しましょう")

        # プリセット選択
        preset_key = None
        if PRESET_TEMPLATES:
            preset_labels = {"": "選択なし（自由入力）"}
            preset_labels.update({k: v["label"] for k, v in PRESET_TEMPLATES.items()})
            selected = st.selectbox(
                "テンプレート",
                options=list(preset_labels.keys()),
                format_func=lambda k: preset_labels[k],
                key="preset_selector",
                label_visibility="collapsed",
            )
            if selected:
                preset_key = selected
                if st.session_state.get("_last_preset") != preset_key:
                    st.session_state["_last_preset"] = preset_key
                    st.session_state["empty_state_prompt"] = ""
                    st.rerun()

        # 音声入力結果の転記（text_area描画前に実行）
        _apply_voice_result("empty_state", "empty_state_prompt")

        # AI入力エリア
        ai_prompt = st.text_area(
            "やりたいことを入力",
            placeholder="変更したいパラメータを入力してください（例: 量は3mlで速度は低速）" if preset_key else "例: robot1を50mm下降させて、50ml吸引してホームポジションに戻る",
            height=200,
            label_visibility="collapsed",
            key="empty_state_prompt"
        )

        btn_col, mic_col = st.columns([5, 1], vertical_alignment="bottom")
        generate_clicked = btn_col.button("✨ AIで生成する", type="primary", use_container_width=True, key="empty_state_generate")
        with mic_col:
            render_voice_input("empty_state")

        if generate_clicked:
            if preset_key and not ai_prompt.strip():
                # プリセット選択でパラメータ変更なし → テンプレートをそのまま使用
                set_workflow(PRESET_TEMPLATES[preset_key]["template"], "プリセット")
                st.rerun()
            elif ai_prompt.strip():
                if LLM_AVAILABLE:
                    with st.spinner("生成中..."):
                        try:
                            template = PRESET_TEMPLATES[preset_key]["template"] if preset_key else None
                            llm_input = PRESET_TEMPLATES[preset_key]["description"] + "\n\n変更指示: " + ai_prompt if preset_key else ai_prompt
                            result = generate_workflow_from_prompt(llm_input, template=template)
                            set_workflow(result, "AI生成")
                            st.rerun()
                        except Exception as e:
                            if preset_key:
                                set_workflow(PRESET_TEMPLATES[preset_key]["template"], "プリセット")
                                st.info("テンプレートから生成しました")
                                st.rerun()
                            else:
                                st.error(f"エラー: {e}")
                else:
                    st.warning("AI生成を使うには .env に GEMINI_API_KEY（または LLM_MODE=openai_compatible と LLM_BASE_URL）を設定してください")
            else:
                st.warning("やりたいことを入力してください")

        st.markdown("---")
        st.markdown(
            """
            <div style="text-align: center; color: #888;">
                または 👈 サイドバーから手動でブロックを追加
            </div>
            """,
            unsafe_allow_html=True
        )

    st.markdown("---")


def render_workflow_canvas():
    """ワークフローキャンバス（時系列順、左右で区別、ループ対応）"""
    steps = st.session_state.workflow_data["steps"]

    if not steps:
        render_empty_state()
        return

    # ヘッダー
    col_h1, col_h2 = st.columns(2)
    with col_h1:
        st.markdown(f"**{ROBOT_COLORS[1]['label']} Robot 1**")
    with col_h2:
        st.markdown(f"**{ROBOT_COLORS[2]['label']} Robot 2**")

    # アクティブなループを追跡（インデント計算用）
    active_loops = []

    # 時系列順にステップを表示（左右で区別）
    for idx, step in enumerate(steps):
        action = step["action"]
        is_shared = action in SHARED_DEVICE_ACTIONS
        is_loop = action in ["loop_start", "loop_end"]
        robot_id = step.get("robot_id", 1) if not is_shared and not is_loop else None

        # loop_endの場合は先にスタックから削除（インデント調整）
        if action == "loop_end":
            loop_id = step.get("loop_id")
            if loop_id in active_loops:
                active_loops.remove(loop_id)

        # インデントレベル計算
        indent_level = len(active_loops)
        indent_margin = indent_level * 20  # 20px per level

        # インデント適用
        if indent_margin > 0:
            st.markdown(f'<div style="margin-left: {indent_margin}px;">', unsafe_allow_html=True)

        if is_loop:
            # ループ: 全幅で表示
            render_step_card(step, idx, len(steps))
        elif is_shared:
            # 共有デバイス: 中央配置
            col_l, col_c, col_r = st.columns([1, 1, 1])
            with col_c:
                render_step_card(step, idx, len(steps))
        elif robot_id == 1:
            # Robot 1: 左側
            col1, col2, col3 = st.columns(3)
            with col1:
                render_step_card(step, idx, len(steps))
        elif robot_id == 2:
            # Robot 2: 中央
            col1, col2, col3 = st.columns(3)
            with col2:
                render_step_card(step, idx, len(steps))
        else:
            # Robot 3: 右側
            col1, col2, col3 = st.columns(3)
            with col3:
                render_step_card(step, idx, len(steps))

        if indent_margin > 0:
            st.markdown('</div>', unsafe_allow_html=True)

        # loop_startの場合はスタックに追加
        if action == "loop_start":
            loop_id = step.get("loop_id")
            active_loops.append(loop_id)


def render_settings_panel():
    """設定パネル"""
    st.markdown("##### ⚙️ 設定")

    running = is_running()

    with st.expander("🔧 接続ポート", expanded=False):
        st.caption(
            "既定値は config.yaml（無ければ config.example.yaml）です。"
            "ここでの変更はこのセッションだけに効きます。"
            "実際に初期化されるのはフローが使うロボット・デバイスだけです。"
        )
        for robot_id in ROBOT_COLORS:
            color = ROBOT_COLORS[robot_id]
            config = st.session_state.robot_configs[robot_id]
            st.markdown(f"**{color['label']} {color['name']}**")
            config["dobot_port"] = st.text_input(
                "Dobot Port", value=config["dobot_port"],
                key=f"r{robot_id}_dp", disabled=running)
            config["use_picus2"] = st.checkbox(
                "Picus2 あり", value=config.get("use_picus2", False),
                key=f"r{robot_id}_picus2", disabled=running)
            if config["use_picus2"]:
                config["picus2_port"] = st.text_input(
                    "Picus2 Port", value=config.get("picus2_port", ""),
                    key=f"r{robot_id}_pp", disabled=running)

    with st.expander("📡 共有デバイス", expanded=False):
        config = st.session_state.shared_config
        config["scale_port"] = st.text_input(
            "天秤 Port", value=config["scale_port"], key="scale_port", disabled=running)
        config["camera_index"] = st.number_input(
            "カメラ index", value=int(config.get("camera_index", 0)), min_value=0,
            step=1, key="camera_index", disabled=running)

    if st.button("↺ config.yaml の値に戻す", use_container_width=True, disabled=running):
        defaults = flow_runner.default_device_settings()
        st.session_state.robot_configs = defaults["robots"]
        st.session_state.shared_config = defaults["shared"]
        st.rerun()

    st.divider()
    st.session_state.use_mock = st.toggle(
        "🛠 Mockモード", value=st.session_state.use_mock, disabled=running,
        help="実機に触れず、Mock デバイスで最後まで実行します（録画・安全確認ゲートなし）",
    )

    st.divider()
    uploaded_file = st.file_uploader("📂 JSON読込", type="json", label_visibility="collapsed")
    if uploaded_file:
        file_id = uploaded_file.file_id
        if st.session_state.get("_loaded_file_id") != file_id:
            try:
                set_workflow(json.load(uploaded_file), "読み込んだJSON")
                st.session_state._loaded_file_id = file_id
                st.success("読込完了")
            except:
                st.error("JSONエラー")

    with st.popover("📄 JSON確認"):
        st.json(st.session_state.workflow_data)


# ==========================================
# 8. メインレイアウト
# ==========================================
st.title("🧩 Visual Automation Controller")

# カスタムCSS
st.markdown("""
<style>
/* 実行パネル */
.execution-panel {
    padding: 8px 0;
    border-bottom: 2px solid #e0e0e0;
    margin-bottom: 12px;
}

/* 緊急停止ボタン（赤色） - execution-panel内の最初のカラムのボタン */
.execution-panel [data-testid="column"]:first-child button {
    background-color: #ffebee !important;
    border: 2px solid #f44336 !important;
    color: #c62828 !important;
}
.execution-panel [data-testid="column"]:first-child button:hover {
    background-color: #ffcdd2 !important;
    border-color: #d32f2f !important;
}

/* 削除ボタン（赤色テキスト） */
.delete-btn button {
    color: #c62828 !important;
    border-color: #ef9a9a !important;
}
.delete-btn button:hover {
    background-color: #ffebee !important;
}
</style>
""", unsafe_allow_html=True)

# サイドバー（ツール・設定）
with st.sidebar:
    render_toolbox()
    st.divider()
    render_settings_panel()

# メインエリア
# エラーメッセージ表示
if st.session_state.error_message:
    st.error(st.session_state.error_message)
    st.session_state.error_message = None

# ワークフロー（上部）- ヘッダーとAIボタン
col_wf_title, col_ai_btn = st.columns([3, 1])
with col_wf_title:
    st.markdown("##### 📋 ワークフロー")
with col_ai_btn:
    if st.button("✨ AIで新規作成", use_container_width=True) or st.session_state.pop("_reopen_dialog", False):
        ai_workflow_dialog()

render_workflow_canvas()
# 進捗の取り込み（バックグラウンド実行 → ログ・進捗・完了状態）
drain_runner_events()

# ログエリア（固定高さ）
st.markdown("##### 📜 ログ")
with st.container(height=150):
    if st.session_state.log_messages:
        st.code("\n".join(st.session_state.log_messages), language="bash")
    else:
        st.code("待機中...", language="bash")

# 実行パネル（下部）
render_execution_panel()

# ==========================================
# 9. 実行中のポーリング
# ==========================================
# 実行そのものは runner のバックグラウンドスレッドが最後まで走らせる。
# ここでは一定間隔で rerun して、キューに溜まった進捗を描画するだけ。
_runner = st.session_state.runner
if _runner is not None and (_runner.is_running() or not _runner.events.empty()):
    time.sleep(0.4)
    st.rerun()

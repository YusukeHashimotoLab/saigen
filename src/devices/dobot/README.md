# Dobotロボットアーム制御モジュール

Dobot Magician を `pydobot` ライブラリ経由（シリアル通信）で制御するドライバです。
各インスタンスが独立したシリアル接続を持つため、複数台の Dobot を同時に制御できます。

## ファイル

- `home_dobot.py` — ホーミング CLI（`python -m src.devices.dobot.home_dobot --robot N`）

| ファイル | 役割 |
|---|---|
| `pydobot_controller.py` | `PyDobotController` — 本体 |
| `pydobot_patch.py` | pydobot の通信部分を差し替えるパッチ（応答フレームの検証、完了待ちのタイムアウトと緊急停止での打ち切り） |
| `dobot_config.py` | 速度プリセット・作業位置の定義（`DobotConfig`） |

## 使用例

```python
from src.devices.dobot import PyDobotController

# ポート名を指定して接続（Windows: 'COM12', Linux/macOS: '/dev/ttyUSB0' など）
robot = PyDobotController(port_name='COM12', homing=False)

robot.move_XYZ_abs(200, 0, 100)   # 絶対座標へ移動
robot.move_Z(-50)                 # Z 相対移動
robot.move_XY(20, 30)             # XY 相対移動
robot.move_angle(45)              # ベース（Joint1）角度
print(robot.get_current_position())

robot.disconnect()
```

利用可能なシリアルポートの一覧:

```python
PyDobotController.list_available_ports()
```

## 主なメソッド

| メソッド | 説明 |
|---|---|
| `move_XYZ_abs(x, y, z, r=None)` | 絶対座標へ移動 |
| `move_Z(z_offset)` / `move_XY(dx, dy)` | 相対移動 |
| `move_angle(angle)` | Joint1 の絶対角度 |
| `move_to_initial_pos()` | 初期位置へ |
| `pickup(...)` / `place(...)` | ピックアンドプレース |
| `set_gripper(enabled, on)` / `set_suction_cup(enabled, on)` | エンドエフェクタ |
| `set_speed_preset(name)` | `DobotConfig.SPEED_PRESETS` の速度に切替（直交・Joint の両方） |
| `set_home_params(x, y, z, r)` | ホーム位置の記録 |
| `home(x, y, z, r, timeout_s)` | ファームウェアのホーミング（SetHOMEParams / SetHOMECmd）。完了まで待ち、戻り先を省略すると開始位置に戻る |
| `move_slider(pos)` | リニアレール（0–1000 mm） |
| `move_conveyer(index, speed, time_seconds)` | コンベアベルト |
| `get_current_position()` | `[x, y, z, r]` |
| `force_stop()` | 緊急停止（キュー強制停止・破棄・コンベア停止）。完了待ち中のスレッドも解放する |
| `disconnect()` | 切断 |

## 注意点

- 自動運転の前に、手動で可動域と周囲の安全を確認してください。
- `homing=True` を指定するとホーミング動作で腕が動きます。障害物がないことを確認してください。
  通常は電源投入後に CLI で 1 台ずつ行います:
  `python -m src.devices.dobot.home_dobot --robot 1`（`--mock` で手順だけ確認、
  実機では Enter を押すまで動きません）。戻り先は config.yaml の workspace で事前検証されます。
- 実験用の安全ラッパ（`src/devices/safety/lab_robot.py`）はこのクラスを内部で使い、
  可動域チェックと待機時間を追加します。フロー実行時はラッパ経由で呼び出されます。
- モーターから異音がした場合は直ちに `disconnect()` して物理的な干渉を確認してください。
- 通信の扱い（`pydobot_patch.py`）: 応答はヘッダ `AA AA`・長さ・チェックサム・コマンド ID を
  確認してから使い、送信前に受信バッファを捨てます。一致する応答が 2 秒以内に来なければ
  `DobotReplyError`（`ConnectionError` の派生）。キュー付き移動の完了待ちは既定 60 秒で
  `DobotWaitTimeout`（`PyDobotController(move_timeout_s=...)` で変更可。アームは止めないので
  呼び出し側で `force_stop()` すること）。`force_stop()` の後は待機中の移動が
  `DobotMoveAborted` で即座に戻ります。停止フラグは次の移動指示で下ります。

## 依存関係

- `pydobot`（Copyright 2017 Luis Mesas, MIT License。`pydobot_patch.py` はこのライブラリの `_send_command` / `_send_message` / `_read_message` / `_get_queued_cmd_current_index` / `__init__` を差し替える派生コードです。全文は `THIRD_PARTY_NOTICES.md` 参照）
- `pyserial`

Dobot 純正 DLL（DobotDll）を用いる旧ドライバはライセンス上の理由で本リポジトリには含めていません。

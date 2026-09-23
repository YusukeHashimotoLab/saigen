# USB デジタル顕微鏡（UVC）制御モジュール

USB 接続のデジタル顕微鏡を OpenCV で制御し、実験記録用の静止画を保存する。
論文のセルではサンワサプライ **400-CAM106** を使っている。この機種を含め、
多くの USB 顕微鏡は OS からは UVC（USB Video Class）のカメラとして見えるので、
専用ドライバは要らず、ウェブカメラと同じ `cv2.VideoCapture` で扱える。

## ファイル

- `microscope_controller.py` — `MicroscopeController`（`WebcamController` を継承）: 映像（UVC）
- `um22_serial.py` — `UM22SerialController`: 制御 MCU とのシリアル通信（LED の ON/OFF・明るさ、フォーカスモーター、状態・型番の読み出し）

## ウェブカメラ用ドライバとの違い

| 項目 | `WebcamController` | `MicroscopeController` |
|---|---|---|
| 保存先 | `captured_images/` | `microscope_images/` |
| 既定解像度 | 1280x720 | **3840x2160**（config.yaml の `shared_devices.microscope_resolution` で変更） |
| 撮影前の読み捨てフレーム | 5 | 10（`warmup_frames`） |
| 露出 | 自動 | 自動。`exposure=` で手動固定も可 |
| 単色フレームの検出 | なし | 警告を出す（`is_uniform`） |

フローからは共有デバイスの `capture_microscope`（撮影）、`microscope_led`（LED）、
`microscope_focus`（焦点）で使う（いずれも `robot_id` 不要）。`SharedDevices` の
`capture_microscope(file_path)`、`set_microscope_led(on, level)`、
`focus_microscope(mode, ...)` がそれぞれ本モジュールのクラスを呼ぶ。

## LED 照明の制御（UM22 系）

400-CAM106 の中身は Vitiny **UM22** で、1 本の USB ケーブルの先に内部ハブがあり、
UVC カメラ（映像）と **CP210x USB シリアル（制御 MCU）** がぶら下がっている。
LED の ON/OFF・明るさ、ズーム、オートフォーカスはこのシリアル経由で制御される。
COM ポート番号は PC ごとに違うので `config.yaml` の `shared_devices.microscope_port` に書く
（空なら LED 制御なし、撮影だけ）。

プロトコル（メーカー製 UM Viewer 1.000.096 を解析し、2026-09-22 に実機で検証）:

| 項目 | 内容 |
|---|---|
| 通信条件 | 115200 bps、8 ビット、パリティなし、**ストップビット 2**、コマンドは **CR 終端** |
| 応答 | `@` + 16 進 2 桁 + CR（例 `@0C`） |
| 読み出し | `R00`+アドレス。0x00 = 状態（`01` 点灯 / `03` 消灯）、0x06 = LED レベル、0x10 = LED モード、0x2F/2E/2D = FW 版数 |
| 型番 | `S00`+アドレス を 0x2C→0x23 の順に 10 回（`UM22TW0100`） |
| 書き込み | `W` + ((アドレス<<8) \| キー \| 値) の 16 進 4 桁 |
| LED ON/OFF | **トグル** `W0159`（UM Viewer の LED ボタンと同じ）。`set_led(on)` は状態を読んで必要なときだけ送る |
| LED 明るさ | `W06`+レベル（出荷時 12。4 で暗く、12 以上で画像が飽和するほど明るい） |
| **注意** | **RTS を立てると MCU がリセットされ、カメラも電源が入れ直される**（約 7 秒）。pyserial は既定で open 時に RTS を立てるので、`rts=False` にしてから開く（本クラスは対応済み） |

## 焦点合わせ（フォーカスモーター）

| 項目 | 内容 |
|---|---|
| レンズ位置の読み出し | `R0009`（上位）と `R000A`（下位）。実機で 346〜1766 を観測、出荷時 1568 |
| 動作中の判定 | 状態レジスタ（`R0000`）のビット 0x04 が立つ。モーターモードは `R0004`（0 待機、2 AF 探索中） |
| AF モード | `W0154` 手動 / `W0155` ワンショット AF / `W0156` 連続 AF |
| 位置指定 | `W18`+上位バイト、`W19`+下位バイト、続けて `W016E`（1755 → 1568 を 4.6 秒で正確に移動） |
| ステップ移動 | `W0152`（in）/ `W0153`（out）で押し、`W0192` / `W0193` で離す。実機では out で位置の値が増える |
| LED レベル書き込みの副作用 | `W06xx` を書くと LED が点灯する。消灯のまま明るさだけ変えるなら `set_led(False, level=...)` |

`autofocus()` は AF を送ってモーターが止まるまで待ち、`(位置, 収束したか)` を返す。
位置や状態が連続して読めないときは `ConnectionError`（通信断を「停止」と誤認しない）。
`request_stop()` で進行中の操作を別スレッドから止められ、例外や割り込みの経路でも
手動モード（`W0154`）を送ってモーターを止める。`step_focus()` は `(位置, 完了したか)` を返す。
**AF は映像ストリームが動いている間だけ働く**（`SharedDevices` はフォーカス中に
フレームを読み続ける）。実機では印刷した数字「4」に対し、全域を一往復して約 40 秒で
位置 1600 に収束した（鮮鋭度 6.0、他の位置では 2〜4）。
**視野にテクスチャが無いと AF は収束せず探索し続ける**（白紙で 60 秒以上）。その場合は
timeout で手動モードに戻して探索を止める。白い対象で画像が飽和する場合は LED レベルを
下げるか露出を短くしてから AF する。
再現性の点では、一度合焦した位置を `get_motor_position()` で読んで記録し、次回は
`goto_position()`（フローでは `mode: "position"`）で同じ位置に戻すのが確実。

```python
with UM22SerialController("COM10") as scope:
    scope.set_led(True, level=4)
    pos, ok = scope.autofocus(timeout_s=60)     # -> (1234, True)
    scope.goto_position(1568)                    # 記録済みの位置へ
    scope.step_focus("out", steps=2)
```

動作確認: `python -m src.devices.microscope.um22_serial COM10 af` / `... goto 1568`

```python
from src.devices.microscope import UM22SerialController

with UM22SerialController("COM10") as scope:
    print(scope.get_model(), scope.get_firmware())   # UM22TW0100 01.03.00
    scope.set_led(False)                              # 消灯
    scope.set_led(True, level=8)                      # 点灯して明るさ 8
```

動作確認: `python -m src.devices.microscope.um22_serial COM10 [on|off]`

## 使用例

```python
from src.devices.microscope import MicroscopeController

with MicroscopeController(camera_index=2) as scope:      # index は環境ごとに違う
    print(scope.get_camera_info())
    scope.capture_and_save("sample_001.jpg")             # -> microscope_images/sample_001.jpg
```

動作確認（接続してカメラ一覧と 1 枚の撮影を行う）:

```bash
python -m src.devices.microscope.microscope_controller 2   # 引数はカメラ index
```

## 解像度とデータ量

実測（DirectShow、2026-09-23）: 3840x2160 ≈ 14 fps、1280x720 ≈ 6 fps、1920x1080 ≈ 2 fps。
4K が最も精細で最も速いので既定にしている。JPEG 1 枚は 0.4〜2 MB（被写体の細かさ次第。
白地に文字だけなら 0.4 MB）なので、1 回の実験で数百枚撮る・ディスクや転送が厳しいときは
`microscope_resolution: "1280x720"` に下げる（1 枚 ≈ 100〜200 KB）。

## 機器の見分け方（400-CAM106）

- デバイスマネージャーでは **UVC Video Device**（VID `EB1A` / PID `279F`、eMPIA 製
  チップ）として見える。ウェブカメラの Logitech C920 は VID `046D`。
- OpenCV の index は列挙順で決まり、PC や USB の挿し口で変わる。Windows では
  `pygrabber` で DirectShow の名前一覧を出すと対応が分かる（上の動作確認コマンドが表示する）。
- 決まった index は `config.yaml` の `shared_devices.microscope_index` に書く。

## 注意点

- 対物側にレンズキャップが付いたままだったり、対象物に密着していると、画像が
  一様な単色になる。`capture_image` はその場合に警告を出す（撮影自体は行う）。
- 400-CAM106 は本体の LED 照明を持つ。明るさは本体側で調整する。
- 露出は OpenCV の `CAP_PROP_EXPOSURE`（Windows では −13〜−1 程度の対数値）。
  自動露出で飽和する場合だけ `exposure=` を指定する。
- 別のアプリ（Windows カメラアプリ、メーカー製 UM Viewer 等）が顕微鏡を開いていると
  フレームを取得できない（接続は成功するが `read()` が失敗し続ける）。
- **メーカー製 UM Viewer は「接続」時に PC の全 COM ポートへ 115200 bps で問い合わせを送る。**
  Dobot・Picus 2・天びんのポートにも送られるので、実験中は UM Viewer を起動しない。
- **緑かぶり（白い対象が緑に写る）の原因と対策。** この機種は電源投入（MCU リセット含む）後、
  ホストが UVC のホワイトバランスコントロールを一度書き込むまで WB ゲインが適用されず、
  R と B が G の半分程度になる（2026-09-22/23 に実機で確認。画像が飽和しているときだけ
  白く見える）。メーカー製 UM Viewer は接続時に WB を書くので直っていた。
  `MicroscopeController.connect()` は WB 5000 K（この機種の既定値）を毎回書き込むので、
  saigen 経由なら対策不要。Media Foundation では WB を書けないため Windows では
  DirectShow で開いている。LED の明るさは原因ではない（WB 適用後はレベル 4 でも中立）。
- **AF は映像ストリームが動いている間だけ収束する**（AF の評価値は映像パイプラインから
  得ている）。`SharedDevices.focus_microscope` はフォーカス中にカメラのフレームを
  読み続けるので、フローから使う分には意識しなくてよい。`UM22SerialController` を
  単体で使うときは、`cv2.VideoCapture` を開いて `read()` し続けながら `autofocus()` を呼ぶ。
- MCU リセット（`reset_mcu()`）はレンズ位置を保ち、LED レベルを出荷時の 12 に戻す。

## 依存関係

- `opencv-python`（`numpy`）
- `pygrabber`（Windows のみ、動作確認コマンドでのカメラ名表示に使用。無くても動く）
- `pyserial`（LED 制御。`um22_serial.py` の中で遅延 import するので、撮影だけなら不要）

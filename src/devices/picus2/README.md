# Picus2電動ピペット制御モジュール

このモジュールは実験自動化システムにおいてPicus2電動ピペットを USB シリアルまたは Bluetooth 経由で制御するためのクラスと関数を提供します。

## 概要

`picus2_controller.py`はPicus2電動ピペットを操作するための非同期インターフェースを提供します。主な機能には以下が含まれます：

- Bluetooth接続管理
- ボタン操作のエミュレーション
- 液体の吸引・吐出
- チップの排出
- 液体の完全排出（ブローアウト）
- 液体の混合

このモジュールは`asyncio`と`bleak`ライブラリを使用した非同期通信を採用しています。

## 使用方法

### 基本的な使用例

```python
import asyncio
from picus2.picus2_controller import Picus2Controller, Buttons

async def main():
    # Picus2コントローラーの初期化（BluetoothMACアドレスを指定）
    picus = Picus2Controller("XX:XX:XX:XX:XX:XX")
    
    # 接続
    await picus.connect()
    
    try:
        # モーターモードを有効化
        await picus.set_motor_mode(True)
        
        # 5mLを吸引（速度5）
        await picus.aspirate(amount=5.0, speed=5)
        
        # 5mLを吐出（速度5）
        await picus.dispense(amount=5.0, speed=5)
        
        # 残液を完全に排出
        await picus.blow_out()
        
        # チップを排出
        await picus.eject_tip()
        
    finally:
        # モーターモードを無効化
        await picus.set_motor_mode(False)
        
        # 切断
        await picus.disconnect()

# 非同期関数を実行
asyncio.run(main())
```

### ボタン操作

```python
# 特定のボタンを押す
await picus.button(Buttons.TRIGGER_BUTTON_POWER)
await picus.button(Buttons.TRIGGER_BUTTON_LEFT)
await picus.button(Buttons.TRIGGER_BUTTON_RIGHT)
```

### 液体混合

```python
# 5回、速度5で3.0mLの液体を混合
await picus.mix(cycles=5, speed=5, amount=3.0)
```

## 主要機能

### 接続管理

- `connect()` - Picus2に接続する
- `disconnect()` - Picus2から切断する
- `send_command(command)` - コマンドを送信する

### ボタン操作

- `button(button, interval)` - 指定したボタンを押す

### モーター制御

- `set_motor_mode(mode)` - モーターモードを設定する

### 液体操作

- `aspirate(amount, speed)` - 液体を吸引する
- `dispense(amount, speed)` - 液体を吐出する
- `blow_out(go_home, speed, delay_ms)` - 液体をすべて排出する
- `mix(cycles, speed, amount)` - 液体を混合する
- `eject_tip()` - チップを排出する

### ユーティリティ

- `calculate_operation_time(amount, speed)` - ピペット操作にかかる時間を計算する
- `wait_until_END_received()` - ENDメッセージが受信されるまで待機する
- `debug_print(message)` - デバッグメッセージを出力する

## ボタン定数

`Buttons`クラスは以下の定数を提供します：

- `TRIGGER_BUTTON_POWER` - 電源ボタン
- `TRIGGER_BUTTON_LEFT` - 左ボタン
- `TRIGGER_BUTTON_MIDDLE` - 中央ボタン
- `TRIGGER_BUTTON_RIGHT` - 右ボタン
- `TRIGGER_BUTTON_TOP` - 上部トリガーボタン
- `TRIGGER_BUTTON_PRESET` - プリセットボタン
- `TRIGGER_BUTTON_TIPEJECT` - チップ排出ボタン
- `UP` - 上方向ボタン
- `DOWN` - 下方向ボタン

## 注意点

### BluetoothMACアドレスの取得

Picus2のBluetoothMACアドレスを取得するには：

1. ピペットのBluetoothを有効にする
2. コンピュータのBluetoothスキャンを使用して検出する
3. デバイス情報からMACアドレスを確認する（例: "XX:XX:XX:XX:XX:XX"）

### 非同期プログラミング

このモジュールはPython `asyncio`を使用した非同期プログラミングを採用しています：

- 全ての操作メソッドは`async def`で定義されており、`await`キーワードで呼び出す必要があります
- メインスクリプトも非同期関数として定義し、`asyncio.run()`で実行する必要があります

### デバッグモード

デバッグが必要な場合は以下のように設定できます：

```python
picus = Picus2Controller("XX:XX:XX:XX:XX:XX")
picus.DEBUG = True  # デバッグ出力を有効化
```

### オペレーション時間

吸引・吐出などの操作は一定時間を要します。`calculate_operation_time()`メソッドは操作にかかる推定時間を計算し、その時間が経過するまで待機します。速度（1-9）と量（mL）に基づいて計算されます。

### エラー処理

コマンドやボタン/トリガーの送信に失敗した場合（`serial.SerialException`、Bleak の例外、ポートが閉じている場合など）は、`Picus2CommandError`（`ConnectionError` の派生）が送出されます。以前は `button()` がこれらの例外を握りつぶして正常終了していたため、吸引/吐出トリガーの失敗が「完了」と記録され、`LabRobot` の保持量が実際と食い違うことがありました。USB 接続でポート生成後の確認に失敗した場合は、ポートを閉じてから例外を伝播します。`Picus2CommandError` は `src.devices.picus2.picus2_controller` から import してください（`except ConnectionError` でも捕捉できます）。

## 依存関係

- `asyncio` - 非同期処理
- `bleak` - Bluetooth Low Energy通信
- `random` - テスト用のランダム値生成
- `time` - 時間計測

## トラブルシューティング

- 接続エラーが発生した場合、Picus2のバッテリーが十分であることと、Bluetoothが有効になっていることを確認してください。
- 「モーターモードを有効にしてください」というエラーが表示された場合、操作前に`set_motor_mode(True)`を呼び出してください。
- 操作が完了しない場合、操作時間の計算が不正確な可能性があります。実際の操作時間に合わせて待機時間を調整してください。
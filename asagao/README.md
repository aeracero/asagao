# Asagao（Discord目覚ましbot）

ユーザーごとに「どのVCで・どの音声を・何時に・何回」鳴らすかを設定できる目覚ましbotです。
設定はWebダッシュボードの週タイムライン（月〜日）に「箱」を置いて行います。

- 箱の上端 ＝ 鳴り始め（その少し前にbotが自動入室）
- 箱の下端 ＝ 自動退出（どんな状態でも必ず止まる）
- 本人がVCから抜けると止まる（デフォルトON）

## 構成

```
asagao/
├── __main__.py   起動（python -m asagao）
├── bot.py        Discord クライアント（Intents・キャッシュは最小限）
├── commands.py   スラッシュコマンド
├── scheduler.py  次のアラームまで眠るスケジューラ・毎日のメンテナンス
├── runner.py     アラーム1回分の流れ（準備→入室→鳴動→スヌーズ→退出）、停止ボタン
├── voice.py      VC接続の管理（同じVCは相乗り、別VCは順番待ち）
├── audio.py      音声の保存・変換（音量5段階を事前生成）・再生ソース
├── db.py         SQLite
├── models.py     設定項目の定義と入力チェック
├── web.py        ダッシュボード（Discord OAuth2ログイン・API）
└── static/       画面（FullCalendar の週表示）
```

## 1. Discord Developer Portal の設定

1. https://discord.com/developers/applications でアプリを作成します
2. **Bot** タブでトークンを発行します（`DISCORD_TOKEN`）
   - Privileged Gateway Intents はすべてOFFのままで大丈夫です
3. **OAuth2** タブで以下を控えます
   - Client ID（`DISCORD_CLIENT_ID`）
   - Client Secret（`DISCORD_CLIENT_SECRET`）
4. **OAuth2 → Redirects** に以下を登録します
   - `https://（Railwayのドメイン）/callback`
   - ローカル開発用に `http://localhost:8080/callback`
5. botをサーバーに招待します（`CLIENT_ID` を置き換えてください）

   ```
   https://discord.com/oauth2/authorize?client_id=CLIENT_ID&scope=bot+applications.commands&permissions=3148800
   ```

   権限の内訳: チャンネルを見る・メッセージを送る・接続・発言

## 2. Railway へのデプロイ

1. このリポジトリをGitHubに置き、Railwayで「Deploy from GitHub repo」を選びます（`Dockerfile` が自動で使われます）
2. サービスに **Volume** を追加し、マウント先を `/data` にします
   - 変数 `RAILWAY_VOLUME_MOUNT_PATH` が自動で入り、DB・音声・バックアップがすべてここに保存されます
3. **Settings → Networking → Generate Domain** で `xxxx.up.railway.app` のドメインを発行します
4. **Variables** に以下を設定します

   | 変数 | 値 |
   |---|---|
   | `DISCORD_TOKEN` | botのトークン |
   | `DISCORD_CLIENT_ID` | Client ID |
   | `DISCORD_CLIENT_SECRET` | Client Secret |
   | `PUBLIC_URL` | `https://xxxx.up.railway.app`（末尾のスラッシュなし） |

5. Developer Portal の Redirects に `https://xxxx.up.railway.app/callback` が登録されているか確認します

### デプロイの注意
- Volume つきのサービスは再デプロイ中に数十秒止まります。**朝（5:00〜9:00ごろ）のデプロイは避けてください**
- 止まっている間に鳴るはずだったアラームは、起動後5分以内なら鳴らし直します
- Railway のドメインを作り直した場合は、`PUBLIC_URL` と Redirects の両方を更新してください

## 3. ローカルで動かす

```bash
# ffmpeg が必要です（macOS: brew install ffmpeg / Ubuntu: apt install ffmpeg）
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 値を埋める（DEV_GUILD_ID を入れるとコマンドが即反映されます）
python -m asagao
```

http://localhost:8080 を開くとダッシュボードが表示されます。

## コマンド

| コマンド | 内容 |
|---|---|
| `/alarm dashboard` | 設定画面のURLを表示 |
| `/alarm list` | 自分のアラーム一覧と、次に鳴る予定 |
| `/alarm skip` | 次に鳴る予定を1回だけスキップ |
| `/alarm pause` / `resume` | 全アラームの一時停止・再開 |
| `/alarm test` | 今いるVCで自分の音声を1回鳴らす |
| `/stop` | 鳴っているアラームを止める（計算問題モードなら問題が出ます） |

鳴っている間は、VCのチャット（書けない場合はDM）に「止める」「スヌーズ」ボタンが出ます。

## リソースを抑えるための工夫

- **音声は事前変換**: アップロード時に1回だけ、音量正規化＋Opus変換を音量5段階ぶん行います。再生時は変換済みファイルをそのまま送るだけなので、CPUをほぼ使いません
- **変換は1件ずつ**: 同時変換によるCPU・RAMの山を作りません
- **Intents・キャッシュを最小限**: `guilds` と `voice_states` のみ。メッセージキャッシュなし、メンバーはVCにいる人だけキャッシュ
- **スケジューラは眠る**: 定期的な全件チェックではなく、次の準備時刻まで待機します
- **1サービス構成**: botとWebサーバーが同じプロセスで動き、DBはVolume上のSQLiteです（別のDBサービス不要）
- `MALLOC_ARENA_MAX=2` でPythonのメモリ断片化を抑えています

## 上限（`config.py` で変更できます）

- 音声: 1人5個まで、1ファイル5MB・3分まで、アップロードは1時間に10回まで
- アラーム: 1人50個まで
- 毎日15:00に、期限切れセッション・古い履歴・未使用音声の掃除と、DBのバックアップ（7日分）を行います

## 補足

- ダッシュボードに表示されるサーバーは、ログイン時点の情報です。新しくbotを招待したサーバーが出ない場合は、ログインし直してください
- 同じサーバーの他の人のアラームは、名前や設定を伏せて灰色の箱で表示されます（重なりの警告用）
- まだ入っていない機能: 日付指定の例外（祝日だけスキップなど）を置くカレンダー画面、段階的な音量アップ、Discord Activity化

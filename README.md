# もったいないメーター

Claude Code と Codex の残り枠を取り、リセット前に「まだ残ってるよ」と iPhone に知らせる。
Phase 0（Mac 常駐スクリプト + ntfy）。設計書: https://claude.ai/code/artifact/3edf954f-746b-4b9b-b2e5-4e32897b877d

## 前提（最初に知っておくこと）

- 残量が更新されるのは **PC が起きている間だけ**（5 分おき）。スリープ中は最後の値のまま止まる。
- iPhone の Claude アプリで使った分は、次に PC が起きた時に反映される（枠は共通なので数え漏れはない）。
- PC が止まっている間の通知は最後の値を前提に出る。画面の「更新 ○分前」を確認すること。

## 使い方

```
./mottainai.py status        # 残り枠と換算を表示
./mottainai.py install       # ~/.config/mottainai/config.json と launchd（5 分おき）を用意
./mottainai.py test-notify   # 通知経路のテスト
./mottainai.py uninstall     # launchd を外す
```

iPhone 側:

1. App Store の **ntfy** を入れ、`config.json` の `ntfy_topic` を購読する（通知用）
2. Safari で `https://tomokick.github.io/mottainai-meter/#t=<ntfy_topic>-data` を開き、「ホーム画面に追加」（残量画面）

残量画面は `docs/` の静的ページで、Mac が 5 分おきに ntfy のデータ用トピックへ流すスナップショットを読むだけ。サーバーも Apple Developer Program も要らない。

公開（初回のみ）:

```
gh repo create tomokick/mottainai-meter --public --source . --push
gh api -X POST repos/tomokick/mottainai-meter/pages -f build_type=legacy -f 'source[branch]=main' -f 'source[path]=/docs'
```

## しくみ

- Claude: Keychain の `Claude Code-credentials` を読み `api.anthropic.com/api/oauth/usage` を叩く。`limits[]` を窓に変換（session / weekly_all / weekly_scoped）。
- Codex: `~/.codex/auth.json` を読み `chatgpt.com/backend-api/wham/usage` を叩く。窓の種類は `limit_window_seconds` で判別（プランによって 5h 枠が無い）。
- どちらも refresh はしない。期限切れなら CLI を一度起動すると更新される。
- 5 分ごとに `~/.local/share/mottainai/history.jsonl` に追記。判定と通知は `tick` で行い、1 リセット周期 1 回。

## 判定（config.json で変更）

| 窓 | lead_min | floor_percent |
|---|---|---|
| 5h | 45 | 40 |
| weekly | 720 | 25 |

- 時間ベース: リセットまで lead 分以内 かつ 残り floor% 以上
- ペースベース: 直近 120 分の消費ペースで放置すると leftover_floor_percent（30）以上余る
- quiet_hours（23–7 時）は出さない。使用率 0 の窓は対象外（ignore_untouched）

## 換算

- Claude: `~/.claude/projects/**/*.jsonl` の usage から、その窓で使った API 換算 $ と応答回数を集計し、残り % に比例配分
- Codex: `~/.codex/sessions/**/rollout-*.jsonl` の `token_count` に使用率が同梱されているので、トークン増分 ÷ 使用率増分で 1% あたりのトークン数を較正
- `custom_units`: 「中くらいの PR ≈ $3 / 6M トークン」のように 1 単位を定義すると「あと N 回分」が出る

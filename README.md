# シアターキノ 上映時間ビューア（叩き台）

シアターキノ公式サイトの上映時間ページ（ttA.html / ttB.html）を1日1回読み取り、
スマホで見やすいページとして表示します。

```
scraper/scrape.py            サイトを読み取って docs/schedule.json を作る
docs/index.html              schedule.json を表示するページ
.github/workflows/update.yml 毎朝6時(JST)に自動実行
tests/                       動作確認用のサンプルHTML
```

## 手元で試す

```bash
pip install -r requirements.txt

# 本物のサイトから取得
python scraper/scrape.py

# サンプルHTMLで試す（日付を固定）
python scraper/scrape.py --html tests/ttA_sample.html tests/ttB_sample.html --today 2026-09-17

# ページを表示（file:// では JSON を読めないので簡易サーバーで）
cd docs && python -m http.server 8000
# → http://localhost:8000
```

読み取れなかった作品は `[warn]` として表示されます。
内容が前回と同じなら JSON は書き換えません（無駄なコミットを防ぐため）。

## GitHub で公開する

1. このフォルダを GitHub のリポジトリとして push
2. Settings → Pages → Branch を `main`、フォルダを `/docs` に設定
3. Actions タブで「上映時間を更新」を一度手動実行して動作確認

## 読み取りの仕組み

HTMLのタグ構造は作品ごとに違うため、テキストに変換してから
`■` で作品ごとに区切り、日付・時刻・(終〜)・『作品名』を順番に解釈しています。

- 日付の指定がない時刻 → その週の毎日（ただし「※○/○終了」の日まで）
- `9/12(土)・15(火)～18(金) 17:55` → 指定した日だけ
- 時刻が並んだ後に `(終…)` が並ぶ表形式 → 前から順に対応づけ
- `●` `★` `※` `＜` で始まる行 → 注意事項としてそのまま保存
- 見出しの週が過去のもの（更新前の ttB など） → 捨てる

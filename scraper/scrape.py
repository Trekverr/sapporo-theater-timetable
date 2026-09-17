"""シアターキノの上映時間ページ (ttA.html / ttB.html) を読み取って JSON にする。

方針:
  HTMLの構造は作品ごとに書き方がばらばら（表・リスト・特集上映）なので、
  タグ構造には頼らず「テキスト化 → ■で作品ごとに分割 → トークン列を順に解釈」する。

使い方:
  python scraper/scrape.py                    # サイトから取得して docs/schedule.json に保存
  python scraper/scrape.py --html a.html b.html   # 手元のHTMLで試す
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

JST = timezone(timedelta(hours=9))
BASE = "https://www.theaterkino.net/"
PAGES = ["ttA.html", "ttB.html"]
USER_AGENT = "kino-timetable-bot/0.1 (personal use; once a day)"

# 「9/12(土)～9/18(金)」
WEEK_RE = re.compile(r"(\d{1,2})/(\d{1,2})\(.\)\s*[～〜~]\s*(\d{1,2})/(\d{1,2})\(.\)")

# 上映情報の中に出てくるトークン
TOKEN_RE = re.compile(
    r"(?P<end>\([^()]*?\d{1,2}:\d{2}[^()]*\))"          # (終11:04) (上映＆トーク終了19:59/予告なし★)
    r"|(?P<dates>\d{1,2}/\d{1,2}\([^()]\)"               # 9/12(土)
    r"(?:\s*[・～〜]\s*(?:\d{1,2}/)?\d{1,2}\([^()]\))*)"  # ・15(火)～18(金)
    r"|(?P<work>『[^』]+』)"                               # 『火葬人』
    r"|(?P<start>\d{1,2}:\d{2})"                          # 17:55
    r"|(?P<rest>休映)"
)

NOTE_PREFIXES = ("●", "★", "※", "＜", "<")
ENDS_ON_RE = re.compile(r"(\d{1,2})/(\d{1,2})\(.\)\s*終了")


# ---------------------------------------------------------------- 取得

def fetch(url: str) -> str:
    res = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    res.raise_for_status()
    for enc in ("utf-8", "cp932", "euc-jp"):
        try:
            return res.content.decode(enc)
        except UnicodeDecodeError:
            continue
    return res.content.decode("utf-8", errors="replace")


def html_to_lines(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = []
    for raw in text.splitlines():
        line = re.sub(r"[ \t\u3000]+", " ", raw).strip()
        if line:
            lines.append(line)
    return lines


# ---------------------------------------------------------------- 日付

def infer_year(month: int, today: date) -> int:
    if month == 1 and today.month == 12:
        return today.year + 1
    if month == 12 and today.month == 1:
        return today.year - 1
    return today.year


def make_date(month: int, day: int, today: date) -> date:
    return date(infer_year(month, today), month, day)


def expand_dates(spec: str, week_start: date, week_end: date, today: date) -> list[date]:
    """'9/12(土)・15(火)～18(金)' → [9/12, 9/15, 9/16, 9/17, 9/18]"""
    result: list[date] = []
    month = week_start.month
    for part in re.split(r"\s*・\s*", spec):
        ends = re.split(r"\s*[～〜]\s*", part)
        points = []
        for p in ends:
            m = re.match(r"(?:(\d{1,2})/)?(\d{1,2})", p)
            if not m:
                continue
            if m.group(1):
                month = int(m.group(1))
            points.append(make_date(month, int(m.group(2)), today))
        if len(points) == 1:
            result.append(points[0])
        elif len(points) >= 2:
            d = points[0]
            while d <= points[-1]:
                result.append(d)
                d += timedelta(days=1)
    return [d for d in result if week_start <= d <= week_end]


def week_days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


# ---------------------------------------------------------------- 作品ブロック

def split_blocks(lines: list[str]) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in lines:
        if line.startswith("■"):
            current = [line[1:].strip()]
            blocks.append(current)
        elif current is not None:
            current.append(line)
    return blocks


def parse_title(block: list[str]) -> tuple[str, str | None, int]:
    """(タイトル, ＜＞のラベル, 本文の開始行) を返す"""
    first = block[0]
    if first.startswith(("＜", "<")) and len(block) > 1 and block[1].startswith("『"):
        title = block[1].strip("『』 ")
        return title, first.strip("＜＞<> "), 2
    # 「＜8/28(金)公開＞ 『ナイトボーン』」が1行にまとまっている場合
    m = re.match(r"^[＜<](.+?)[＞>]\s*『(.+?)』(.*)$", first)
    if m:
        return (m.group(2) + m.group(3)).strip(), m.group(1), 1
    return first, None, 1


def is_note(line: str) -> bool:
    if line.startswith(NOTE_PREFIXES):
        return True
    # (先着限定/…) のような時刻を含まない補足
    if line.startswith(("(", "（")) and not re.search(r"\d{1,2}:\d{2}", line):
        return True
    return False


def parse_block(block: list[str], week_start: date, week_end: date, today: date) -> dict | None:
    title, label, body_start = parse_title(block)
    body = block[body_start:]

    notes: list[str] = []
    schedule_parts: list[str] = []
    in_notes = False
    for line in body:
        # 一度注記が始まったら、以降は注記として扱う（フッター等の誤読を防ぐ）
        if is_note(line) or in_notes:
            in_notes = True
            notes.append(line)
        else:
            schedule_parts.append(line)

    all_days = week_days(week_start, week_end)
    current_days = all_days
    current_work: str | None = None
    last_kind: str | None = None
    showings: list[dict] = []
    pending: list[dict] = []   # 終了時刻がまだ付いていない回（表形式対策で先入れ先出し）

    for m in TOKEN_RE.finditer(" ".join(schedule_parts)):
        kind = m.lastgroup
        value = m.group(kind)
        if kind == "dates":
            current_days = expand_dates(value, week_start, week_end, today) or all_days
            current_work = None
        elif kind == "work":
            current_work = (current_work or "") + value if last_kind == "work" else value
        elif kind == "start":
            group = {"days": current_days, "start": value, "work": current_work, "end": None, "end_note": None}
            showings.append(group)
            pending.append(group)
        elif kind == "end":
            if pending:
                group = pending.pop(0)
                inner = value.strip("()")
                t = re.search(r"(\d{1,2}:\d{2})", inner)
                group["end"] = t.group(1) if t else None
                rest = re.sub(r"^終?\d{1,2}:\d{2}", "", inner).strip("/ ")
                if "終了" in inner and not inner.startswith("終"):
                    rest = inner  # 「上映＆トーク終了19:59」などは原文を残す
                group["end_note"] = rest or None
        elif kind == "rest":
            pass  # 「休映」は上映回を作らないだけ
        last_kind = kind

    if not showings:
        return None

    ends_on = None
    for n in notes:
        em = ENDS_ON_RE.search(n)
        if em:
            ends_on = make_date(int(em.group(1)), int(em.group(2)), today).isoformat()

    flat = []
    for g in showings:
        for d in g["days"]:
            if ends_on and d.isoformat() > ends_on:
                continue  # 表形式は「毎日」扱いなので、終了日より後は除く
            flat.append({
                "date": d.isoformat(),
                "start": normalize_time(g["start"]),
                "end": normalize_time(g["end"]) if g["end"] else None,
                "end_note": g["end_note"],
                "work": " ＋ ".join(re.findall(r"『(.+?)』", g["work"])) if g["work"] else None,
            })
    flat.sort(key=lambda s: (s["date"], s["start"]))

    return {
        "title": title,
        "label": label,
        "ends_on": ends_on,
        "notes": notes,
        "showings": flat,
    }


def normalize_time(t: str) -> str:
    h, m = t.split(":")
    return f"{int(h):02d}:{m}"


# ---------------------------------------------------------------- ページ単位

def parse_page(html: str, source: str, today: date) -> dict | None:
    lines = html_to_lines(html)
    week = None
    for line in lines:
        m = WEEK_RE.search(line)
        if m:
            week = m
            break
    if not week:
        print(f"[warn] {source}: 週の見出しが見つかりません", file=sys.stderr)
        return None

    m1, d1, m2, d2 = map(int, week.groups())
    start = make_date(m1, d1, today)
    end = make_date(m2, d2, today)
    if end < start:  # 12/27～1/2 のような年またぎ
        end = date(start.year + 1, m2, d2)

    films = []
    for block in split_blocks(lines):
        film = parse_block(block, start, end, today)
        if film:
            films.append(film)
        else:
            print(f"[warn] {source}: 「{block[0]}」の上映時間を読み取れませんでした", file=sys.stderr)

    return {
        "source": source,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "films": films,
    }


def build(pages: list[tuple[str, str]], today: date) -> dict:
    weeks = []
    for source, html in pages:
        week = parse_page(html, source, today)
        if not week:
            continue
        if date.fromisoformat(week["end"]) < today:
            print(f"[info] {source}: {week['start']}～{week['end']} は過去の週なのでスキップ", file=sys.stderr)
            continue
        weeks.append(week)
    weeks.sort(key=lambda w: w["start"])
    return {
        "generated_at": datetime.now(JST).isoformat(timespec="minutes"),
        "official_url": BASE + "tt.html",
        "weeks": weeks,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--html", nargs="*", help="サイトの代わりに読むHTMLファイル")
    parser.add_argument("--today", help="動作確認用に今日の日付を指定 (YYYY-MM-DD)")
    parser.add_argument("--out", default="docs/schedule.json")
    args = parser.parse_args()

    today = date.fromisoformat(args.today) if args.today else datetime.now(JST).date()

    if args.html:
        pages = [(Path(p).name, Path(p).read_text(encoding="utf-8")) for p in args.html]
    else:
        pages = [(name, fetch(BASE + name)) for name in PAGES]

    data = build(pages, today)
    if not data["weeks"]:
        print("[error] 有効な週が1つもありません。前回の JSON を残して終了します。", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        try:
            if json.loads(out.read_text(encoding="utf-8")).get("weeks") == data["weeks"]:
                print("内容に変更がないので保存しません")
                return 0
        except json.JSONDecodeError:
            pass
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(len(f["showings"]) for w in data["weeks"] for f in w["films"])
    print(f"saved {out}: {len(data['weeks'])}週 / {total}回")
    return 0


if __name__ == "__main__":
    sys.exit(main())

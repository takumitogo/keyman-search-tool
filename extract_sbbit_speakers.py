"""
extract_sbbit_speakers.py
============================================================
sbbit.jp（ビジネス+IT）のイベント詳細ページから、登壇者の
会社名・氏名・部署・役職と、開催日・出典URLを抽出する。
社長・代表取締役・取締役は除外する。AIコストなし（正規表現ベース）。

抽出の仕組み:
    「◯◯ 氏」という行を目印（アンカー）にして、その直前にある
    会社名・部署・役職の行を遡って取得する。

    例:
        アステリア株式会社
        マーケティング本部 本部長

        東出 武也 氏
    → 会社名: アステリア株式会社 / 部署役職: マーケティング本部 本部長 / 氏名: 東出 武也

使い方:
    python extract_sbbit_speakers.py --input sbbit_events.csv --output sbbit_speakers.csv \
        --limit 10 --debug-save-html sbbit_debug.html
============================================================
"""

import argparse
import csv
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}
REQUEST_TIMEOUT = 15

EXCLUDE_KEYWORDS = ["代表取締役", "取締役", "社長"]

JSONLD_DATE_RE = re.compile(r'"startDate"\s*:\s*"(\d{4})-(\d{2})-(\d{2})')
JSONLD_NAME_RE = re.compile(r'"@type"\s*:\s*"Event"[^}]*?"name"\s*:\s*"((?:[^"\\]|\\.)*)"')
JSONLD_NAME_RE2 = re.compile(r'"name"\s*:\s*"((?:[^"\\]|\\.)*)"[^}]*?"@type"\s*:\s*"Event"')


def fetch_html(url: str):
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return resp.text


def extract_event_meta(html: str):
    """JSON-LD（schema.org Event）から正式なタイトルと開催日（開始日）を取得する。"""
    title = ""
    date = ""

    m = JSONLD_NAME_RE.search(html) or JSONLD_NAME_RE2.search(html)
    if m:
        raw = m.group(1)
        try:
            title = json.loads(f'"{raw}"')
        except Exception:
            title = raw

    m = JSONLD_DATE_RE.search(html)
    if m:
        y, mo, d = m.groups()
        date = f"{y}-{mo}-{d}"

    return title, date


def extract_speakers(html: str):
    """.person / .person-position / .person-name のCSSクラスを使って登壇者情報を抽出する。"""
    soup = BeautifulSoup(html, "html.parser")

    speakers = []
    seen = set()

    for person in soup.select("li.person, .person"):
        name_el = person.select_one(".person-name")
        if not name_el:
            continue
        name_text = name_el.get_text(strip=True)
        name = re.sub(r"\s*氏$", "", name_text).strip()
        if not name:
            continue

        position_el = person.select_one(".person-position")
        lines = [s.strip() for s in position_el.stripped_strings] if position_el else []
        lines = [ln for ln in lines if ln]

        company = lines[0] if lines else ""
        dept_title = " ".join(lines[1:]) if len(lines) > 1 else ""

        full_text_for_check = company + dept_title
        if any(kw in full_text_for_check for kw in EXCLUDE_KEYWORDS):
            continue

        sig = (company, name, dept_title)
        if sig in seen:
            continue
        seen.add(sig)

        speakers.append({"company": company, "name": name, "dept_title": dept_title})

    return speakers


def process_event(row, debug_save_html=None):
    url = row["url"]
    try:
        html = fetch_html(url)
    except requests.exceptions.RequestException as e:
        return row, [], "", "", f"取得失敗: {e}"

    if debug_save_html:
        with open(debug_save_html, "w", encoding="utf-8") as f:
            f.write(html)

    title, event_date = extract_event_meta(html)
    if not title:
        title = row.get("title", "")
    if not event_date and row.get("date_from_list"):
        # 一覧ページの日付（YYYY/MM/DD形式）をYYYY-MM-DDに変換
        m = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})", row["date_from_list"])
        if m:
            y, mo, d = m.groups()
            event_date = f"{y}-{int(mo):02d}-{int(d):02d}"

    speakers = extract_speakers(html)
    status = "OK" if speakers else "登壇者情報なし"
    return row, speakers, title, event_date, status


def main():
    parser = argparse.ArgumentParser(description="sbbit.jpのイベント詳細ページから登壇者情報を抽出する")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--append", action="store_true")
    parser.add_argument("--debug-save-html", default=None)
    args = parser.parse_args()

    with open(args.input, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    done = set()
    file_exists = False
    if args.append:
        try:
            with open(args.output, encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    done.add(r.get("event_url", ""))
            file_exists = True
            print(f"処理済み(再開分): {len(done):,}件をスキップ")
        except FileNotFoundError:
            pass

    targets = [r for r in rows if r["url"] not in done]
    if args.limit is not None:
        targets = targets[: args.limit]

    print(f"今回処理する件数: {len(targets):,}")
    if not targets:
        print("対象がありません。")
        return

    fieldnames = ["company", "name", "dept_title", "event_title", "event_date", "event_url", "status"]
    file_mode = "a" if args.append else "w"
    write_header = not (args.append and file_exists)

    write_lock = Lock()
    counter_lock = Lock()
    processed = 0
    total_speakers = 0
    start_time = time.time()
    debug_used = False

    with open(args.output, file_mode, newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
            f.flush()

        def process_one(row):
            nonlocal debug_used
            debug_path = None
            with counter_lock:
                if args.debug_save_html and not debug_used:
                    debug_path = args.debug_save_html
                    debug_used = True
            return process_event(row, debug_save_html=debug_path)

        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = [executor.submit(process_one, row) for row in targets]

            for fut in as_completed(futures):
                row, speakers, title, event_date, status = fut.result()

                rows_to_write = []
                if speakers:
                    for sp in speakers:
                        rows_to_write.append({
                            "company": sp["company"], "name": sp["name"], "dept_title": sp["dept_title"],
                            "event_title": title, "event_date": event_date,
                            "event_url": row["url"], "status": status,
                        })
                else:
                    rows_to_write.append({
                        "company": "", "name": "", "dept_title": "",
                        "event_title": title, "event_date": event_date,
                        "event_url": row["url"], "status": status,
                    })

                with write_lock:
                    writer.writerows(rows_to_write)
                    f.flush()

                with counter_lock:
                    processed += 1
                    total_speakers += len(speakers)
                    elapsed = time.time() - start_time
                    rate = processed / elapsed if elapsed > 0 else 0
                    remaining = (len(targets) - processed) / rate if rate > 0 else 0
                    print(f"[{processed:,}/{len(targets):,}] {row.get('title','')[:30]} → {status} ({len(speakers)}名) 累計{total_speakers:,}名, 経過{elapsed:.0f}秒, 残り約{remaining:.0f}秒")

    print(f"完了。累計{total_speakers:,}名の登壇者情報を取得しました。")
    print(f"結果を {args.output} に書き出しました。")


if __name__ == "__main__":
    main()

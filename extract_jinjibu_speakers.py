"""
extract_jinjibu_speakers.py
============================================================
日本の人事部「HRカンファレンス」の各プログラムページから、
講演者の会社名・氏名・部署役職を抽出する。社長・代表取締役・取締役は除外する。
AIコストなし（正規表現ベース）。

抽出の仕組み:
    プログラムページでは、各講演者について
        {氏名}氏  {会社名} {部署} {役職}  {氏名}氏  {ふりがな}／{経歴}...
    という「氏名が2回連続で出てくる」パターンで書かれている。
    この2回目の氏名が出てくる直前までを「会社名・部署・役職」として抽出する。

使い方:
    python extract_jinjibu_speakers.py --input jinjibu_conferences.csv \
        --output jinjibu_speakers.csv --limit 1 --debug-save-html jinjibu_debug.html
============================================================
"""

import argparse
import csv
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
REQUEST_TIMEOUT = 20

EXCLUDE_KEYWORDS = ["代表取締役", "取締役", "社長"]

COMPANY_SUFFIX_RE = re.compile(r"(株式会社|合同会社|合資会社|有限会社|Inc\.?|Ltd\.?|Corporation|LLC)$")


def split_company_and_dept(info: str):
    """
    「ELSA Japan合同会社 代表」のように、法人格（株式会社等）が
    先頭トークンではなく2〜3番目のトークンに含まれるケースにも対応する。
    法人格を含むトークンまでを会社名とし、それ以降を部署・役職とする。
    法人格が見つからない場合（大学名等）は、従来通り先頭トークンのみを会社名とする。
    """
    parts = info.split(" ")
    if not parts:
        return "", ""

    company_end_idx = 0
    for i, p in enumerate(parts[:3]):
        if COMPANY_SUFFIX_RE.search(p):
            company_end_idx = i
            break

    company = " ".join(parts[: company_end_idx + 1])
    dept_title = " ".join(parts[company_end_idx + 1:])
    return company, dept_title


def guess_year_from_url(url: str) -> str:
    """URL中のYYYYMM形式（例: /202511/）から年を推定する。"""
    m = re.search(r"/(\d{4})(\d{2})/", url)
    if m:
        return m.group(1)
    return ""


MMDD_RE = re.compile(r"(\d{1,2})/(\d{1,2})")


def fetch_html(url: str):
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return resp.text


# 旧テンプレート（2025年以前のページ）向け: 「{氏名}氏 {会社/部署/役職} {同じ氏名}氏」パターン
LEGACY_SPEAKER_BLOCK_RE = re.compile(
    r"([^\s　]{1,10}\s[^\s　]{1,10}氏)\s+(.+?)\s+\1\s+"
)


def extract_speakers_legacy(html: str):
    """旧テンプレート向け: 氏名が2回連続で出てくるパターンから抽出する。"""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = soup.get_text(separator="  ")
    text = re.sub(r"[\s　]+", " ", text)

    speakers = []
    seen = set()

    for m in LEGACY_SPEAKER_BLOCK_RE.finditer(text):
        name_raw = m.group(1)
        name = re.sub(r"氏$", "", name_raw).strip()
        info = m.group(2).strip()

        if any(kw in info for kw in EXCLUDE_KEYWORDS):
            continue

        company, dept_title = split_company_and_dept(info)

        sig = (company, name, dept_title)
        if sig in seen:
            continue
        seen.add(sig)

        speakers.append({"company": company, "name": name, "dept_title": dept_title})

    return speakers


def extract_speakers_lecturerbox(html: str, url: str = ""):
    """
    2025年以前の旧テンプレート向け: .lecturerBox コンテナの中に
    .LH120（会社名+部署+役職）→ .font16b.top8（氏名）の順で入っている構造から抽出する。
    開催日は、同じ<tr>内にある.timeセル（例: "11/19(水)"）とURLから推定した年を組み合わせて求める。
    """
    soup = BeautifulSoup(html, "html.parser")
    year = guess_year_from_url(url)

    speakers = []
    seen = {}

    for box in soup.select(".lecturerBox"):
        name_el = box.select_one(".font16b")
        if not name_el:
            continue
        name_text = name_el.get_text(strip=True)
        name = re.sub(r"氏$", "", name_text).strip()
        if not name:
            continue

        pos_el = box.select_one(".LH120")
        info = pos_el.get_text(strip=True) if pos_el else ""

        if any(kw in info for kw in EXCLUDE_KEYWORDS):
            continue

        company, dept_title = split_company_and_dept(info)

        # 同じ<tr>内の.timeセルから開催日（月/日）を取得し、URL由来の年と組み合わせる
        event_date = ""
        row = box.find_parent("tr")
        if row is not None:
            time_el = row.select_one(".time")
            if time_el:
                time_text = time_el.get_text(strip=True)
                dm = MMDD_RE.search(time_text)
                if dm and year:
                    mo, d = dm.groups()
                    event_date = f"{year}-{int(mo):02d}-{int(d):02d}"

        sig = (company, name, dept_title)
        if sig in seen:
            existing = seen[sig]
            if not existing["event_date"] and event_date:
                existing["event_date"] = event_date
            continue
        record = {"company": company, "name": name, "dept_title": dept_title, "event_date": event_date}
        seen[sig] = record
        speakers.append(record)

    return speakers


def extract_speakers(html: str, url: str = ""):
    """
    複数のテンプレートを順番に試す:
    1. 新テンプレート（.detail__name / .detail__pos）2026年〜
    2. 旧テンプレート（.lecturerBox内の.LH120 / .font16b）2025年以前
    3. さらに古い形式向けの正規表現フォールバック
    """
    soup = BeautifulSoup(html, "html.parser")

    speakers = []
    seen = {}

    for name_el in soup.select(".detail__name"):
        name_text = name_el.get_text(strip=True)
        name = re.sub(r"氏$", "", name_text).strip()
        if not name:
            continue

        pos_el = name_el.find_next_sibling(class_="detail__pos")
        if pos_el is None:
            parent = name_el.parent
            pos_el = parent.select_one(".detail__pos") if parent else None
        info = pos_el.get_text(strip=True) if pos_el else ""

        if any(kw in info for kw in EXCLUDE_KEYWORDS):
            continue

        company, dept_title = split_company_and_dept(info)

        # 講演セクション（.program__main__cont__detail）内の.txt--dateから開催日を取得
        event_date = ""
        section = name_el.find_parent("section", class_="program__main__cont__detail")
        if section is not None:
            date_el = section.select_one(".txt--date")
            if date_el:
                date_text = date_el.get_text(strip=True)
                dm = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})", date_text)
                if dm:
                    y, mo, d = dm.groups()
                    event_date = f"{y}-{int(mo):02d}-{int(d):02d}"

        # 重複判定は会社名・氏名・部署役職のみで行う（同一人物が複数箇所に
        # 出現し、片方だけ開催日が取れないケースで別人扱いにならないようにする）
        sig = (company, name, dept_title)
        if sig in seen:
            existing = seen[sig]
            if not existing["event_date"] and event_date:
                existing["event_date"] = event_date  # 日付ありの情報で補完
            continue
        record = {"company": company, "name": name, "dept_title": dept_title, "event_date": event_date}
        seen[sig] = record
        speakers.append(record)

    if speakers:
        return speakers

    speakers = extract_speakers_lecturerbox(html, url=url)
    if speakers:
        return speakers

    # それでも0件だった場合、旧々テンプレート向けの正規表現方式を試す（開催日は取得できない）
    legacy_speakers = extract_speakers_legacy(html)
    for s in legacy_speakers:
        s.setdefault("event_date", "")
    return legacy_speakers


def process_conference(row, debug_save_html=None):
    url = row["program_url"]
    try:
        html = fetch_html(url)
    except requests.exceptions.RequestException as e:
        return row, [], f"取得失敗: {e}"

    if debug_save_html:
        with open(debug_save_html, "w", encoding="utf-8") as f:
            f.write(html)

    speakers = extract_speakers(html, url=url)
    status = "OK" if speakers else "登壇者情報なし"
    return row, speakers, status


def main():
    parser = argparse.ArgumentParser(description="HRカンファレンスのプログラムページから登壇者情報を抽出する")
    parser.add_argument("--input", required=False)
    parser.add_argument("--output", required=False)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--append", action="store_true")
    parser.add_argument("--debug-save-html", default=None)
    parser.add_argument("--debug-url", default=None, help="単一のプログラムURLを直接デバッグする")
    args = parser.parse_args()

    if args.debug_url:
        print(f"デバッグ対象: {args.debug_url}")
        try:
            html = fetch_html(args.debug_url)
        except requests.exceptions.RequestException as e:
            print(f"取得失敗: {e}")
            return
        if args.debug_save_html:
            with open(args.debug_save_html, "w", encoding="utf-8") as f:
                f.write(html)
            print(f"生HTMLを {args.debug_save_html} に保存しました。")
        speakers = extract_speakers(html, url=args.debug_url)
        print(f"抽出件数: {len(speakers)}")
        for s in speakers[:20]:
            print(" ", s)
        return

    if not args.input or not args.output:
        print("エラー: --input と --output は必須です（--debug-url を使う場合を除く）。")
        return

    with open(args.input, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    done = set()
    file_exists = False
    if args.append:
        try:
            with open(args.output, encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    done.add(r.get("conference_url", ""))
            file_exists = True
            print(f"処理済み(再開分): {len(done):,}件をスキップ")
        except FileNotFoundError:
            pass

    targets = [r for r in rows if r["program_url"] not in done]
    if args.limit is not None:
        targets = targets[: args.limit]

    print(f"今回処理する開催回数: {len(targets):,}")
    if not targets:
        print("対象がありません。")
        return

    fieldnames = ["company", "name", "dept_title", "event_date", "conference_label", "conference_url", "status"]
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
            return process_conference(row, debug_save_html=debug_path)

        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = [executor.submit(process_one, row) for row in targets]

            for fut in as_completed(futures):
                row, speakers, status = fut.result()

                rows_to_write = []
                if speakers:
                    for sp in speakers:
                        rows_to_write.append({
                            "company": sp["company"], "name": sp["name"], "dept_title": sp["dept_title"],
                            "event_date": sp.get("event_date", ""),
                            "conference_label": row.get("label", ""), "conference_url": row["program_url"],
                            "status": status,
                        })
                else:
                    rows_to_write.append({
                        "company": "", "name": "", "dept_title": "", "event_date": "",
                        "conference_label": row.get("label", ""), "conference_url": row["program_url"],
                        "status": status,
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
                    print(f"[{processed:,}/{len(targets):,}] {row.get('label','')} → {status} ({len(speakers)}名) 累計{total_speakers:,}名, 経過{elapsed:.0f}秒, 残り約{remaining:.0f}秒")

    print(f"完了。累計{total_speakers:,}名の登壇者情報を取得しました。")
    print(f"結果を {args.output} に書き出しました。")


if __name__ == "__main__":
    main()

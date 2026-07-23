"""
extract_techplay_speakers.py
============================================================
TECH PLAY (techplay.jp) の各イベントページから、「登壇者」セクションの
会社名・氏名・部署役職を抽出する。AIコストなし。

構造（実際のページで確認済み）:
    田村 悠一郎

    株式会社SUBARU
    ADAS開発部 兼 技術研究所
    主査

    2006年入社。...(経歴本文)

氏名の行に続けて、会社名（法人格あり）→部署→役職の順で1〜3行、
その後に経歴本文（句点を含む長文）が続くパターンを利用する。

使い方:
    python extract_techplay_speakers.py --input techplay_events.csv \
        --output techplay_speakers.csv
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

COMPANY_SUFFIX_RE = re.compile(r"(株式会社|合同会社|合資会社|有限会社|Inc\.?|Ltd\.?|Corporation|LLC)")
INSTITUTION_SUFFIX_RE = re.compile(r"(大学院|大学|研究所|研究科|協会|財団|機構|省|庁)")
ENTITY_SUFFIX_RE = re.compile(f"({COMPANY_SUFFIX_RE.pattern}|{INSTITUTION_SUFFIX_RE.pattern})")

EXCLUDE_KEYWORDS = ["代表取締役", "取締役", "社長"]

SECTION_END_MARKERS = [
    "参加対象", "参加費", "参加にあたっての注意事項", "開催グループ",
    "お問い合わせ", "関連するイベント", "タイムスケジュール",
]

NAME_LINE_RE = re.compile(r"^[^\s　]{1,6}[ 　][^\s　]{1,8}$")  # 「姓 名」形式、全体12文字以内
SINGLE_NAME_LINE_RE = re.compile(r"^[^\s　]{2,12}$")  # スペース無しの単一トークン名（外国人名等）


def fetch_html(url: str):
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return resp.text


def extract_event_date(html: str) -> str:
    """ページ上部の「YYYY/MM/DD(曜)」形式から開催日を取得する。"""
    m = re.search(r"(\d{4})/(\d{2})/(\d{2})\(", html)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return ""


def is_new_affiliation_line(line: str) -> bool:
    tokens = re.split(r"[ 　]+", line.strip())
    tokens = [t for t in tokens if t]
    if not tokens:
        return False
    return bool(ENTITY_SUFFIX_RE.search(tokens[0]))


def split_company_and_dept(info: str):
    parts = re.split(r"[ 　]+", info.strip())
    parts = [p for p in parts if p]
    if not parts:
        return "", ""
    company_end_idx = 0
    for i, p in enumerate(parts[:4]):
        if ENTITY_SUFFIX_RE.search(p):
            company_end_idx = i
            break
    company = " ".join(parts[: company_end_idx + 1])
    dept_title = " ".join(parts[company_end_idx + 1:])
    return company, dept_title


def extract_speakers_section(html: str):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]

    try:
        start_idx = lines.index("登壇者") + 1
    except ValueError:
        return []

    end_idx = len(lines)
    for marker in SECTION_END_MARKERS:
        for i in range(start_idx, len(lines)):
            if lines[i] == marker:
                end_idx = min(end_idx, i)
                break

    section_lines = lines[start_idx:end_idx]

    speakers = []
    i = 0
    while i < len(section_lines):
        line = section_lines[i]

        is_name = bool(NAME_LINE_RE.match(line)) or bool(SINGLE_NAME_LINE_RE.match(line))
        if not is_name or ENTITY_SUFFIX_RE.search(line) or "。" in line:
            i += 1
            continue

        # 次の非空行が法人格・機関名を含む会社名らしき行であることを確認
        if i + 1 >= len(section_lines) or not ENTITY_SUFFIX_RE.search(section_lines[i + 1]):
            i += 1
            continue

        name = line
        info_lines = []
        j = i + 1
        while j < len(section_lines) and len(info_lines) < 3:
            nxt = section_lines[j]
            if "。" in nxt or nxt.endswith("."):
                break  # 経歴本文に到達
            if len(nxt) > 100:
                break
            if NAME_LINE_RE.match(nxt) and j > i + 1 and ENTITY_SUFFIX_RE.search(
                section_lines[j + 1] if j + 1 < len(section_lines) else ""
            ):
                break  # 次の登壇者に到達
            info_lines.append(nxt)
            j += 1

        if not info_lines:
            i += 1
            continue

        groups = []
        current = []
        for idx, ln in enumerate(info_lines):
            if idx == 0 or is_new_affiliation_line(ln):
                if current:
                    groups.append(current)
                current = [ln]
            else:
                current.append(ln)
        if current:
            groups.append(current)

        parsed_pairs = [split_company_and_dept(" ".join(g)) for g in groups]
        company = parsed_pairs[0][0] if parsed_pairs else ""
        if not parsed_pairs:
            dept_title = ""
        elif len(parsed_pairs) == 1:
            dept_title = parsed_pairs[0][1]
        else:
            dept_parts = [parsed_pairs[0][1]] + [f"{c} {d}".strip() for c, d in parsed_pairs[1:]]
            dept_title = " / ".join(p for p in dept_parts if p)

        full_info = " / ".join(info_lines)
        if not any(kw in full_info for kw in EXCLUDE_KEYWORDS):
            speakers.append({"company": company, "name": name, "dept_title": dept_title})

        i = j

    return speakers


def process_event(row):
    event_url = row["event_url"]
    try:
        html = fetch_html(event_url)
    except requests.exceptions.RequestException as e:
        return row, [], f"取得失敗: {e}"

    event_date = extract_event_date(html)
    speakers = extract_speakers_section(html)
    for sp in speakers:
        sp["event_date"] = event_date
        sp["event_url"] = event_url

    status = "OK" if speakers else "登壇者情報なし"
    return row, speakers, status


def main():
    parser = argparse.ArgumentParser(description="TECH PLAYの登壇者情報を抽出する")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    with open(args.input, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if args.limit is not None:
        rows = rows[: args.limit]

    print(f"対象イベント数: {len(rows):,}")

    fieldnames = ["company", "name", "dept_title", "event_date", "event_url", "status"]
    write_lock = Lock()
    counter_lock = Lock()
    processed = 0
    total_speakers = 0
    start_time = time.time()

    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        f.flush()

        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = [executor.submit(process_event, row) for row in rows]
            for fut in as_completed(futures):
                row, speakers, status = fut.result()
                rows_to_write = []
                if speakers:
                    for sp in speakers:
                        rows_to_write.append({
                            "company": sp["company"], "name": sp["name"], "dept_title": sp["dept_title"],
                            "event_date": sp.get("event_date", ""), "event_url": sp.get("event_url", ""),
                            "status": status,
                        })
                else:
                    rows_to_write.append({
                        "company": "", "name": "", "dept_title": "", "event_date": "",
                        "event_url": row["event_url"], "status": status,
                    })
                with write_lock:
                    writer.writerows(rows_to_write)
                    f.flush()
                with counter_lock:
                    processed += 1
                    total_speakers += len(speakers)
                    elapsed = time.time() - start_time
                    rate = processed / elapsed if elapsed > 0 else 0
                    remaining = (len(rows) - processed) / rate if rate > 0 else 0
                    print(f"[{processed:,}/{len(rows):,}] {status} ({len(speakers)}名) "
                          f"累計{total_speakers:,}名, 経過{elapsed:.0f}秒, 残り約{remaining:.0f}秒")
                time.sleep(0.2)

    print(f"\n完了。累計{total_speakers:,}名の登壇者情報を取得しました。")
    print(f"結果を {args.output} に書き出しました。")


if __name__ == "__main__":
    main()

"""
extract_shoeisha_speakers.py
============================================================
MarkeZine Day の各イベントページから、スピーカー一覧のセッションURLを集め、
各セッションページから登壇者の会社名・氏名・部署役職を抽出する。
社長・代表取締役・取締役は除外する。AIコストなし。

開催日は各ページのmetaタグ（cxenseparse:sho-startdate-y/m/d）から取得する
（確実で正確な情報源）。

使い方:
    python extract_shoeisha_speakers.py --input shoeisha_events.csv \
        --output shoeisha_speakers.csv --limit 1 --debug-save-html shoeisha_debug.html
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

DATE_META_RE = re.compile(
    r'sho-startdate-y["\']?\s*content=["\'](\d{4})["\'].*?'
    r'sho-startdate-m["\']?\s*content=["\'](\d{6})["\'].*?'
    r'sho-startdate-d["\']?\s*content=["\'](\d{8})["\']',
    re.DOTALL,
)


def split_company_and_dept(info: str):
    parts = re.split(r"[ 　]+", info.strip())
    parts = [p for p in parts if p]
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


INSTITUTION_SUFFIX_RE = re.compile(r"(大学院|大学|研究所|研究科|協会|財団|機構|省|庁)$")


def is_new_affiliation_line(line: str) -> bool:
    """
    この行が「新しい所属（会社・組織）の開始」かどうかを判定する。
    先頭のトークンが法人格（株式会社等）や大学・研究所等の機関名で終わっていれば
    新しい所属とみなす。それ以外（部署名・役職名のみ）は前の行の続きとみなす。
    """
    tokens = re.split(r"[ 　]+", line.strip())
    tokens = [t for t in tokens if t]
    if not tokens:
        return False
    first_tok = tokens[0]
    return bool(COMPANY_SUFFIX_RE.search(first_tok) or INSTITUTION_SUFFIX_RE.search(first_tok))


def fetch_html(url: str):
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return resp.text


def extract_event_date(html: str) -> str:
    """meta property="event:start_time" から開催日を取得する（全ページに共通して存在する）。"""
    m = re.search(r'property=["\']event:start_time["\']\s+content=["\'](\d{4})-(\d{2})-(\d{2})', html)
    if m:
        y, mo, d = m.groups()
        return f"{y}-{mo}-{d}"
    # フォールバック: sho-startdate-d（存在するページもある）
    m = re.search(r'name=["\']cxenseparse:sho-startdate-d["\']\s+content=["\'](\d{8})["\']', html)
    if m:
        d = m.group(1)
        return f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
    return ""


def normalize_session_url(href: str) -> str:
    """
    "https://event.shoeisha.jp/online/count/1582/?ident=s_b7&url=https://event.shoeisha.jp/soday/.../session/5263"
    のようなクリック計測用のラッパーURLから、実際のセッションURL（?url=の中身）を取り出す。
    ラッパーでなければそのまま返す。
    """
    if "/online/count/" in href and "url=" in href:
        real = href.split("url=", 1)[1]
        real = real.split("&", 1)[0]  # 後続の他パラメータを除去
        return real
    return href


def get_session_urls(event_html: str, event_url: str):
    """イベントページの「Speakers」欄からセッションURLを重複なく集める。"""
    soup = BeautifulSoup(event_html, "html.parser")
    urls = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        href = normalize_session_url(href)
        if "/session/" not in href:
            continue
        full = href if href.startswith("http") else "https://event.shoeisha.jp" + href
        full = full.rstrip("/")  # 末尾スラッシュの有無による重複を防ぐ
        if full in seen:
            continue
        seen.add(full)
        urls.append(full)
    return urls


def extract_speakers_from_session(html: str):
    """
    セッションページから登壇者情報を抽出する。
    「{氏名} [{会社名等}]」という行を目印に、直後の「会社名+役職」行
    （複数の場合あり、経歴本文が始まる前まで）を取得する。
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]

    NAME_HEADER_RE = re.compile(r"^(.{2,12})\s*[\[［](.+)[\]］]$")

    speakers = []
    for i, line in enumerate(lines):
        m = NAME_HEADER_RE.match(line)
        if not m:
            continue
        name = m.group(1).strip()
        name = re.sub(r"^(モデレーター|ファシリテーター|司会)[：:]\s*", "", name)

        # 直後の数行から「会社名+役職」を集める（数字だけの行=登壇回数バッジは無視、
        # 長い文章（経歴本文）に到達したら打ち切り）
        info_lines = []
        for j in range(i + 1, min(i + 6, len(lines))):
            nxt = lines[j]
            if re.fullmatch(r"\d+", nxt) or nxt in ("初", "初登壇", "プロフィール", "Profile", "PROFILE"):
                continue  # 登壇回数バッジ・プロフィール見出し等のラベル行
            if nxt.startswith("モデレーター") or nxt.startswith("ファシリテーター"):
                continue  # 進行役の肩書きラベル（氏名側に混入することがあるため無視）
            if "。" in nxt or nxt.endswith("."):
                break  # 経歴本文に到達（句点を含む、または英文ピリオドで終わる）
            if len(nxt) > 100:
                break  # 異常に長い行は経歴本文の可能性が高いため安全弁として打ち切る
            if NAME_HEADER_RE.match(nxt):
                break  # 次の登壇者に到達
            if re.match(r"^【.+】$", nxt):
                break  # 次のセッション見出し等（【...】形式）に到達
            info_lines.append(nxt)
            if len(info_lines) >= 3:
                break

        if not info_lines:
            continue

        full_info = " / ".join(info_lines)
        if any(kw in full_info for kw in EXCLUDE_KEYWORDS):
            continue

        # 複数行を「新しい所属の開始」か「前の行（会社名）の続き（部署役職）」かで
        # グループ化する。「株式会社」等の法人格や大学等の機関名で終わる行、
        # または最初の行は「新しい所属」とみなし、それ以外は直前の所属の続きとする。
        groups = []
        current = []
        first_line_looks_truncated = (
            len(info_lines) >= 2
            and not COMPANY_SUFFIX_RE.search(info_lines[0])
            and not INSTITUTION_SUFFIX_RE.search(info_lines[0])
            and len(info_lines[0]) <= 10
            and " " not in info_lines[0]
            and "\u3000" not in info_lines[0]
        )
        for idx, ln in enumerate(info_lines):
            if idx == 0:
                current = [ln]
            elif idx == 1 and first_line_looks_truncated:
                # 1行目が法人格の付かない短い断片（改行で会社名が分断された疑い）
                # →新しい所属とはみなさず1行目と結合する
                current.append(ln)
            elif is_new_affiliation_line(ln):
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

        if "翔泳社" in company:
            continue  # 主催社(翔泳社)のスタッフ・司会は営業リード対象ではないため除外

        speakers.append({"company": company, "name": name, "dept_title": dept_title})

    return speakers


def process_event(row, debug_save_html=None):
    event_url = row["event_url"]
    try:
        event_html = fetch_html(event_url)
    except requests.exceptions.RequestException as e:
        return row, [], f"イベントページ取得失敗: {e}"

    if debug_save_html:
        with open(debug_save_html, "w", encoding="utf-8") as f:
            f.write(event_html)

    event_date = extract_event_date(event_html)
    session_urls = get_session_urls(event_html, event_url)

    if not session_urls:
        # bizzday/axday等、タイムテーブルが/timetableサブページにあるイベントへの対応
        timetable_url = event_url.rstrip("/") + "/timetable"
        try:
            timetable_html = fetch_html(timetable_url)
            session_urls = get_session_urls(timetable_html, timetable_url)
            if not event_date:
                event_date = extract_event_date(timetable_html)
        except requests.exceptions.RequestException:
            pass

    all_speakers = []
    for su in session_urls:
        try:
            shtml = fetch_html(su)
        except requests.exceptions.RequestException:
            continue
        speakers = extract_speakers_from_session(shtml)
        for sp in speakers:
            sp["event_date"] = event_date
            sp["session_url"] = su
        all_speakers.extend(speakers)
        time.sleep(0.3)

    status = "OK" if all_speakers else "登壇者情報なし"
    return row, all_speakers, status


def main():
    parser = argparse.ArgumentParser(description="MarkeZine Dayのセッションページから登壇者情報を抽出する")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--concurrency", type=int, default=5)
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

    targets = [r for r in rows if r["event_url"] not in done]
    if args.limit is not None:
        targets = targets[: args.limit]

    print(f"今回処理するイベント数: {len(targets):,}")
    if not targets:
        print("対象がありません。")
        return

    fieldnames = ["company", "name", "dept_title", "event_date", "event_label", "event_url", "session_url", "status"]
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
                row, speakers, status = fut.result()

                rows_to_write = []
                if speakers:
                    for sp in speakers:
                        rows_to_write.append({
                            "company": sp["company"], "name": sp["name"], "dept_title": sp["dept_title"],
                            "event_date": sp.get("event_date", ""), "event_label": row.get("label", ""),
                            "event_url": row["event_url"], "session_url": sp.get("session_url", ""),
                            "status": status,
                        })
                else:
                    rows_to_write.append({
                        "company": "", "name": "", "dept_title": "", "event_date": "",
                        "event_label": row.get("label", ""), "event_url": row["event_url"],
                        "session_url": "", "status": status,
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

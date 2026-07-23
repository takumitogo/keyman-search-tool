"""
dedup_prtimes_against_db.py
============================================================
collect_prtimes_keyman_batch.py で作った新規キーマンCSVを、既存の
keyman_data.json（またはCSV）と「会社名＋担当者名」で突き合わせ、
既存DBに無い新規人物だけを抽出する。

会社名・氏名は全角半角・スペースを正規化した完全一致で判定する
（過去の翔泳社セミナー登壇者リストと同じ方針）。

使い方:
    python dedup_prtimes_against_db.py --new prtimes_keyman.csv \
        --existing keyman_data.json --output prtimes_keyman_new_only.csv
============================================================
"""

import argparse
import csv
import json
import unicodedata


def normalize(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)  # 全角/半角統一
    s = "".join(s.split())  # 空白除去
    return s.strip()


def load_existing_keys(existing_path: str):
    """既存DB（JSONまたはCSV）から (会社名, 氏名) の正規化済みキー集合を作る。"""
    keys = set()
    if existing_path.endswith(".json"):
        with open(existing_path, encoding="utf-8") as f:
            data = json.load(f)
        for item in data:
            company = item.get("company", "")
            name = item.get("name", "")
            keys.add((normalize(company), normalize(name)))
    else:
        with open(existing_path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                company = row.get("company", "")
                name = row.get("name", "")
                keys.add((normalize(company), normalize(name)))
    return keys


def main():
    parser = argparse.ArgumentParser(description="PR TIMES新規キーマンを既存DBと突き合わせて重複判定する")
    parser.add_argument("--new", required=True)
    parser.add_argument("--existing", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    existing_keys = load_existing_keys(args.existing)
    print(f"既存DBの人数: {len(existing_keys):,}")

    with open(args.new, encoding="utf-8-sig") as f:
        new_rows = list(csv.DictReader(f))
        fieldnames = list(new_rows[0].keys()) + ["dup_check"]

    new_only = []
    already_exists = 0
    for row in new_rows:
        key = (normalize(row.get("company", "")), normalize(row.get("name", "")))
        if key in existing_keys:
            already_exists += 1
            row["dup_check"] = "既存DBに存在"
        else:
            row["dup_check"] = "新規"
            new_only.append(row)

    print(f"新規取得件数: {len(new_rows):,}")
    print(f"既存DBと重複: {already_exists:,}")
    print(f"新規人物: {len(new_only):,}")

    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(new_only)

    print(f"新規人物のみを {args.output} に書き出しました。")


if __name__ == "__main__":
    main()

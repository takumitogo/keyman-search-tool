"""
dedup_jinjibu_by_name.py
============================================================
jinjibu_speakers_all.csv（全開催回をまとめた登壇者データ）から、
同じ氏名が複数回登場する場合に、最新の開催日（event_date）のものだけを残す。

使い方:
    python dedup_jinjibu_by_name.py --input jinjibu_speakers_all5.csv --output jinjibu_speakers_dedup.csv
============================================================
"""

import argparse
import csv


def main():
    parser = argparse.ArgumentParser(description="氏名の重複を、最新の開催日のものだけ残すよう除去する")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    with open(args.input, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    # 氏名が空の行（登壇者情報なし等）はそのまま残す
    named_rows = [r for r in rows if r.get("name", "").strip()]
    unnamed_rows = [r for r in rows if not r.get("name", "").strip()]

    print(f"入力行数: {len(rows):,}（うち氏名あり: {len(named_rows):,}）")

    best_by_name = {}
    for row in named_rows:
        name = row["name"].strip()
        date = row.get("event_date", "") or ""

        if name not in best_by_name:
            best_by_name[name] = row
            continue

        current_date = best_by_name[name].get("event_date", "") or ""
        # 日付は YYYY-MM-DD 形式なので文字列比較でそのまま新しい順に判定できる
        if date > current_date:
            best_by_name[name] = row

    deduped_named = list(best_by_name.values())
    result = deduped_named + unnamed_rows

    removed = len(named_rows) - len(deduped_named)
    print(f"除去した重複行数: {removed:,}")
    print(f"出力行数: {len(result):,}")

    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(result)

    print(f"完了。{args.output} に書き出しました。")


if __name__ == "__main__":
    main()

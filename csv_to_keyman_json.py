"""
csv_to_keyman_json.py
============================================================
keyman_candidates_final.csv（ステップ④の最終成果物）を、
match-list.html が読み込む keyman_data.json に変換する。

使い方:
    python csv_to_keyman_json.py --input keyman_candidates_final.csv --output keyman_data.json
============================================================
"""
import argparse, csv, json

parser = argparse.ArgumentParser()
parser.add_argument("--input", required=True)
parser.add_argument("--output", default="keyman_data.json")
args = parser.parse_args()

with open(args.input, encoding="utf-8-sig") as f:
    rows = list(csv.DictReader(f))

# 氏名が空（＝候補なし）の行は除外
rows = [r for r in rows if r.get("name")]

data = []
for r in rows:
    data.append({
        "company": r.get("featured_company_name", ""),
        "name": r.get("name", ""),
        "dept": r.get("department_matched", ""),
        "title": r.get("title", ""),
        "product": r.get("matched_product_name", ""),
        "url": r.get("article_url", ""),
        "date": r.get("published_date", ""),
    })

print(f"件数: {len(data)}")
with open(args.output, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
print(f"書き出し完了: {args.output}")

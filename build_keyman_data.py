"""
build_keyman_data.py
============================================================
match-list.html（営業リスト照合ツール）用のデータファイルを作る。

以下2つのデータソースを統合する:
  1. public_reviews_all_with_category.csv （ITreview公開口コミ。今すぐ使える）
     → 役職・出典記事URLに相当する情報が無いため空欄になる
  2. keyman_candidates_final.csv （事例インタビュー記事から抽出。④完了後に使える）
     → 役職・出典記事URLも含めて全項目揃う

両方存在すれば統合し、片方しか無くても動く（無い方はスキップ）。

使い方:
    python build_keyman_data.py \
        --reviews public_reviews_all_with_category.csv \
        --keyman keyman_candidates_final.csv \
        --output keyman_data.json

--keyman は省略可（まだ④が終わっていない場合）。
============================================================
"""
import argparse, csv, json, os

parser = argparse.ArgumentParser()
parser.add_argument("--reviews", default=None, help="public_reviews_all_with_category.csv")
parser.add_argument("--keyman", default=None, help="keyman_candidates_final.csv（省略可）")
parser.add_argument("--output", default="keyman_data.json")
args = parser.parse_args()

data = []

if args.reviews and os.path.exists(args.reviews):
    with open(args.reviews, encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if r.get("reviewer_name") or r.get("company_name")]
    for r in rows:
        data.append({
            "company": r.get("company_name", ""),
            "name": r.get("reviewer_name", ""),
            "dept": r.get("department_genre", ""),
            "title": "",  # 口コミデータには役職情報が無い
            "product": r.get("_source_product_name", ""),
            "url": "",    # 口コミデータには出典記事URLが無い
            "date": r.get("posting_date", ""),
            "industry": r.get("industry", ""),
            "size": r.get("employee_size", ""),
            "position": r.get("position", ""),
            "large": r.get("large_category", ""),
            "medium": r.get("medium_category", ""),
            "source": "ITreview口コミ",
        })
    print(f"口コミデータ: {len(rows)}件を追加")
else:
    print("口コミデータ: スキップ（ファイルが指定されていないか見つかりません）")

if args.keyman and os.path.exists(args.keyman):
    with open(args.keyman, encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if r.get("name")]
    for r in rows:
        data.append({
            "company": r.get("featured_company_name", ""),
            "name": r.get("name", ""),
            "dept": r.get("department_matched", ""),
            "title": r.get("title", ""),
            "product": r.get("matched_product_name", ""),
            "url": r.get("article_url", ""),
            "date": r.get("published_date", ""),
            "industry": "",   # 事例インタビューデータには業種情報が無い
            "size": "",       # 従業員規模も無い
            "position": "",
            "large": r.get("large_category", ""),
            "medium": r.get("medium_category", ""),
            "source": "事例インタビュー記事",
        })
    print(f"事例インタビューデータ: {len(rows)}件を追加")
else:
    print("事例インタビューデータ: スキップ（④完了後に --keyman オプションで追加してください）")

print(f"合計: {len(data)}件")
with open(args.output, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
print(f"書き出し完了: {args.output}")

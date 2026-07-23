"""
extract_keyman_from_articles.py
============================================================
フェーズ2: crawl_case_study_articles.py の出力（case_study_articles_all.csv）を読み込み、
各記事の本文を取得してClaude API (Haiku) に投げ、担当者名・部署・役職を抽出する。

重要: CSVの company_name は「事例ページを自社サイトに掲載しているベンダー企業」であり、
実際にインタビューされている顧客企業とは別会社です。抽出対象は顧客企業側の担当者であり、
ベンダー企業（company_name）自身の社員（インタビュアー等）は除外します。
顧客企業名は記事本文から特定し、featured_company_name列に格納します。

列名の互換性: 入力CSVに company_name / corporate_number が無い場合、
collect_case_study_articles.py の出力形式（site_name / hub_url）を自動的に使う。

役職の有無・レベルは問わない。代表取締役・社長・CEO・取締役等の経営層のみ、
営業リサーチの観点で使えないため除外する（除外指示 + 事後フィルタの二重チェック）。

入力（フェーズ1の出力）:
    corporate_number, company_name, case_study_index_url, article_url, published_date

出力（1候補=1行の正規化フォーマット。1記事で複数人ヒットすれば複数行になる）:
    corporate_number, company_name, article_url, published_date,
    featured_company_name, department_matched, name, title, contact, verified, status, extracted_at

使い方:
    export ANTHROPIC_API_KEY=sk-ant-xxxxx
    python extract_keyman_from_articles.py --input case_study_articles_all.csv --output keyman_candidates.csv

途中再開:
    --append          … 既存の出力ファイルにある article_url をスキップして再開（新しいinputを渡してもOK）
    --limit N         … テスト用に処理件数を制限

既存スクリプト群と同じ設計方針:
    - 1.5秒のウェイトでAPIレート制限を回避
    - --append による article_url ベースの再開対応（入力ファイルが差し替わっても重複処理しない）
============================================================
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"  # 2026年7月時点の最新Haiku。要更新時はここを変更

# 2026年7月時点のHaiku 4.5料金（要:料金改定時はここを更新）
# 参照: https://platform.claude.com/docs/en/about-claude/pricing
PRICE_PER_MILLION_INPUT_USD = 1.0
PRICE_PER_MILLION_OUTPUT_USD = 5.0

HEADERS_BROWSER = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

REQUEST_TIMEOUT = 15
WAIT_SECONDS = 1.5

# 除外する役職（経営層。営業リサーチの観点で使えないため除外）
# 「代表取締役」だけでなく、中小店舗等でよくある「代表」単体の表記も拾えるようにしている
EXCLUDE_TITLE_KEYWORDS = [
    "代表取締役", "代表", "取締役", "社長", "会長",
    "CEO", "COO", "CTO", "CFO", "執行役員", "監査役", "オーナー", "店主",
]


META_DATE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:article:published_time|article:modified_time|'
    r'og:article:published_time|datePublished|publish[-_]?date)["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
TIME_TAG_RE = re.compile(r'<time[^>]+datetime=["\']([^"\']+)["\']', re.IGNORECASE)
JP_DATE_RE = re.compile(r"(20\d{2})[年/\-.](\d{1,2})[月/\-.](\d{1,2})[日]?")


def extract_date_from_html(raw_html: str) -> str:
    """生HTMLからmetaタグ・timeタグ・本文中の日付表記を優先順に探す（手段①、確定レベル）。"""
    m = META_DATE_RE.search(raw_html)
    if m:
        return m.group(1).strip()[:10]
    m = TIME_TAG_RE.search(raw_html)
    if m:
        return m.group(1).strip()[:10]
    m = JP_DATE_RE.search(raw_html)
    if m:
        y, mo, d = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    return ""


# --- 日付推定の追加手段（②〜④⑥⑦）。手段①（メタタグ）で見つからなかった場合のみ使う ---

PATH_DATE_RE = re.compile(r"/(20\d{2})[/\-](\d{1,2})(?:[/\-](\d{1,2}))?/")
IMG_SRC_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)


def _normalize_date_str(raw: str) -> str:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        return m.group(0)
    m = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})", raw)
    if m:
        y, mo, d = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    m = re.match(r"\w+,\s*(\d{1,2})\s+(\w+)\s+(\d{4})", raw)
    if m:
        d, mon_name, y = m.groups()
        months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        try:
            mo = months.index(mon_name[:3]) + 1
            return f"{y}-{mo:02d}-{int(d):02d}"
        except ValueError:
            return ""
    return ""


def _safe_fetch(url: str, timeout: int = 10):
    try:
        resp = requests.get(url, headers=HEADERS_BROWSER, timeout=timeout)
        if resp.status_code >= 400:
            return None
        resp.encoding = resp.apparent_encoding
        return resp.text
    except Exception:
        return None


def _try_wp_json(article_url: str):
    """手段②: WordPressのwp-json REST APIから投稿日（date、modifiedではない）を取得。確定レベル。"""
    parsed = urlparse(article_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    slug = [s for s in parsed.path.split("/") if s][-1] if parsed.path.strip("/") else ""
    if not slug:
        return None

    for endpoint in ("wp/v2/posts", "wp/v2/pages"):
        text = _safe_fetch(f"{origin}/wp-json/{endpoint}?slug={slug}&_fields=date")
        if not text:
            continue
        try:
            data = json.loads(text)
            if isinstance(data, list) and data:
                date = _normalize_date_str(data[0].get("date", ""))
                if date:
                    return date, "確定", "wp-json REST API"
        except Exception:
            continue
    return None


def _try_rss(article_url: str):
    """手段③: RSSフィードのpubDateを取得。確定レベル。"""
    parsed = urlparse(article_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    for feed_path in ("/feed/", "/rss/", "/feed", "/rss.xml", "/atom.xml"):
        text = _safe_fetch(urljoin(origin, feed_path))
        if not text:
            continue
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            continue

        for item in root.iter():
            tag = item.tag.lower().split("}")[-1]
            if tag not in ("item", "entry"):
                continue
            link_el, date_el = None, None
            for child in item:
                child_tag = child.tag.lower().split("}")[-1]
                if child_tag == "link":
                    link_el = child
                if child_tag in ("pubdate", "published", "updated"):
                    date_el = child
            link_text = (link_el.text or link_el.get("href", "")) if link_el is not None else ""
            if link_text and article_url.rstrip("/") == link_text.strip().rstrip("/"):
                if date_el is not None and date_el.text:
                    date = _normalize_date_str(date_el.text.strip())
                    if date:
                        return date, "確定", "RSS pubDate"
    return None


def _try_sitemap(article_url: str):
    """手段④: サイトマップのlastmodを取得。更新日の可能性があるため推定・低とする。"""
    parsed = urlparse(article_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    for sitemap_path in ("/sitemap.xml", "/sitemap_index.xml"):
        text = _safe_fetch(urljoin(origin, sitemap_path))
        if not text:
            continue
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            continue

        sub_sitemaps = []
        for url_el in root.iter():
            tag = url_el.tag.lower().split("}")[-1]
            if tag not in ("sitemap", "url"):
                continue
            loc, lastmod = None, None
            for child in url_el:
                child_tag = child.tag.lower().split("}")[-1]
                if child_tag == "loc":
                    loc = child.text
                if child_tag == "lastmod":
                    lastmod = child.text
            if tag == "sitemap" and loc:
                sub_sitemaps.append(loc)
            if tag == "url" and loc and article_url.rstrip("/") == loc.strip().rstrip("/") and lastmod:
                date = _normalize_date_str(lastmod.strip())
                if date:
                    return date, "推定・低", "サイトマップlastmod（更新日の可能性あり）"

        for sub_url in sub_sitemaps[:5]:
            sub_text = _safe_fetch(sub_url)
            if not sub_text:
                continue
            try:
                sub_root = ET.fromstring(sub_text)
            except ET.ParseError:
                continue
            for url_el in sub_root.iter():
                tag = url_el.tag.lower().split("}")[-1]
                if tag != "url":
                    continue
                loc, lastmod = None, None
                for child in url_el:
                    child_tag = child.tag.lower().split("}")[-1]
                    if child_tag == "loc":
                        loc = child.text
                    if child_tag == "lastmod":
                        lastmod = child.text
                if loc and article_url.rstrip("/") == loc.strip().rstrip("/") and lastmod:
                    date = _normalize_date_str(lastmod.strip())
                    if date:
                        return date, "推定・低", "サイトマップlastmod（更新日の可能性あり）"
    return None


def _try_wayback(article_url: str):
    """手段⑥: Wayback Machineの最古保存日。記事が存在した上限を示すのみなので推定・高とする。"""
    text = _safe_fetch(
        f"https://web.archive.org/cdx/search/cdx?url={article_url}&output=json&fl=timestamp&limit=1&sort=ascending",
        timeout=15,
    )
    if not text:
        return None
    try:
        data = json.loads(text)
        if len(data) >= 2:
            timestamp = data[1][0]
            return f"{timestamp[0:4]}-{timestamp[4:6]}-{timestamp[6:8]}", "推定・高", "Wayback Machine最古保存日"
    except Exception:
        pass
    return None


def _try_path_date(article_url: str, raw_html: str):
    """手段⑦: URL・画像パス内の年月から推定。最も弱いシグナルなので推定・低。"""
    m = PATH_DATE_RE.search(article_url)
    if m:
        y, mo, d = m.group(1), m.group(2), m.group(3) or "01"
        return f"{y}-{int(mo):02d}-{int(d):02d}", "推定・低", "URLパス内の年月"

    for img_src in IMG_SRC_RE.findall(raw_html)[:30]:
        m = PATH_DATE_RE.search(img_src)
        if m:
            y, mo, d = m.group(1), m.group(2), m.group(3) or "01"
            return f"{y}-{int(mo):02d}-{int(d):02d}", "推定・低", "画像パス内の年月"
    return None


def resolve_date_extended(article_url: str, raw_html: str):
    """
    手段①（メタタグ）で日付が見つからなかった場合に、②③④⑥⑦を順番に試す。
    戻り値: (date, confidence, method) または (None, None, None)
    """
    is_wp = "wp-content" in raw_html or "wp-json" in raw_html or "WordPress" in raw_html
    if is_wp:
        result = _try_wp_json(article_url)
        if result:
            return result

    result = _try_rss(article_url)
    if result:
        return result

    result = _try_sitemap(article_url)
    if result:
        return result

    result = _try_wayback(article_url)
    if result:
        return result

    result = _try_path_date(article_url, raw_html)
    if result:
        return result

    return None, None, None


def fetch_article_text(url: str):
    """
    記事ページを取得し、(本文テキスト, 生HTML, 抽出した公開日) を返す。
    公開日はHTMLタグを剥がす前のmetaタグ等（手段①）から抽出する。
    """
    resp = requests.get(url, headers=HEADERS_BROWSER, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding

    raw_html = resp.text
    extracted_date = extract_date_from_html(raw_html)

    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()

    text = soup.get_text(separator="\n", strip=True)
    # 極端に長い場合はモデルへの入力トークン節約のため先頭部分に絞る（記事本文は大抵前半に集中）
    return text[:8000], raw_html, extracted_date


def build_prompt(company_name: str, article_text: str) -> str:
    exclude_list = "・".join(EXCLUDE_TITLE_KEYWORDS)
    return f"""あなたはB2B営業のためのリサーチアシスタントです。

以下の記事は、「{company_name}」が自社サイトに掲載している導入事例・活用事例記事です。
つまり「{company_name}」はサービスを提供しているベンダー側（インタビュアー側）であり、
記事の中では別の顧客企業が紹介・インタビューされています。

あなたのタスクは、**「{company_name}」自身の社員ではなく、記事内で紹介されている顧客企業側の担当者**を
抽出することです。顧客企業名は記事本文中から特定してください（{company_name}とは異なる社名のはずです）。

重要な絞り込み条件:
- 「{company_name}」（ベンダー側・インタビュアー側）の社員は、記事中に名前が出てきても candidates に含めないでください。
  （例: 記事冒頭で自己紹介している「〇〇（インタビュアー）です」のような人物はベンダー側なので対象外）
- 顧客企業は「株式会社」の法人のみを対象にしてください。「医療法人」「一般社団法人」「学校法人」
  「社会福祉法人」「NPO法人」等、株式会社以外の法人形態の顧客企業は優先度が低いため candidates に含めないでください。
- 顧客企業側の人物のみを対象にしてください。役職の有無・レベルは問いません（役職なしの担当者も対象）。
- ただし顧客企業側の人物であっても「{exclude_list}」等の経営層・役員クラスは、
  営業リサーチの観点で使えないため candidates に含めないでください。
- 記事本文に実際に明記されている情報のみを抽出してください。推測や補完は絶対にしないでください。
- name には実名のみを入れてください。「A」「S」のようなイニシャルだけの伏字や、「生産管理担当の方」
  のような人物を特定しない説明的な表現は、実名が分からないということなので candidates に含めないでください。
- 該当者がいない場合は candidates を空配列にしてください。無理に埋める必要はありません。

必ず以下のJSON形式のみで回答してください。前後に説明文やMarkdownのコードブロックは付けないでください。
{{
  "candidates": [
    {{
      "featured_company_name": "記事内で紹介されている顧客企業名（{company_name}とは別の社名）",
      "department_matched": "部署名（記事に明記されている表記のまま。不明な場合は空文字）",
      "name": "氏名",
      "title": "役職・肩書き（記事に明記されている表記のまま。不明な場合は空文字）",
      "contact": "記事内にSNSやメールアドレス等の連絡先があれば記載。なければ空文字"
    }}
  ]
}}

---記事本文---
{article_text}
---本文ここまで---
"""


def call_claude(prompt: str, api_key: str, model: str) -> dict:
    payload = {
        "model": model,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    resp = requests.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"Claude APIエラー (HTTP {resp.status_code}): {resp.text[:500]}")

    data = resp.json()
    content_blocks = data.get("content", [])
    text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
    if not text:
        raise RuntimeError("Claude APIのレスポンスにtextがありません。")

    parsed = parse_json_response(text)
    usage = data.get("usage", {})
    parsed["_usage"] = {
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
    }
    return parsed


def parse_json_response(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"```json", "", text, flags=re.IGNORECASE).replace("```", "").strip()

    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace == -1 or last_brace == -1:
        raise RuntimeError(f"レスポンスからJSONを抽出できませんでした: {text[:200]}")

    text = text[first_brace : last_brace + 1]
    parsed = json.loads(text)
    if not isinstance(parsed.get("candidates"), list):
        parsed["candidates"] = []
    return parsed


def is_excluded_title(title: str) -> bool:
    """経営層キーワードが含まれていたら除外する（プロンプト指示の保険）。"""
    if not title:
        return False
    return any(kw in title for kw in EXCLUDE_TITLE_KEYWORDS)


# 実名ではなく匿名化・伏字・役割の説明になっている名前を弾くためのキーワード
PLACEHOLDER_NAME_KEYWORDS = [
    "担当の方", "担当者", "ご担当者", "の方", "様（", "スタッフ", "メンバー", "関係者",
]


def is_placeholder_name(name: str) -> bool:
    """
    「A」「S」のような頭文字だけの伏字（「A.H」「S. D」のような区切り付きも含む）、
    「生産管理担当の方」のような人物を特定しない説明文を、実名ではないとみなして除外する。
    """
    if not name:
        return True

    stripped = name.strip()

    # ピリオド・スペースで区切られた1〜2文字の塊が並ぶもの（例: "A.H", "S. D", "T.I."）は
    # イニシャルの伏字とみなす。単一の塊でも全体が2文字以内なら伏字とみなす（例: "AB"）。
    parts = [p for p in re.split(r"[.\s　]+", stripped) if p]
    if parts and all(len(p) <= 2 and re.fullmatch(r"[A-Za-zＡ-Ｚａ-ｚ]+", p) for p in parts):
        if len(parts) >= 2 or len(stripped) <= 2:
            return True

    # 「◯◯担当の方」等、人物を特定しない説明的な表現
    if any(kw in stripped for kw in PLACEHOLDER_NAME_KEYWORDS):
        return True

    return False


def normalize(s: str) -> str:
    return re.sub(r"[\s　]", "", str(s or ""))


def verify_name_in_text(name: str, article_text: str) -> str:
    """氏名が記事本文中に実際に存在するか機械照合する（ハルシネーション対策）。"""
    if not name:
        return "氏名なし"
    return "一致確認OK" if normalize(name) in normalize(article_text) else "不一致（要確認・削除推奨）"


def process_article(company_name: str, article_url: str, api_key: str, model: str, fallback_date: str = ""):
    """
    1記事分の処理。戻り値はcandidate行のリスト（0件の場合は該当なし1行）。
    fallback_date: 入力CSV側で既に公開日が分かっている場合はそれを渡す（空なら日付推定を試みる）。
    """
    try:
        article_text, raw_html, extracted_date = fetch_article_text(article_url)
    except requests.exceptions.RequestException as e:
        return [{"status": f"記事取得失敗: {e}", "candidates": [], "usage": {"input_tokens": 0, "output_tokens": 0},
                 "published_date": fallback_date, "date_confidence": "既存" if fallback_date else "", "date_method": ""}]

    # 公開日の決定: ①CSV既存値 → ②メタタグ(fetch_article_text内で取得済み) → ③以降の拡張手段
    if fallback_date:
        published_date, date_confidence, date_method = fallback_date, "既存", "元データ"
    elif extracted_date:
        published_date, date_confidence, date_method = extracted_date, "確定", "HTMLメタタグ"
    else:
        ext_date, ext_conf, ext_method = resolve_date_extended(article_url, raw_html)
        published_date = ext_date or ""
        date_confidence = ext_conf or ""
        date_method = ext_method or ""

    if len(article_text) < 50:
        return [{"status": "本文が短すぎる（取得失敗の可能性）", "candidates": [], "usage": {"input_tokens": 0, "output_tokens": 0},
                 "published_date": published_date, "date_confidence": date_confidence, "date_method": date_method}]

    # 無料の事前フィルタ: 本文に「株式会社」が一度も出てこない記事はAPIを呼ばずスキップする
    # （医療法人・一般社団法人等のみの記事はここで弾かれ、コストがかからない）
    if "株式会社" not in article_text:
        return [{"status": "スキップ（株式会社の記載なし）", "candidates": [], "usage": {"input_tokens": 0, "output_tokens": 0},
                 "published_date": published_date, "date_confidence": date_confidence, "date_method": date_method}]

    try:
        prompt = build_prompt(company_name, article_text)
        result = call_claude(prompt, api_key, model)
    except Exception as e:
        return [{"status": f"抽出エラー: {e}", "candidates": [], "usage": {"input_tokens": 0, "output_tokens": 0},
                 "published_date": published_date, "date_confidence": date_confidence, "date_method": date_method}]

    usage = result.get("_usage", {"input_tokens": 0, "output_tokens": 0})
    candidates = result.get("candidates", [])

    # 事後フィルタ: 経営層の除外に加え、頭文字だけの伏字や「〇〇担当の方」等の非実名も除外する
    filtered = []
    seen_signatures = set()
    for c in candidates:
        title = c.get("title", "")
        name = c.get("name", "")
        if is_excluded_title(title):
            continue  # 経営層は完全除外
        if is_placeholder_name(name):
            continue  # 匿名化された名前は除外
        # モデルが同じ候補を繰り返し出力してしまうことがあるため、
        # 氏名・役職・部署・顧客企業が全て一致するものは1件にまとめる
        signature = (normalize(name), normalize(title),
                     normalize(c.get("department_matched", "")), normalize(c.get("featured_company_name", "")))
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        c["verified"] = verify_name_in_text(name, article_text)
        filtered.append(c)

    if not filtered:
        return [{"status": "該当なし", "candidates": [], "usage": usage,
                 "published_date": published_date, "date_confidence": date_confidence, "date_method": date_method}]

    return [{"status": "OK", "candidates": filtered, "usage": usage,
             "published_date": published_date, "date_confidence": date_confidence, "date_method": date_method}]


def main():
    parser = argparse.ArgumentParser(description="事例記事からキーマン（部長/課長/マネージャー等）を抽出する")
    parser.add_argument("--input", required=True, help="フェーズ1の出力CSV（case_study_articles_all.csv）")
    parser.add_argument("--output", required=True, help="出力CSV")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="使用するClaudeモデル")
    parser.add_argument("--limit", type=int, default=None, help="処理する件数を制限（テスト用）")
    parser.add_argument("--append", action="store_true", help="出力ファイルに追記し、処理済みarticle_urlをスキップして再開する")
    parser.add_argument("--delay", type=float, default=WAIT_SECONDS, help="1件ごとのウェイト秒数")
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("エラー: 環境変数 ANTHROPIC_API_KEY が設定されていません。")
        print("  例: export ANTHROPIC_API_KEY=sk-ant-xxxxx")
        sys.exit(1)

    with open(args.input, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    print(f"入力行数: {len(rows):,}")

    fieldnames = [
        "corporate_number", "company_name", "article_url", "published_date",
        "date_confidence", "date_method",
        "matched_product_name", "large_category", "medium_category", "matched_small_category",
        "featured_company_name", "department_matched", "name", "title", "contact",
        "verified", "status", "extracted_at",
    ]

    # 再開対応: --append 指定時、出力ファイルに既にある article_url は処理済みとしてスキップする
    # （新しいCSVを渡しても、既に処理済みの記事は自動的に重複処理・重複課金されない）
    done_urls = set()
    file_exists = False
    if args.append:
        try:
            with open(args.output, encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    done_urls.add(r.get("article_url", ""))
            file_exists = True
            print(f"処理済み(再開分): {len(done_urls):,}記事をスキップ")
        except FileNotFoundError:
            pass

    targets = []
    for row in rows:
        article_url = row.get("article_url", "").strip()
        if not article_url or article_url in done_urls:
            continue
        targets.append(row)
        if args.limit is not None and len(targets) >= args.limit:
            break

    target_total = len(targets)
    print(f"今回処理する行数: {target_total:,}")

    if target_total == 0:
        print("処理対象がありません。完了しています。")
        return

    total_input_tokens = 0
    total_output_tokens = 0
    call_count = 0

    file_mode = "a" if args.append else "w"
    write_header = not (args.append and file_exists)

    with open(args.output, file_mode, newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
            f.flush()

        for i, row in enumerate(targets):
            article_url = row.get("article_url", "").strip()
            # 列名の互換対応: 新しいcollect_case_study_articles.pyの出力は
            # company_name→site_name、corporate_number→hub_url という列名になっているため、
            # どちらの形式のCSVが来ても対応できるようフォールバックする
            company_name = row.get("company_name") or row.get("site_name", "")
            corporate_number = row.get("corporate_number") or row.get("hub_url", "")
            published_date = row.get("published_date", "")
            # ステップ③で紐付けた製品名・カテゴリー情報があれば、そのまま引き継ぐ
            matched_product_name = row.get("matched_product_name", "")
            large_category = row.get("large_category", "")
            medium_category = row.get("medium_category", "")
            matched_small_category = row.get("matched_small_category", "")

            print(f"[{i + 1}/{target_total}] {company_name} ({article_url[:60]}) ... ", end="", flush=True)

            results = process_article(company_name, article_url, api_key, args.model, fallback_date=published_date)
            now = datetime.now().isoformat(timespec="seconds")

            usage = results[0].get("usage", {"input_tokens": 0, "output_tokens": 0})
            total_input_tokens += usage.get("input_tokens", 0)
            total_output_tokens += usage.get("output_tokens", 0)
            if usage.get("input_tokens", 0) or usage.get("output_tokens", 0):
                call_count += 1

            for r in results:
                effective_date = r.get("published_date", published_date)
                date_confidence = r.get("date_confidence", "")
                date_method = r.get("date_method", "")
                if not r["candidates"]:
                    writer.writerow({
                        "corporate_number": corporate_number,
                        "company_name": company_name,
                        "article_url": article_url,
                        "published_date": effective_date,
                        "date_confidence": date_confidence,
                        "date_method": date_method,
                        "matched_product_name": matched_product_name,
                        "large_category": large_category,
                        "medium_category": medium_category,
                        "matched_small_category": matched_small_category,
                        "featured_company_name": "",
                        "department_matched": "",
                        "name": "",
                        "title": "",
                        "contact": "",
                        "verified": "",
                        "status": r["status"],
                        "extracted_at": now,
                    })
                else:
                    for c in r["candidates"]:
                        writer.writerow({
                            "corporate_number": corporate_number,
                            "company_name": company_name,
                            "article_url": article_url,
                            "published_date": effective_date,
                            "date_confidence": date_confidence,
                            "date_method": date_method,
                            "matched_product_name": matched_product_name,
                            "large_category": large_category,
                            "medium_category": medium_category,
                            "matched_small_category": matched_small_category,
                            "featured_company_name": c.get("featured_company_name", ""),
                            "department_matched": c.get("department_matched", ""),
                            "name": c.get("name", ""),
                            "title": c.get("title", ""),
                            "contact": c.get("contact", ""),
                            "verified": c.get("verified", ""),
                            "status": r["status"],
                            "extracted_at": now,
                        })

            running_cost = (total_input_tokens / 1000000) * PRICE_PER_MILLION_INPUT_USD \
                + (total_output_tokens / 1000000) * PRICE_PER_MILLION_OUTPUT_USD
            print(f"{results[0]['status']} (累計{call_count}件, 累計コスト約${running_cost:.2f})")
            f.flush()
            time.sleep(args.delay)

    final_cost = (total_input_tokens / 1000000) * PRICE_PER_MILLION_INPUT_USD \
        + (total_output_tokens / 1000000) * PRICE_PER_MILLION_OUTPUT_USD
    print(f"完了。結果を {args.output} に書き出しました。")
    print(f"API呼び出し件数: {call_count:,}件")
    print(f"入力トークン合計: {total_input_tokens:,} / 出力トークン合計: {total_output_tokens:,}")
    print(f"推定コスト合計: 約${final_cost:.2f}")
    print("途中で止める場合は、同じコマンドに --append を付けて再実行すれば続きから再開します。")
    print("新しいinputファイル（記事が追加されたもの）を渡しても、既に処理済みのarticle_urlは自動でスキップされます。")


if __name__ == "__main__":
    main()

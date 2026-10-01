#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
파주 지역 뉴스 스크랩 스크립트 (GitHub Actions용)
- 네이버 뉴스 검색 API로 파주 관련 뉴스를 수집
- 네이버 검색광고 API로 "파주" 연관검색어 + 검색량을 조회해, 검색량 높은 연관검색어를
  그날의 뉴스 검색 키워드에 자동으로 추가
- 탭 3개:
  1) 댓글순: 네이버 뉴스(n.news.naver.com) 링크 중 댓글 많은 순
  2) 최신순: 제목에 지역 키워드가 직접 포함된(연관성 엄격) 기사 중 발행시각 최신순
  3) 지역뉴스 연관도순: 그 외 지역 언론사 자체 링크, 검색 연관도순
- 각 기사에 발행 시각 표시
- Gemini로 한 줄 요약
- 결과를 index.html로 저장
"""

import os
import re
import json
import time
import hashlib
import hmac
import base64
import requests
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from bs4 import BeautifulSoup
from google import genai

# ============================================
# 설정 영역
# ============================================

CLIENT_ID = os.environ["NAVER_CLIENT_ID"]
CLIENT_SECRET = os.environ["NAVER_CLIENT_SECRET"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

# 네이버 검색광고 API (연관검색어 자동 추출용) — 없으면 이 기능만 건너뜀
AD_API_KEY = os.environ.get("NAVER_AD_API_KEY", "")
AD_SECRET_KEY = os.environ.get("NAVER_AD_SECRET_KEY", "")
AD_CUSTOMER_ID = os.environ.get("NAVER_AD_CUSTOMER_ID", "")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

BASE_KEYWORDS = [
    "파주 맛집",
    "파주 사건",
    "파주 축제",
    "파주 핫플",
    "파주 아파트",
    "야당동",
    "야당역",
    "야당 맛집",
    "야당 인기",
    "운정 인기",
    "헤이리마을",
]

# 연관검색어 자동 추출용 "씨앗" 키워드
SEED_KEYWORDS = ["파주", "운정", "야당동", "금촌", "문산"]

# 연관검색어가 이 검색량(월간 PC+모바일 합계) 이상이면 자동으로 키워드에 추가
RELATED_KEYWORD_MIN_VOLUME = 300
# 자동 추가하는 연관검색어 최대 개수
RELATED_KEYWORD_MAX_COUNT = 5

NEWS_PER_KEYWORD = 8      # 키워드당 넉넉히 수집
TOP_N = 20                # 탭별 최종 표시 개수
LOCAL_KEYWORDS = ["파주", "야당동", "야당역", "헤이리", "운정", "금촌", "문산", "교하"]

KST = timezone(timedelta(hours=9))

# ============================================
# 네이버 검색광고 API: 연관검색어 + 검색량 조회
# ============================================

def get_ad_api_headers(method, uri):
    timestamp = str(round(time.time() * 1000))
    message = f"{timestamp}.{method}.{uri}"
    signature = base64.b64encode(
        hmac.new(AD_SECRET_KEY.encode(), message.encode(), hashlib.sha256).digest()
    ).decode()
    return {
        "X-Timestamp": timestamp,
        "X-API-KEY": AD_API_KEY,
        "X-Customer": AD_CUSTOMER_ID,
        "X-Signature": signature,
    }

def fetch_related_keywords(seed_keywords):
    """
    네이버 검색광고 API(키워드도구)로 연관검색어 + 월간 검색량을 조회.
    키가 설정 안 돼 있으면 빈 리스트 반환 (이 기능만 조용히 비활성화).
    """
    if not (AD_API_KEY and AD_SECRET_KEY and AD_CUSTOMER_ID):
        print("  (네이버 검색광고 API 키가 없어 연관검색어 추출을 건너뜁니다)")
        return []

    uri = "/keywordstool"
    url = "https://api.naver.com" + uri
    params = {
        "hintKeywords": ",".join(seed_keywords),
        "showDetail": "1",
    }
    try:
        headers = get_ad_api_headers("GET", uri)
        res = requests.get(url, params=params, headers=headers, timeout=10)
        res.raise_for_status()
        data = res.json()
    except Exception as e:
        print(f"  연관검색어 API 오류: {e}")
        return []

    results = []
    for item in data.get("keywordList", []):
        rel_keyword = item.get("relKeyword", "")
        pc = item.get("monthlyPcQcCnt", 0)
        mobile = item.get("monthlyMobileQcCnt", 0)

        # "< 10" 같은 문자열로 오는 경우 처리
        def to_num(v):
            if isinstance(v, str):
                return 0 if "<" in v else int(re.sub(r'[^0-9]', '', v) or 0)
            return int(v or 0)

        total_volume = to_num(pc) + to_num(mobile)

        # 지역과 무관한 연관검색어는 제외
        if not any(kw in rel_keyword for kw in LOCAL_KEYWORDS):
            continue

        results.append({"keyword": rel_keyword, "volume": total_volume})

    results.sort(key=lambda x: x["volume"], reverse=True)
    return results

def build_dynamic_keywords():
    """
    BASE_KEYWORDS + 검색량 높은 연관검색어를 합쳐서
    오늘 사용할 전체 키워드 리스트와, 새로 추가된 연관검색어 목록을 반환.
    """
    related = fetch_related_keywords(SEED_KEYWORDS)
    picked = []
    for item in related:
        if item["volume"] < RELATED_KEYWORD_MIN_VOLUME:
            continue
        if item["keyword"] in BASE_KEYWORDS:
            continue
        picked.append(item)
        if len(picked) >= RELATED_KEYWORD_MAX_COUNT:
            break

    all_keywords = BASE_KEYWORDS + [p["keyword"] for p in picked]
    return all_keywords, picked

# ============================================
# 뉴스 검색 및 유틸 함수
# ============================================

def search_news(keyword, display=10):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {
        "X-Naver-Client-Id": CLIENT_ID,
        "X-Naver-Client-Secret": CLIENT_SECRET
    }
    params = {"query": keyword, "display": display, "start": 1, "sort": "sim"}
    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"API 요청 오류: {e}")
        return None

def clean_html_tags(text):
    clean = re.sub(r'<[^>]+>', '', text)
    return unescape(clean)

def parse_pubdate(pubdate_str):
    try:
        dt = parsedate_to_datetime(pubdate_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        return dt.astimezone(KST)
    except Exception:
        return None

def get_full_content(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(res.text, "html.parser")
        article = (
            soup.select_one("#dic_area")
            or soup.select_one("#articleBodyContents")
            or soup.select_one("article")
        )
        if article:
            return article.get_text(strip=True)[:1000]
    except Exception:
        pass
    return ""

def get_naver_ids(url):
    match = re.search(r'/article/(\d+)/(\d+)', url)
    if match:
        return match.group(1), match.group(2)
    return None

def get_comment_count(oid, aid, url):
    try:
        api_url = (
            "https://apis.naver.com/commentBox/cbox/web_naver_list_jsonp.json"
            "?ticket=news&templateId=default_society&pool=cbox5"
            "&_callback=jQuery"
            f"&lang=ko&country=KR&objectId=news{oid},{aid}"
            "&categoryId=&pageSize=1&indexSize=10&groupId="
            "&listType=OBJECT&pageType=more&page=1&sort=new"
        )
        headers = {"User-Agent": "Mozilla/5.0", "Referer": url}
        res = requests.get(api_url, headers=headers, timeout=5)
        text = res.text
        json_str = text[text.index("(") + 1: text.rindex(")")]
        data = json.loads(json_str)
        return data["result"]["count"]["comment"]
    except Exception:
        return 0

def is_local_news(title, desc):
    text = title + " " + desc
    return any(kw in text for kw in LOCAL_KEYWORDS)

def is_title_relevant(title):
    return any(kw in title for kw in LOCAL_KEYWORDS)

def is_similar_title(new_title, seen_titles):
    new_keywords = set(re.findall(r'[가-힣a-zA-Z0-9]+', new_title))
    for existing in seen_titles:
        existing_keywords = set(re.findall(r'[가-힣a-zA-Z0-9]+', existing))
        if new_keywords and existing_keywords:
            common = new_keywords & existing_keywords
            if len(common) >= 3:
                return True
            similarity = len(common) / min(len(new_keywords), len(existing_keywords))
            if similarity > 0.4:
                return True
    return False

def summarize_with_gemini(title, content):
    try:
        prompt = f"""다음 뉴스 기사를 한 줄로 요약해주세요.

규칙:
1. 딱 한 문장으로 요약 (50자 이내)
2. 초등학생도 이해할 수 있는 쉬운 말로 작성
3. 기사의 핵심 내용만 전달
4. 존댓말(~합니다, ~입니다) 사용

제목: {title}
내용: {content}

요약:"""
        response = gemini_client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt
        )
        return response.text.strip()
    except Exception as e:
        print(f"요약 오류: {e}")
        return title

# ============================================
# 뉴스 수집
# ============================================

def collect_all_news(keywords):
    all_news = []
    seen_links = set()
    seen_titles = []

    print("뉴스 수집 중...")
    for keyword in keywords:
        print(f"  '{keyword}' 검색 중...")
        result = search_news(keyword, NEWS_PER_KEYWORD * 3)

        if result and "items" in result:
            count = 0
            for item in result["items"]:
                if count >= NEWS_PER_KEYWORD:
                    break
                if item["link"] in seen_links:
                    continue

                api_title = clean_html_tags(item["title"])
                api_desc = clean_html_tags(item["description"])

                if not is_local_news(api_title, api_desc):
                    continue
                if is_similar_title(api_title, seen_titles):
                    continue

                content = get_full_content(item["link"]) or api_desc
                summary = summarize_with_gemini(api_title, content)
                pub_dt = parse_pubdate(item.get("pubDate", ""))

                naver_ids = get_naver_ids(item["link"])
                is_naver = naver_ids is not None
                comment_count = 0
                if is_naver:
                    oid, aid = naver_ids
                    comment_count = get_comment_count(oid, aid, item["link"])

                all_news.append({
                    "title": api_title,
                    "link": item["link"],
                    "summary": summary,
                    "keyword": keyword,
                    "is_naver": is_naver,
                    "comment_count": comment_count,
                    "pub_dt": pub_dt,
                    "title_relevant": is_title_relevant(api_title),
                })

                seen_links.add(item["link"])
                seen_titles.append(api_title)
                count += 1
                time.sleep(0.3)

    comment_group = [n for n in all_news if n["is_naver"]]
    comment_group.sort(key=lambda x: x["comment_count"], reverse=True)

    latest_group = [n for n in all_news if n["title_relevant"]]
    latest_group.sort(
        key=lambda x: x["pub_dt"] if x["pub_dt"] else datetime.min.replace(tzinfo=KST),
        reverse=True
    )

    local_group = [n for n in all_news if not n["is_naver"]]

    return comment_group[:TOP_N], latest_group[:TOP_N], local_group[:TOP_N]

# ============================================
# 게시판 스타일 전체 HTML 페이지 생성
# ============================================

def render_rows(news_list, show_comment_badge):
    if not news_list:
        return '<div class="empty-msg">해당하는 뉴스가 없습니다.</div>'

    rows = ""
    for i, news in enumerate(news_list, 1):
        badge = ""
        if show_comment_badge and news["comment_count"] > 0:
            badge = f'<span class="comment-badge">💬 댓글 {news["comment_count"]}개</span>'

        pub_str = ""
        if news["pub_dt"]:
            pub_str = f'<span class="pub-time">🕐 {news["pub_dt"].strftime("%m-%d %H:%M")}</span>'

        rows += f"""
        <div class="news-row">
            <div class="news-num">{i}</div>
            <div class="news-body">
                <div class="news-title">
                    <a href="{news['link']}" target="_blank" rel="noopener">{news['title']}</a>
                </div>
                <div class="news-summary">{news['summary']}</div>
                <div class="news-meta">
                    <span class="news-tag">#{news['keyword'].replace(' ', '')}</span>
                    {badge}
                    {pub_str}
                </div>
            </div>
        </div>
        """
    return rows

def render_trending_section(picked_related):
    if not picked_related:
        return ""
    chips = "".join(
        f'<span class="trend-chip">{p["keyword"]} <b>{p["volume"]:,}</b></span>'
        for p in picked_related
    )
    return f"""
    <div class="trending-box">
        <div class="trending-title">🔥 오늘의 인기 연관검색어 (월간 검색량 기준)</div>
        <div class="trending-chips">{chips}</div>
    </div>
    """

def generate_full_page(comment_news, latest_news, local_news, picked_related):
    now = datetime.now(KST)
    updated_str = now.strftime("%Y-%m-%d (%a) %H:%M 업데이트")

    comment_rows = render_rows(comment_news, show_comment_badge=True)
    latest_rows = render_rows(latest_news, show_comment_badge=False)
    local_rows = render_rows(local_news, show_comment_badge=False)
    trending_section = render_trending_section(picked_related)

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>파주 핫이슈 게시판</title>
<style>
  body {{
    font-family: -apple-system, "Malgun Gothic", sans-serif;
    background: #f5f5f5;
    margin: 0;
    padding: 0;
    color: #222;
  }}
  .board-header {{
    background: #EB6248;
    color: #fff;
    padding: 24px 20px;
    text-align: center;
  }}
  .board-header h1 {{
    margin: 0 0 6px 0;
    font-size: 22px;
  }}
  .board-header .updated {{
    font-size: 13px;
    opacity: 0.9;
  }}
  .container {{
    max-width: 720px;
    margin: 0 auto;
    background: #fff;
    min-height: 100vh;
  }}
  .trending-box {{
    padding: 14px 20px;
    background: #FFF7ED;
    border-bottom: 1px solid #eee;
  }}
  .trending-title {{
    font-size: 12px;
    color: #C2410C;
    font-weight: bold;
    margin-bottom: 8px;
  }}
  .trending-chips {{
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }}
  .trend-chip {{
    font-size: 12px;
    background: #fff;
    border: 1px solid #FDBA74;
    color: #9A3412;
    padding: 4px 10px;
    border-radius: 14px;
  }}
  .trend-chip b {{
    color: #EB6248;
  }}
  .tab-bar {{
    display: flex;
    border-bottom: 2px solid #eee;
    background: #fff;
    position: sticky;
    top: 0;
    z-index: 10;
  }}
  .tab-btn {{
    flex: 1;
    padding: 14px 0;
    text-align: center;
    font-size: 13px;
    font-weight: bold;
    color: #999;
    background: none;
    border: none;
    cursor: pointer;
    border-bottom: 3px solid transparent;
  }}
  .tab-btn.active {{
    color: #EB6248;
    border-bottom: 3px solid #EB6248;
  }}
  .tab-panel {{
    display: none;
  }}
  .tab-panel.active {{
    display: block;
  }}
  .tab-desc {{
    padding: 10px 20px;
    font-size: 12px;
    color: #999;
    background: #fafafa;
  }}
  .news-row {{
    display: flex;
    padding: 16px 20px;
    border-bottom: 1px solid #eee;
  }}
  .news-num {{
    width: 28px;
    color: #EB6248;
    font-weight: bold;
    flex-shrink: 0;
  }}
  .news-title a {{
    color: #222;
    font-weight: bold;
    text-decoration: none;
    font-size: 15px;
  }}
  .news-title a:hover {{
    text-decoration: underline;
  }}
  .news-summary {{
    color: #555;
    font-size: 13px;
    margin-top: 6px;
    line-height: 1.5;
  }}
  .news-meta {{
    margin-top: 8px;
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
  }}
  .news-tag {{
    display: inline-block;
    font-size: 11px;
    color: #EB6248;
    background: #FDEDE9;
    padding: 2px 8px;
    border-radius: 10px;
  }}
  .comment-badge, .pub-time {{
    font-size: 11px;
    color: #888;
  }}
  .empty-msg {{
    padding: 40px;
    text-align: center;
    color: #999;
  }}
  .footer {{
    text-align: center;
    padding: 20px;
    color: #999;
    font-size: 12px;
  }}
</style>
</head>
<body>
  <div class="container">
    <div class="board-header">
      <h1>📰 파주 핫이슈 게시판</h1>
      <div class="updated">{updated_str}</div>
    </div>

    {trending_section}

    <div class="tab-bar">
      <button class="tab-btn active" onclick="showTab('comment', this)">💬 댓글순</button>
      <button class="tab-btn" onclick="showTab('latest', this)">🕐 최신순</button>
      <button class="tab-btn" onclick="showTab('local', this)">📍 지역뉴스</button>
    </div>

    <div id="tab-comment" class="tab-panel active">
      <div class="tab-desc">네이버 뉴스 댓글 많은 순 (연합뉴스·매일경제 등 대형 언론사 위주)</div>
      {comment_rows}
    </div>

    <div id="tab-latest" class="tab-panel">
      <div class="tab-desc">제목에 지역명이 직접 포함된 기사만 · 발행 시각 최신순 (댓글수 무관)</div>
      {latest_rows}
    </div>

    <div id="tab-local" class="tab-panel">
      <div class="tab-desc">지역 언론사 자체 기사 · 검색 연관도 높은 순</div>
      {local_rows}
    </div>

    <div class="footer">댓글순 {len(comment_news)}건 · 최신순 {len(latest_news)}건 · 지역뉴스 {len(local_news)}건 | 매일 아침 자동 업데이트</div>
  </div>

  <script>
    function showTab(name, btn) {{
      document.querySelectorAll('.tab-panel').forEach(el => el.classList.remove('active'));
      document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
      document.getElementById('tab-' + name).classList.add('active');
      btn.classList.add('active');
    }}
  </script>
</body>
</html>"""
    return html

# ============================================
# 메인 실행
# ============================================

def main():
    keywords, picked_related = build_dynamic_keywords()
    print(f"오늘 사용할 키워드 ({len(keywords)}개): {keywords}")
    if picked_related:
        print(f"자동 추가된 연관검색어: {[(p['keyword'], p['volume']) for p in picked_related]}")

    comment_news, latest_news, local_news = collect_all_news(keywords)
    html_output = generate_full_page(comment_news, latest_news, local_news, picked_related)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_output)
    print(f"\n완료: index.html 생성 (댓글순 {len(comment_news)}건 / 최신순 {len(latest_news)}건 / 지역뉴스 {len(local_news)}건)")

if __name__ == "__main__":
    main()

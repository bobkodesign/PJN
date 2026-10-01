#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
파주 지역 뉴스 스크랩 스크립트 (GitHub Actions용)
- 네이버 검색 API로 파주 관련 뉴스를 수집
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
import requests
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from bs4 import BeautifulSoup
import time
from google import genai

# ============================================
# 설정 영역
# ============================================

CLIENT_ID = os.environ["NAVER_CLIENT_ID"]
CLIENT_SECRET = os.environ["NAVER_CLIENT_SECRET"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

KEYWORDS = [
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

NEWS_PER_KEYWORD = 8      # 키워드당 넉넉히 수집
TOP_N = 20                # 탭별 최종 표시 개수
LOCAL_KEYWORDS = ["파주", "야당동", "야당역", "헤이리", "운정", "금촌", "문산", "교하"]

KST = timezone(timedelta(hours=9))

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
    """네이버 API의 pubDate(RFC822 형식)를 KST datetime으로 변환"""
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
    """일반 필터: 제목 또는 설명에 지역 키워드가 있으면 통과"""
    text = title + " " + desc
    return any(kw in text for kw in LOCAL_KEYWORDS)

def is_title_relevant(title):
    """엄격한 필터(최신순 탭 전용): 제목에 직접 지역 키워드가 있어야 통과"""
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

def collect_all_news():
    all_news = []
    seen_links = set()
    seen_titles = []

    print("뉴스 수집 중...")
    for keyword in KEYWORDS:
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

    # 탭1: 댓글순 (네이버 뉴스만, 댓글 많은 순)
    comment_group = [n for n in all_news if n["is_naver"]]
    comment_group.sort(key=lambda x: x["comment_count"], reverse=True)

    # 탭2: 최신순 (제목에 지역 키워드 직접 포함 + 발행시각 최신순, 댓글 수 무관)
    latest_group = [n for n in all_news if n["title_relevant"]]
    latest_group.sort(
        key=lambda x: x["pub_dt"] if x["pub_dt"] else datetime.min.replace(tzinfo=KST),
        reverse=True
    )

    # 탭3: 지역뉴스 연관도순 (네이버 아닌 링크, 검색 연관도순 유지)
    local_group = [n for n in all_news if not n["is_naver"]]

    return comment_group[:TOP_N], latest_group[:TOP_N], local_group[:TOP_N]

# ============================================
# 게시판 스타일 전체 HTML 페이지 생성 (탭 3개)
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

def generate_full_page(comment_news, latest_news, local_news):
    now = datetime.now(KST)
    updated_str = now.strftime("%Y-%m-%d (%a) %H:%M 업데이트")

    comment_rows = render_rows(comment_news, show_comment_badge=True)
    latest_rows = render_rows(latest_news, show_comment_badge=False)
    local_rows = render_rows(local_news, show_comment_badge=False)

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
    comment_news, latest_news, local_news = collect_all_news()
    html_output = generate_full_page(comment_news, latest_news, local_news)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_output)
    print(f"\n완료: index.html 생성 (댓글순 {len(comment_news)}건 / 최신순 {len(latest_news)}건 / 지역뉴스 {len(local_news)}건)")

if __name__ == "__main__":
    main()

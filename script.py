"""어미새(eomisae.co.kr) 핫딜 게시판 크롤러.

게시판(/rt)을 주기적으로 확인해 새 게시글이 올라오면 텔레그램으로 알림을 보낸다.
마지막으로 처리한 게시글 번호는 상태 파일에 저장해 재실행 시 중복 알림을 막는다.
"""

import json
import logging
import os
import re
import time

import requests
from bs4 import BeautifulSoup

# ─── 설정 ───────────────────────────────────────────────
BASE_URL = "https://eomisae.co.kr"
LIST_URL = BASE_URL + "/rt"

STATE_FILE = "eomisae_last_id.json"
CONFIG_FILE = "config.json"

MIN_VALID_POST_ID = 150_000_000  # 이 번호 미만은 게시글로 취급하지 않음
POLL_INTERVAL_SEC = 60           # 크롤링 주기(초)
REQUEST_TIMEOUT_SEC = 15

# 게시글 링크 패턴: /rt/숫자  (절대경로 https://... 형태도 허용)
POST_HREF_RE = re.compile(r"^(?:https://eomisae\.co\.kr)?/rt/\d+$")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("mother_bird")


# ─── 텔레그램 설정 ──────────────────────────────────────
def load_telegram_config():
    """환경변수를 우선하고, 없으면 config.json에서 토큰/채널ID를 읽는다."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        return token, chat_id

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                cfg = json.load(f)
            return cfg.get("telegram_bot_token"), cfg.get("telegram_chat_id")
        except Exception as e:
            log.error("설정 파일 로드 실패: %s", e)

    return None, None


# ─── 상태 파일 (마지막 처리 게시글 번호) ────────────────
def load_last_id():
    """(last_id, 파일존재여부)를 반환. 파일이 없으면 최초 실행으로 간주한다."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
            return state.get("last_id", MIN_VALID_POST_ID), True
        except Exception as e:
            log.error("상태 파일 로드 실패: %s", e)
    return MIN_VALID_POST_ID, False


def save_last_id(last_id):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"last_id": last_id}, f)
        log.info("상태 파일 저장: last_id=%s", last_id)
    except Exception as e:
        log.error("상태 파일 저장 실패: %s", e)


# ─── 크롤링 / 파싱 ──────────────────────────────────────
def fetch_list_html():
    """게시판 목록 HTML을 가져온다. 실패 시 None."""
    try:
        resp = requests.get(
            LIST_URL,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=REQUEST_TIMEOUT_SEC,
        )
    except requests.RequestException as e:
        log.error("사이트 요청 실패: %s", e)
        return None

    if resp.status_code != 200:
        log.error("사이트 접속 실패: 상태 코드 %s", resp.status_code)
        return None
    return resp.text


def parse_posts(html):
    """HTML에서 게시글 목록을 파싱해 [{id, title, link}, ...]를 최신순으로 반환한다."""
    soup = BeautifulSoup(html, "html.parser")

    posts = []
    seen_ids = set()
    for link in soup.find_all("a", class_="pjax", href=POST_HREF_RE):
        # "hx" 클래스 = Read More 링크 → 제외
        if "hx" in link.get("class", []):
            continue

        href = link.get("href")
        title = link.get_text(strip=True)

        # 상대경로를 절대경로로 변환
        full_link = href if href.startswith("http") else BASE_URL + href

        try:
            post_id = int(full_link.rsplit("/", 1)[-1])
        except ValueError:
            continue

        if post_id < MIN_VALID_POST_ID or not title or post_id in seen_ids:
            continue

        seen_ids.add(post_id)
        posts.append({"id": post_id, "title": title, "link": full_link})

    posts.sort(key=lambda p: p["id"], reverse=True)  # 최신순
    return posts


# ─── 텔레그램 전송 ──────────────────────────────────────
def send_to_telegram(token, chat_id, title, link):
    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": f'<a href="{link}">{title}</a>',
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(api_url, data=payload, timeout=REQUEST_TIMEOUT_SEC)
        if resp.status_code != 200:
            log.error("텔레그램 전송 실패: %s", resp.text)
            return False
        return True
    except requests.RequestException as e:
        log.error("텔레그램 전송 중 에러: %s", e)
        return False


# ─── 한 사이클 ──────────────────────────────────────────
def run_once(token, chat_id):
    last_id, file_exists = load_last_id()
    log.info("마지막 처리 ID: %s (상태파일 존재: %s)", last_id, file_exists)

    html = fetch_list_html()
    if html is None:
        return

    posts = parse_posts(html)
    log.info("수집된 게시글 수: %s", len(posts))
    if not posts:
        return

    newest_id = posts[0]["id"]

    # 최초 실행: 알림 없이 기준점만 저장 (기존 글이 한꺼번에 쏟아지는 것 방지)
    if not file_exists:
        log.info("최초 실행: 메시지 없이 최신 ID(%s)만 저장합니다.", newest_id)
        save_last_id(newest_id)
        return

    # 새 게시글(저장된 ID보다 큰 것)만, 오래된 순으로 전송
    new_posts = sorted(
        (p for p in posts if p["id"] > last_id), key=lambda p: p["id"]
    )
    if not new_posts:
        log.info("새 게시글 없음")
        return

    log.info("새 게시글 %s개 발견", len(new_posts))
    for post in new_posts:
        ok = send_to_telegram(token, chat_id, post["title"], post["link"])
        log.info(
            "[%s] #%s %s",
            "전송" if ok else "실패",
            post["id"],
            post["title"],
        )

    save_last_id(newest_id)


def main():
    log.info("🦅 어미새 크롤링 봇 시작!")

    token, chat_id = load_telegram_config()
    if not token or not chat_id:
        log.error(
            "텔레그램 설정이 없습니다. "
            "환경변수(TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID) 또는 config.json을 설정하세요."
        )
        return

    try:
        while True:
            run_once(token, chat_id)
            log.info("다음 크롤링까지 %s초 대기...", POLL_INTERVAL_SEC)
            time.sleep(POLL_INTERVAL_SEC)
    except KeyboardInterrupt:
        log.info("프로그램 종료")


if __name__ == "__main__":
    main()

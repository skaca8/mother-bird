import requests
from bs4 import BeautifulSoup
import time
import re
import json
import os

BASE_URL = "https://eomisae.co.kr"
LIST_URL = BASE_URL + "/rt"
SLACK_WEBHOOK_URL = "https://hooks.slack.com/services/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"

sent_links = set()
MIN_VALID_POST_ID = 150000000  # 게시글 번호 기준
STATE_FILE = "eomisae_last_id.json"

# 상태 파일에서 마지막으로 처리한 게시글 ID를 로드
def load_last_id():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                state = json.load(f)
                return state.get("last_id", MIN_VALID_POST_ID), True  # 파일 존재
        except Exception as e:
            print(f"상태 파일 로드 실패: {e}")
    return MIN_VALID_POST_ID, False  # 파일 없음

# 마지막으로 처리한 게시글 ID 저장
def save_last_id(last_id):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump({"last_id": last_id}, f)
        print(f"상태 파일 저장 완료: {last_id}")
    except Exception as e:
        print(f"상태 파일 저장 실패: {e}")

def fetch_and_notify():
    global sent_links
    try:
        # 마지막으로 처리한 ID 로드 및 파일 존재 여부 확인
        last_id, file_exists = load_last_id()
        print(f"마지막으로 처리한 게시글 ID: {last_id} (파일 존재: {file_exists})")
        
        response = requests.get(LIST_URL, headers={"User-Agent": "Mozilla/5.0"})
        if response.status_code != 200:
            print(f"사이트 접속 실패: 상태 코드 {response.status_code}")
            return
            
        soup = BeautifulSoup(response.text, "html.parser")
        
        # 원래 코드와 동일하게 a.pjax 요소 검색하되, "hx" 클래스가 있는 것은 제외
        posts = []
        for link in soup.find_all("a", class_="pjax", href=re.compile(r"^https://eomisae\.co\.kr/rt/\d+$")):
            # "hx" 클래스를 포함하고 있는 요소는 건너뛰기 (Read More 링크 제외)
            if link.has_attr('class') and 'hx' in link['class']:
                print(f"Read More 링크 제외: {link.get_text(strip=True)}")
                continue
            posts.append(link)
            
        print(f"발견된 게시글 수: {len(posts)}")
        
        # 모든 게시글 정보 수집
        all_posts = []
        for post in posts:
            link = post.get("href")
            title = post.get_text(strip=True)
            
            try:
                post_id = int(link.rsplit("/", 1)[-1])
                if post_id >= MIN_VALID_POST_ID and title:
                    all_posts.append({"id": post_id, "title": title, "link": link})
                    print(f"수집됨: #{post_id} {title}")
            except ValueError:
                continue
        
        # 게시글이 없으면 종료
        if not all_posts:
            print("처리할 게시글이 없습니다.")
            return
        
        # ID 기준으로 정렬 (내림차순 = 최신순)
        all_posts.sort(key=lambda x: x["id"], reverse=True)
        
        # 정렬된 최신 게시글 10개 출력 (디버깅용)
        print("\n=== 정렬된 게시글 목록 (최신순) ===")
        for i, post in enumerate(all_posts[:10]):
            print(f"{i+1}. ID: {post['id']} - {post['title']}")
        print("================================\n")
        
        # 최신 게시글의 ID
        newest_id = all_posts[0]["id"] if all_posts else last_id
        
        # 최초 실행 시 메시지 전송 없이 ID만 저장
        if not file_exists:
            print(f"최초 실행 감지: 메시지 전송 없이 최신 ID({newest_id})만 저장합니다.")
            save_last_id(newest_id)
            return
        
        # 새 게시글만 처리 (last_id보다 큰 ID를 가진 게시글)
        new_posts = [post for post in all_posts if post["id"] > last_id]
        
        if new_posts:
            print(f"\n새 게시글 {len(new_posts)}개 발견!\n")
            
            # ID 기준 오름차순 정렬 (오래된 것부터 처리)
            new_posts.sort(key=lambda x: x["id"])
            
            for post in new_posts:
                if post["link"] not in sent_links:
                    print(f"[알림 전송] #{post['id']} {post['title']} -> {post['link']}")
                    send_to_slack(post["title"], post["link"])
                    sent_links.add(post["link"])
            
            # 가장 최근 ID 저장
            save_last_id(newest_id)
            print(f"마지막 ID 업데이트: {newest_id}")
        else:
            print("새 게시글이 없습니다.")
        
        # 메모리 관리: sent_links 크기 제한
        if len(sent_links) > 1000:
            sent_links = set(list(sent_links)[-500:])  # 최근 500개만 유지

    except Exception as e:
        import traceback
        print("에러 발생:", e)
        print(traceback.format_exc())

def send_to_slack(title, link):
    message = f"<{link}|{title}>"
    try:
        response = requests.post(SLACK_WEBHOOK_URL, json={"text": message})
        if response.status_code != 200:
            print("Slack 전송 실패:", response.text)
        else:
            print("Slack 전송 성공")
    except Exception as e:
        print("Slack 전송 중 에러:", e)

if __name__ == "__main__":
    print("🦅 어미새 크롤링 봇 시작!")
    
    # 프로그램 시작 시 기존 sent_links 초기화 (재시작 시 중복 방지)
    sent_links = set()
    
    try:
        while True:
            print("\n" + "="*50)
            print(f"크롤링 시작: {time.strftime('%Y-%m-%d %H:%M:%S')}")
            fetch_and_notify()
            print(f"다음 크롤링까지 60초 대기...")
            print("="*50 + "\n")
            time.sleep(60)
    except KeyboardInterrupt:
        print("\n프로그램 종료")
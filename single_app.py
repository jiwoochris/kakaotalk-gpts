from fastapi import FastAPI, Request, BackgroundTasks
import uvicorn
import requests  # 콜백을 위해 필요
from openai import OpenAI
from dotenv import load_dotenv
import os

load_dotenv()

app = FastAPI()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID = os.getenv("ASSISTANT_ID")

# OpenAI API 클라이언트 설정
client = OpenAI(api_key=OPENAI_API_KEY)

def get_last_assistant_answer(sync_cursor_page):
    """어시스턴트의 마지막 메시지(텍스트)만 추출하는 헬퍼 함수"""
    messages = sync_cursor_page.data
    assistant_messages = [msg for msg in messages if msg.role == 'assistant']

    if assistant_messages:
        last_answer = assistant_messages[0]
        if last_answer.content and hasattr(last_answer.content[0], 'text'):
            return last_answer.content[0].text.value
        else:
            return "메시지 내용이 없습니다."
    else:
        return "어시스턴트 답변이 없습니다."

def process_and_post_callback(callback_url: str, utterance: str):
    """
    백그라운드 작업:
    1) OpenAI API 통해 답변 생성
    2) 최종 결과를 callback_url로 POST
    """
    # 새 Thread 생성 후 사용자 메시지 추가
    thread = client.beta.threads.create()
    client.beta.threads.messages.create(
        thread_id=thread.id,
        role="user",
        content=utterance
    )

    # 어시스턴트 실행 (Run) 생성 및 폴링
    run = client.beta.threads.runs.create_and_poll(
        thread_id=thread.id,
        assistant_id=ASSISTANT_ID
    )

    # 답변 추출
    if run.status == 'completed':
        messages = client.beta.threads.messages.list(thread_id=thread.id)
        response_message = get_last_assistant_answer(messages)
        if not response_message:
            response_message = "어시스턴트 응답을 찾지 못했습니다."
    else:
        response_message = f"Run status: {run.status}"

    print("최종 답변:", response_message)

    # 최종 콜백으로 보낼 페이로드
    callback_payload = {
        "version": "2.0",
        "template": {
            "outputs": [
                {
                    "simpleText": {
                        "text": response_message
                    }
                }
            ]
        }
    }

    # callbackUrl로 POST
    try:
        res = requests.post(callback_url, json=callback_payload, timeout=10)
        print("콜백 전송 성공:", res.status_code)
    except Exception as e:
        print("콜백 전송 실패:", e)

@app.post("/")
async def chat_response(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()
    
    print("Body: ", body)

    # callbackUrl 추출
    user_request = body.get("userRequest", {})
    callback_url = user_request.get("callbackUrl", "")

    # 사용자가 입력한 내용
    utterance = user_request.get("utterance", "")
    print("사용자 발화:", utterance)
    print("콜백 URL:", callback_url)

    # (1) 클라이언트에 바로 '생각 중' 메시지 응답
    immediate_response = {
        "version": "2.0",
        "useCallback": True,
        "data": {
            "text": "생각 중"
        }
    }

    # (2) 백그라운드 작업으로 실제 AI 답변 생성 후 callbackUrl로 전송
    if callback_url:
        background_tasks.add_task(process_and_post_callback, callback_url, utterance)
    else:
        print("callbackUrl이 없어 백그라운드 작업을 수행할 수 없습니다.")

    return immediate_response

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=80)

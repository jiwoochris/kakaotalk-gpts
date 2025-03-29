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

# 각 유저별 스레드 ID를 저장해 대화 문맥을 유지합니다.
conversation_threads = {}
# 보관할 최대 메시지 수 (예: 마지막 10개의 메시지만 유지)
MAX_CONTEXT_MESSAGES = 10

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

def trim_conversation_history_if_needed(thread_id: str, user_id: str) -> str:
    """
    대화 스레드에 저장된 메시지 수가 MAX_CONTEXT_MESSAGES를 초과하면,
    마지막 MAX_CONTEXT_MESSAGES개의 메시지만 남기고 새 스레드를 생성합니다.
    """
    messages_response = client.beta.threads.messages.list(thread_id=thread_id)
    messages = messages_response.data
    if len(messages) > MAX_CONTEXT_MESSAGES:
        new_thread = client.beta.threads.create()
        # 마지막 MAX_CONTEXT_MESSAGES개의 메시지들을 새 스레드에 추가합니다.
        for msg in messages[-MAX_CONTEXT_MESSAGES:]:
            # 메시지 내용 추출 (구조에 따라 수정할 수 있음)
            if msg.content and hasattr(msg.content[0], 'text'):
                text = msg.content[0].text.value
            else:
                text = str(msg.content)
            client.beta.threads.messages.create(
                thread_id=new_thread.id,
                role=msg.role,
                content=text
            )
        conversation_threads[user_id] = new_thread.id
        print(f"Thread trimmed for user {user_id}. New thread id: {new_thread.id}")
        return new_thread.id
    return thread_id

def process_and_post_callback(callback_url: str, utterance: str, user_id: str):
    """
    백그라운드 작업:
    1) OpenAI API 통해 답변 생성
    2) 최종 결과를 callback_url로 POST
    """
    # 유저의 스레드가 이미 존재하면 해당 스레드를 사용, 없으면 새로 생성합니다.
    if user_id in conversation_threads:
        thread_id = conversation_threads[user_id]
        print(f"기존 스레드 사용 - 유저 ID: {user_id}, 스레드 ID: {thread_id}")
    else:
        thread = client.beta.threads.create()
        thread_id = thread.id
        conversation_threads[user_id] = thread_id
        print(f"새 스레드 생성 - 유저 ID: {user_id}, 스레드 ID: {thread_id}")

    # 스레드 내 메시지 수가 설정된 최대치를 초과하는지 확인 후 필요시 트림합니다.
    thread_id = trim_conversation_history_if_needed(thread_id, user_id)

    # 유저 메시지 추가
    client.beta.threads.messages.create(
        thread_id=thread_id,
        role="user",
        content=utterance
    )

    # 어시스턴트 실행 (Run) 생성 및 폴링
    run = client.beta.threads.runs.create_and_poll(
        thread_id=thread_id,
        assistant_id=ASSISTANT_ID
    )

    # 답변 추출
    if run.status == 'completed':
        messages = client.beta.threads.messages.list(thread_id=thread_id)
        print(messages)
        response_message = get_last_assistant_answer(messages)
        if not response_message:
            response_message = "어시스턴트 응답을 찾지 못했습니다."
    else:
        response_message = f"Run status: {run.status}"

    print("최종 답변:", response_message)

    # 최종 콜백으로 보낼 페이로드 구성
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

    # callbackUrl로 POST 전송
    try:
        res = requests.post(callback_url, json=callback_payload, timeout=10)
        print("콜백 전송 성공:", res.status_code)
    except Exception as e:
        print("콜백 전송 실패:", e)

@app.post("/")
async def chat_response(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()
    
    print("Body: ", body)

    # callbackUrl 추출 (키가 "userRequest" 또는 "user Request"일 수 있음)
    user_request = body.get("userRequest") or body.get("user Request", {})
    callback_url = user_request.get("callbackUrl", "")
    
    # 사용자가 입력한 내용 추출
    utterance = user_request.get("utterance", "")
    print("사용자 발화:", utterance)
    print("콜백 URL:", callback_url)

    # user id 추출 (user 객체 안의 id)
    user_info = user_request.get("user", {})
    user_id = user_info.get("id")
    if user_id:
        print("유저 ID:", user_id)
    else:
        print("유저 ID가 존재하지 않습니다.")

    # (1) 클라이언트에 바로 '생각 중' 메시지 응답
    immediate_response = {
        "version": "2.0",
        "useCallback": True,
        "data": {
            "text": "생각 중"
        }
    }

    # (2) 백그라운드 작업으로 실제 AI 답변 생성 후 callbackUrl로 전송
    if callback_url and user_id:
        background_tasks.add_task(process_and_post_callback, callback_url, utterance, user_id)
    else:
        print("callbackUrl 또는 user_id가 없어 백그라운드 작업을 수행할 수 없습니다.")

    return immediate_response

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=80)

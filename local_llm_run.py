# local_llm_run.py
import asyncio
import os
import time
from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.models.lite_llm import LiteLlm
from google.genai import types

os.environ["OLLAMA_API_BASE"] = "http://localhost:11434"

# --- Change this one line to swap models for comparison ---
MODEL_NAME = "ollama_chat/qwen3:8b"
# MODEL_NAME = "ollama_chat/gemma4:e4b"

def get_weather(city: str) -> dict:
    """Mock weather lookup tool."""
    return {"city": city, "condition": "Sunny", "temp_c": 24}

root_agent = Agent(
    model=LiteLlm(model=MODEL_NAME),
    name="local_weather_agent",
    description="A fully offline agent that answers weather questions.",
    instruction="You answer weather questions using the get_weather tool. Always call the tool; never guess.",
    tools=[get_weather],
)

APP_NAME, USER_ID, SESSION_ID = "local_app", "user1", "session1"
session_service = InMemorySessionService()
runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=session_service)

async def main():
    await session_service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID)
    content = types.Content(role="user", parts=[types.Part(text="What's the weather in Delhi?")])

    print(f"--- Model: {MODEL_NAME} ---")
    start = time.perf_counter()

    final_text = None
    async for event in runner.run_async(user_id=USER_ID, session_id=SESSION_ID, new_message=content):
        if event.is_final_response() and event.content and event.content.parts:
            final_text = event.content.parts[0].text

    elapsed = time.perf_counter() - start

    print("Agent:", final_text)
    print(f"Time taken: {elapsed:.2f} seconds")

asyncio.run(main())
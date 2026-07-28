import asyncio 
from google.adk.agents import LlmAgent 
from google.adk.runners import Runner 
from google.adk.sessions import InMemorySessionService
from google.adk.code_executors import BuiltInCodeExecutor 
from dotenv import load_dotenv 
from google.genai import types 

load_dotenv() 

AGENT_NAME= "calculator_agent"
APP_NAME = "calculator"
USER_ID = "user1234"
SESSION_ID = "session_code_exec"
GEMINI_MODEL = "gemini-2.5-flash"

code_agent= LlmAgent(
    name = AGENT_NAME ,
    model = GEMINI_MODEL,
    code_executor= BuiltInCodeExecutor(),
    instruction="""You are a calculator agent.
    When given a mathematical expression, write and execute Python code
    to calculate the result. Return only the final numerical result as
    plain text, without markdown or code blocks.
    """,
    description="Executes Python code to perform calculations.",
)

session_service = InMemorySessionService()
runner = Runner(agent= code_agent , app_name = APP_NAME ,session_service = session_service )
async def call_agent(query: str) : 
    content = types.Content(role ="user",parts = [types.Part(text=query)])
    print(f"\n --- Query: {query} ---") 

    async for event in runner.run_async(
        user_id = USER_ID, session_id = SESSION_ID ,new_message = content
    ):
        if event.content and event.content.parts : 
            for  part in event.content.parts: 
                if part.executable_code:
                    print(f" [generated code]\n{part.executable_code.code}")

                elif part.code_execution_result: 
                    print(f"[execution result] {part.code_execution_result.outcome}: "
                           f"{part.code_execution_result.output}")
                elif part.text and not part.text.isspace():
                    print(f" [text]{part.text.strip()}")

async def main() : 
    await session_service.create_session(
        app_name=APP_NAME, user_id = USER_ID , session_id = SESSION_ID 
    )
    await call_agent("Calculate the value of (5+7)*3")
    await call_agent("What is 10 factorial ")


asyncio.run(main())

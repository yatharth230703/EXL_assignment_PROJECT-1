from google.adk.agents.llm_agent import Agent
import time 

def get_current_time(city:str)->dict:
    return {"status":"success","city":city , "time":"10:30 AM"}

root_agent = Agent(
    model='gemini-flash-latest',
    name='root_agent',
    description='Tells the current time in a city',
    instruction='You are a helpful agent that tells the current time in a city. Use the tool "get current time " for this purpose and to execute your task skillfully . Refuse to answer literally anything else.',
    tools = [get_current_time],
)


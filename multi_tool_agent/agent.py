import datetime 
from zoneinfo import ZoneInfo
from google.adk.agents import Agent 

def get_weather(city: str) ->dict:
    if city.lower() == "new york":
        return{
            "status": "success",
            "report":(
                "The weather in New York is sunny with a temparature of 25 degrees"
                " Celsius (77 degrees Farenheit)."
            ),
        }
    else: 
        return{
            "status":"error",
            "error_message": f"Weather information for '{city}' is not available" ,
        }
def get_current_time(city:str)->dict:
    if city.lower() == "new york":
        tz_identifier = "America/New_York"
    else:
        return{
            "status":"error",
            "error_message": (
                f"Sorry, I don't have timezone information for {city},"
            ),
        }
    tx =ZoneInfo(tz_identifier)
    now = datetime.datetime.now(tx) 
    report =(
        f'The current time in {city} is {now.strftime("%Y-%M-%D %H%M:%S %Z%z")}'
    )
    return {"status":"success","report": report}

root_agent = Agent(
    name="weather_time_agent",
    model = "gemini-flash-latest",
    description=(
        "Agent to answer questions about the time and weather in a city"
    ),
    instruction= (
        "You are a helpful agent who can answer user questions about the time and weather in a city. Don't answer literally anything else"
    ),
    tools=[get_weather, get_current_time],
)
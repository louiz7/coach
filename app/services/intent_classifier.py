import httpx
from app.config import settings


INTENTS = [
    "PROGRESS_LOG",       # User is logging a workout / exercise / weight
    "MODIFY_PLAN",        # User wants to permanently change their plan structure/exercises
    "VIEW_PLAN",          # User wants to see / retrieve their existing plan
    "NEW_PLAN",           # User wants a completely new plan built from scratch
    "STREAK_CHECK",       # User asks about their streak / consistency
    "WHOOP_DATA",         # User asks about their WHOOP / recovery / HRV / sleep data
    "CONNECT_WHOOP",      # User wants to connect / link / add their WHOOP device
    "EXERCISE_QUESTION",  # How-to, form, technique, exercise science
    "NUTRITION_QUESTION", # Diet, macros, food, supplements
    "PERFORMANCE_DATA",   # User asks what weight/reps they used before, personal records
    "CALENDAR_LINK",      # User wants to add their plan to their phone calendar
    "FOOD_LOG",           # User sends a food photo for calorie analysis
    "GENERAL",            # Everything else
]

SYSTEM_PROMPT = """Classify the user message into one OR MORE intent categories.
A message may have multiple intents. Reply ONLY with a comma-separated list of matching categories.

Categories and rules:

PROGRESS_LOG — user is logging completed exercise, sets, reps, weight, distance, or time.
  YES: "did 5x5 bench at 100kg", "ran 5k today", "finished my workout",
       "about 80 in 10 rep sessions", "8x10 pull ups", "managed 80 pull ups",
       "just did legs", "completed my session", "hit the gym", "did chest today",
       "I did X sets of Y", "X reps of Y exercise", any report of completed physical activity,
       "just finished", "done with my workout", "trained today", "went for a run",
       "lifted today", "smashed my session", "got my workout in", "did [exercise] today",
       numbers + exercise name with no question mark (e.g. "80 pull ups", "100kg squat", "5k in 25min")
  NO: asking about how to do an exercise, asking what to do today, future tense plans

MODIFY_PLAN — user wants to permanently change something in their training plan.
  YES: "swap squats for leg press", "add more cardio", "I want weight and rep tracking",
       "customize the plan", "make it harder", "add a rest day", "remove deadlifts",
       "change the exercises", "I don't like X", "can you add X", "not listening" (if prior context shows plan frustration),
       "include specific weights", "more sets", "fewer reps"
  NO: "how do I do squats" (EXERCISE_QUESTION), "I'm tired today" (GENERAL)

VIEW_PLAN — user wants to see/retrieve their existing plan.
  YES: "show me my plan", "what's my workout today", "send me the plan", "what day is it"
  NO: user wants to change it (MODIFY_PLAN)

NEW_PLAN — user wants a brand new plan built from scratch.
  YES: "build me a new plan", "start over", "create a plan for me"

STREAK_CHECK — user asks about consistency, streak, how often they've trained.

WHOOP_DATA — user asks about recovery score, HRV, sleep, biometrics, WHOOP stats.

CONNECT_WHOOP — user wants to connect, link, add, or set up their WHOOP device/account.
  YES: "connect my whoop", "link whoop", "add whoop", "how do I connect whoop", "whoop connect", "set up whoop"
  NO: asking about WHOOP data they already have (WHOOP_DATA)

EXERCISE_QUESTION — form, technique, how to do an exercise, which exercise is best for X.

NUTRITION_QUESTION — diet, macros, protein, food, supplements, calories.

PERFORMANCE_DATA — user asks about their own previous lifts, weights used, personal records, or workout history.
  YES: "what did I bench last time", "how much was I squatting", "what weight did I use", "what are my numbers",
       "what's my max", "check my history", "how have I been progressing", "what did I lift last week"
  NO: logging a new workout (PROGRESS_LOG), asking how to improve technique (EXERCISE_QUESTION)

CALENDAR_LINK — user wants to add their training plan to their phone / Apple / Google calendar.
  YES: "add to calendar", "sync to calendar", "calendar link", "put it in my calendar",
       "subscribe to calendar", "add workouts to calendar", "calendar integration", "ics",
       "how do I add it to my calendar", "can you send the calendar link"
  NO: asking about their schedule or plan content (VIEW_PLAN)

FOOD_LOG — user sends a food photo, asks to track calories, or asks if calorie tracking is possible.
  YES: message starts with [USER SENT AN IMAGE ATTACHMENT], "what's the calorie count", "how many calories is this",
       "log my meal", "track my food", "calorie check", "can I track my calories",
       "can you track calories", "can you track them", "track calories with you",
       "do you track food", "can I log food", "do you track what I eat", "calorie tracking",
       "analyze this", "what's in this", "how many calories", "is this healthy", "what did I eat",
       any message where the user seems to be asking about the calorie content of something they just sent
  NO: general nutrition questions about macros, supplements, or diet advice without a specific item to analyse (NUTRITION_QUESTION)

GENERAL — anything else: motivation, feelings, general chat, frustration not about the plan.

CONTEXT NOTE: If the conversation history shows the assistant just updated a plan, and the user
expresses dissatisfaction ("you not listening", "that's not what I meant", "still wrong") —
classify as MODIFY_PLAN.

Example output: PROGRESS_LOG, EXERCISE_QUESTION"""


async def classify_intents(
    text: str,
    context_messages: list[dict] | None = None,
) -> list[str]:
    """Return a list of intent strings for the given user message.

    Args:
        text: The current user message.
        context_messages: Optional list of recent conversation turns
            (OpenAI message format: [{"role": "user"|"assistant", "content": "..."}])
            to give the classifier conversational context.
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if context_messages:
        messages.extend(context_messages[-4:])  # last 2 turns (4 messages)
    messages.append({"role": "user", "content": text})

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.OPENROUTER_API_KEY}"},
                json={
                    "model": "deepseek/deepseek-v4-flash",
                    "messages": messages,
                    "max_tokens": 30,
                    "temperature": 0,
                },
            )
            response.raise_for_status()
            raw = response.json()["choices"][0]["message"]["content"].strip()
            intents = [i.strip() for i in raw.split(",") if i.strip() in INTENTS]
            return intents if intents else ["GENERAL"]
    except Exception:
        return ["GENERAL"]

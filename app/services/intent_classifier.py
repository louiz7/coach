import httpx
from app.config import settings


INTENTS = [
    "PROGRESS_LOG",    # User is logging a workout / exercise / weight
    "PLAN_REQUEST",    # User wants a new or modified training plan
    "STREAK_CHECK",    # User asks about their streak / consistency
    "WHOOP_DATA",      # User asks about their WHOOP / recovery / HRV / sleep data
    "EXERCISE_QUESTION",
    "NUTRITION_QUESTION",
    "GENERAL",
]

SYSTEM_PROMPT = (
    "Classify the user message into one OR MORE of these categories. "
    "A message may contain multiple intents (e.g. logging a workout AND asking a question). "
    "Reply with a comma-separated list of matching categories, nothing else. "
    "Categories: PROGRESS_LOG, PLAN_REQUEST, STREAK_CHECK, WHOOP_DATA, EXERCISE_QUESTION, "
    "NUTRITION_QUESTION, GENERAL\n"
    "WHOOP_DATA: user asks about recovery score, HRV, sleep, biometric data, WHOOP stats, or how they're feeling based on data.\n"
    "PLAN_REQUEST: user explicitly wants to SEE their plan, BUILD a new plan, or PERMANENTLY CHANGE the plan structure. "
    "Examples: 'build me a plan', 'change bench press to dumbbell press', 'add a leg day', 'show me my plan', 'what's my workout today', 'send me the full plan'.\n"
    "NOT PLAN_REQUEST: questions about progress, timeline, expectations ('when will I see results', 'how long until I'm stronger'), "
    "feeling tired today ('I don't feel well today', 'I'm tired'), asking how exercises work, nutrition questions. "
    "If the user says they feel bad/tired/sore today and want it easier TODAY — that is GENERAL, not PLAN_REQUEST. "
    "Only use PLAN_REQUEST if they clearly want to see or permanently modify the plan.\n"
    "Example: 'PROGRESS_LOG, EXERCISE_QUESTION'"
)


async def classify_intents(text: str) -> list[str]:
    """Return a list of intent strings for the given user message."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                json={
                    "model": "gpt-4o-mini",
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": text},
                    ],
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

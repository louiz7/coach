"""Seed / re-seed the single canonical Kano persona.

Idempotent — safe to re-run:
- Upserts the canonical "calm" persona (insert if missing, update prompt
  + description + reactivate if exists).
- Deactivates any persona whose name isn't in the canonical set.
- Migrates any existing user whose `persona_id` still points at a now-
  deactivated legacy persona by reassigning to the canonical one.

Run with:  python seed_personas.py
"""
import asyncio

from sqlalchemy import select, update

from app.database import async_session, engine, Base
from app.models.coach_persona import CoachPersona
from app.models.user import User


DESCRIPTIONS = {
    "calm": "Calm & supportive",
}

_OUTPUT_FORMAT = (
    "output format: when it feels natural, split your response into multiple "
    "short messages using [MSG] as a separator. think of it like double or "
    "triple texting. The way a real person would send two quick thoughts "
    "instead of one long message. not every response needs a split. use it "
    "when there's a natural pause, a shift in thought, or when a punchline "
    "lands better alone."
)

PERSONAS = [
    {
        "name": "calm",
        "system_prompt": f"""identity: you are Kano, a personal coach for training, nutrition, recovery, and performance. not an assistant, not an app. a coach with real opinions and the expectation that users make actual progress. you give concrete recommendations, you don't dodge, and you correct people when they're wrong. direct, but never dramatic. you communicate with your users via iMessage. keep that in mind: short, human, no formatting tricks.

style & personality: calm, steady, grounding. you make the user feel like they have a plan and someone in their corner. no pressure, no panic, no guilt-tripping. measured tone, clear guidance. you acknowledge where the user is without being soft on what they need to do. no fluff, no hedging, no filler. never open with "of course!", "great question!", or any filler phrase. no therapy-speak, no AI disclaimers.

{_OUTPUT_FORMAT}

few-shot examples:

User: i skipped the gym today
Kano: happens. just don't let it become a pattern [MSG] what does tomorrow look like for you?

User: i'm not seeing results and i'm getting frustrated
Kano: frustration usually means the effort is there but something in the system is off [MSG] let's figure out what [MSG] how's your sleep been this week?

User: should i take creatine?
Kano: yes, it's one of the few supplements with solid evidence behind it. creatine monohydrate, 3-5g daily, no loading needed.

User: i feel like i'm not making progress
Kano: progress is often slower than it feels. what were you lifting three months ago compared to now?""",
    },
]

CANONICAL_NAMES = {p["name"] for p in PERSONAS}


async def seed():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session() as db:
        # 1. Upsert each canonical persona by name.
        name_to_id: dict[str, str] = {}
        for spec in PERSONAS:
            name = spec["name"]
            description = DESCRIPTIONS[name]
            res = await db.execute(
                select(CoachPersona).where(CoachPersona.name == name)
            )
            existing = res.scalar_one_or_none()
            if existing:
                existing.system_prompt = spec["system_prompt"]
                existing.description = description
                existing.is_active = True
                name_to_id[name] = str(existing.id)
                action = "updated"
            else:
                new = CoachPersona(
                    name=name,
                    description=description,
                    system_prompt=spec["system_prompt"],
                    avatar_url="",
                    is_active=True,
                )
                db.add(new)
                await db.flush()  # populate new.id
                name_to_id[name] = str(new.id)
                action = "inserted"
            print(f"  {action}: {name}")

        # 2. Deactivate any persona whose name isn't canonical (keeps the row
        #    so users with FK references stay valid; they'll be migrated below).
        res = await db.execute(
            select(CoachPersona).where(
                ~CoachPersona.name.in_(CANONICAL_NAMES),
                CoachPersona.is_active == True,  # noqa: E712
            )
        )
        legacy = res.scalars().all()
        legacy_ids = [p.id for p in legacy]
        for p in legacy:
            p.is_active = False
            print(f"  deactivated legacy: {p.name}")

        # 3. Migrate users whose persona_id still points at a deactivated
        #    legacy persona. We now have a single canonical persona, so all
        #    stale users get reassigned to it.
        if legacy_ids:
            canonical_id = name_to_id["calm"]
            res = await db.execute(
                select(User).where(User.persona_id.in_(legacy_ids))
            )
            stale_users = res.scalars().all()
            for u in stale_users:
                u.persona_id = canonical_id
            if stale_users:
                print(f"  migrated {len(stale_users)} user(s) to 'calm' persona")

        await db.commit()
        print(f"\nDone. {len(PERSONAS)} canonical personas active.")


if __name__ == "__main__":
    asyncio.run(seed())

# Prompt for twinnn Agent: Persistent Project Tagging

We share a Linq phone number (`+1 (945) 394-3079`) between two projects: **twinnn** and **hercules**. Both projects receive all incoming messages via separate webhooks on the same VPS.

We already filter out initial "whats hercules" messages in twinnn, but now there's a second problem: **when a user is onboarding on Hercules and sends follow-up messages** (like their name, goal, or "yes"), **twinnn also replies** because twinnn still has that user in its own DB and processes every message from that phone number.

We need a **persistent project tagging system** in twinnn so that once a user is claimed by Hercules, twinnn ignores all their future messages.

---

## What needs to be done

### 1. Add a `project` column to the `users` (or equivalent) table

- Column: `project` (VARCHAR or similar text column)
- Default value: `"twinnn"` (so existing users keep working as before)
- Possible values: `"twinnn"`, `"hercules"`, `"unknown"`
- Create an Alembic migration for this change

### 2. Update the message worker to filter by project

At the **very top** of the message worker function `process_message()` (or equivalent), add this logic **before any DB writes, LLM calls, or response logic**:

```python
# Step 1: Check if this is a Hercules init message
text_lower = text.lower().strip()
is_hercules_init = any(phrase in text_lower for phrase in [
    "whats hercules", "what's hercules", "what is hercules"
])

# Step 2: If it's a Hercules init → tag the user as Hercules in our DB and stop
if is_hercules_init:
    # Look up or find user by phone/chat_id
    user = await get_user_by_chat_or_phone(db, chat_id, phone)
    if user:
        user.project = "hercules"
        await db.commit()
    return  # Do not process further — Hercules handles it

# Step 3: If user already tagged as Hercules → ignore everything
user = await get_user_by_chat_or_phone(db, chat_id, phone)
if user and user.project == "hercules":
    return  # User belongs to Hercules, silently ignore
```

### 3. Apply the migration on the VPS

After deploying:

```sh
docker compose exec -T api alembic upgrade head
```

### 4. Manually tag the existing test user

Since the Hercules user already exists in twinnn's DB (phone `+4917645997563`), manually update it:

```sh
docker compose exec -T db psql -U <user> -d <dbname> -c "UPDATE users SET project='hercules' WHERE phone='+4917645997563';"
```

---

## Important rules

- The `is_hercules_init` check must happen **before** any DB writes or LLM calls
- The `project == "hercules"` check must happen **before** any response logic
- Do NOT delete the existing user — just set their `project` to `"hercules"`
- Default project for all new users must be `"twinnn"` so existing behavior doesn't break
- Deploy and test by sending a "whats hercules?" message followed by normal chat — twinnn should stay silent throughout

# =============================================================================
# UPSC SAMURAI BOT — gemini.py
# =============================================================================
# Handles all Gemini API communication for question generation.
#
# Responsibilities:
#   1. Select subject, topic, difficulty, question type (weighted random)
#   2. Build the full prompt using prompts.py
#   3. Call Gemini 1.5 Flash API
#   4. Parse and validate the JSON response
#   5. Retry once on failure — silently skip if retry also fails
#   6. Log every successfully generated question to SQLite
#   7. Provide recent question context to avoid repeats
#
# API used: Gemini 1.5 Flash (free tier)
#   - 15 requests per minute
#   - 1 million tokens per day
#   - More than sufficient for a quiz bot at this scale
#
# Setup:
#   pip install google-generativeai
#   Set environment variable: GEMINI_API_KEY=your_key_here
# =============================================================================

from __future__ import annotations

import os
import json
import random
import logging
import sqlite3
import time
from datetime import datetime
from typing import Optional

import google.generativeai as genai

from prompts import (
    MENTOR_PERSONA,
    QUESTION_GENERATION_PROMPT,
    EXPLANATION_DELIVERY_PROMPT,
    SUBJECT_TOPIC_MAP,
    SUBJECT_WEIGHTS,
    DIFFICULTY_WEIGHTS,
    QUESTION_TYPE_WEIGHTS_DEFAULT,
    QUESTION_TYPE_WEIGHTS_RECENT,
    QUESTION_TYPE_WEIGHTS_CLASSIC,
    QUESTION_TYPE_WEIGHTS_BALANCED,
    ERA_DESCRIPTIONS,
    DEFAULT_ERA,
)

# -----------------------------------------------------------------------------
# LOGGING SETUP
# -----------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# GEMINI CLIENT SETUP
# -----------------------------------------------------------------------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise EnvironmentError(
        "GEMINI_API_KEY environment variable is not set. "
        "Get your key from https://aistudio.google.com and set it in Railway."
    )

genai.configure(api_key=GEMINI_API_KEY)

# Using Gemini 3 Flash Preview — fast, low-cost, ideal for structured JSON output
GEMINI_MODEL = "gemini-3-flash-preview"

# Generation config — low temperature for factual accuracy, JSON output
GENERATION_CONFIG = genai.types.GenerationConfig(
    temperature=0.7,        # Low enough for factual accuracy, high enough for variety
    top_p=0.9,
    top_k=40,
    max_output_tokens=8192, # Large enough for question + full explanation JSON
)

# Safety settings — relaxed for educational content
SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]

# -----------------------------------------------------------------------------
# DATABASE SETUP
# Questions are logged per group_id to prevent repetition.
# Schema: question_log table stores every successfully generated question.
# -----------------------------------------------------------------------------
DB_PATH = os.environ.get("DB_PATH", "upsc_samurai.db")

def init_db():
    """
    Initialise the SQLite database.
    Creates all required tables if they don't exist.
    Call this once at bot startup.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Question log — one row per generated question per group
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS question_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id        TEXT NOT NULL,
            subject         TEXT NOT NULL,
            topic           TEXT NOT NULL,
            question_text   TEXT NOT NULL,
            correct_option  TEXT NOT NULL,
            question_type   TEXT NOT NULL,
            difficulty      TEXT NOT NULL,
            era             TEXT NOT NULL,
            generated_at    TEXT NOT NULL,
            poll_message_id TEXT,
            explanation_sent INTEGER DEFAULT 0
        )
    """)

    # Group settings — admin-configured options per group
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS group_settings (
            group_id    TEXT PRIMARY KEY,
            subject     TEXT DEFAULT 'auto',
            topic       TEXT DEFAULT 'auto',
            difficulty  TEXT DEFAULT 'auto',
            era         TEXT DEFAULT 'default',
            schedule    TEXT DEFAULT NULL,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()
    logger.info("Database initialised at %s", DB_PATH)


def get_group_settings(group_id: str) -> dict:
    """
    Fetch the current settings for a group.
    Returns defaults if group has no settings yet.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT subject, topic, difficulty, era, schedule "
        "FROM group_settings WHERE group_id = ?",
        (group_id,)
    )
    row = cursor.fetchone()
    conn.close()

    if row:
        return {
            "subject": row[0],
            "topic": row[1],
            "difficulty": row[2],
            "era": row[3],
            "schedule": row[4],
        }
    # Defaults for a new group
    return {
        "subject": "auto",
        "topic": "auto",
        "difficulty": "auto",
        "era": DEFAULT_ERA,
        "schedule": None,
    }


def save_group_settings(group_id: str, **kwargs):
    """
    Save or update settings for a group.
    Only updates the fields passed as kwargs.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now = datetime.utcnow().isoformat()

    # Upsert — insert if not exists, update if exists
    cursor.execute(
        "INSERT INTO group_settings (group_id, created_at, updated_at) "
        "VALUES (?, ?, ?) ON CONFLICT(group_id) DO UPDATE SET updated_at = ?",
        (group_id, now, now, now)
    )

    # Update only the fields that were passed
    allowed_fields = {"subject", "topic", "difficulty", "era", "schedule"}
    for field, value in kwargs.items():
        if field in allowed_fields:
            cursor.execute(
                f"UPDATE group_settings SET {field} = ? WHERE group_id = ?",
                (value, group_id)
            )

    conn.commit()
    conn.close()


def get_recent_questions(group_id: str, limit: int = 20) -> list[str]:
    """
    Fetch the question text of the last N questions asked in a group.
    Passed to Gemini as context to avoid generating similar questions.
    Returns a list of question opening lines (first 120 chars each).
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT question_text FROM question_log "
        "WHERE group_id = ? "
        "ORDER BY generated_at DESC LIMIT ?",
        (group_id, limit)
    )
    rows = cursor.fetchall()
    conn.close()
    # Return just the first 120 characters of each question to keep prompt lean
    return [row[0][:120] for row in rows]


def log_question(group_id: str, question_data: dict, poll_message_id: str = None):
    """
    Log a successfully generated question to the database.
    Called immediately after Gemini returns a valid question.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO question_log
           (group_id, subject, topic, question_text, correct_option,
            question_type, difficulty, era, generated_at, poll_message_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            group_id,
            question_data.get("subject", ""),
            question_data.get("topic", ""),
            question_data.get("question", ""),
            question_data.get("correct_option", ""),
            question_data.get("question_type", ""),
            question_data.get("difficulty", ""),
            question_data.get("era", ""),
            datetime.utcnow().isoformat(),
            poll_message_id,
        )
    )
    conn.commit()
    conn.close()
    logger.info(
        "Logged question for group %s | Subject: %s | Topic: %s",
        group_id,
        question_data.get("subject"),
        question_data.get("topic"),
    )


def mark_explanation_sent(group_id: str, question_text: str):
    """
    Mark that the explanation for a question has been sent.
    Prevents double-posting explanations if the bot restarts mid-session.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE question_log SET explanation_sent = 1 "
        "WHERE group_id = ? AND question_text = ?",
        (group_id, question_text)
    )
    conn.commit()
    conn.close()


# -----------------------------------------------------------------------------
# WEIGHTED RANDOM SELECTION HELPERS
# -----------------------------------------------------------------------------

def weighted_choice(weights_dict: dict) -> str:
    """
    Select a key from a dictionary based on weighted probabilities.
    Example: {"HOW_MANY": 35, "MULTI_STATEMENT": 30} → picks HOW_MANY 35% of the time.
    """
    keys = list(weights_dict.keys())
    weights = list(weights_dict.values())
    return random.choices(keys, weights=weights, k=1)[0]


def select_subject(group_subject: str) -> str:
    """
    Pick the subject for question generation.
    If admin has set a specific subject, use that.
    Otherwise pick randomly based on SUBJECT_WEIGHTS (15-year UPSC distribution).
    """
    if group_subject != "auto":
        return group_subject
    return weighted_choice(SUBJECT_WEIGHTS)


def select_topic(subject: str, group_topic: str) -> str:
    """
    Pick the topic within the selected subject.
    If admin has set a specific topic, use that.
    Otherwise pick randomly from the subject's topic list.
    """
    if group_topic != "auto":
        return group_topic
    topics = SUBJECT_TOPIC_MAP.get(subject, [])
    if not topics:
        return "General"
    return random.choice(topics)


def select_difficulty(group_difficulty: str) -> str:
    """
    Pick the difficulty level.
    If admin has set a specific difficulty, use that.
    Otherwise pick based on DIFFICULTY_WEIGHTS (current era defaults).
    """
    if group_difficulty != "auto":
        return group_difficulty.upper()
    return weighted_choice(DIFFICULTY_WEIGHTS)


def select_question_type(era: str) -> str:
    """
    Pick the question type based on the group's era setting.
    Maps era label to the correct weight dictionary.
    Default uses the 75/25 recent-to-classic blend.
    """
    era_map = {
        "default": QUESTION_TYPE_WEIGHTS_DEFAULT,        # 75% recent + 25% classic
        "recent": QUESTION_TYPE_WEIGHTS_RECENT,          # 100% modern UPSC
        "how_many": QUESTION_TYPE_WEIGHTS_RECENT,        # alias → modern UPSC
        "assertion_reasoning": QUESTION_TYPE_WEIGHTS_RECENT,  # alias → modern UPSC
        "elimination": QUESTION_TYPE_WEIGHTS_CLASSIC,    # 100% classic
        "balanced": QUESTION_TYPE_WEIGHTS_BALANCED,      # raw 15-year distribution
    }
    weights = era_map.get(era, QUESTION_TYPE_WEIGHTS_DEFAULT)
    return weighted_choice(weights)


def determine_era_style(question_type: str, era_setting: str) -> str:
    """
    Determine which era FORMAT to use in the prompt.
    HOW_MANY → how_many format
    ASSERTION_REASONING → assertion_reasoning format
    MULTI_STATEMENT → depends on era setting
    Others → elimination (classic options)
    """
    if question_type == "HOW_MANY":
        return "how_many"
    if question_type == "ASSERTION_REASONING":
        return "assertion_reasoning"
    if question_type == "MULTI_STATEMENT":
        # In recent/default era, use how_many for multi-statement too sometimes
        if era_setting in ("recent", "default") and random.random() < 0.4:
            return "how_many"
        return "elimination"
    return "elimination"


# -----------------------------------------------------------------------------
# PROMPT BUILDER
# -----------------------------------------------------------------------------

def build_generation_prompt(
    subject: str,
    topic: str,
    difficulty: str,
    question_type: str,
    era_style: str,
    avoid_questions: list[str],
) -> str:
    """
    Assemble the full question generation prompt.
    Injects all runtime variables into the QUESTION_GENERATION_PROMPT template.
    """
    # Format the avoid list — keep it concise to save tokens
    if avoid_questions:
        avoid_text = "\n".join(f"- {q}" for q in avoid_questions[:15])
    else:
        avoid_text = "None — this is the first question for this group."

    return QUESTION_GENERATION_PROMPT.format(
        persona=MENTOR_PERSONA,
        subject=subject,
        topic=topic,
        difficulty=difficulty,
        question_type=question_type,
        era=era_style,
        avoid_questions=avoid_text,
    )


def build_explanation_prompt(question_data: dict) -> str:
    """
    Build the explanation delivery prompt.
    Used after a poll closes to generate the mentor explanation message.
    """
    return EXPLANATION_DELIVERY_PROMPT.format(
        persona=MENTOR_PERSONA,
        question_json=json.dumps(question_data, indent=2),
    )


# -----------------------------------------------------------------------------
# GEMINI API CALLER
# -----------------------------------------------------------------------------

def _call_gemini(prompt: str) -> Optional[str]:
    """
    Make a single API call to Gemini 1.5 Flash.
    Returns the raw text response, or None if it fails.
    Internal function — use generate_question() externally.
    """
    try:
        model = genai.GenerativeModel(
            model_name=GEMINI_MODEL,
            generation_config=GENERATION_CONFIG,
            safety_settings=SAFETY_SETTINGS,
        )
        response = model.generate_content(prompt)

        # Check if response was blocked by safety filters
        if not response.candidates:
            logger.warning("Gemini returned no candidates — likely safety filter triggered.")
            return None

        text = response.text.strip()
        if not text:
            logger.warning("Gemini returned empty text.")
            return None

        return text

    except Exception as e:
        logger.error("Gemini API call failed: %s", str(e))
        return None


def _parse_question_json(raw_text: str) -> Optional[dict]:
    """
    Parse and validate the JSON response from Gemini.
    Gemini sometimes wraps JSON in markdown fences — this strips them.
    Returns the parsed dict if valid, None if invalid.
    """
    try:
        text = raw_text.strip()

        # Strip markdown code fences if present
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1]).strip()
            if text.endswith("```"):
                text = text[:-3].strip()

        # Extract the JSON object — find first { and last } in case of extra text
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start:end + 1]

        data = json.loads(text)

        # Validate required fields
        required = ["question", "options", "correct_option", "explanation"]
        for field in required:
            if field not in data:
                logger.warning("Missing required field in Gemini response: %s", field)
                return None

        # Validate options count
        if len(data.get("options", [])) != 4:
            logger.warning("Gemini returned wrong number of options: %d", len(data.get("options", [])))
            return None

        # Validate correct_option
        if data.get("correct_option", "").lower() not in ["a", "b", "c", "d"]:
            logger.warning("Invalid correct_option value: %s", data.get("correct_option"))
            return None

        return data

    except json.JSONDecodeError as e:
        logger.error("Failed to parse Gemini JSON response: %s", str(e))
        logger.debug("Raw response was: %s", raw_text[:500])
        return None


# -----------------------------------------------------------------------------
# MAIN PUBLIC FUNCTIONS
# -----------------------------------------------------------------------------

def generate_question(group_id: str) -> Optional[dict]:
    """
    Generate a UPSC Prelims question for a specific Telegram group.

    Flow:
    1. Load group settings from DB
    2. Select subject, topic, difficulty, question type (weighted random)
    3. Fetch recent questions to avoid repeats
    4. Build prompt and call Gemini
    5. Retry once if first call fails or returns invalid JSON
    6. Log successful question to DB
    7. Return question dict, or None if both attempts fail

    Returns:
        dict with keys: subject, topic, difficulty, question_type, era,
                        question, statements, options, correct_option,
                        explanation (nested dict)
        None if generation failed after retry
    """
    settings = get_group_settings(group_id)

    # Select parameters
    subject = select_subject(settings["subject"])
    topic = select_topic(subject, settings["topic"])
    difficulty = select_difficulty(settings["difficulty"])
    question_type = select_question_type(settings["era"])
    era_style = determine_era_style(question_type, settings["era"])

    # Get recent questions for this group to build avoid context
    recent = get_recent_questions(group_id, limit=20)

    # Build the prompt
    prompt = build_generation_prompt(
        subject=subject,
        topic=topic,
        difficulty=difficulty,
        question_type=question_type,
        era_style=era_style,
        avoid_questions=recent,
    )

    logger.info(
        "Generating question for group %s | %s > %s | %s | %s | %s",
        group_id, subject, topic, difficulty, question_type, era_style
    )

    # Attempt 1
    raw = _call_gemini(prompt)
    question_data = _parse_question_json(raw) if raw else None

    # Retry once if first attempt failed
    if question_data is None:
        logger.warning("First attempt failed for group %s. Retrying in 3 seconds...", group_id)
        time.sleep(3)  # Brief pause before retry to avoid rate limiting
        raw = _call_gemini(prompt)
        question_data = _parse_question_json(raw) if raw else None

    # Both attempts failed — silently skip (caller handles the silence)
    if question_data is None:
        logger.error(
            "Both attempts failed for group %s. Silently skipping this post.",
            group_id
        )
        return None

    # Enrich the response with the selected parameters
    # (Gemini should return these but we enforce them to be safe)
    question_data["subject"] = question_data.get("subject", subject)
    question_data["topic"] = question_data.get("topic", topic)
    question_data["difficulty"] = question_data.get("difficulty", difficulty)
    question_data["question_type"] = question_data.get("question_type", question_type)
    question_data["era"] = question_data.get("era", era_style)

    # Log to database
    log_question(group_id, question_data)

    return question_data


def generate_explanation(question_data: dict) -> Optional[str]:
    """
    Generate a mentor-style explanation for a completed poll.

    Called after a Telegram poll closes.
    Uses the explanation prompt from prompts.py.

    Returns:
        str — the formatted Telegram message (under 900 chars)
        None if generation failed
    """
    prompt = build_explanation_prompt(question_data)

    raw = _call_gemini(prompt)
    if not raw:
        logger.warning("Failed to generate explanation. Returning fallback.")
        return _fallback_explanation(question_data)

    # Explanation is plain text, not JSON — return as-is after cleanup
    return raw.strip()


def _fallback_explanation(question_data: dict) -> str:
    """
    Minimal fallback explanation when Gemini fails.
    Ensures the bot always posts something after a poll closes.
    """
    correct = question_data.get("correct_option", "?").upper()
    explanation = question_data.get("explanation", {})
    reason = explanation.get("correct_answer_reason", "Explanation unavailable.")
    concept = explanation.get("concept_to_remember", "")

    text = f"Answer: ({correct})\n\n{reason}"
    if concept:
        text += f"\n\nKey concept: {concept}"
    return text


def format_question_for_telegram(question_data: dict) -> tuple[str, str, list[str], int]:
    """
    Convert the question dict into a text message + Telegram Poll.

    Telegram quiz polls have a 300-char question limit, so we split into:
    - A full text message (sent first, no limit) — contains header + question + statements
    - A short poll question (just the question stem, trimmed to 300 chars)

    Returns:
        tuple of:
        - question_message (str): Full question to send as a text message before the poll
        - poll_question (str): Short poll question text (max 300 chars)
        - options (list[str]): List of 4 option strings (without the (a)(b)(c)(d) prefix)
        - correct_option_index (int): 0-indexed correct answer (0=a, 1=b, 2=c, 3=d)
    """
    subject = question_data.get("subject", "").replace("_", " ").title()
    difficulty = question_data.get("difficulty", "")
    question = question_data.get("question", "")
    statements = question_data.get("statements", [])

    # Full text message — sent before the poll, no character limit
    header = f"[{subject} | {difficulty}]"
    if statements:
        statements_text = "\n".join(statements)
        question_message = f"{header}\n\n{question}\n\n{statements_text}"
    else:
        question_message = f"{header}\n\n{question}"

    # Poll question — just the stem, trimmed to Telegram's 300-char limit
    poll_question = question.strip()
    if len(poll_question) > 300:
        poll_question = poll_question[:297] + "..."

    # Strip the (a) (b) (c) (d) prefix from options
    raw_options = question_data.get("options", [])
    clean_options = []
    for opt in raw_options:
        opt = opt.strip()
        if len(opt) > 3 and opt[0] == "(" and opt[2] == ")":
            opt = opt[4:].strip()
        clean_options.append(opt[:100])  # Telegram option limit: 100 chars

    # Convert letter to 0-based index
    letter = question_data.get("correct_option", "a").lower()
    index_map = {"a": 0, "b": 1, "c": 2, "d": 3}
    correct_index = index_map.get(letter, 0)

    return question_message, poll_question, clean_options, correct_index


# -----------------------------------------------------------------------------
# QUICK TEST — run this file directly to test question generation
# Usage: python gemini.py
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s"
    )

    print("\n" + "="*60)
    print("UPSC SAMURAI — Gemini Module Test")
    print("="*60)

    # Init DB
    init_db()

    # Use a test group ID
    TEST_GROUP_ID = "test_group_001"

    print(f"\nGenerating a test question for group: {TEST_GROUP_ID}")
    print("This will use your GEMINI_API_KEY from environment.\n")

    question = generate_question(TEST_GROUP_ID)

    if question:
        print("SUCCESS — Question generated:\n")
        print(f"Subject:  {question.get('subject')}")
        print(f"Topic:    {question.get('topic')}")
        print(f"Type:     {question.get('question_type')}")
        print(f"Era:      {question.get('era')}")
        print(f"Difficulty: {question.get('difficulty')}")
        print(f"\nQuestion:\n{question.get('question')}")

        statements = question.get("statements", [])
        if statements:
            print("\nStatements:")
            for s in statements:
                print(f"  {s}")

        print(f"\nOptions:")
        for opt in question.get("options", []):
            print(f"  {opt}")

        print(f"\nCorrect: ({question.get('correct_option').upper()})")

        exp = question.get("explanation", {})
        print(f"\nExplanation: {exp.get('correct_answer_reason', '')[:200]}")
        print(f"Trap type: {exp.get('trap_type', 'NONE')}")
        print(f"Trap: {exp.get('trap_explanation', '')[:150]}")
        print(f"Concept: {exp.get('concept_to_remember', '')}")
        print(f"Source: {exp.get('source_hint', '')}")

        print("\n" + "-"*60)
        print("Telegram format preview:")
        q_msg, q_poll, opts, correct_idx = format_question_for_telegram(question)
        print(f"\nText Message:\n{q_msg}")
        print(f"\nPoll Question (trimmed):\n{q_poll}")
        print(f"\nPoll Options:")
        for i, opt in enumerate(opts):
            marker = " ← CORRECT" if i == correct_idx else ""
            print(f"  {i+1}. {opt}{marker}")

        print("\n" + "-"*60)
        print("Generating explanation message...")
        explanation_msg = generate_explanation(question)
        print(f"\nExplanation message:\n{explanation_msg}")

    else:
        print("FAILED — No question generated. Check your GEMINI_API_KEY and logs.")
        sys.exit(1)

    print("\n" + "="*60)
    print("Test complete. gemini.py is working correctly.")
    print("="*60 + "\n")

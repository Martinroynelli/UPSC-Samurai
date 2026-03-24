# =============================================================================
# UPSC SAMURAI BOT — bot.py
# =============================================================================
# Main entry point. Handles:
#   - Bot startup and shutdown
#   - Admin command handlers (/quiz, /setsubject, /settopic, etc.)
#   - Poll lifecycle (post quiz poll → wait 60s → post explanation)
#   - Scheduled daily auto-posting per group
#   - Admin permission checking
#
# Library: python-telegram-bot v20+ (async)
# Install: pip install python-telegram-bot apscheduler
#
# Environment variables required:
#   TELEGRAM_TOKEN   — from BotFather
#   GEMINI_API_KEY   — from Google AI Studio
#   DB_PATH          — optional, defaults to upsc_samurai.db
#
# Run locally:  python bot.py
# Deploy:       Railway reads Procfile → python bot.py
# =============================================================================

from __future__ import annotations

import os
import logging
import asyncio
from datetime import datetime

from telegram import (
    Update,
    Poll,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    PollAnswerHandler,
    ContextTypes,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from gemini import (
    init_db,
    generate_question,
    generate_explanation,
    format_question_for_telegram,
    get_group_settings,
    save_group_settings,
    get_recent_questions,
    mark_explanation_sent,
)
from prompts import (
    SUBJECT_TOPIC_MAP,
    MSG_WELCOME,
    MSG_ADMIN_ONLY,
    MSG_SETTINGS_DISPLAY,
    MSG_QUIZ_GENERATING,
    MSG_QUIZ_ERROR,
    MSG_VALID_SUBJECTS,
    MSG_VALID_ERAS,
)

# -----------------------------------------------------------------------------
# LOGGING
# -----------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# CONSTANTS
# -----------------------------------------------------------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise EnvironmentError(
        "TELEGRAM_TOKEN environment variable is not set. "
        "Get your token from @BotFather and set it in Railway."
    )

EXPLANATION_FALLBACK_DELAY = 300  # Seconds before fallback explanation if nobody answers

# In-memory store: poll_id → question_data + group_id
# Used to match a closed poll to its question for explanation posting
active_polls: dict[str, dict] = {}

# Scheduler for daily auto-posts
scheduler = AsyncIOScheduler()


# -----------------------------------------------------------------------------
# ADMIN CHECK
# -----------------------------------------------------------------------------

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Check if the user who sent the command is a group admin or creator.
    Also returns True if the command is sent in a private chat (DM).
    """
    chat = update.effective_chat
    user = update.effective_user

    # Allow all commands in private chat
    if chat.type == "private":
        return True

    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in ("administrator", "creator")
    except Exception as e:
        logger.error("Admin check failed: %s", str(e))
        return False


# -----------------------------------------------------------------------------
# CORE QUIZ POSTING FUNCTION
# -----------------------------------------------------------------------------

async def post_quiz(
    chat_id: int | str,
    context: ContextTypes.DEFAULT_TYPE,
    group_id: str,
):
    """
    The central function that generates and posts a quiz poll.
    Called by /quiz command and by the scheduler.

    Flow:
    1. Show "generating..." message
    2. Call Gemini to generate question
    3. If None returned → silently return (retry already happened in gemini.py)
    4. Format for Telegram poll
    5. Post the poll (quiz type, 60s timer, anonymous=False)
    6. Store poll in active_polls dict
    7. Schedule explanation to post after 65 seconds
    """
    # Show typing indicator
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    # Generating message — gives user feedback while Gemini works
    gen_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=MSG_QUIZ_GENERATING,
    )

    # Generate question
    question_data = generate_question(group_id)

    # Delete the "generating..." message
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=gen_msg.message_id)
    except Exception:
        pass  # Not critical if delete fails

    # Silently skip if generation failed (retry already happened inside gemini.py)
    if question_data is None:
        logger.warning("Question generation failed for group %s. Skipping post.", group_id)
        # Only show error if this was a manual /quiz command, not scheduled
        # We can't easily distinguish here, so we send a quiet error
        await context.bot.send_message(
            chat_id=chat_id,
            text=MSG_QUIZ_ERROR,
        )
        return

    # Format for Telegram — returns full text message + short poll question
    question_message, poll_question, options, correct_index = format_question_for_telegram(question_data)

    # Post the quiz poll
    try:
        # Send full question as a text message first (no character limit)
        await context.bot.send_message(
            chat_id=chat_id,
            text=question_message,
        )

        # Send the poll with just the short question stem — no timer
        poll_message = await context.bot.send_poll(
            chat_id=chat_id,
            question=poll_question,
            options=options,
            type=Poll.QUIZ,                 # Quiz mode shows correct answer on answer
            correct_option_id=correct_index,
            is_anonymous=False,             # Non-anonymous so we can track answers
            # No open_period — poll stays open, explanation posts on first answer
        )

        # Store in active_polls so PollAnswerHandler can post explanation
        poll_id = poll_message.poll.id
        active_polls[poll_id] = {
            "question_data": question_data,
            "group_id": group_id,
            "chat_id": chat_id,
            "message_id": poll_message.message_id,
            "posted_at": datetime.utcnow().isoformat(),
            "explanation_posted": False,
        }

        logger.info(
            "Poll posted | group: %s | poll_id: %s | subject: %s | topic: %s",
            group_id,
            poll_id,
            question_data.get("subject"),
            question_data.get("topic"),
        )

        # Fallback: post explanation after 5 minutes if nobody answers
        asyncio.create_task(
            post_explanation_after_delay(
                chat_id=chat_id,
                poll_id=poll_id,
                question_data=question_data,
                context=context,
                delay=300,
                is_fallback=True,
            )
        )

    except Exception as e:
        logger.error("Failed to post poll for group %s: %s", group_id, str(e))
        await context.bot.send_message(
            chat_id=chat_id,
            text=MSG_QUIZ_ERROR,
        )


async def post_explanation_after_delay(
    chat_id: int | str,
    poll_id: str,
    question_data: dict,
    context: ContextTypes.DEFAULT_TYPE,
    delay: int,
    is_fallback: bool = False,
):
    """
    Post the mentor explanation after a delay.
    - Called immediately (delay=2) by PollAnswerHandler on first answer.
    - Called as fallback (delay=300) if nobody answers within 5 minutes.
    """
    await asyncio.sleep(delay)

    # Fallback task: skip if explanation was already posted by PollAnswerHandler
    if is_fallback:
        poll_info = active_polls.get(poll_id)
        if not poll_info or poll_info.get("explanation_posted"):
            active_polls.pop(poll_id, None)
            return

    try:
        explanation = generate_explanation(question_data)

        if explanation:
            await context.bot.send_message(
                chat_id=chat_id,
                text=explanation,
                parse_mode="Markdown",
            )
            mark_explanation_sent(
                group_id=str(chat_id),
                question_text=question_data.get("question", ""),
            )
            logger.info("Explanation posted for poll %s in chat %s", poll_id, chat_id)

        active_polls.pop(poll_id, None)

    except Exception as e:
        logger.error(
            "Failed to post explanation for poll %s in chat %s: %s",
            poll_id, chat_id, str(e)
        )


# -----------------------------------------------------------------------------
# COMMAND HANDLERS
# -----------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start — Welcome message.
    Initialises group settings if this is a new group.
    """
    chat_id = str(update.effective_chat.id)
    group_id = chat_id

    # Ensure group has default settings in DB
    save_group_settings(group_id)

    await update.message.reply_text(MSG_WELCOME)
    logger.info("/start received from group %s", group_id)


async def cmd_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /quiz — Immediately post one quiz poll.
    Admin only in groups. Anyone can use in private chat.
    """
    if not await is_admin(update, context):
        await update.message.reply_text(MSG_ADMIN_ONLY)
        return

    chat_id = update.effective_chat.id
    group_id = str(chat_id)

    await post_quiz(chat_id=chat_id, context=context, group_id=group_id)


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /settings — Show the current configuration for this group.
    """
    if not await is_admin(update, context):
        await update.message.reply_text(MSG_ADMIN_ONLY)
        return

    group_id = str(update.effective_chat.id)
    settings = get_group_settings(group_id)

    # Count today's questions
    from gemini import DB_PATH
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    cursor.execute(
        "SELECT COUNT(*) FROM question_log WHERE group_id = ? AND generated_at LIKE ?",
        (group_id, f"{today}%")
    )
    count = cursor.fetchone()[0]
    conn.close()

    msg = MSG_SETTINGS_DISPLAY.format(
        subject=settings["subject"],
        topic=settings["topic"],
        difficulty=settings["difficulty"],
        era=settings["era"],
        schedule=settings["schedule"] or "Not set",
        count=count,
    )
    await update.message.reply_text(msg)


async def cmd_setsubject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setsubject [subject] — Set the subject for quiz questions.
    Use 'auto' to let the bot choose based on UPSC weightage.

    Example: /setsubject polity
             /setsubject auto
    """
    if not await is_admin(update, context):
        await update.message.reply_text(MSG_ADMIN_ONLY)
        return

    group_id = str(update.effective_chat.id)

    if not context.args:
        await update.message.reply_text(
            "Please specify a subject.\n\n" + MSG_VALID_SUBJECTS
        )
        return

    subject = context.args[0].lower().strip()

    if subject != "auto" and subject not in SUBJECT_TOPIC_MAP:
        await update.message.reply_text(
            f"'{subject}' is not a valid subject.\n\n" + MSG_VALID_SUBJECTS
        )
        return

    save_group_settings(group_id, subject=subject, topic="auto")
    await update.message.reply_text(
        f"Subject set to: *{subject}*\nTopic reset to: *auto*\n\n"
        f"Use /settopic to narrow within this subject, or /quiz to post now.",
        parse_mode="Markdown",
    )


async def cmd_settopic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /settopic [topic] — Narrow questions to a specific topic within the subject.
    Use 'auto' to pick randomly from the subject's topic list.

    Example: /settopic Fundamental Rights
             /settopic auto
    """
    if not await is_admin(update, context):
        await update.message.reply_text(MSG_ADMIN_ONLY)
        return

    group_id = str(update.effective_chat.id)

    if not context.args:
        settings = get_group_settings(group_id)
        subject = settings.get("subject", "auto")
        if subject != "auto" and subject in SUBJECT_TOPIC_MAP:
            topics = SUBJECT_TOPIC_MAP[subject]
            topic_list = "\n".join(f"• {t}" for t in topics)
            await update.message.reply_text(
                f"Topics available for *{subject}*:\n\n{topic_list}\n\n"
                f"Use: /settopic Fundamental Rights",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                "Please set a subject first with /setsubject, "
                "then use /settopic to narrow within it."
            )
        return

    # Join all args to support multi-word topics
    topic = " ".join(context.args).strip()
    save_group_settings(group_id, topic=topic)
    await update.message.reply_text(
        f"Topic set to: *{topic}*\n\nUse /quiz to post a question now.",
        parse_mode="Markdown",
    )


async def cmd_setdifficulty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setdifficulty [easy|medium|hard|auto]
    Set the difficulty level for questions.

    Example: /setdifficulty hard
             /setdifficulty auto
    """
    if not await is_admin(update, context):
        await update.message.reply_text(MSG_ADMIN_ONLY)
        return

    group_id = str(update.effective_chat.id)

    if not context.args:
        await update.message.reply_text(
            "Please specify a difficulty.\n\n"
            "Options: easy, medium, hard, auto\n\n"
            "Example: /setdifficulty hard"
        )
        return

    difficulty = context.args[0].lower().strip()
    if difficulty not in ("easy", "medium", "hard", "auto"):
        await update.message.reply_text(
            "Invalid difficulty. Choose from: easy, medium, hard, auto"
        )
        return

    save_group_settings(group_id, difficulty=difficulty)
    await update.message.reply_text(
        f"Difficulty set to: *{difficulty}*",
        parse_mode="Markdown",
    )


async def cmd_setera(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setera [default|recent|elimination|balanced]
    Set the question format era.

    default     — 75% recent (2022-2025) + 25% classic (recommended)
    recent      — 100% modern UPSC format (HOW_MANY + ASSERTION_REASONING heavy)
    elimination — 100% classic format (2011-2021 style)
    balanced    — raw 15-year distribution

    Example: /setera default
    """
    if not await is_admin(update, context):
        await update.message.reply_text(MSG_ADMIN_ONLY)
        return

    group_id = str(update.effective_chat.id)

    if not context.args:
        await update.message.reply_text(MSG_VALID_ERAS)
        return

    era = context.args[0].lower().strip()
    valid_eras = ("default", "recent", "elimination", "balanced")

    if era not in valid_eras:
        await update.message.reply_text(
            f"Invalid era. Choose from: {', '.join(valid_eras)}\n\n" + MSG_VALID_ERAS
        )
        return

    save_group_settings(group_id, era=era)
    await update.message.reply_text(
        f"Era set to: *{era}*\n\n"
        f"Questions will now follow the *{era}* format pattern.",
        parse_mode="Markdown",
    )


async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /schedule [HH:MM] — Set a daily auto-post time (IST).
    /schedule off     — Disable scheduled posting.

    Example: /schedule 09:00
             /schedule off
    """
    if not await is_admin(update, context):
        await update.message.reply_text(MSG_ADMIN_ONLY)
        return

    group_id = str(update.effective_chat.id)
    chat_id = update.effective_chat.id

    if not context.args:
        await update.message.reply_text(
            "Please specify a time in HH:MM format (IST).\n\n"
            "Example: /schedule 09:00\n"
            "To disable: /schedule off"
        )
        return

    arg = context.args[0].strip().lower()

    # Disable scheduling
    if arg == "off":
        _remove_scheduled_job(group_id)
        save_group_settings(group_id, schedule=None)
        await update.message.reply_text(
            "Scheduled posting has been turned off for this group."
        )
        return

    # Validate HH:MM format
    try:
        hour, minute = arg.split(":")
        hour = int(hour)
        minute = int(minute)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Invalid time format. Use HH:MM (24-hour IST).\n"
            "Example: /schedule 09:00"
        )
        return

    # Schedule the job
    _remove_scheduled_job(group_id)  # Remove existing job first
    _add_scheduled_job(
        group_id=group_id,
        chat_id=chat_id,
        hour=hour,
        minute=minute,
        context=update.get_bot(),
    )

    save_group_settings(group_id, schedule=f"{hour:02d}:{minute:02d}")
    await update.message.reply_text(
        f"Daily quiz scheduled at *{hour:02d}:{minute:02d} IST* every day.\n\n"
        f"Use /schedule off to stop.",
        parse_mode="Markdown",
    )


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /pause — Stop the scheduled daily quiz for this group.
    Same as /schedule off.
    """
    if not await is_admin(update, context):
        await update.message.reply_text(MSG_ADMIN_ONLY)
        return

    group_id = str(update.effective_chat.id)
    _remove_scheduled_job(group_id)
    save_group_settings(group_id, schedule=None)
    await update.message.reply_text(
        "Scheduled posting paused. Use /schedule HH:MM to resume."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /help — Show all available commands.
    """
    help_text = (
        "*UPSC Samurai — Commands*\n\n"
        "*For Admins:*\n"
        "/quiz — Post a question now\n"
        "/settings — View current config\n"
        "/setsubject [subject] — Set subject (or 'auto')\n"
        "/settopic [topic] — Set topic within subject\n"
        "/setdifficulty [easy|medium|hard|auto]\n"
        "/setera [default|recent|elimination|balanced]\n"
        "/schedule [HH:MM] — Daily auto-post time (IST)\n"
        "/pause — Stop scheduled posting\n\n"
        "*For Everyone:*\n"
        "/help — Show this message\n\n"
        "*Subjects available:*\n"
        "polity, economy, environment, geography,\n"
        "modern\\_history, ancient\\_medieval\\_culture,\n"
        "science\\_technology, current\\_affairs\\_ir\n\n"
        "*Era modes:*\n"
        "default (75% recent + 25% classic) ← recommended\n"
        "recent, elimination, balanced"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


# -----------------------------------------------------------------------------
# POLL ANSWER HANDLER
# -----------------------------------------------------------------------------

async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Fires when a user answers the quiz poll.
    Posts the explanation immediately on the first answer received.
    Subsequent answers from other users are ignored (explanation already on its way).
    """
    poll_id = update.poll_answer.poll_id

    if poll_id not in active_polls:
        return  # Unknown poll (e.g. from a previous bot session), ignore

    poll_info = active_polls[poll_id]

    # Only the first answer triggers the explanation
    if poll_info.get("explanation_posted"):
        return

    poll_info["explanation_posted"] = True
    logger.info("First answer received for poll %s — posting explanation.", poll_id)

    asyncio.create_task(
        post_explanation_after_delay(
            chat_id=poll_info["chat_id"],
            poll_id=poll_id,
            question_data=poll_info["question_data"],
            context=context,
            delay=2,  # 2-second pause so the answer animation settles
        )
    )


# -----------------------------------------------------------------------------
# SCHEDULER HELPERS
# -----------------------------------------------------------------------------

def _get_job_id(group_id: str) -> str:
    return f"daily_quiz_{group_id}"


def _remove_scheduled_job(group_id: str):
    """Remove a group's scheduled job if it exists."""
    job_id = _get_job_id(group_id)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
        logger.info("Removed scheduled job for group %s", group_id)


def _add_scheduled_job(
    group_id: str,
    chat_id: int,
    hour: int,
    minute: int,
    context,
):
    """
    Add a daily cron job for a group.
    Runs at the specified hour:minute in IST (Asia/Kolkata).
    """
    job_id = _get_job_id(group_id)

    scheduler.add_job(
        func=_scheduled_quiz_job,
        trigger=CronTrigger(
            hour=hour,
            minute=minute,
            timezone="Asia/Kolkata",
        ),
        id=job_id,
        args=[chat_id, group_id, context],
        replace_existing=True,
        name=f"Daily quiz for group {group_id}",
    )
    logger.info(
        "Scheduled daily quiz for group %s at %02d:%02d IST",
        group_id, hour, minute
    )


async def _scheduled_quiz_job(chat_id: int, group_id: str, bot):
    """
    The actual function called by the scheduler at the set time.
    Creates a minimal context-like wrapper to reuse post_quiz().
    """
    logger.info("Running scheduled quiz for group %s", group_id)

    class _BotWrapper:
        """Minimal wrapper so post_quiz() can call context.bot.x methods."""
        def __init__(self, bot_instance):
            self.bot = bot_instance

    await post_quiz(
        chat_id=chat_id,
        context=_BotWrapper(bot),
        group_id=group_id,
    )


async def restore_schedules(application: Application):
    """
    On bot startup, restore all scheduled jobs from the database.
    This ensures schedules survive bot restarts on Railway.
    """
    from gemini import DB_PATH
    import sqlite3

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT group_id, schedule FROM group_settings "
        "WHERE schedule IS NOT NULL"
    )
    rows = cursor.fetchall()
    conn.close()

    restored = 0
    for group_id, schedule_str in rows:
        try:
            hour, minute = schedule_str.split(":")
            _add_scheduled_job(
                group_id=group_id,
                chat_id=int(group_id),
                hour=int(hour),
                minute=int(minute),
                context=application.bot,
            )
            restored += 1
        except Exception as e:
            logger.error(
                "Failed to restore schedule for group %s: %s", group_id, str(e)
            )

    logger.info("Restored %d scheduled jobs from database.", restored)


# -----------------------------------------------------------------------------
# BOT STARTUP
# -----------------------------------------------------------------------------

def main():
    """
    Build and run the bot.
    This is the only function called when you do: python bot.py
    """

    # Initialise database
    init_db()
    logger.info("UPSC Samurai starting up...")

    # Build the application
    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    # Register command handlers
    application.add_handler(CommandHandler("start",         cmd_start))
    application.add_handler(CommandHandler("help",          cmd_help))
    application.add_handler(CommandHandler("quiz",          cmd_quiz))
    application.add_handler(CommandHandler("settings",      cmd_settings))
    application.add_handler(CommandHandler("setsubject",    cmd_setsubject))
    application.add_handler(CommandHandler("settopic",      cmd_settopic))
    application.add_handler(CommandHandler("setdifficulty", cmd_setdifficulty))
    application.add_handler(CommandHandler("setera",        cmd_setera))
    application.add_handler(CommandHandler("schedule",      cmd_schedule))
    application.add_handler(CommandHandler("pause",         cmd_pause))
    application.add_handler(PollAnswerHandler(handle_poll_answer))

    # Set bot command menu (shows up in Telegram's command picker)
    async def post_init(app: Application):
        try:
            await app.bot.set_my_commands([
                BotCommand("quiz",          "Post a question now"),
                BotCommand("settings",      "View current config"),
                BotCommand("setsubject",    "Set subject"),
                BotCommand("settopic",      "Set topic within subject"),
                BotCommand("setdifficulty", "Set difficulty"),
                BotCommand("setera",        "Set question format era"),
                BotCommand("schedule",      "Set daily auto-post time"),
                BotCommand("pause",         "Stop scheduled posting"),
                BotCommand("help",          "Show all commands"),
            ])
        except Exception as e:
            logger.warning("Could not register bot commands (non-fatal): %s", e)
        # Restore scheduled jobs from DB
        await restore_schedules(app)
        # Start the scheduler
        scheduler.start()
        logger.info("UPSC Samurai is live.")

    application.post_init = post_init

    # Start polling (runs forever until Ctrl+C or Railway stops the process)
    logger.info("Starting polling...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,  # Ignore commands sent while bot was offline
    )


if __name__ == "__main__":
    main()

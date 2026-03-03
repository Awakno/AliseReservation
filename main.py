import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import awlise
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("auto_reservation")


@dataclass
class Config:
    telegram_token: str
    telegram_chat_id: int | None
    timezone: str
    reservation_time: str
    reserve_date_offset_days: int


ACTION_CACHE: dict[str, dict] = {}


def load_config() -> Config:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

    raw_chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    chat_id = int(raw_chat) if raw_chat else None

    raw_offset = int(os.getenv("RESERVE_DATE_OFFSET_DAYS", "1"))
    if raw_offset < 1:
        logger.warning(
            "RESERVE_DATE_OFFSET_DAYS=%s is invalid for policy 'min 1 day before'; forcing to 1.",
            raw_offset,
        )

    return Config(
        telegram_token=token,
        telegram_chat_id=chat_id,
        timezone=os.getenv("TIMEZONE", "Europe/Paris"),
        reservation_time=os.getenv("RESERVATION_TIME", "09:00"),
        reserve_date_offset_days=max(1, raw_offset),
    )


def call_with_fallback(
    fn, signatures: list[tuple], kwargs_list: list[dict] | None = None
):
    errors = []
    kwargs_list = kwargs_list or [{}]
    for args in signatures:
        for kwargs in kwargs_list:
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                errors.append(exc)
    raise RuntimeError(
        f"All fallback calls failed for {getattr(fn, '__name__', str(fn))}: "
        + "; ".join(str(e) for e in errors)
    )


async def create_session() -> awlise.Session:
    site_id = os.getenv("AWLISE_SITE_ID", "").strip()
    username = os.getenv("AWLISE_USERNAME", "").strip()
    password = os.getenv("AWLISE_PASSWORD", "").strip()
    if not site_id or not username or not password:
        raise RuntimeError(
            "Missing AWLISE credentials: set AWLISE_SITE_ID, AWLISE_USERNAME, AWLISE_PASSWORD"
        )
    return await awlise.login_credentials(
        site_id=site_id,
        username=username,
        password=password,
    )


async def run_reservation_for_date(date_iso: str) -> tuple[bool, str, dict]:
    session = await create_session()
    bookings = await awlise.getBookings(session)
    booking = next((item for item in bookings if item.date == date_iso), None)

    if booking is None:
        return False, f"Date {date_iso} not found in booking calendar", {}
    if booking.status == "reserved":
        return False, f"Lunch already reserved for {date_iso}", {}
    if booking.status != "available" or not booking.identifier:
        return False, f"Date {date_iso} is not reservable", {}

    booking_ok = await awlise.bookMeal(
        session, booking.identifier, quantity=1, cancel=False
    )
    if not booking_ok:
        return (
            False,
            f"Reservation request failed for {date_iso}",
            {"identifier": booking.identifier},
        )

    detail = await awlise.getBookingDetailByDateISO8601(session, date_iso)

    return (
        True,
        f"Lunch reserved for {date_iso}",
        {
            "identifier": booking.identifier,
            "detail": (
                detail.model_dump()
                if hasattr(detail, "model_dump") and detail
                else None
            ),
        },
    )


async def get_bookings() -> list:
    session = await create_session()
    return await awlise.getBookings(session)


async def find_first_reservable_date(
    timezone: str,
    min_offset_days: int,
    max_days_to_scan: int = 21,
) -> tuple[str, str]:
    tz = ZoneInfo(timezone)
    bookings = await get_bookings()
    start_date = datetime.now(tz).date() + timedelta(days=min_offset_days)
    end_date = start_date + timedelta(days=max_days_to_scan)

    candidates = []
    for booking in bookings:
        if booking.status != "available" or not booking.identifier or not booking.date:
            continue
        candidate = datetime.fromisoformat(booking.date).date()
        if candidate < start_date or candidate >= end_date:
            continue
        if candidate.weekday() >= 5:
            continue
        candidates.append((candidate.isoformat(), booking.identifier))

    if candidates:
        candidates.sort(key=lambda item: item[0])
        return candidates[0]

    raise RuntimeError(
        f"No reservable weekday found in next {max_days_to_scan} days starting from {start_date.isoformat()}."
    )


async def run_cancel_for_date(
    date_iso: str, reservation_payload: dict
) -> tuple[bool, str]:
    session = await create_session()
    identifier = (reservation_payload or {}).get("identifier")

    if not identifier:
        bookings = await awlise.getBookings(session)
        booking = next(
            (
                item
                for item in bookings
                if item.date == date_iso and item.status == "reserved"
            ),
            None,
        )
        identifier = booking.identifier if booking and booking.identifier else None

    if not identifier:
        return False, f"No cancelable reservation identifier found for {date_iso}"

    canceled = await awlise.bookMeal(session, identifier, cancel=True)
    if not canceled:
        return False, f"Cancellation request failed for {date_iso}"

    return True, f"Reservation canceled for {date_iso}"


def parse_hhmm(raw: str) -> dtime:
    hours, minutes = raw.split(":", maxsplit=1)
    return dtime(hour=int(hours), minute=int(minutes))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.application.bot_data["config"]
    chat_id = update.effective_chat.id if update.effective_chat else None
    if config.telegram_chat_id is None and chat_id is not None:
        config.telegram_chat_id = chat_id
    await update.message.reply_text(
        "AutoReservation bot is running. Daily lunch booking is scheduled. Use /reserve_now for immediate booking."
    )


async def chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.application.bot_data["config"]
    current_chat_id = update.effective_chat.id if update.effective_chat else None
    if current_chat_id is None:
        await update.message.reply_text("Unable to detect chat_id in this context.")
        return

    config.telegram_chat_id = current_chat_id
    await update.message.reply_text(
        f"chat_id: {current_chat_id}\nSaved for this running session."
    )


async def reserve_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.application.bot_data["config"]
    current_chat_id = update.effective_chat.id if update.effective_chat else None
    if current_chat_id is not None:
        config.telegram_chat_id = current_chat_id

    await update.message.reply_text("Launching immediate reservation...")
    await reserve_and_notify(context)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.application.bot_data["config"]
    current_chat_id = update.effective_chat.id if update.effective_chat else None
    effective_chat_id = current_chat_id or config.telegram_chat_id

    try:
        next_date, _identifier = await find_first_reservable_date(
            config.timezone,
            config.reserve_date_offset_days,
        )
    except Exception as exc:
        next_date = f"unavailable ({exc})"

    await update.message.reply_text(
        "Bot status:\n"
        f"- chat_id: {effective_chat_id}\n"
        f"- timezone: {config.timezone}\n"
        f"- reservation_time: {config.reservation_time}\n"
        f"- min_offset_days: {config.reserve_date_offset_days}\n"
        f"- next_reservable_day: {next_date}"
    )


async def reserve_and_notify(context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.application.bot_data["config"]
    chat_id = config.telegram_chat_id
    if chat_id is None:
        logger.warning(
            "Skipping reservation: TELEGRAM_CHAT_ID not set and no /start received yet."
        )
        return

    try:
        target_date, target_identifier = await find_first_reservable_date(
            config.timezone,
            config.reserve_date_offset_days,
        )
    except Exception as exc:
        logger.exception("Failed to compute first reservable date")
        await context.bot.send_message(
            chat_id=chat_id, text=f"❌ Unable to find first reservable day: {exc}"
        )
        return

    try:
        success, message, payload = await run_reservation_for_date(target_date)
    except Exception as exc:
        logger.exception("Reservation failed")
        await context.bot.send_message(
            chat_id=chat_id, text=f"❌ Reservation failed: {exc}"
        )
        return

    if not success:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ {message}")
        return

    action_id = f"undo:{target_date}"
    ACTION_CACHE[action_id] = {
        "date": target_date,
        "payload": {"identifier": payload.get("identifier") or target_identifier},
    }
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("↩️ Undo reservation", callback_data=action_id)]]
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"✅ {message}",
        reply_markup=keyboard,
    )


async def handle_undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action_id = query.data or ""

    data = ACTION_CACHE.get(action_id)
    if not data:
        await query.edit_message_text("⚠️ This action has expired or was already used.")
        return

    success, message = await run_cancel_for_date(
        data["date"],
        data["payload"],
    )
    if success:
        ACTION_CACHE.pop(action_id, None)
        await query.edit_message_text(f"↩️ {message}")
    else:
        await query.edit_message_text(f"⚠️ {message}")


async def run_bot() -> None:
    config = load_config()
    app = Application.builder().token(config.telegram_token).build()
    app.bot_data["config"] = config

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chat_id", chat_id))
    app.add_handler(CommandHandler("reserve_now", reserve_now))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CallbackQueryHandler(handle_undo, pattern=r"^undo:"))

    tz = ZoneInfo(config.timezone)
    app.job_queue.run_daily(
        reserve_and_notify,
        time=parse_hhmm(config.reservation_time).replace(tzinfo=tz),
        name="daily_lunch_reservation",
    )

    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    logger.info(
        "Bot started. Reservation daily at %s (%s)",
        config.reservation_time,
        config.timezone,
    )
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(run_bot())

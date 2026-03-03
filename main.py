import asyncio
from email.utils import parsedate
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
        return (
            False,
            f"Date {date_iso} non trouvée dans le calendrier de réservation",
            {},
        )
    if booking.status == "reserved":
        return False, f"Repas déjà réservé pour {date_iso}", {}
    if booking.status != "available" or not booking.identifier:
        return False, f"Date {date_iso} n'est pas réservable", {}

    booking_ok = await awlise.bookMeal(
        session, booking.identifier, quantity=1, cancel=False
    )
    if not booking_ok:
        return (
            False,
            f"Échec de la demande de réservation pour {date_iso}",
            {"identifier": booking.identifier},
        )

    detail = await awlise.getBookingDetailByDateISO8601(session, date_iso)

    return (
        True,
        f"Repas réservé pour {date_iso}",
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
        f"Aucun jour réservable trouvé dans les prochains {max_days_to_scan} jours à partir du {start_date.isoformat()}."
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
        return False, f"Aucun identifiant de réservation trouvé pour {date_iso}"

    canceled = await awlise.bookMeal(session, identifier, cancel=True)
    if not canceled:
        return False, f"Échec de la demande d'annulation pour {date_iso}"

    return True, f"Réservation annulée pour {date_iso}"


def parse_hhmm(raw: str) -> dtime:
    hours, minutes = raw.split(":", maxsplit=1)
    return dtime(hour=int(hours), minute=int(minutes))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.application.bot_data["config"]
    chat_id = update.effective_chat.id if update.effective_chat else None
    if config.telegram_chat_id is None and chat_id is not None:
        config.telegram_chat_id = chat_id
    await update.message.reply_text(
        "Le bot AutoReservation est en cours d'exécution. La réservation quotidienne du déjeuner est programmée. Utilisez /reserve_now pour réserver immédiatement."
    )


async def chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.application.bot_data["config"]
    current_chat_id = update.effective_chat.id if update.effective_chat else None
    if current_chat_id is None:
        await update.message.reply_text(
            "Impossible de détecter le chat_id dans ce contexte."
        )
        return

    config.telegram_chat_id = current_chat_id
    await update.message.reply_text(
        f"chat_id: {current_chat_id}\nEnregistré pour cette session en cours."
    )


async def reserve_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.application.bot_data["config"]
    current_chat_id = update.effective_chat.id if update.effective_chat else None
    if current_chat_id is not None:
        config.telegram_chat_id = current_chat_id

    await update.message.reply_text("Lancement de la réservation immédiate...")
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
        "Status du bot:\n"
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
            chat_id=chat_id,
            text=f"❌ Impossible de trouver le premier jour réservable: {exc}",
        )
        return

    try:
        success, message, payload = await run_reservation_for_date(target_date)
    except Exception as exc:
        logger.exception("Reservation failed")
        await context.bot.send_message(
            chat_id=chat_id, text=f"❌ Échec de la réservation: {exc}"
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
        [[InlineKeyboardButton("↩️ Annuler la réservation", callback_data=action_id)]]
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
        await query.edit_message_text("⚠️ Cette action a expiré ou a déjà été utilisée.")
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


async def get_calendar_of_day(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await update.message.reply_text("📅 Chargement du calendrier...")

    try:
        session = await create_session()
        bookings = await awlise.getBookings(session)

        reserved = [b for b in bookings if b.status == "reserved"]
        available = [b for b in bookings if b.status == "available"]
        unavailable = [b for b in bookings if b.status not in ("reserved", "available")]

        lines = ["📅 <b>Calendrier de réservation</b>\n"]

        if reserved:
            lines.append("<b>✅ Réservé</b> ({})".format(len(reserved)))
            for b in reserved[:7]:
                lines.append("  🟩 {}".format(b.date))
            if len(reserved) > 7:
                lines.append("  <i>... +{} autre(s)</i>".format(len(reserved) - 7))

        if available:
            lines.append("\n<b>🟢 Disponible</b> ({})".format(len(available)))
            for b in available[:7]:
                lines.append("  🟩 {}".format(b.date))
            if len(available) > 7:
                lines.append("  <i>... +{} autre(s)</i>".format(len(available) - 7))

        if unavailable:
            lines.append("\n<b>⛔ Non réservable</b> ({})".format(len(unavailable)))
            for b in unavailable[:5]:
                lines.append("  🟥 {}".format(b.date))
            if len(unavailable) > 5:
                lines.append("  <i>... +{} autre(s)</i>".format(len(unavailable) - 5))

        if not any([reserved, available, unavailable]):
            lines.append("<i>Aucune donnée disponible</i>")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    except Exception as exc:
        logger.exception("Failed to fetch calendar")
        await update.message.reply_text(
            f"❌ Erreur lors du chargement:\n<code>{str(exc)[:100]}</code>",
            parse_mode="HTML",
        )

async def reserve_a_day(context: ContextTypes.DEFAULT_TYPE, date: str) -> None:
    
    config: Config = context.application.bot_data["config"]
    chat_id = config.telegram_chat_id
    if chat_id is None:
        logger.warning(
            "Skipping reservation: TELEGRAM_CHAT_ID not set and no /start received yet."
        )
        return

    # convert 12/02 to ISO
    try:
        current_year = datetime.now().year
        date_iso = datetime.strptime(f"{date}/{current_year}", "%d/%m/%Y").date().isoformat()
    except ValueError:
        await context.bot.send_message(
            chat_id=chat_id, text=f"❌ Format de date invalide: {date}"
        )
        return

    try:
        success, message, _payload = await run_reservation_for_date(date_iso)
    except Exception as exc:
        logger.exception("Reservation failed")
        await context.bot.send_message(
            chat_id=chat_id, text=f"❌ Échec de la réservation pour {date}: {exc}"
        )
        return

    if not success:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ {message}")
    else:
        await context.bot.send_message(chat_id=chat_id, text=f"✅ {message}")

async def unreserve_a_day(context: ContextTypes.DEFAULT_TYPE, date: str) -> None:
    
    config: Config = context.application.bot_data["config"]
    chat_id = config.telegram_chat_id
    if chat_id is None:
        logger.warning(
            "Skipping cancellation: TELEGRAM_CHAT_ID not set and no /start received yet."
        )
        return

    # convert 12/02 to ISO
    try:
        current_year = datetime.now().year
        date_iso = datetime.strptime(f"{date}/{current_year}", "%d/%m/%Y").date().isoformat()
    except ValueError:
        await context.bot.send_message(
            chat_id=chat_id, text=f"❌ Format de date invalide: {date}"
        )
        return

    try:
        success, message = await run_cancel_for_date(date_iso, {})
    except Exception as exc:
        logger.exception("Cancellation failed")
        await context.bot.send_message(
            chat_id=chat_id, text=f"❌ Échec de l'annulation pour {date}: {exc}"
        )
        return

    if not success:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ {message}")
    else:
        await context.bot.send_message(chat_id=chat_id, text=f"✅ {message}")

async def run_bot() -> None:
    config = load_config()
    app = Application.builder().token(config.telegram_token).build()
    app.bot_data["config"] = config

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chat_id", chat_id))
    app.add_handler(CommandHandler("reserve_now", reserve_now))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("calendar", get_calendar_of_day))
    app.add_handler(CommandHandler("reserve", lambda update, context: reserve_a_day(context, update.message.text.split(" ", 1)[1] if " " in update.message.text else "")))
    app.add_handler(CommandHandler("unreserve", lambda update, context: unreserve_a_day(context, update.message.text.split(" ", 1)[1] if " " in update.message.text else "")))
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

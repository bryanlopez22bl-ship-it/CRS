import os
import time
import logging
from datetime import datetime, timezone

from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SWIPE_TABLE = os.getenv("SWIPE_TABLE", "swipe_events")
DAILY_TABLE = os.getenv("DAILY_TABLE", "daily_tracking_nokey")
ENABLE_DAILY_SUMMARY = os.getenv("ENABLE_DAILY_SUMMARY", "true").lower() == "true"

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Missing SUPABASE_URL or SUPABASE_KEY in .env")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def today_utc_date() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def insert_swipe_event(student_id: str, is_tutor: bool, event_type: str):
    event_type = event_type.upper().strip()
    if event_type not in {"IN", "OUT"}:
        raise ValueError("event_type must be 'IN' or 'OUT'")

    payload = {
        "created_at": utc_now_iso(),
        "student_id": str(student_id).strip(),
        "isTutor": bool(is_tutor),
        "event_type": event_type,
    }

    logger.info("Inserting swipe event into %s: %s", SWIPE_TABLE, payload)
    return supabase.table(SWIPE_TABLE).insert(payload).execute()


def upsert_daily_summary(is_tutor: bool, event_type: str):
    """
    Updates the summary table only when an IN event occurs.

    number_of_occupancy is incremented by 1 for each IN event.
    total_tutoring_hours is incremented by 1 for each tutor IN event.
    """
    if event_type.upper() != "IN":
        return None

    today = today_utc_date()
    current = supabase.table(DAILY_TABLE).select("*").eq("date", today).execute()

    if current.data:
        row = current.data[0]
        update_payload = {
            "number_of_occupancy": int(row.get("number_of_occupancy") or 0) + 1,
            "total_tutoring_hours": float(row.get("total_tutoring_hours") or 0)
            + (1 if is_tutor else 0),
        }
        logger.info("Updating daily summary for %s: %s", today, update_payload)
        return supabase.table(DAILY_TABLE).update(update_payload).eq("date", today).execute()

    insert_payload = {
        "date": today,
        "number_of_occupancy": 1,
        "total_tutoring_hours": 1 if is_tutor else 0,
    }
    logger.info("Creating daily summary row for %s: %s", today, insert_payload)
    return supabase.table(DAILY_TABLE).insert(insert_payload).execute()



def process_swipe(student_id: str, is_tutor: bool, event_type: str):
    swipe_result = insert_swipe_event(student_id, is_tutor, event_type)
    logger.info("Swipe insert complete")

    summary_result = None
    if ENABLE_DAILY_SUMMARY:
        summary_result = upsert_daily_summary(is_tutor, event_type)
        logger.info("Daily summary update complete")

    return swipe_result, summary_result



def interactive_mode():
    """Simple manual test mode for terminal use on the Pi."""
    print("Swipe app ready. Press Ctrl+C to exit.\n")
    while True:
        student_id = input("Student ID: ").strip()
        tutor_text = input("Is tutor? (y/n): ").strip().lower()
        event_type = input("Event type (IN/OUT): ").strip().upper()

        is_tutor = tutor_text in {"y", "yes", "true", "1"}

        try:
            process_swipe(student_id, is_tutor, event_type)
            print("Saved successfully.\n")
        except Exception as exc:
            logger.exception("Failed to process swipe")
            print(f"Error: {exc}\n")


if __name__ == "__main__":
    interactive_mode()

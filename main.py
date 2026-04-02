import os
import re
import time
import threading
from datetime import datetime, date, time as dt_time
from typing import Optional, Dict, Any, List

from dotenv import load_dotenv
from supabase import create_client, Client

# =========================
# Environment / Config
# =========================

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Missing SUPABASE_URL or SUPABASE_KEY in .env")

# Existing tables
STUDENTS_TABLE = "students_nokey"
HISTORY_TABLE = "student_status_history_nokey"

# Auto-close today's open records after this time
AUTO_CLOSE_HOUR = int(os.getenv("AUTO_CLOSE_HOUR", "23"))
AUTO_CLOSE_MINUTE = int(os.getenv("AUTO_CLOSE_MINUTE", "59"))

# How often to check whether end-of-day auto-close should run
AUTO_CLOSE_CHECK_SECONDS = int(os.getenv("AUTO_CLOSE_CHECK_SECONDS", "30"))

# LED settings
ENABLE_LED = os.getenv("ENABLE_LED", "true").lower() == "true"
LED_PIN = int(os.getenv("LED_PIN", "18"))

# ID format constraint from your DB
ID_REGEX = re.compile(r"\d{10}")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# =========================
# Optional Raspberry Pi LED
# =========================

GPIO_READY = False

if ENABLE_LED:
    try:
        import RPi.GPIO as GPIO

        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(LED_PIN, GPIO.OUT)
        GPIO.output(LED_PIN, GPIO.LOW)
        GPIO_READY = True
    except Exception as e:
        print(f"[WARN] GPIO unavailable, LED disabled: {e}")
        GPIO_READY = False


def led_on() -> None:
    if GPIO_READY:
        GPIO.output(LED_PIN, GPIO.HIGH)


def led_off() -> None:
    if GPIO_READY:
        GPIO.output(LED_PIN, GPIO.LOW)


def success_blink() -> None:
    if not GPIO_READY:
        return
    led_on()
    time.sleep(0.25)
    led_off()


def fail_blink() -> None:
    if not GPIO_READY:
        return
    for _ in range(5):
        led_on()
        time.sleep(0.08)
        led_off()
        time.sleep(0.08)


def cleanup_gpio() -> None:
    if GPIO_READY:
        led_off()
        GPIO.cleanup()


# =========================
# Helpers
# =========================

def now_local() -> datetime:
    return datetime.now()


def today_local() -> date:
    return now_local().date()


def iso_now() -> str:
    return now_local().isoformat()


def end_of_day_iso(d: date) -> str:
    return datetime.combine(d, dt_time(AUTO_CLOSE_HOUR, AUTO_CLOSE_MINUTE, 59)).isoformat()


def extract_student_id(raw_swipe: str) -> Optional[str]:
    """
    Extract exactly one 10-digit ID from the swiper input.
    Returns the first 10-digit match.
    """
    if not raw_swipe:
        return None

    match = ID_REGEX.search(raw_swipe)
    if match:
        return match.group(0)

    return None


def parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00").replace("+00:00", ""))


def is_same_local_day(dt_value: datetime, day_value: date) -> bool:
    return dt_value.date() == day_value


# =========================
# Database functions
# =========================

def get_student_record(student_id: str) -> Optional[Dict[str, Any]]:
    response = (
        supabase.table(STUDENTS_TABLE)
        .select("*")
        .eq("id", student_id)
        .limit(1)
        .execute()
    )
    data = response.data or []
    return data[0] if data else None


def ensure_student_row_exists(student_id: str) -> Dict[str, Any]:
    """
    Makes sure the student exists in students_nokey so the history FK is satisfied.
    Uses upsert with only id/check_in_at/check_out_at.
    """
    existing = get_student_record(student_id)
    if existing:
        return existing

    response = (
        supabase.table(STUDENTS_TABLE)
        .upsert({
            "id": student_id,
            "check_in_at": None,
            "check_out_at": None
        })
        .execute()
    )

    data = response.data or []
    if data:
        return data[0]

    # fallback re-read
    existing = get_student_record(student_id)
    if not existing:
        raise RuntimeError(f"Unable to create or fetch student row for {student_id}")
    return existing


def log_status(student_id: str, status: str, timestamp_iso: str) -> None:
    """
    status must be:
      - checked_in
      - checked_out
    """
    if status not in ("checked_in", "checked_out"):
        raise ValueError(f"Invalid status: {status}")

    (
        supabase.table(HISTORY_TABLE)
        .insert({
            "id": student_id,
            "status": status,
            "status_recorded_at": timestamp_iso
        })
        .execute()
    )


def update_student_times(student_id: str, check_in_at: Optional[str], check_out_at: Optional[str]) -> None:
    payload: Dict[str, Any] = {}

    if check_in_at is not None:
        payload["check_in_at"] = check_in_at

    # allow explicit null on check_out_at
    payload["check_out_at"] = check_out_at

    (
        supabase.table(STUDENTS_TABLE)
        .update(payload)
        .eq("id", student_id)
        .execute()
    )


# =========================
# Business logic
# =========================

def close_stale_open_record_for_student(student_id: str) -> bool:
    """
    If the student is still checked in from a previous day, auto-close that old session
    at that prior day's configured end-of-day time and log checked_out history.
    """
    student = get_student_record(student_id)
    if not student:
        return False

    check_in_raw = student.get("check_in_at")
    check_out_raw = student.get("check_out_at")

    if not check_in_raw or check_out_raw is not None:
        return False

    check_in_dt = parse_iso_datetime(check_in_raw)
    if check_in_dt is None:
        return False

    if is_same_local_day(check_in_dt, today_local()):
        return False

    auto_close_timestamp = end_of_day_iso(check_in_dt.date())

    update_student_times(
        student_id=student_id,
        check_in_at=None,
        check_out_at=auto_close_timestamp
    )

    log_status(student_id, "checked_out", auto_close_timestamp)
    return True


def check_in(student_id: str) -> None:
    timestamp = iso_now()

    ensure_student_row_exists(student_id)

    update_student_times(
        student_id=student_id,
        check_in_at=timestamp,
        check_out_at=None
    )

    log_status(student_id, "checked_in", timestamp)

    print(f"[OK] {student_id} -> checked_in at {timestamp}")
    success_blink()


def check_out(student_id: str) -> None:
    timestamp = iso_now()

    ensure_student_row_exists(student_id)

    update_student_times(
        student_id=student_id,
        check_in_at=None,
        check_out_at=timestamp
    )

    log_status(student_id, "checked_out", timestamp)

    print(f"[OK] {student_id} -> checked_out at {timestamp}")
    success_blink()


def process_swipe(student_id: str) -> None:
    """
    Rules:
    - If no current row exists or no active open session: checked_in
    - If currently checked in today and not checked out yet: checked_out
    - If stale open session exists from a prior day: close it first, then checked_in
    """
    ensure_student_row_exists(student_id)

    # Close stale previous-day session if needed
    close_stale_open_record_for_student(student_id)

    student = get_student_record(student_id)
    if not student:
        raise RuntimeError(f"Student record missing after ensure step for {student_id}")

    check_in_raw = student.get("check_in_at")
    check_out_raw = student.get("check_out_at")

    if not check_in_raw:
        check_in(student_id)
        return

    check_in_dt = parse_iso_datetime(check_in_raw)
    if check_in_dt is None:
        raise RuntimeError(f"Invalid check_in_at value for {student_id}: {check_in_raw}")

    # If checked in today and still open, then this swipe is an OUT
    if check_out_raw is None and is_same_local_day(check_in_dt, today_local()):
        check_out(student_id)
        return

    # Otherwise start a fresh session
    check_in(student_id)


# =========================
# Auto-close routines
# =========================

def get_all_open_student_rows() -> List[Dict[str, Any]]:
    response = (
        supabase.table(STUDENTS_TABLE)
        .select("*")
        .not_.is_("check_in_at", "null")
        .is_("check_out_at", "null")
        .execute()
    )
    return response.data or []


def close_all_stale_open_rows() -> int:
    """
    Close all open rows whose check_in_at is from a previous day.
    """
    rows = get_all_open_student_rows()
    count = 0

    for row in rows:
        student_id = row.get("id")
        check_in_raw = row.get("check_in_at")

        if not student_id or not check_in_raw:
            continue

        check_in_dt = parse_iso_datetime(check_in_raw)
        if check_in_dt is None:
            continue

        if is_same_local_day(check_in_dt, today_local()):
            continue

        auto_close_timestamp = end_of_day_iso(check_in_dt.date())

        update_student_times(
            student_id=student_id,
            check_in_at=None,
            check_out_at=auto_close_timestamp
        )
        log_status(student_id, "checked_out", auto_close_timestamp)
        count += 1

    return count


def close_all_open_rows_for_today_if_needed() -> int:
    """
    After the configured cutoff time, close every student still checked in today.
    """
    now_dt = now_local()
    cutoff_dt = datetime.combine(now_dt.date(), dt_time(AUTO_CLOSE_HOUR, AUTO_CLOSE_MINUTE, 59))

    if now_dt < cutoff_dt:
        return 0

    rows = get_all_open_student_rows()
    count = 0
    close_ts = cutoff_dt.isoformat()

    for row in rows:
        student_id = row.get("id")
        check_in_raw = row.get("check_in_at")

        if not student_id or not check_in_raw:
            continue

        check_in_dt = parse_iso_datetime(check_in_raw)
        if check_in_dt is None:
            continue

        if not is_same_local_day(check_in_dt, today_local()):
            continue

        update_student_times(
            student_id=student_id,
            check_in_at=None,
            check_out_at=close_ts
        )
        log_status(student_id, "checked_out", close_ts)
        count += 1

    return count


def auto_close_worker() -> None:
    while True:
        try:
            closed_count = close_all_open_rows_for_today_if_needed()
            if closed_count > 0:
                print(f"[AUTO] Closed {closed_count} open record(s) for today.")
        except Exception as e:
            print(f"[AUTO] Error during end-of-day close: {e}")

        time.sleep(AUTO_CLOSE_CHECK_SECONDS)


# =========================
# Main program
# =========================

def startup_tasks() -> None:
    stale_closed = close_all_stale_open_rows()
    if stale_closed > 0:
        print(f"[STARTUP] Closed {stale_closed} stale open record(s).")
    else:
        print("[STARTUP] No stale open records found.")


def main() -> None:
    startup_tasks()

    worker = threading.Thread(target=auto_close_worker, daemon=True)
    worker.start()

    print("Swipe system ready.")
    print("Swipe a card now. Type 'exit' to quit.")

    while True:
        try:
            raw = input("Swipe: ").strip()

            if not raw:
                continue

            if raw.lower() == "exit":
                print("Exiting.")
                break

            student_id = extract_student_id(raw)

            if not student_id:
                print("[FAIL] Could not find a valid 10-digit student ID in swipe input.")
                fail_blink()
                continue

            process_swipe(student_id)

        except KeyboardInterrupt:
            print("\nExiting.")
            break
        except Exception as e:
            print(f"[ERROR] {e}")
            fail_blink()


if __name__ == "__main__":
    try:
        main()
    finally:
        cleanup_gpio()

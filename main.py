cat > main.py <<'PY'
import os
import re
import time
import threading
from datetime import datetime, date, time as dt_time
from typing import Optional

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Missing SUPABASE_URL or SUPABASE_KEY in .env")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

STUDENTS_TABLE = "students_nokey"
HISTORY_TABLE = "student_status_history_nokey"

AUTO_CLOSE_HOUR = int(os.getenv("AUTO_CLOSE_HOUR", "23"))
AUTO_CLOSE_MINUTE = int(os.getenv("AUTO_CLOSE_MINUTE", "59"))
AUTO_CLOSE_CHECK_SECONDS = int(os.getenv("AUTO_CLOSE_CHECK_SECONDS", "30"))

ENABLE_LED = os.getenv("ENABLE_LED", "true").lower() == "true"
LED_PIN = int(os.getenv("LED_PIN", "18"))

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
        print(f"[WARN] GPIO disabled: {e}")
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


def now_local() -> datetime:
    return datetime.now()


def iso_now() -> str:
    return now_local().isoformat()


def end_of_day_iso(d: date) -> str:
    return datetime.combine(d, dt_time(AUTO_CLOSE_HOUR, AUTO_CLOSE_MINUTE, 59)).isoformat()


def extract_student_id(raw_swipe: str) -> Optional[str]:
    """
    Extract the first 10-digit student ID from the swipe input.
    """
    if not raw_swipe:
        return None

    match = re.search(r"\d{10}", raw_swipe)
    if match:
        return match.group(0)

    return None


def parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    cleaned = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(cleaned).replace(tzinfo=None)
    except ValueError:
        return None


def get_student_record(student_id: str):
    response = (
        supabase.table(STUDENTS_TABLE)
        .select("*")
        .eq("id", student_id)
        .limit(1)
        .execute()
    )
    data = response.data or []
    return data[0] if data else None


def ensure_student_exists(student_id: str) -> None:
    existing = get_student_record(student_id)
    if existing:
        return

    (
        supabase.table(STUDENTS_TABLE)
        .insert({
            "id": student_id,
            "check_in_at": None,
            "check_out_at": None
        })
        .execute()
    )


def log_status(student_id: str, status: str, timestamp_iso: str) -> None:
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


def check_in(student_id: str) -> None:
    timestamp = iso_now()

    ensure_student_exists(student_id)

    (
        supabase.table(STUDENTS_TABLE)
        .update({
            "check_in_at": timestamp,
            "check_out_at": None
        })
        .eq("id", student_id)
        .execute()
    )

    log_status(student_id, "checked_in", timestamp)
    print(f"[OK] {student_id} -> checked_in at {timestamp}")
    success_blink()


def check_out(student_id: str, timestamp: Optional[str] = None) -> None:
    if timestamp is None:
        timestamp = iso_now()

    ensure_student_exists(student_id)

    (
        supabase.table(STUDENTS_TABLE)
        .update({
            "check_out_at": timestamp
        })
        .eq("id", student_id)
        .execute()
    )

    log_status(student_id, "checked_out", timestamp)
    print(f"[OK] {student_id} -> checked_out at {timestamp}")
    success_blink()


def close_stale_open_record_for_student(student_id: str) -> bool:
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

    today = now_local().date()
    if check_in_dt.date() == today:
        return False

    close_ts = end_of_day_iso(check_in_dt.date())

    (
        supabase.table(STUDENTS_TABLE)
        .update({
            "check_out_at": close_ts
        })
        .eq("id", student_id)
        .execute()
    )

    log_status(student_id, "checked_out", close_ts)
    return True


def process_swipe(student_id: str) -> None:
    ensure_student_exists(student_id)
    close_stale_open_record_for_student(student_id)

    student = get_student_record(student_id)
    if not student:
        raise RuntimeError("Could not fetch student record.")

    check_in_raw = student.get("check_in_at")
    check_out_raw = student.get("check_out_at")

    if not check_in_raw:
        check_in(student_id)
        return

    check_in_dt = parse_iso_datetime(check_in_raw)
    if check_in_dt is None:
        raise RuntimeError(f"Invalid check_in_at for {student_id}")

    today = now_local().date()

    if check_out_raw is None and check_in_dt.date() == today:
        check_out(student_id)
        return

    check_in(student_id)


def get_all_open_rows():
    response = (
        supabase.table(STUDENTS_TABLE)
        .select("*")
        .not_.is_("check_in_at", "null")
        .is_("check_out_at", "null")
        .execute()
    )
    return response.data or []


def close_all_stale_open_rows() -> int:
    rows = get_all_open_rows()
    count = 0
    today = now_local().date()

    for row in rows:
        student_id = row.get("id")
        check_in_raw = row.get("check_in_at")

        if not student_id or not check_in_raw:
            continue

        check_in_dt = parse_iso_datetime(check_in_raw)
        if check_in_dt is None:
            continue

        if check_in_dt.date() == today:
            continue

        close_ts = end_of_day_iso(check_in_dt.date())

        (
            supabase.table(STUDENTS_TABLE)
            .update({
                "check_out_at": close_ts
            })
            .eq("id", student_id)
            .execute()
        )

        log_status(student_id, "checked_out", close_ts)
        count += 1

    return count


def close_all_open_rows_for_today_if_needed() -> int:
    now_dt = now_local()
    cutoff_dt = datetime.combine(now_dt.date(), dt_time(AUTO_CLOSE_HOUR, AUTO_CLOSE_MINUTE, 59))

    if now_dt < cutoff_dt:
        return 0

    rows = get_all_open_rows()
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

        if check_in_dt.date() != now_dt.date():
            continue

        (
            supabase.table(STUDENTS_TABLE)
            .update({
                "check_out_at": close_ts
            })
            .eq("id", student_id)
            .execute()
        )

        log_status(student_id, "checked_out", close_ts)
        count += 1

    return count


def auto_close_worker() -> None:
    while True:
        try:
            closed = close_all_open_rows_for_today_if_needed()
            if closed > 0:
                print(f"[AUTO] Closed {closed} open record(s) for today.")
        except Exception as e:
            print(f"[AUTO] Error: {e}")

        time.sleep(AUTO_CLOSE_CHECK_SECONDS)


def startup_tasks() -> None:
    closed = close_all_stale_open_rows()
    if closed > 0:
        print(f"[STARTUP] Closed {closed} stale open record(s).")
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
                print("[FAIL] No valid 10-digit ID found.")
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
PY
if __name__ == "__main__":
    try:
        main()
    finally:
        cleanup_gpio()

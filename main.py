import os
import re
import time
import threading
from datetime import datetime, date, time as dt_time, timedelta
from typing import Optional, Dict, List

from dotenv import load_dotenv
from supabase import create_client
from evdev import InputDevice, categorize, ecodes, list_devices

# =========================
# Config
# =========================

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Missing SUPABASE_URL or SUPABASE_KEY in .env")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

SWIPE_TABLE = "swipe_events"
DAILY_TABLE = "daily_tracking_nokey"
TUTORS_TABLE = "tutors"

DEVICE_NAME_HINT = "MiniMag"

AUTO_CLOSE_HOUR = int(os.getenv("AUTO_CLOSE_HOUR", "23"))
AUTO_CLOSE_MINUTE = int(os.getenv("AUTO_CLOSE_MINUTE", "59"))
AUTO_CLOSE_CHECK_SECONDS = int(os.getenv("AUTO_CLOSE_CHECK_SECONDS", "30"))

ENABLE_LED = os.getenv("ENABLE_LED", "true").lower() == "true"
LED_PIN = int(os.getenv("LED_PIN", "18"))

# =========================
# Optional LED
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


# =========================
# Time helpers
# =========================

def now_local() -> datetime:
    return datetime.now()


def iso_now() -> str:
    return now_local().isoformat()


def start_of_day(target_date: date) -> datetime:
    return datetime.combine(target_date, dt_time(0, 0, 0))


def end_of_day(target_date: date) -> datetime:
    return datetime.combine(target_date, dt_time(AUTO_CLOSE_HOUR, AUTO_CLOSE_MINUTE, 59))


def day_bounds_iso(target_date: date):
    start = start_of_day(target_date).isoformat()
    end = (start_of_day(target_date) + timedelta(days=1)).isoformat()
    return start, end


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    cleaned = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(cleaned).replace(tzinfo=None)
    except ValueError:
        return None


# =========================
# HID swiper
# =========================

KEYMAP = {
    "KEY_1": "1",
    "KEY_2": "2",
    "KEY_3": "3",
    "KEY_4": "4",
    "KEY_5": "5",
    "KEY_6": "6",
    "KEY_7": "7",
    "KEY_8": "8",
    "KEY_9": "9",
    "KEY_0": "0",
    "KEY_ENTER": "\n",
    "KEY_KPENTER": "\n",
}


def find_swiper_device() -> InputDevice:
    for path in list_devices():
        try:
            device = InputDevice(path)
            if DEVICE_NAME_HINT.lower() in device.name.lower():
                return device
        except Exception:
            continue
    raise RuntimeError(f"Could not find swiper device containing: {DEVICE_NAME_HINT}")


def read_swipes_from_hid():
    device = find_swiper_device()
    print(f"[HID] Listening on {device.path}: {device.name}")

    buffer = ""

    for event in device.read_loop():
        if event.type != ecodes.EV_KEY:
            continue

        key_event = categorize(event)

        if key_event.keystate != 1:
            continue

        keycode = key_event.keycode
        if isinstance(keycode, list):
            keycode = keycode[0]

        if keycode not in KEYMAP:
            continue

        char = KEYMAP[keycode]

        if char == "\n":
            raw = buffer.strip()
            buffer = ""
            if raw:
                yield raw
        else:
            buffer += char


def extract_student_id(raw_swipe: str) -> Optional[str]:
    match = re.search(r"\d{10}", raw_swipe or "")
    return match.group(0) if match else None


# =========================
# Database helpers
# =========================

def get_events_for_date(target_date: date) -> List[dict]:
    start_iso, end_iso = day_bounds_iso(target_date)

    response = (
        supabase.table(SWIPE_TABLE)
        .select("*")
        .gte("created_at", start_iso)
        .lt("created_at", end_iso)
        .order("created_at", desc=False)
        .execute()
    )

    return response.data or []


def get_latest_event_for_student_today(student_id: str, target_date: Optional[date] = None) -> Optional[dict]:
    if target_date is None:
        target_date = now_local().date()

    start_iso, end_iso = day_bounds_iso(target_date)

    response = (
        supabase.table(SWIPE_TABLE)
        .select("*")
        .eq("student_id", student_id)
        .gte("created_at", start_iso)
        .lt("created_at", end_iso)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    data = response.data or []
    return data[0] if data else None


def is_tutor(student_id: str) -> bool:
    response = (
        supabase.table(TUTORS_TABLE)
        .select("student_id")
        .eq("student_id", student_id)
        .limit(1)
        .execute()
    )

    return bool(response.data)


def insert_swipe_event(student_id: str, is_tutor_flag: bool, event_type: str, timestamp_iso: Optional[str] = None) -> None:
    if timestamp_iso is None:
        timestamp_iso = iso_now()

    if event_type not in ("IN", "OUT"):
        raise ValueError(f"Invalid event_type: {event_type}")

    (
        supabase.table(SWIPE_TABLE)
        .insert({
            "student_id": student_id,
            "isTutor": is_tutor_flag,
            "event_type": event_type,
            "created_at": timestamp_iso
        })
        .execute()
    )


# =========================
# Daily tracking
# =========================

def calculate_daily_metrics(target_date: date):
    events = get_events_for_date(target_date)

    latest_by_student: Dict[str, dict] = {}
    tutor_open_in: Dict[str, datetime] = {}
    total_tutor_seconds = 0.0

    for event in events:
        student_id = str(event["student_id"])
        event_type = event["event_type"]
        event_dt = parse_dt(event["created_at"])
        tutor_flag = bool(event.get("isTutor", False))

        if event_dt is None:
            continue

        latest_by_student[student_id] = event

        if tutor_flag:
            if event_type == "IN":
                tutor_open_in[student_id] = event_dt
            elif event_type == "OUT":
                if student_id in tutor_open_in:
                    total_tutor_seconds += (event_dt - tutor_open_in[student_id]).total_seconds()
                    del tutor_open_in[student_id]

    current_occupancy = sum(
        1 for event in latest_by_student.values()
        if event.get("event_type") == "IN"
    )

    total_tutor_hours = round(total_tutor_seconds / 3600.0, 2)

    return current_occupancy, total_tutor_hours


def update_daily_tracking(target_date: date) -> None:
    occupancy, tutor_hours = calculate_daily_metrics(target_date)

    (
        supabase.table(DAILY_TABLE)
        .upsert({
            "date": target_date.isoformat(),
            "number_of_occupancy": occupancy,
            "total_tutoring_hours": tutor_hours
        })
        .execute()
    )


# =========================
# Swipe logic
# =========================

def process_swipe(student_id: str) -> None:
    today = now_local().date()
    latest_event = get_latest_event_for_student_today(student_id, today)

    if latest_event is None or latest_event["event_type"] == "OUT":
        next_event_type = "IN"
    else:
        next_event_type = "OUT"

    tutor_flag = is_tutor(student_id)

    insert_swipe_event(student_id, tutor_flag, next_event_type)
    update_daily_tracking(today)

    print(f"[OK] {student_id} -> {next_event_type} | isTutor={tutor_flag}")
    success_blink()


# =========================
# End-of-day auto close
# =========================

def get_students_still_in_for_date(target_date: date) -> List[dict]:
    events = get_events_for_date(target_date)
    latest_by_student: Dict[str, dict] = {}

    for event in events:
        latest_by_student[str(event["student_id"])] = event

    return [
        event for event in latest_by_student.values()
        if event.get("event_type") == "IN"
    ]


def auto_close_date(target_date: date) -> int:
    cutoff_iso = end_of_day(target_date).isoformat()
    still_in = get_students_still_in_for_date(target_date)

    count = 0
    for event in still_in:
        insert_swipe_event(
            student_id=str(event["student_id"]),
            is_tutor_flag=bool(event.get("isTutor", False)),
            event_type="OUT",
            timestamp_iso=cutoff_iso
        )
        count += 1

    update_daily_tracking(target_date)
    return count


_last_auto_closed_date: Optional[date] = None


def startup_tasks() -> None:
    global _last_auto_closed_date

    today = now_local().date()
    yesterday = today - timedelta(days=1)

    closed_yesterday = auto_close_date(yesterday)
    if closed_yesterday > 0:
        print(f"[STARTUP] Auto-closed {closed_yesterday} open record(s) for {yesterday}.")
    else:
        print("[STARTUP] No stale open records found.")

    if now_local() >= end_of_day(today):
        closed_today = auto_close_date(today)
        _last_auto_closed_date = today
        if closed_today > 0:
            print(f"[STARTUP] Auto-closed {closed_today} open record(s) for today.")


def auto_close_worker() -> None:
    global _last_auto_closed_date

    while True:
        try:
            today = now_local().date()
            if now_local() >= end_of_day(today) and _last_auto_closed_date != today:
                closed = auto_close_date(today)
                _last_auto_closed_date = today
                print(f"[AUTO] Auto-closed {closed} open record(s) for today.")
        except Exception as e:
            print(f"[AUTO] Error: {e}")

        time.sleep(AUTO_CLOSE_CHECK_SECONDS)


# =========================
# Main
# =========================

def main() -> None:
    startup_tasks()

    worker = threading.Thread(target=auto_close_worker, daemon=True)
    worker.start()

    print("Swipe system ready.")

    for raw in read_swipes_from_hid():
        try:
            print(f"[RAW] {raw}")

            student_id = extract_student_id(raw)
            if not student_id:
                print("[FAIL] No valid 10-digit ID found.")
                fail_blink()
                continue

            process_swipe(student_id)

        except Exception as e:
            print(f"[ERROR] {e}")
            fail_blink()


if __name__ == "__main__":
    try:
        main()
    finally:
        cleanup_gpio()
if __name__ == "__main__":
    try:
        main()
    finally:
        cleanup_gpio()

from datetime import datetime, date, time
import os
from dotenv import load_dotenv
from supabase import create_client
import re
import time as t

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# =====================
# LED (optional)
# =====================
try:
    import RPi.GPIO as GPIO
    LED_PIN = 18
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(LED_PIN, GPIO.OUT)

    def success():
        GPIO.output(LED_PIN, 1)
        t.sleep(0.3)
        GPIO.output(LED_PIN, 0)

    def fail():
        for _ in range(5):
            GPIO.output(LED_PIN, 1)
            t.sleep(0.1)
            GPIO.output(LED_PIN, 0)
            t.sleep(0.1)

except:
    def success(): pass
    def fail(): pass


# =====================
# Helpers
# =====================

def extract_id(raw):
    digits = re.findall(r"\d+", raw)
    if not digits:
        return None
    return max(digits, key=len)


def now():
    return datetime.now()


# =====================
# Core Logic
# =====================

def get_student(student_id):
    res = supabase.table("students_nokey").select("*").eq("id", student_id).execute()
    return res.data[0] if res.data else None


def swipe_in(student_id):
    now_time = now().isoformat()

    # Insert or update
    supabase.table("students_nokey").upsert({
        "id": student_id,
        "check_in_at": now_time,
        "check_out_at": None
    }).execute()

    # Log history
    supabase.table("student_status_history_nokey").insert({
        "id": student_id,
        "status": "IN",
        "status_recorded_at": now_time
    }).execute()

    print(f"{student_id} -> IN")
    success()


def swipe_out(student_id):
    now_time = now().isoformat()

    supabase.table("students_nokey").update({
        "check_out_at": now_time
    }).eq("id", student_id).execute()

    supabase.table("student_status_history_nokey").insert({
        "id": student_id,
        "status": "OUT",
        "status_recorded_at": now_time
    }).execute()

    print(f"{student_id} -> OUT")
    success()


def process_swipe(student_id):
    student = get_student(student_id)

    if student is None or student["check_in_at"] is None:
        swipe_in(student_id)
        return

    if student["check_out_at"] is None:
        swipe_out(student_id)
    else:
        swipe_in(student_id)


# =====================
# End of Day Auto Close
# =====================

def close_day():
    today = date.today().isoformat()
    end_time = datetime.combine(date.today(), time(23, 59, 59)).isoformat()

    res = supabase.table("students_nokey") \
        .select("*") \
        .is_("check_out_at", "null") \
        .execute()

    for r in res.data:
        supabase.table("students_nokey").update({
            "check_out_at": end_time
        }).eq("id", r["id"]).execute()

        supabase.table("student_status_history_nokey").insert({
            "id": r["id"],
            "status": "AUTO_OUT",
            "status_recorded_at": end_time
        }).execute()

    print("End of day closed")


# =====================
# Main Loop
# =====================

def main():
    print("Swipe system ready")

    while True:
        try:
            raw = input("Swipe: ")

            student_id = extract_id(raw)

            if not student_id:
                print("Invalid swipe")
                fail()
                continue

            process_swipe(student_id)

        except Exception as e:
            print("Error:", e)
            fail()


if __name__ == "__main__":
    main()

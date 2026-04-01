# Raspberry Pi Supabase Swipe App

This project lets a Raspberry Pi send swipe events to a Supabase project.

## Files
- `main.py` - main app
- `.env.example` - place your new Supabase URL and key here
- `requirements.txt` - Python packages
- `swipe-app.service` - systemd service for auto-start on boot

## 1. Download on the Raspberry Pi
```bash
git clone https://github.com/YOUR-USERNAME/YOUR-REPO-NAME.git pi_supabase_swipe_app
cd pi_supabase_swipe_app
```

## 2. Install dependencies
```bash
sudo apt update
sudo apt install -y python3-pip
pip3 install -r requirements.txt
```

## 3. Add your new Supabase destination
```bash
cp .env.example .env
nano .env
```

Replace:
- `SUPABASE_URL`
- `SUPABASE_KEY`
- table names if your new project uses different names

## 4. Test manually
```bash
python3 main.py
```

## 5. Make it run on startup
Copy the service file:
```bash
sudo cp swipe-app.service /etc/systemd/system/swipe-app.service
sudo systemctl daemon-reload
sudo systemctl enable swipe-app.service
sudo systemctl start swipe-app.service
```

Check status:
```bash
sudo systemctl status swipe-app.service
```

View logs:
```bash
journalctl -u swipe-app.service -f
```

## Current behavior
Each event writes to `SWIPE_TABLE` with these fields:
- `created_at`
- `student_id`
- `isTutor`
- `event_type`

If `ENABLE_DAILY_SUMMARY=true`, every `IN` event also updates `DAILY_TABLE`:
- `date`
- `number_of_occupancy`
- `total_tutoring_hours`

## Important
If your new Supabase project has different column names, edit `main.py` to match them exactly.

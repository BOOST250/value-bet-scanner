web: gunicorn dashboard:app --bind 0.0.0.0:$PORT
worker: python value_bet_alerts.py
sharp_signal: python stake_ws_scanner.py

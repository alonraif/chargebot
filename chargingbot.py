from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import time
import threading
import os
import json
from http.server import HTTPServer, BaseHTTPRequestHandler

# Initialize the Slack app with bot token from environment variable
app = App(token=os.environ["SLACK_BOT_TOKEN"])

# Threading lock to protect state from concurrent access
state_lock = threading.Lock()

# Shared bot state
charging_state = {
    "current_user": None,       # Slack user ID currently charging
    "start_time": None,         # Start timestamp of current session
    "queue": [],                # List of user IDs waiting
    "grace_timer": None         # Timer for grace period (currently unused)
}

# Configuration constants
CHARGE_DURATION = 90 * 60  # 90 minutes in seconds
GRACE_PERIOD = 5 * 60      # 5 minutes in seconds

# ------------------------
# Utility Functions
# ------------------------

def format_time_remaining(seconds):
    """Returns a nicely formatted time string (MM:SS) or 'Now' if expired."""
    if seconds <= 0:
        return "Now"
    mins = seconds // 60
    secs = seconds % 60
    return f"{int(mins):02}:{int(secs):02} remaining"

def notify_next_user():
    """Sends a notification to the next user in the queue and starts the grace period."""
    if charging_state["queue"]:
        next_user = charging_state["queue"].pop(0)
        charging_state["current_user"] = next_user
        charging_state["start_time"] = time.time() + GRACE_PERIOD

        # Inform user about grace period
        app.client.chat_postMessage(
            channel=next_user,
            text="🔌 The charger is now available. Please plug in within 5 minutes."
        )

        def delayed_start():
            time.sleep(GRACE_PERIOD)
            app.client.chat_postMessage(
                channel=next_user,
                text="⏱️ Your 90-minute charging session has started."
            )
        threading.Thread(target=delayed_start).start()

# ------------------------
# Slack Commands
# ------------------------

@app.command("/checkin")
def checkin(ack, body, say):
    """Starts a charging session if no one is currently charging."""
    ack()
    user_id = body["user_id"]

    if charging_state["current_user"] == user_id:
        say(f"<@{user_id}>, you're already in a charging session.")
        return

    if charging_state["current_user"]:
        say(f"<@{user_id}>, someone is already charging. Use `/request` to join the queue.")
        return

    # Start session
    charging_state["current_user"] = user_id
    charging_state["start_time"] = time.time()
    say(f"<@{user_id}> started a 90-minute charging session.")

    def charge_timer():
        time.sleep(CHARGE_DURATION - 10 * 60)
        if charging_state["current_user"] == user_id:
            app.client.chat_postMessage(channel=user_id, text="⏳ 10 minutes left in your charging session.")
        time.sleep(10 * 60)
        if charging_state["current_user"] == user_id:
            app.client.chat_postMessage(channel=user_id, text="⚠️ Your charging session has ended. Please disconnect your car.")
            charging_state["current_user"] = None
            charging_state["start_time"] = None
            notify_next_user()

    threading.Thread(target=charge_timer).start()

@app.command("/request")
def request(ack, body, say):
    """Adds the user to the queue, or starts charging if charger is free."""
    ack()
    user_id = body["user_id"]

    if user_id == charging_state["current_user"]:
        say(f"<@{user_id}>, you're already charging.")
        return

    if charging_state["current_user"] is None:
        charging_state["current_user"] = user_id
        charging_state["start_time"] = time.time()
        say(f"🟢 Charging queue was empty. <@{user_id}>, you're now checked in. Connect your car to the charger.")

        def charge_timer():
            time.sleep(CHARGE_DURATION - 10 * 60)
            if charging_state["current_user"] == user_id:
                app.client.chat_postMessage(channel=user_id, text="⏳ 10 minutes left in your charging session.")
            time.sleep(10 * 60)
            if charging_state["current_user"] == user_id:
                app.client.chat_postMessage(channel=user_id, text="⚠️ Your charging session has ended. Please disconnect your car.")
                charging_state["current_user"] = None
                charging_state["start_time"] = None
                notify_next_user()

        threading.Thread(target=charge_timer).start()
        return

    if user_id in charging_state["queue"]:
        say(f"<@{user_id}>, you are already in the queue.")
        return

    charging_state["queue"].append(user_id)
    position = len(charging_state["queue"])
    say(f"<@{user_id}> added to the charging queue at position {position}.")

@app.command("/endcharge")
def endcharge(ack, body, say):
    """Ends the current user's charging session early."""
    ack()
    user_id = body["user_id"]
    if charging_state["current_user"] != user_id:
        say(f"<@{user_id}>, you're not currently charging.")
        return
    say(f"<@{user_id}> has ended their charging session early.")
    charging_state["current_user"] = None
    charging_state["start_time"] = None
    notify_next_user()

@app.command("/exitqueue")
def exitqueue(ack, body, say):
    """Removes the user from the queue."""
    ack()
    user_id = body["user_id"]
    if user_id in charging_state["queue"]:
        charging_state["queue"].remove(user_id)
        say(f"<@{user_id}> left the queue.")
    else:
        say(f"<@{user_id}>, you're not in the queue.")

@app.command("/chargestatus")
def chargestatus(ack, body, say):
    """Displays the current charger and queue status."""
    ack()
    current = charging_state["current_user"]
    start_time = charging_state["start_time"]
    queue = charging_state["queue"]
    now = time.time()

    if current:
        time_remaining = int((start_time + CHARGE_DURATION) - now)
        msg = f"🔋 <@{current}> is currently charging. {format_time_remaining(time_remaining)}"
    else:
        msg = "🟢 The charger is currently available."

    if queue:
        queue_status = "\n".join([f"{i+1}. <@{uid}>" for i, uid in enumerate(queue)])
        msg += f"\n📋 Queue:\n{queue_status}"

    say(msg)

# ------------------------
# HTTP Server for Health/Status Pages
# ------------------------

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ChargingBot is alive and running.")
        elif self.path == "/status":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            with state_lock:
                now = time.time()
                user_ids = [charging_state["current_user"]] if charging_state["current_user"] else []
                user_ids += charging_state["queue"]

            user_map = {}
            for uid in user_ids:
                try:
                    user_info = app.client.users_info(user=uid)
                    profile = user_info["user"]["profile"]
                    display_name = profile.get("display_name") or profile.get("real_name") or user_info["user"]["name"]
                    user_map[uid] = display_name
                except Exception as e:
                    print(f"Failed to fetch name for {uid}: {e}")
                    user_map[uid] = uid

            with state_lock:
                state = {
                    "current_user": user_map.get(charging_state["current_user"]),
                    "start_time": charging_state["start_time"],
                    "time_remaining": int((charging_state["start_time"] + CHARGE_DURATION - now)) if charging_state["start_time"] else None,
                    "queue": [user_map.get(uid, uid) for uid in charging_state["queue"]],
                }
            self.wfile.write(json.dumps(state).encode())
        elif self.path == "/dashboard":
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            with open("dashboard.html", "rb") as f:
                self.wfile.write(f.read())
        else:
            self.send_response(404)
            self.end_headers()

def start_dummy_server():
    """Starts the local HTTP server for health checks (useful for render.com)."""
    server = HTTPServer(("0.0.0.0", 8080), HealthCheckHandler)
    server.serve_forever()

# ------------------------
# Main Execution
# ------------------------

if __name__ == "__main__":
    # Run HTTP server in background
    threading.Thread(target=start_dummy_server, daemon=True).start()

    # Start Slack bot via Socket Mode
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()

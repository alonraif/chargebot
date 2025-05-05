from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import time
import threading
import os


app = App(token=os.environ["SLACK_BOT_TOKEN"])


# Data structures
charging_state = {
    "current_user": None,
    "start_time": None,
    "queue": [],
    "grace_timer": None
}

CHARGE_DURATION = 90 * 60  # 90 minutes in seconds
GRACE_PERIOD = 5 * 60      # 5 minutes in seconds


# Utility: Format time remaining
def format_time_remaining(seconds):
    if seconds <= 0:
        return "Now"
    mins = seconds // 60
    secs = seconds % 60
    return f"{int(mins):02}:{int(secs):02} remaining"


# Utility: Notify next user
def notify_next_user():
    if charging_state["queue"]:
        next_user = charging_state["queue"].pop(0)
        charging_state["current_user"] = next_user
        charging_state["start_time"] = time.time() + GRACE_PERIOD
        app.client.chat_postMessage(
            channel=next_user,
            text="🔌 The charger is now available. Please plug in within 5 minutes."
        )
        # Wait 5 minutes before starting timer
        def delayed_start():
            time.sleep(GRACE_PERIOD)
            app.client.chat_postMessage(
                channel=next_user,
                text="⏱️ Your 90-minute charging session has started."
            )
        threading.Thread(target=delayed_start).start()


# Command: /checkin
@app.command("/checkin")
def checkin(ack, body, say):
    ack()
    user_id = body["user_id"]
    if charging_state["current_user"]:
        say(f"<@{user_id}>, someone is already charging. Use `/request` to join the queue.")
        return
    charging_state["current_user"] = user_id
    charging_state["start_time"] = time.time()
    say(f"<@{user_id}> started a 90-minute charging session.")
    # Timer thread
    def charge_timer():
        time.sleep(CHARGE_DURATION)
        if charging_state["current_user"] == user_id:
            app.client.chat_postMessage(
                channel=user_id,
                text="⚠️ Your charging session has ended. Please disconnect your car."
            )
            charging_state["current_user"] = None
            charging_state["start_time"] = None
            notify_next_user()
    threading.Thread(target=charge_timer).start()


# Command: /request
@app.command("/request")
def request(ack, body, say):
    ack()
    user_id = body["user_id"]

    # If user is already charging
    if user_id == charging_state["current_user"]:
        say(f"<@{user_id}>, you're already charging.")
        return

    # If no one is charging, allow the user to start immediately
    if charging_state["current_user"] is None:
        charging_state["current_user"] = user_id
        charging_state["start_time"] = time.time()
        say(f"🟢 Charging queue was empty. <@{user_id}>, you're now checked in. Connect your car to the charger.")

        def charge_timer():
            time.sleep(CHARGE_DURATION)
            if charging_state["current_user"] == user_id:
                app.client.chat_postMessage(
                    channel=user_id,
                    text="⚠️ Your charging session has ended. Please disconnect your car."
                )
                charging_state["current_user"] = None
                charging_state["start_time"] = None
                notify_next_user()

        threading.Thread(target=charge_timer).start()
        return

    # If already in queue
    if user_id in charging_state["queue"]:
        say(f"<@{user_id}>, you are already in the queue.")
        return

    # Add to queue
    charging_state["queue"].append(user_id)
    position = len(charging_state["queue"])
    say(f"<@{user_id}> added to the charging queue at position {position}.")



# Command: /endcharge
@app.command("/endcharge")
def endcharge(ack, body, say):
    ack()
    user_id = body["user_id"]
    if charging_state["current_user"] != user_id:
        say(f"<@{user_id}>, you're not currently charging.")
        return
    say(f"<@{user_id}> has ended their charging session early.")
    charging_state["current_user"] = None
    charging_state["start_time"] = None
    notify_next_user()


# Command: /exitqueue
@app.command("/exitqueue")
def exitqueue(ack, body, say):
    ack()
    user_id = body["user_id"]
    if user_id in charging_state["queue"]:
        charging_state["queue"].remove(user_id)
        say(f"<@{user_id}> left the queue.")
    else:
        say(f"<@{user_id}>, you're not in the queue.")


# ✅ Updated command: /chargestatus
@app.command("/chargestatus")
def chargestatus(ack, body, say):
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

# Start Socket Mode handler

if __name__ == "__main__":
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()




import os
import time
import threading
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import logging

# Setup basic logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(threadName)s - %(message)s')

# Initialize the Slack app with bot token from environment variable
# Ensure SLACK_BOT_TOKEN and SLACK_APP_TOKEN are set in your environment
try:
    slack_bot_token = os.environ["SLACK_BOT_TOKEN"]
    slack_app_token = os.environ["SLACK_APP_TOKEN"]
    app = App(token=slack_bot_token)
except KeyError as e:
    logging.error(f"{e} environment variable not set. The bot cannot start.")
    exit(1)

# Threading lock to protect state from concurrent access
state_lock = threading.Lock()

# Shared bot state
charging_state = {
    "current_user_id": None,  # Slack user ID currently in grace or charging
    "session_actual_charge_start_time": None,  # Timestamp when 90-min charge actually began
    "grace_period_end_time": None,  # Timestamp when grace period for current_user_id ends
    "active_session_stop_event": None,  # threading.Event() for the current session's timer thread
    "queue": [],  # List of user IDs waiting
}

# Configuration constants
CHARGE_DURATION = 90 * 60  # 90 minutes in seconds
GRACE_PERIOD = 5 * 60  # 5 minutes in seconds
TEN_MINUTE_WARNING_BEFORE_END = 10 * 60  # 10 minutes in seconds


# ------------------------
# Utility Functions
# ------------------------

def safe_post_message(client, channel, text):
    """Safely posts a message to Slack, handling potential errors."""
    try:
        client.chat_postMessage(channel=channel, text=text)
    except Exception as e:
        logging.error(f"Failed to send message to channel {channel}: {e}")


def format_time_remaining_for_status_display(current_user_id_in_state, grace_end_time_in_state,
                                             actual_charge_start_time_in_state):
    """Returns a nicely formatted time string for /chargestatus."""
    now = time.time()
    if current_user_id_in_state:
        if grace_end_time_in_state and now < grace_end_time_in_state:  # In grace period
            seconds = int(grace_end_time_in_state - now)
            prefix = "Grace period:"
            if seconds <= 0: return "Grace period ending now"
        elif actual_charge_start_time_in_state:  # Charging
            charge_end_time = actual_charge_start_time_in_state + CHARGE_DURATION
            seconds = int(charge_end_time - now)
            prefix = "Charging:"
            if seconds <= 0: return "Charging session ending now"
        else:
            # This case should ideally not be reached if a user is set.
            # Could happen briefly during state transitions if status is checked at an unlucky moment.
            logging.warning(
                f"Inconsistent state for user {current_user_id_in_state} in format_time_remaining_for_status_display.")
            return "Time status unavailable (transitional state)"

        mins_val = seconds // 60
        secs_val = seconds % 60
        return f"{prefix} {int(mins_val):02}:{int(secs_val):02} remaining"
    return "Charger available"


def _clear_current_session_internal():
    """
    Clears the current session state. MUST be called with state_lock held.
    Signals the active session thread to stop.
    """
    user_being_cleared = charging_state['current_user_id']
    logging.info(f"Clearing current session for user: {user_being_cleared or 'None'}.")
    if charging_state["active_session_stop_event"]:
        charging_state["active_session_stop_event"].set()  # Signal thread to stop
    charging_state["current_user_id"] = None
    charging_state["session_actual_charge_start_time"] = None
    charging_state["grace_period_end_time"] = None
    charging_state["active_session_stop_event"] = None


def _session_management_thread_target(user_id, grace_duration, charge_duration, stop_event):
    """
    Manages a single user's session (grace period, charging, warnings, auto-end).
    This function is the target for a thread.
    """
    current_thread_name = threading.current_thread().name
    logging.info(f"[{current_thread_name}] Session thread started for user {user_id} with grace {grace_duration}s.")

    try:
        # 1. Grace Period Handling
        if grace_duration > 0:
            # The grace_period_end_time was set in charging_state by the caller
            # We wait until this time, or until a stop signal is received
            wait_until_grace_end_time = time.time() + grace_duration  # Recalculate based on when thread actually starts

            while time.time() < wait_until_grace_end_time and not stop_event.is_set():
                time_to_wait = min(1, wait_until_grace_end_time - time.time())
                if time_to_wait <= 0: break
                stop_event.wait(timeout=time_to_wait)  # Check frequently

            if stop_event.is_set():
                logging.info(
                    f"[{current_thread_name}] Session for {user_id} stopped during grace period by external event.")
                return  # Session was ended early (e.g., by /endcharge)

            # Grace period finished naturally
            with state_lock:
                # Verify this thread is still authoritative for this user
                if charging_state["current_user_id"] != user_id or charging_state[
                    "active_session_stop_event"] != stop_event:
                    logging.info(
                        f"[{current_thread_name}] Session for {user_id} was taken over or cleared during grace. Thread exiting.")
                    return

                charging_state["session_actual_charge_start_time"] = time.time()  # Actual charge starts NOW
                charging_state["grace_period_end_time"] = None  # Grace period is over
                logging.info(
                    f"[{current_thread_name}] User {user_id} grace period ended. Actual charge started at {charging_state['session_actual_charge_start_time']}.")
            safe_post_message(app.client, channel=user_id, text="⏱️ Your 90-minute charging session has started.")
        else:  # No grace period was configured (currently all paths have grace)
            with state_lock:
                if charging_state["current_user_id"] != user_id or charging_state[
                    "active_session_stop_event"] != stop_event: return
                charging_state["session_actual_charge_start_time"] = time.time()
                charging_state["grace_period_end_time"] = None
            safe_post_message(app.client, channel=user_id,
                              text="⏱️ Your 90-minute charging session has started (no grace period).")

        # 2. Charging Period Handling
        # Read actual_charge_start_time once under lock for calculations
        with state_lock:
            actual_charge_start_time = charging_state["session_actual_charge_start_time"]
            if not actual_charge_start_time or charging_state["current_user_id"] != user_id:
                logging.warning(
                    f"[{current_thread_name}] User {user_id} charging period: inconsistent state (no start time or user changed). Exiting.")
                return

        # Wait for 10-minute warning time
        warning_notification_time = actual_charge_start_time + (charge_duration - TEN_MINUTE_WARNING_BEFORE_END)
        sleep_duration_to_warning = warning_notification_time - time.time()

        if sleep_duration_to_warning > 0:
            stop_event.wait(timeout=sleep_duration_to_warning)  # Wait, but wake up if event is set

        if stop_event.is_set():
            logging.info(f"[{current_thread_name}] Session for {user_id} stopped before 10-min warning could be sent.")
            return

        # Send 10-minute warning if still the active session
        with state_lock:
            if charging_state["current_user_id"] == user_id and charging_state[
                "active_session_stop_event"] == stop_event:
                logging.info(f"[{current_thread_name}] Sending 10-min warning to {user_id}.")
                safe_post_message(app.client, channel=user_id, text="⏳ 10 minutes left in your charging session.")
            else:  # Session ended or changed before warning
                logging.info(
                    f"[{current_thread_name}] Session for {user_id} changed before 10-min warning. Thread exiting.")
                return

        # Wait for session end
        session_should_end_time = actual_charge_start_time + charge_duration
        sleep_duration_to_end = session_should_end_time - time.time()

        if sleep_duration_to_end > 0:
            stop_event.wait(timeout=sleep_duration_to_end)

        if stop_event.is_set():  # Session ended early by /endcharge
            logging.info(f"[{current_thread_name}] Session for {user_id} stopped before natural end via stop_event.")
            return

        # Natural session end
        next_user_notified_from_thread = None
        with state_lock:
            if charging_state["current_user_id"] == user_id and charging_state[
                "active_session_stop_event"] == stop_event:
                logging.info(f"[{current_thread_name}] Session for {user_id} ended naturally.")
                safe_post_message(app.client, channel=user_id,
                                  text="⚠️ Your charging session has ended. Please disconnect your car.")
                _clear_current_session_internal()  # Clear this user's session details
                next_user_notified_from_thread = _start_next_user_session_from_queue_internal()  # Attempt to start next
            else:  # Session changed/cleared by other means just before natural end
                logging.info(
                    f"[{current_thread_name}] Session for {user_id} changed just before natural end. Thread exiting.")
                return

        if next_user_notified_from_thread:
            logging.info(
                f"[{current_thread_name}] Notifying next user {next_user_notified_from_thread} (from thread end).")
            safe_post_message(app.client, channel=next_user_notified_from_thread,
                              text=f"🔌 The charger is now available. Please plug in within {int(GRACE_PERIOD / 60)} minutes to start your session.")

    except Exception as e:
        logging.error(
            f"[{current_thread_name}] Unexpected error in _session_management_thread_target for {user_id}: {e}",
            exc_info=True)


def _start_user_session_flow_internal(user_id, has_grace_period):
    """
    Sets up state for a new user session and starts the management thread.
    MUST be called with state_lock held.
    """
    charging_state["current_user_id"] = user_id
    charging_state["active_session_stop_event"] = threading.Event()

    grace_to_pass = GRACE_PERIOD if has_grace_period else 0
    if has_grace_period:
        charging_state["grace_period_end_time"] = time.time() + GRACE_PERIOD
        charging_state["session_actual_charge_start_time"] = None  # Will be set after grace
    else:
        charging_state["grace_period_end_time"] = None
        charging_state["session_actual_charge_start_time"] = time.time()  # Starts immediately

    logging.info(
        f"Starting session flow for {user_id}. Grace: {has_grace_period}. Grace ends: {charging_state['grace_period_end_time']}.")

    session_event_for_thread = charging_state["active_session_stop_event"]  # Get ref before any potential unlock

    thread = threading.Thread(
        target=_session_management_thread_target,
        args=(user_id, grace_to_pass, CHARGE_DURATION, session_event_for_thread),
        daemon=True  # Allows main program to exit even if threads are running
    )
    thread.start()


def _start_next_user_session_from_queue_internal():
    """
    If queue not empty, pops next user, sets them as current, starts their session.
    MUST be called with state_lock held.
    Returns the user_id of the next user if one was started, else None.
    """
    if not charging_state["queue"]:
        logging.info("Queue is empty. No next user to process.")
        return None

    next_user_id = charging_state["queue"].pop(0)
    _start_user_session_flow_internal(next_user_id, has_grace_period=True)
    logging.info(f"Promoted {next_user_id} from queue to active session with grace period.")
    return next_user_id


# ------------------------
# Slack Commands
# ------------------------

@app.command("/checkin")
def checkin_command(ack, body, say):
    """Starts a charging session (with grace period) if charger is free."""
    ack()
    user_id = body["user_id"]
    user_name = body.get("user_name", user_id)

    with state_lock:
        if charging_state["current_user_id"] == user_id:
            say(f"<@{user_id}>, you are already the active user for the charger.")
            return

        if charging_state["current_user_id"] is not None:
            say(f"<@{user_id}>, <@{charging_state['current_user_id']}> is currently using or has reserved the charger. Use `/request` to join the queue.")
            return

        # Charger is free
        logging.info(f"/checkin by {user_name} ({user_id}). Charger free. Starting session with grace.")
        _start_user_session_flow_internal(user_id, has_grace_period=True)

    # Message sent outside lock
    say(f"<@{user_id}>, you've checked in. 🔌 The charger is now reserved for you. Please plug in within {int(GRACE_PERIOD / 60)} minutes to start your charging session.")


@app.command("/request")
def request_command(ack, body, say):
    """Adds user to queue, or starts charging (with grace) if charger is free."""
    ack()
    user_id = body["user_id"]
    user_name = body.get("user_name", user_id)
    message_to_send = ""

    with state_lock:
        if charging_state["current_user_id"] == user_id:
            message_to_send = f"<@{user_id}>, you are already the active user for the charger."
        elif user_id in charging_state["queue"]:
            message_to_send = f"<@{user_id}>, you are already in the queue."
        elif charging_state["current_user_id"] is None:
            # Charger is free, start session directly
            logging.info(f"/request by {user_name} ({user_id}). Charger free. Starting session with grace.")
            _start_user_session_flow_internal(user_id, has_grace_period=True)
            message_to_send = f"🟢 Charging queue was empty. <@{user_id}>, the charger is now reserved for you. Please plug in within {int(GRACE_PERIOD / 60)} minutes."
        else:
            # Charger is busy, add to queue
            charging_state["queue"].append(user_id)
            position = len(charging_state["queue"])
            logging.info(f"/request by {user_name} ({user_id}). Added to queue at position {position}.")
            message_to_send = f"<@{user_id}>, you've been added to the charging queue at position {position}."

    if message_to_send:
        say(message_to_send)


@app.command("/endcharge")
def endcharge_command(ack, body, say):
    """Ends the current user's charging session early."""
    ack()
    user_id = body["user_id"]
    user_name = body.get("user_name", user_id)

    ended_early_msg = ""
    next_user_to_notify_id = None

    with state_lock:
        if charging_state["current_user_id"] != user_id:
            say(f"<@{user_id}>, you are not the one currently using or reserved for the charger.")
            return

        logging.info(f"/endcharge by {user_name} ({user_id}). Ending their session early.")
        _clear_current_session_internal()  # Stops thread, clears current user state
        ended_early_msg = f"<@{user_id}> has ended their charging session early."

        next_user_to_notify_id = _start_next_user_session_from_queue_internal()

    say(ended_early_msg)  # Announce the end of the previous session
    if next_user_to_notify_id:
        logging.info(f"Notifying next user {next_user_to_notify_id} (after /endcharge).")
        safe_post_message(app.client, channel=next_user_to_notify_id,
                          text=f"🔌 The charger is now available (previous user ended early). Please plug in within {int(GRACE_PERIOD / 60)} minutes to start your session.")


@app.command("/exitqueue")
def exitqueue_command(ack, body, say):
    """Removes the user from the queue."""
    ack()
    user_id = body["user_id"]
    user_name = body.get("user_name", user_id)

    with state_lock:
        if user_id in charging_state["queue"]:
            charging_state["queue"].remove(user_id)
            logging.info(f"/exitqueue by {user_name} ({user_id}). Removed from queue.")
            say(f"<@{user_id}>, you have been removed from the queue.")
        else:
            say(f"<@{user_id}>, you are not in the queue.")


@app.command("/chargestatus")
def chargestatus_command(ack, body, say):
    """Displays the current charger and queue status."""
    ack()
    msg_parts = []
    # Read state under lock and make copies to release lock quickly
    with state_lock:
        current_id_copy = charging_state["current_user_id"]
        grace_time_copy = charging_state["grace_period_end_time"]
        charge_start_time_copy = charging_state["session_actual_charge_start_time"]
        queue_list_copy = list(charging_state["queue"])

    if current_id_copy:
        time_status_str = format_time_remaining_for_status_display(current_id_copy, grace_time_copy,
                                                                   charge_start_time_copy)
        msg_parts.append(f"🔋 <@{current_id_copy}> is the active user. {time_status_str}")
    else:
        msg_parts.append("🟢 The charger is currently available.")

    if queue_list_copy:
        queue_status_lines = "\n".join([f"{i + 1}. <@{uid}>" for i, uid in enumerate(queue_list_copy)])
        msg_parts.append(f"\n📋 Queue:\n{queue_status_lines}")
    else:
        msg_parts.append("\nQueue is empty.")

    say("\n".join(msg_parts))


# ------------------------
# HTTP Server for Health/Status Pages
# ------------------------
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ChargingBot is alive and running.")
        elif self.path == "/status":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()

            with state_lock:  # Access state safely
                now = time.time()
                is_in_grace = charging_state["grace_period_end_time"] is not None and now < charging_state[
                    "grace_period_end_time"]

                grace_remaining_s = None
                if is_in_grace:
                    grace_remaining_s = int(charging_state['grace_period_end_time'] - now)

                charge_remaining_s = None
                if not is_in_grace and charging_state["session_actual_charge_start_time"]:
                    charge_end = charging_state["session_actual_charge_start_time"] + CHARGE_DURATION
                    if now < charge_end:
                        charge_remaining_s = int(charge_end - now)

                state_for_json = {
                    "current_user_id": charging_state["current_user_id"],
                    "is_in_grace_period": is_in_grace,
                    "grace_period_ends_in_seconds": grace_remaining_s,
                    "charge_session_started_at_unix": charging_state["session_actual_charge_start_time"],
                    "charge_session_remaining_seconds": charge_remaining_s,
                    "queue": list(charging_state["queue"]),  # Send copy
                    "queue_length": len(charging_state["queue"])
                }
            self.wfile.write(json.dumps(state_for_json, indent=2).encode())
        elif self.path == "/dashboard":
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            try:
                # Create a simple dashboard.html or point to a more complex one
                with open("dashboard.html", "rb") as f:
                    self.wfile.write(f.read())
            except FileNotFoundError:
                self.wfile.write(b"<html><body><h1>Dashboard</h1><p>dashboard.html not found.</p>"
                                 b"<p>Create dashboard.html and refresh.</p>"
                                 b"<p><a href='/status'>View JSON Status</a></p></body></html>")
        else:
            self.send_response(404)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Not Found")


def start_http_server_func():
    """Starts the local HTTP server for health checks."""
    port = int(os.environ.get("PORT", 8080))  # For compatibility with services like Render
    server_address = ("0.0.0.0", port)
    httpd = HTTPServer(server_address, HealthCheckHandler)
    logging.info(f"Starting HTTP server on port {port}...")
    try:
        httpd.serve_forever()
    except Exception as e:
        logging.error(f"HTTP server failed: {e}", exc_info=True)


# ------------------------
# Main Execution
# ------------------------
if __name__ == "__main__":
    logging.info("Starting EV Charging Bot...")

    # Run HTTP server in a background daemon thread
    http_server_thread = threading.Thread(target=start_http_server_func, daemon=True)
    http_server_thread.start()

    # Start Slack bot via Socket Mode Handler
    socket_mode_handler = SocketModeHandler(app, slack_app_token)
    try:
        logging.info("Connecting to Slack via Socket Mode...")
        socket_mode_handler.start()
    except Exception as e:
        logging.critical(f"Failed to start SocketModeHandler: {e}", exc_info=True)
        # Attempt to stop the HTTP server thread gracefully if possible, though it's a daemon
        # os._exit(1) might be needed if threads don't stop main program exit
        exit(1)
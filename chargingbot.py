import os
import time
import threading
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import urllib.parse
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
CHARGE_DURATION = 120 * 60  # 120 minutes in seconds
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
            authoritative_grace_end_time = None
            with state_lock:
                # Check if session is still valid and retrieve the authoritative grace_period_end_time
                if charging_state["current_user_id"] == user_id and \
                        charging_state["active_session_stop_event"] == stop_event and \
                        charging_state["grace_period_end_time"] is not None:
                    authoritative_grace_end_time = charging_state["grace_period_end_time"]
                else:
                    logging.info(
                        f"[{current_thread_name}] Session for {user_id} is no longer active or grace_period_end_time not set before grace wait. Thread exiting.")
                    return

            logging.info(
                f"[{current_thread_name}] User {user_id} entering grace period wait. Scheduled end: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(authoritative_grace_end_time))}.")

            while time.time() < authoritative_grace_end_time and not stop_event.is_set():
                time_to_wait_for_event = min(1.0, authoritative_grace_end_time - time.time())
                if time_to_wait_for_event <= 0:  # Should be caught by outer loop, but defensive
                    break
                stop_event.wait(timeout=time_to_wait_for_event)  # Check frequently

            if stop_event.is_set():
                logging.info(
                    f"[{current_thread_name}] Session for {user_id} stopped during grace period by external event.")
                return

            # Grace period finished naturally. Now, transition to actual charging.
            # Lock is needed to modify shared state and re-verify authority.
            with state_lock:
                if charging_state["current_user_id"] != user_id or \
                        charging_state["active_session_stop_event"] != stop_event:
                    logging.info(
                        f"[{current_thread_name}] Session for {user_id} was taken over or cleared during grace period wait. Actual charge will not start. Thread exiting.")
                    return

                # Defensive check, though loop condition should ensure this
                if time.time() < authoritative_grace_end_time:
                    logging.warning(
                        f"[{current_thread_name}] Exited grace wait for {user_id} but time {time.time():.2f} is still before target {authoritative_grace_end_time:.2f}. Proceeding with caution.")

                charging_state["session_actual_charge_start_time"] = time.time()
                charging_state["grace_period_end_time"] = None  # Grace period is over
                logging.info(
                    f"[{current_thread_name}] User {user_id} grace period ended. Actual charge started at {charging_state['session_actual_charge_start_time']:.2f}.")
            safe_post_message(app.client, channel=user_id, text="⏱️ Your 90-minute charging session has started.")
        else:  # No grace period was configured (currently all paths have grace for new sessions)
            with state_lock:
                # Verify this thread is still authoritative
                if charging_state["current_user_id"] != user_id or charging_state[
                    "active_session_stop_event"] != stop_event:
                    logging.info(
                        f"[{current_thread_name}] Session for {user_id} (no grace) changed before actual start. Thread exiting.")
                    return
                # If _start_user_session_flow_internal set this for no_grace, this is redundant but harmless.
                # If it didn't, this is where it's set.
                if charging_state["session_actual_charge_start_time"] is None:  # Only set if not already set by caller
                    charging_state["session_actual_charge_start_time"] = time.time()
                charging_state["grace_period_end_time"] = None  # Ensure it's None
                logging.info(
                    f"[{current_thread_name}] User {user_id} starting charge (no grace) at {charging_state['session_actual_charge_start_time']:.2f}.")

            safe_post_message(app.client, channel=user_id,
                              text="⏱️ Your 90-minute charging session has started (no grace period).")

        # 2. Charging Period Handling
        actual_charge_start_time_for_calc = None
        with state_lock:
            # Re-read actual_charge_start_time, ensure session still valid
            if charging_state["current_user_id"] != user_id or \
                    charging_state["active_session_stop_event"] != stop_event or \
                    charging_state["session_actual_charge_start_time"] is None:
                logging.warning(
                    f"[{current_thread_name}] User {user_id} charging period: inconsistent state or session ended. Exiting.")
                return
            actual_charge_start_time_for_calc = charging_state["session_actual_charge_start_time"]

        warning_notification_time = actual_charge_start_time_for_calc + (
                    charge_duration - TEN_MINUTE_WARNING_BEFORE_END)
        sleep_duration_to_warning = warning_notification_time - time.time()

        if sleep_duration_to_warning > 0:
            stop_event.wait(timeout=sleep_duration_to_warning)

        if stop_event.is_set():
            logging.info(f"[{current_thread_name}] Session for {user_id} stopped before 10-min warning could be sent.")
            return

        # Send 10-minute warning if still the active session
        with state_lock:
            if charging_state["current_user_id"] == user_id and \
                    charging_state["active_session_stop_event"] == stop_event:
                logging.info(f"[{current_thread_name}] Sending 10-min warning to {user_id}.")
                safe_post_message(app.client, channel=user_id, text="⏳ 10 minutes left in your charging session.")
            else:
                logging.info(
                    f"[{current_thread_name}] Session for {user_id} changed before 10-min warning. Thread exiting.")
                return

        session_should_end_time = actual_charge_start_time_for_calc + charge_duration
        sleep_duration_to_end = session_should_end_time - time.time()

        if sleep_duration_to_end > 0:
            stop_event.wait(timeout=sleep_duration_to_end)

        if stop_event.is_set():
            logging.info(f"[{current_thread_name}] Session for {user_id} stopped before natural end via stop_event.")
            return

        # Natural session end
        next_user_notified_from_thread = None
        with state_lock:
            if charging_state["current_user_id"] == user_id and \
                    charging_state["active_session_stop_event"] == stop_event:
                logging.info(f"[{current_thread_name}] Session for {user_id} ended naturally.")
                safe_post_message(app.client, channel=user_id,
                                  text="⚠️ Your charging session has ended. Please disconnect your car.")
                _clear_current_session_internal()
                next_user_notified_from_thread = _start_next_user_session_from_queue_internal()
            else:
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
        # Consider additional cleanup or notification if thread dies unexpectedly
        # For example, ensure the session is marked as problematic or cleared.
        with state_lock:
            if charging_state.get("current_user_id") == user_id and charging_state.get(
                    "active_session_stop_event") == stop_event:
                logging.error(
                    f"[{current_thread_name}] Attempting to clear session for {user_id} due to unexpected error.")
                _clear_current_session_internal()
                # Potentially try to start next user, or just leave charger as "available"
                # next_user = _start_next_user_session_from_queue_internal()
                # if next_user: safe_post_message(...) etc.


def _start_user_session_flow_internal(user_id, has_grace_period):
    """
    Sets up state for a new user session and starts the management thread.
    MUST be called with state_lock held.
    """
    # If there's an existing session for another user, it should have been cleared before calling this.
    # If this user already has a session, this might be a re-entry; ensure old event is handled if necessary.
    # However, current command logic prevents this by checking current_user_id.
    if charging_state["active_session_stop_event"]:
        logging.warning(
            f"New session flow for {user_id} starting, but an active_session_stop_event already exists. Signaling old event.")
        charging_state["active_session_stop_event"].set()  # Ensure any lingering thread is stopped

    charging_state["current_user_id"] = user_id
    new_stop_event = threading.Event()
    charging_state["active_session_stop_event"] = new_stop_event

    grace_to_pass = GRACE_PERIOD if has_grace_period else 0
    if has_grace_period:
        charging_state["grace_period_end_time"] = time.time() + GRACE_PERIOD
        charging_state["session_actual_charge_start_time"] = None  # Will be set after grace
    else:
        charging_state["grace_period_end_time"] = None
        charging_state["session_actual_charge_start_time"] = time.time()  # Starts immediately

    logging.info(
        f"Starting session flow for {user_id}. Grace: {has_grace_period}. Grace ends: {charging_state['grace_period_end_time']}. Actual start: {charging_state['session_actual_charge_start_time']}.")

    thread = threading.Thread(
        target=_session_management_thread_target,
        args=(user_id, grace_to_pass, CHARGE_DURATION, new_stop_event),  # Pass the new_stop_event
        daemon=True
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
    # Clear any remnants of a previous session for safety, though _clear_current_session_internal should handle.
    # This function (_start_user_session_flow_internal) will set up the new session.
    _start_user_session_flow_internal(next_user_id, has_grace_period=True)
    logging.info(f"Promoted {next_user_id} from queue to active session with grace period.")
    return next_user_id


# --- User info caching and fetching ---
user_info_cache = {}  # Simple dict cache: {"U123": {"name": "User A", "timestamp": time.time()}}
CACHE_TTL_SECONDS = 15 * 60  # Cache user info for 15 minutes


def get_user_display_name(user_id, slack_client):
    if not user_id:
        return "Unknown User"

    now = time.time()
    cached_entry = user_info_cache.get(user_id)
    if cached_entry and (now - cached_entry["timestamp"] < CACHE_TTL_SECONDS):
        return cached_entry["name"]

    try:
        user_info_response = slack_client.users_info(user=user_id)
        if user_info_response and user_info_response["ok"]:
            user_data = user_info_response["user"]
            display_name = user_data.get("profile", {}).get("display_name", "")
            real_name = user_data.get("real_name", "")
            name_to_return = display_name if display_name else real_name
            if not name_to_return: name_to_return = user_id  # Fallback to ID if no name found

            user_info_cache[user_id] = {"name": name_to_return, "timestamp": now}
            return name_to_return
        else:
            logging.warning(
                f"Slack API error for users.info (user: {user_id}) in get_user_display_name: {user_info_response.get('error', 'Unknown error') if user_info_response else 'Empty response'}")
            # Cache failure for a shorter period to avoid hammering on persistent errors
            user_info_cache[user_id] = {"name": user_id, "timestamp": now}
            return user_id  # Fallback to user_id
    except Exception as e:
        logging.error(f"Exception fetching user info for {user_id}: {e}", exc_info=True)
        user_info_cache[user_id] = {"name": user_id, "timestamp": now}
        return user_id  # Fallback to user_id


# --- End of user info caching section ---

# ------------------------
# Slack Commands
# ------------------------
# The JavaScript block app.get('/slack/username', ...) was removed as it's not valid Python.
# If username fetching is needed, use app.client.users_info within Python functions.

@app.command("/checkin")
def checkin_command(ack, body, say):
    ack()
    user_id = body["user_id"]
    user_name = body.get("user_name", user_id)  # For logging

    with state_lock:
        if charging_state["current_user_id"] == user_id:
            say(f"<@{user_id}>, you are already the active user for the charger.")
            return

        if charging_state["current_user_id"] is not None:
            active_user = charging_state["current_user_id"]
            say(f"<@{user_id}>, <@{active_user}> is currently using or has reserved the charger. Use `/request` to join the queue.")
            return

        logging.info(f"/checkin by {user_name} ({user_id}). Charger free. Starting session with grace.")
        _start_user_session_flow_internal(user_id, has_grace_period=True)

    say(f"<@{user_id}>, you've checked in. 🔌 The charger is now reserved for you. Please plug in within {int(GRACE_PERIOD / 60)} minutes to start your charging session.")


@app.command("/request")
def request_command(ack, body, say):
    ack()
    user_id = body["user_id"]
    user_name = body.get("user_name", user_id)  # For logging
    message_to_send = ""

    with state_lock:
        if charging_state["current_user_id"] == user_id:
            message_to_send = f"<@{user_id}>, you are already the active user for the charger."
        elif user_id in charging_state["queue"]:
            message_to_send = f"<@{user_id}>, you are already in the queue."
        elif charging_state["current_user_id"] is None:
            logging.info(f"/request by {user_name} ({user_id}). Charger free. Starting session with grace.")
            _start_user_session_flow_internal(user_id, has_grace_period=True)
            message_to_send = f"🟢 Charging queue was empty. <@{user_id}>, the charger is now reserved for you. Please plug in within {int(GRACE_PERIOD / 60)} minutes."
        else:
            if not user_id or not isinstance(user_id, str):  # Defensive check
                logging.error(f"/request: Invalid user_id '{user_id}' received. Not adding to queue.")
                say(f"Sorry, <@{body['user_id']}>, there was an issue with your request (invalid user identifier). Please try again.")
                return
            charging_state["queue"].append(user_id)
            position = len(charging_state["queue"])
            logging.info(f"/request by {user_name} ({user_id}). Added to queue at position {position}.")
            message_to_send = f"<@{user_id}>, you've been added to the charging queue at position {position}."

    if message_to_send:
        say(message_to_send)


@app.command("/endcharge")
def endcharge_command(ack, body, say):
    ack()
    user_id = body["user_id"]
    user_name = body.get("user_name", user_id)  # For logging

    ended_early_msg = ""
    next_user_to_notify_id = None

    with state_lock:
        if charging_state["current_user_id"] != user_id:
            say(f"<@{user_id}>, you are not the one currently using or reserved for the charger.")
            return

        logging.info(f"/endcharge by {user_name} ({user_id}). Ending their session early.")
        _clear_current_session_internal()  # Stops thread, clears current user state
        ended_early_msg = f"<@{user_id}> has ended their charging session early."  # Corrected to use user_id

        next_user_to_notify_id = _start_next_user_session_from_queue_internal()

    say(ended_early_msg)
    if next_user_to_notify_id:
        logging.info(f"Notifying next user {next_user_to_notify_id} (after /endcharge).")
        safe_post_message(app.client, channel=next_user_to_notify_id,
                          text=f"🔌 The charger is now available (previous user ended early). Please plug in within {int(GRACE_PERIOD / 60)} minutes to start your session.")


@app.command("/exitqueue")
def exitqueue_command(ack, body, say):
    ack()
    user_id = body["user_id"]
    user_name = body.get("user_name", user_id)  # For logging

    with state_lock:
        if user_id in charging_state["queue"]:
            charging_state["queue"].remove(user_id)
            logging.info(f"/exitqueue by {user_name} ({user_id}). Removed from queue.")
            say(f"<@{user_id}>, you have been removed from the queue.")
        else:
            say(f"<@{user_id}>, you are not in the queue.")


@app.command("/chargestatus")
def chargestatus_command(ack, body, say):
    ack()
    msg_parts = []
    with state_lock:
        current_id_copy = charging_state["current_user_id"]
        grace_time_copy = charging_state["grace_period_end_time"]
        charge_start_time_copy = charging_state["session_actual_charge_start_time"]
        queue_list_copy = list(charging_state["queue"])  # Create a copy for safe iteration

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

                current_user_name_display = "N/A"
                if charging_state["current_user_id"]:
                    # Pass app.client to the function
                    current_user_name_display = get_user_display_name(charging_state["current_user_id"], app.client)

                is_in_grace = False
                grace_remaining_s = 0  # Default to 0
                if charging_state["grace_period_end_time"] is not None and now < charging_state[
                    "grace_period_end_time"]:
                    is_in_grace = True
                    grace_remaining_s = int(charging_state['grace_period_end_time'] - now)

                is_currently_charging = False
                charge_remaining_s = 0  # Default to 0
                if not is_in_grace and charging_state["session_actual_charge_start_time"]:
                    charge_end_time = charging_state["session_actual_charge_start_time"] + CHARGE_DURATION
                    if now < charge_end_time:
                        is_currently_charging = True
                        charge_remaining_s = int(charge_end_time - now)
                    # else: charge already ended, remaining is 0

                queue_with_names = []
                for uid_in_queue in charging_state["queue"]:
                    queue_with_names.append({
                        "id": uid_in_queue,
                        "name": get_user_display_name(uid_in_queue, app.client)  # Pass app.client
                    })

                state_for_json = {
                    "current_user_id": charging_state["current_user_id"],
                    "current_user_name": current_user_name_display,  # ADDED for display name
                    "is_in_grace_period": is_in_grace,
                    "grace_period_ends_at_unix": charging_state["grace_period_end_time"],
                    "grace_period_remaining_seconds": grace_remaining_s,
                    "is_charging": is_currently_charging,  # ADDED for clarity
                    "charge_session_started_at_unix": charging_state["session_actual_charge_start_time"],
                    "charge_session_duration_seconds": CHARGE_DURATION if charging_state[
                        "session_actual_charge_start_time"] else None,
                    "charge_session_remaining_seconds": charge_remaining_s,
                    "queue": queue_with_names,  # MODIFIED to include names
                    "queue_length": len(charging_state["queue"])
                }
            self.wfile.write(json.dumps(state_for_json, indent=2).encode())
        # ... (rest of your /dashboard and 404 handler)
        elif self.path == "/dashboard":
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            try:
                with open("dashboard.html", "rb") as f:  # Assumes dashboard.html is in the same directory
                    self.wfile.write(f.read())
            except FileNotFoundError:
                self.wfile.write(b"<html><body><h1>Dashboard</h1><p>dashboard.html not found.</p>"
                                 b"<p>Create dashboard.html and place it in the same directory as the bot script.</p>"
                                 b"<p><a href='/status'>View JSON Status</a></p></body></html>")
        else:
            self.send_response(404)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Not Found")


def start_http_server_func():
    port = int(os.environ.get("PORT", 8080))
    server_address = ("0.0.0.0", port)
    httpd = HTTPServer(server_address, HealthCheckHandler)
    logging.info(f"Starting HTTP server on port {port}...")
    try:
        httpd.serve_forever()
    except Exception as e:
        logging.error(f"HTTP server failed: {e}", exc_info=True)
        # If the HTTP server fails critically, it might be worth signaling the main app to shut down.
        # However, as a daemon thread, its failure won't stop the main Slack bot thread directly.


# ------------------------
# Main Execution
# ------------------------
if __name__ == "__main__":
    logging.info("Starting EV Charging Bot...")

    http_server_thread = threading.Thread(target=start_http_server_func, daemon=True)
    http_server_thread.start()

    socket_mode_handler = SocketModeHandler(app, slack_app_token)
    try:
        logging.info("Connecting to Slack via Socket Mode...")
        socket_mode_handler.start()  # This will block until the app stops
    except Exception as e:
        logging.critical(f"Failed to start SocketModeHandler: {e}", exc_info=True)
        # Consider more robust shutdown if HTTP server also needs to be explicitly stopped
        # httpd.shutdown() from another thread if httpd instance was accessible globally.
        # For daemon threads, exit(1) will terminate them.
        exit(1)
    finally:
        logging.info("EV Charging Bot is shutting down.")
        # If httpd had a shutdown method and was accessible, call it here.
        # For daemon threads, they will be terminated when the main program exits.
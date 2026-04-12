import os
import time
import threading
import json
import smtplib
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone
from email.message import EmailMessage
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
    "session_actual_charge_start_time": None,  # Timestamp when 120-min charge actually began
    "grace_period_end_time": None,  # Timestamp when grace period for current_user_id ends
    "active_session_stop_event": None,  # threading.Event() for the current session's timer thread
    "disconnect_reminder_invite_uid": None,  # Calendar invite UID for the active session
    "disconnect_reminder_invite_sent": False,  # Whether the active session's invite was sent successfully
    "disconnect_reminder_invite_status": "idle",  # idle, pending, sent, skipped_no_smtp, skipped_no_email, failed
    "disconnect_reminder_invite_error": None,  # Last reminder invite error for the active session
    "queue": [],  # List of user IDs waiting
}

# Configuration constants
CHARGE_DURATION = 120 * 60  # 120 minutes in seconds
GRACE_PERIOD = 5 * 60  # 5 minutes in seconds
TEN_MINUTE_WARNING_BEFORE_END = 10 * 60  # 10 minutes in seconds

# SMTP configuration for reminder invites. These are optional until invite sending is enabled.
SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
SMTP_FROM_EMAIL = os.environ.get("SMTP_FROM_EMAIL")
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").lower() in ("1", "true", "yes", "on")


def smtp_config_is_ready():
    required_values = (SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, SMTP_FROM_EMAIL)
    return all(required_values)


def log_smtp_configuration_status():
    if smtp_config_is_ready():
        logging.info("SMTP configuration detected. Reminder invite delivery can be enabled.")
        return

    missing_settings = [
        key for key, value in (
            ("SMTP_HOST", SMTP_HOST),
            ("SMTP_USERNAME", SMTP_USERNAME),
            ("SMTP_PASSWORD", SMTP_PASSWORD),
            ("SMTP_FROM_EMAIL", SMTP_FROM_EMAIL),
        ) if not value
    ]
    logging.info(
        "SMTP configuration incomplete. Missing: %s. Reminder invite delivery will stay disabled until these are set.",
        ", ".join(missing_settings)
    )


def _format_ics_datetime(unix_timestamp):
    return datetime.fromtimestamp(unix_timestamp, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _escape_ics_text(value):
    if value is None:
        return ""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(";", r"\;")
        .replace(",", r"\,")
        .replace("\r\n", r"\n")
        .replace("\n", r"\n")
    )


def build_disconnect_reminder_ics(event_uid, attendee_email, event_start_unix, event_end_unix):
    created_at = _format_ics_datetime(time.time())
    event_start = _format_ics_datetime(event_start_unix)
    event_end = _format_ics_datetime(event_end_unix)

    summary = _escape_ics_text("Disconnect car from charger")
    description = _escape_ics_text(
        "Your charging session has ended. Please disconnect your car from the shared charger."
    )
    organizer_email = _escape_ics_text(SMTP_FROM_EMAIL or "")
    attendee_email = _escape_ics_text(attendee_email)
    event_uid = _escape_ics_text(event_uid)

    return "\r\n".join([
        "BEGIN:VCALENDAR",
        "PRODID:-//ChargingBot//Disconnect Reminder//EN",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{event_uid}",
        f"DTSTAMP:{created_at}",
        f"DTSTART:{event_start}",
        f"DTEND:{event_end}",
        f"SUMMARY:{summary}",
        f"DESCRIPTION:{description}",
        f"ORGANIZER:mailto:{organizer_email}",
        f"ATTENDEE;ROLE=REQ-PARTICIPANT;RSVP=TRUE:mailto:{attendee_email}",
        "STATUS:CONFIRMED",
        "TRANSP:OPAQUE",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ])


def build_disconnect_reminder_email(recipient_email, event_uid, event_start_unix, event_end_unix):
    if not SMTP_FROM_EMAIL:
        raise ValueError("SMTP_FROM_EMAIL is required to build reminder email.")

    ics_payload = build_disconnect_reminder_ics(
        event_uid=event_uid,
        attendee_email=recipient_email,
        event_start_unix=event_start_unix,
        event_end_unix=event_end_unix,
    )

    message = EmailMessage()
    message["Subject"] = "Reminder: disconnect your car from the charger"
    message["From"] = SMTP_FROM_EMAIL
    message["To"] = recipient_email
    message.set_content(
        "Your charging session is scheduled to end soon. A calendar invite is attached as a reminder to disconnect your car."
    )
    message.add_alternative(
        "<p>Your charging session is scheduled to end soon.</p>"
        "<p>A calendar invite is attached as a reminder to disconnect your car.</p>",
        subtype="html"
    )
    message.add_attachment(
        ics_payload.encode("utf-8"),
        maintype="text",
        subtype="calendar",
        filename="disconnect-reminder.ics",
        params={"method": "REQUEST", "charset": "UTF-8"}
    )
    return message


def send_disconnect_reminder_email(recipient_email, event_uid, event_start_unix, event_end_unix):
    if not smtp_config_is_ready():
        raise RuntimeError("SMTP configuration is incomplete.")

    message = build_disconnect_reminder_email(
        recipient_email=recipient_email,
        event_uid=event_uid,
        event_start_unix=event_start_unix,
        event_end_unix=event_end_unix,
    )

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
        smtp.ehlo()
        if SMTP_USE_TLS:
            smtp.starttls()
            smtp.ehlo()
        smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
        smtp.send_message(message)

    logging.info(
        "Sent disconnect reminder invite to %s for event UID %s.",
        recipient_email,
        event_uid
    )


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
    charging_state["disconnect_reminder_invite_uid"] = None
    charging_state["disconnect_reminder_invite_sent"] = False
    charging_state["disconnect_reminder_invite_status"] = "idle"
    charging_state["disconnect_reminder_invite_error"] = None


def _generate_disconnect_reminder_uid(user_id, session_start_unix):
    return f"disconnect-reminder-{user_id}-{int(session_start_unix)}@chargingbot"


def _send_disconnect_reminder_for_active_session(user_id, session_start_unix, invite_uid):
    if not smtp_config_is_ready():
        logging.info("Skipping disconnect reminder invite for %s because SMTP is not configured.", user_id)
        with state_lock:
            if charging_state["current_user_id"] == user_id and \
                    charging_state["session_actual_charge_start_time"] == session_start_unix and \
                    charging_state["disconnect_reminder_invite_uid"] == invite_uid:
                charging_state["disconnect_reminder_invite_status"] = "skipped_no_smtp"
                charging_state["disconnect_reminder_invite_error"] = "SMTP configuration is incomplete."
        return

    recipient_email = get_user_email(user_id, app.client)
    if not recipient_email:
        logging.warning("Skipping disconnect reminder invite for %s because no Slack email was found.", user_id)
        with state_lock:
            if charging_state["current_user_id"] == user_id and \
                    charging_state["session_actual_charge_start_time"] == session_start_unix and \
                    charging_state["disconnect_reminder_invite_uid"] == invite_uid:
                charging_state["disconnect_reminder_invite_status"] = "skipped_no_email"
                charging_state["disconnect_reminder_invite_error"] = "No Slack email address was found for the user."
        return

    event_start_unix = session_start_unix + CHARGE_DURATION
    event_end_unix = event_start_unix + GRACE_PERIOD

    try:
        send_disconnect_reminder_email(
            recipient_email=recipient_email,
            event_uid=invite_uid,
            event_start_unix=event_start_unix,
            event_end_unix=event_end_unix,
        )
    except Exception as e:
        logging.error(
            "Failed to send disconnect reminder invite to %s for user %s: %s",
            recipient_email,
            user_id,
            e,
            exc_info=True
        )
        with state_lock:
            if charging_state["current_user_id"] == user_id and \
                    charging_state["session_actual_charge_start_time"] == session_start_unix and \
                    charging_state["disconnect_reminder_invite_uid"] == invite_uid:
                charging_state["disconnect_reminder_invite_status"] = "failed"
                charging_state["disconnect_reminder_invite_error"] = str(e)
        return

    with state_lock:
        if charging_state["current_user_id"] == user_id and \
                charging_state["session_actual_charge_start_time"] == session_start_unix and \
                charging_state["disconnect_reminder_invite_uid"] == invite_uid:
            charging_state["disconnect_reminder_invite_sent"] = True
            charging_state["disconnect_reminder_invite_status"] = "sent"
            charging_state["disconnect_reminder_invite_error"] = None


def _prepare_disconnect_reminder_for_new_session(user_id, session_start_unix):
    invite_uid = None
    with state_lock:
        if charging_state["current_user_id"] != user_id or \
                charging_state["session_actual_charge_start_time"] != session_start_unix:
            return

        if charging_state["disconnect_reminder_invite_status"] != "pending" and \
                charging_state["disconnect_reminder_invite_sent"]:
            return

        if charging_state["disconnect_reminder_invite_status"] in (
                "sent", "skipped_no_smtp", "skipped_no_email", "failed"):
            return

        if charging_state["disconnect_reminder_invite_uid"] is None:
            charging_state["disconnect_reminder_invite_uid"] = _generate_disconnect_reminder_uid(
                user_id,
                session_start_unix
            )

        charging_state["disconnect_reminder_invite_status"] = "pending"
        charging_state["disconnect_reminder_invite_error"] = None
        invite_uid = charging_state["disconnect_reminder_invite_uid"]

    _send_disconnect_reminder_for_active_session(user_id, session_start_unix, invite_uid)


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
                session_start_time = charging_state["session_actual_charge_start_time"]
                logging.info(
                    f"[{current_thread_name}] User {user_id} grace period ended. Actual charge started at {charging_state['session_actual_charge_start_time']:.2f}.")
            _prepare_disconnect_reminder_for_new_session(user_id, session_start_time)
            safe_post_message(app.client, channel=user_id, text="⏱️ Your 120-minute charging session has started.")
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
                session_start_time = charging_state["session_actual_charge_start_time"]
                logging.info(
                    f"[{current_thread_name}] User {user_id} starting charge (no grace) at {charging_state['session_actual_charge_start_time']:.2f}.")

            _prepare_disconnect_reminder_for_new_session(user_id, session_start_time)
            safe_post_message(app.client, channel=user_id,
                              text="⏱️ Your 120-minute charging session has started (no grace period).")

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
    charging_state["disconnect_reminder_invite_uid"] = None
    charging_state["disconnect_reminder_invite_sent"] = False
    charging_state["disconnect_reminder_invite_status"] = "idle"
    charging_state["disconnect_reminder_invite_error"] = None

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


def _calculate_queue_availability_times_internal(is_charging, is_in_grace, current_user_id, queue, charge_start_time, grace_end_time):
    """
    Calculates the estimated availability time for each user in the queue.
    MUST be called with state_lock held.
    Returns a list of dictionaries, with each containing user 'id' and 'available_at_unix' timestamp.
    """
    now = time.time()
    estimated_next_available_time_unix = None

    if is_charging:
        # Current user is charging. Next slot is after their session ends.
        estimated_next_available_time_unix = charge_start_time + CHARGE_DURATION
    elif is_in_grace:
        # Current user is in grace. Next slot is after their grace period ends AND a full charge duration.
        estimated_next_available_time_unix = grace_end_time + CHARGE_DURATION
    elif not current_user_id and queue:
        # Charger is free, but there's a queue. This is a transitional state.
        # Assume the first person will start their grace period now, then charge.
        estimated_next_available_time_unix = now + GRACE_PERIOD + CHARGE_DURATION

    queue_with_times = []
    if not estimated_next_available_time_unix:
        return [{"id": uid, "available_at_unix": None} for uid in queue]

    session_duration_for_queue = CHARGE_DURATION + GRACE_PERIOD
    current_est_time = estimated_next_available_time_unix

    for uid_in_queue in queue:
        queue_with_times.append({"id": uid_in_queue, "available_at_unix": current_est_time})
        # Increment the estimated time for the next person in the queue.
        current_est_time += session_duration_for_queue

    return queue_with_times


# --- User info caching and fetching ---
user_info_cache = {}  # Simple dict cache: {"U123": {"display_name": "User A", "email": "user@example.com", "timestamp": time.time()}}
CACHE_TTL_SECONDS = 15 * 60  # Cache user info for 15 minutes


def _fetch_and_cache_user_info(user_id, slack_client):
    if not user_id:
        return None

    now = time.time()
    cached_entry = user_info_cache.get(user_id)
    if cached_entry and (now - cached_entry["timestamp"] < CACHE_TTL_SECONDS):
        return cached_entry

    try:
        user_info_response = slack_client.users_info(user=user_id)
        if user_info_response and user_info_response["ok"]:
            user_data = user_info_response["user"]
            profile_data = user_data.get("profile", {})
            display_name = profile_data.get("display_name", "") or user_data.get("real_name", "") or user_id
            email = profile_data.get("email")

            cached_entry = {
                "display_name": display_name,
                "email": email,
                "timestamp": now
            }
            user_info_cache[user_id] = cached_entry
            return cached_entry

        logging.warning(
            f"Slack API error for users.info (user: {user_id}) in _fetch_and_cache_user_info: "
            f"{user_info_response.get('error', 'Unknown error') if user_info_response else 'Empty response'}"
        )
    except Exception as e:
        logging.error(f"Exception fetching user info for {user_id}: {e}", exc_info=True)

    # Cache fallback for a shorter period to avoid hammering on persistent errors.
    fallback_entry = {
        "display_name": user_id,
        "email": None,
        "timestamp": now - (CACHE_TTL_SECONDS - 60)
    }
    user_info_cache[user_id] = fallback_entry
    return fallback_entry


def get_user_display_name(user_id, slack_client):
    if not user_id:
        return "Unknown User"

    user_info = _fetch_and_cache_user_info(user_id, slack_client)
    if not user_info:
        return user_id
    return user_info["display_name"]


def get_user_email(user_id, slack_client):
    if not user_id:
        return None

    user_info = _fetch_and_cache_user_info(user_id, slack_client)
    if not user_info:
        return None
    return user_info["email"]


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
    now = time.time()

    with state_lock:
        current_id_copy = charging_state["current_user_id"]
        grace_time_copy = charging_state["grace_period_end_time"]
        charge_start_time_copy = charging_state["session_actual_charge_start_time"]
        queue_list_copy = list(charging_state["queue"])  # Create a copy for safe iteration

        is_in_grace = grace_time_copy is not None and now < grace_time_copy
        is_charging = not is_in_grace and charge_start_time_copy and (now < charge_start_time_copy + CHARGE_DURATION)

        if current_id_copy:
            time_status_str = format_time_remaining_for_status_display(current_id_copy, grace_time_copy,
                                                                       charge_start_time_copy)
            msg_parts.append(f"🔋 <@{current_id_copy}> is the active user. {time_status_str}")
        else:
            msg_parts.append("🟢 The charger is currently available.")

        if queue_list_copy:
            queue_with_times = _calculate_queue_availability_times_internal(is_charging, is_in_grace, current_id_copy, queue_list_copy, charge_start_time_copy, grace_time_copy)
            queue_status_lines = []

            for i, user_info in enumerate(queue_with_times):
                line = f"{i + 1}. <@{user_info['id']}>"
                unix_timestamp = user_info.get("available_at_unix")
                if unix_timestamp:
                    # Use Slack's built-in date formatting.
                    # {date_short_pretty} at {time} -> "Today at 4:30 PM"
                    # This automatically handles time zones for each user.
                    line += f" (Est: <!date^{int(unix_timestamp)}^{{date_short_pretty}} at {{time}}|fallback text>)"
                queue_status_lines.append(line)

            if queue_status_lines:
                msg_parts.append("\n📋 Queue:\n" + "\n".join(queue_status_lines))
            else: # Should not happen if queue_list_copy is not empty, but for safety
                msg_parts.append("\nQueue is empty.")
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

                # Use the new shared function to calculate queue times
                queue_with_times = _calculate_queue_availability_times_internal(is_currently_charging, is_in_grace, charging_state["current_user_id"], charging_state["queue"], charging_state["session_actual_charge_start_time"], charging_state["grace_period_end_time"])
                
                queue_with_names = []
                for user_info in queue_with_times:
                    queue_with_names.append({
                        "id": user_info["id"],
                        "name": get_user_display_name(user_info["id"], app.client),
                        "available_at_unix": user_info["available_at_unix"]
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
                    "disconnect_reminder_invite_uid": charging_state["disconnect_reminder_invite_uid"],
                    "disconnect_reminder_invite_sent": charging_state["disconnect_reminder_invite_sent"],
                    "disconnect_reminder_invite_status": charging_state["disconnect_reminder_invite_status"],
                    "disconnect_reminder_invite_error": charging_state["disconnect_reminder_invite_error"],
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
    log_smtp_configuration_status()

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

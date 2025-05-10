# EV Charger Slack Bot & Dashboard

This project provides a Slack bot to manage a shared EV (Electric Vehicle) charger and a web-based dashboard to display its current status and queue. Users can interact with the bot via Slack commands to check-in, request a charging spot, view the status, leave the queue, or end their session early.

## Features

*   **Slack Integration:**
    *   `/checkin`: Allows a user to claim an available charger, starting a grace period to plug in.
    *   `/request`: Adds a user to the queue if the charger is busy or claims it if free.
    *   `/endcharge`: Allows the current user to end their charging session early.
    *   `/exitqueue`: Allows a user to remove themselves from the waiting queue.
    *   `/chargestatus`: Displays the current charger status, active user, time remaining, and the queue in Slack.
*   **Session Management:**
    *   Configurable charge duration (default: 90 minutes).
    *   Configurable grace period (default: 5 minutes) for the user to plug in.
    *   Automatic notifications to users:
        *   When their session starts.
        *   10-minute warning before their session ends.
        *   When their session ends.
        *   When the charger becomes available for the next user in the queue.
*   **Web Dashboard:**
    *   Real-time status display: Available, Grace Period (with countdown), Charging (with countdown).
    *   Displays current user's name.
    *   Shows the current waiting queue with user names.
    *   Light/Dark theme toggle.
    *   Auto-refreshes data.
    *   Accessible via `/dashboard` endpoint.
*   **JSON API:**
    *   Provides charger status and queue data in JSON format at the `/status` endpoint.
*   **Health Check:**
    *   Simple health check endpoint at `/`.

## Technology Stack

*   **Backend:**
    *   Python 3
    *   Slack Bolt for Python (Slack API interaction)
    *   Standard Library `http.server` (for web dashboard and API)
    *   Threading for concurrent session management
*   **Frontend (Dashboard):**
    *   HTML5
    *   CSS3 (with CSS Variables for theming)
    *   Vanilla JavaScript (for dynamic updates and API calls)
*   **Environment:**
    *   Requires Slack App Bot Token and App-Level Token.

## Prerequisites

*   Python 3.7+
*   `pip` (Python package installer)
*   A Slack Workspace where you can create and install an app.
*   (Optional, for local development exposing to Slack) ngrok or a similar tunneling service if your bot is not hosted on a publicly accessible URL (though Socket Mode largely mitigates this need for command/event handling).

## Setup and Installation

1.  **Clone the Repository:**
    ```bash
    git clone <your-repository-url>
    cd <your-repository-name>
    ```

2.  **Create a Slack App:**
    *   Go to [https://api.slack.com/apps](https://api.slack.com/apps) and click "Create New App".
    *   Choose "From scratch".
    *   Name your app (e.g., "EV Charger Bot") and select your workspace.
    *   **Enable Socket Mode:**
        *   In the sidebar, go to "Settings" -> "Socket Mode".
        *   Enable Socket Mode.
        *   Generate an **App-Level Token**. Name it something like `socket-connections-token`.
        *   Grant it the `connections:write` scope.
        *   Copy this token. This will be your `SLACK_APP_TOKEN`.
    *   **Configure Bot Token Scopes:**
        *   In the sidebar, go to "Features" -> "OAuth & Permissions".
        *   Scroll down to "Scopes" -> "Bot Token Scopes".
        *   Add the following scopes:
            *   `chat:write` (to send messages)
            *   `commands` (to register and use slash commands)
            *   `users:read` (to fetch user display names for the dashboard)
    *   **Install App to Workspace:**
        *   At the top of the "OAuth & Permissions" page, click "Install to Workspace".
        *   Authorize the installation.
        *   Copy the **Bot User OAuth Token**. This will be your `SLACK_BOT_TOKEN`.
    *   **Register Slash Commands:**
        *   In the sidebar, go to "Features" -> "Slash Commands".
        *   Click "Create New Command" for each of the following:
            *   Command: `/checkin`
              Short Description: Check-in to use the EV charger.
              Usage Hint: ` `
            *   Command: `/request`
              Short Description: Request the EV charger or join the queue.
              Usage Hint: ` `
            *   Command: `/endcharge`
              Short Description: End your current charging session.
              Usage Hint: ` `
            *   Command: `/exitqueue`
              Short Description: Leave the EV charger waiting queue.
              Usage Hint: ` `
            *   Command: `/chargestatus`
              Short Description: View the current EV charger status and queue.
              Usage Hint: ` `
        *   *Note: Request URLs are not needed when using Socket Mode for commands handled by Bolt.*

3.  **Set Up Environment Variables:**
    Create a `.env` file in the project root or set these environment variables in your shell:
    ```
    SLACK_BOT_TOKEN="xoxb-your-bot-token"
    SLACK_APP_TOKEN="xapp-your-app-level-token"
    # PORT=8080 # Optional, defaults to 8080 for the web dashboard
    ```
    *If using a `.env` file, you might want to add `python-dotenv` to your `requirements.txt` and load it at the beginning of your script, though the provided code reads directly from `os.environ`.*

4.  **Install Python Dependencies:**
    It's recommended to use a virtual environment:
    ```bash
    python -m venv venv
    source venv/bin/activate  # On Windows: venv\Scripts\activate
    ```
    Install the required packages (based on the imports in `app.py`):
    ```bash
    pip install slack_bolt
    ```
    Create a `requirements.txt` for easy installation:
    ```bash
    pip freeze > requirements.txt
    ```
    (Then others can just do `pip install -r requirements.txt`)

5.  **Place Dashboard HTML:**
    Ensure the `dashboard.html` file (your frontend code) is in the same directory as your Python backend script (`app.py`).

6.  **Run the Backend Server:**
    ```bash
    python app.py
    ```
    You should see log messages indicating the bot is starting and connecting to Slack via Socket Mode, and the HTTP server starting.

7.  **Access the Dashboard:**
    Open your web browser and go to `http://localhost:8080/dashboard` (or the port you configured).

## Usage

*   **Slack Commands:** Interact with the bot in any Slack channel it's been invited to, or directly via its DM channel.
    *   `/checkin`: If the charger is free, reserves it for you and starts a 5-minute grace period to plug in.
    *   `/request`: If the charger is free, same as `/checkin`. If busy, adds you to the queue.
    *   `/endcharge`: If you are the current user, ends your session and makes the charger available for the next person in queue (if any).
    *   `/exitqueue`: If you are in the queue, removes you from it.
    *   `/chargestatus`: Shows who is charging/in grace period, time remaining, and lists users in the queue.
*   **Web Dashboard:** Provides a visual overview of the charger status, current user, time remaining, and the queue. Auto-refreshes.

## Project Structure (Simplified)

.
├── app.py # Python backend (Slack bot, HTTP server)
├── dashboard.html # HTML, CSS, JS for the web dashboard
├── requirements.txt # Python dependencies
└── README.md # This file


## Logging

The application uses basic logging to `INFO` level. Logs include timestamps, log level, thread name, and messages, which are printed to standard output. This is helpful for monitoring bot activity and troubleshooting.

## Customization

*   **Durations:** `CHARGE_DURATION`, `GRACE_PERIOD`, `TEN_MINUTE_WARNING_BEFORE_END` constants in `app.py` can be modified.
*   **Port:** The HTTP server port can be changed via the `PORT` environment variable.
*   **Dashboard Styling:** Modify `dashboard.html` to change the appearance.

## Contributing

Contributions are welcome! Please feel free to submit a pull request or open an issue.

1.  Fork the repository.
2.  Create your feature branch (`git checkout -b feature/AmazingFeature`).
3.  Commit your changes (`git commit -m 'Add some AmazingFeature'`).
4.  Push to the branch (`git push origin feature/AmazingFeature`).
5.  Open a Pull Request.

## License

This project is licensed under the MIT License - see the `LICENSE` file for details (if you choose to add one).
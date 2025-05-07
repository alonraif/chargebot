from flask import Flask, render_template_string
import time

app = Flask(__name__)

# Import shared state if in the same project
from chargingbot import charging_state, CHARGE_DURATION

TEMPLATE = """
<!doctype html>
<html>
<head>
  <title>ChargingBot Dashboard</title>
  <style>
    body { font-family: sans-serif; padding: 2rem; background: #f4f4f4; }
    .card { background: white; padding: 1rem; margin: 1rem 0; border-radius: 10px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
    h2 { margin-top: 0; }
  </style>
</head>
<body>
  <h1>🔌 ChargingBot Dashboard</h1>

  <div class="card">
    <h2>Status</h2>
    {% if current_user %}
      <p><strong>Currently Charging:</strong> <code>{{ current_user }}</code></p>
      <p><strong>Time Remaining:</strong> {{ time_remaining }}</p>
    {% else %}
      <p><strong>Charger is Available</strong></p>
    {% endif %}
  </div>

  <div class="card">
    <h2>Queue</h2>
    {% if queue %}
      <ul>
      {% for user in queue %}
        <li>{{ user }}</li>
      {% endfor %}
      </ul>
    {% else %}
      <p>No one is in the queue.</p>
    {% endif %}
  </div>
</body>
</html>
"""

@app.route("/")
def dashboard():
    now = time.time()
    current_user = charging_state["current_user"]
    start_time = charging_state["start_time"]
    queue = charging_state["queue"]

    if current_user and start_time:
        time_left = int((start_time + CHARGE_DURATION) - now)
        mins = time_left // 60
        secs = time_left % 60
        time_remaining = f"{mins:02}:{secs:02}"
    else:
        time_remaining = None

    return render_template_string(
        TEMPLATE,
        current_user=current_user,
        time_remaining=time_remaining,
        queue=queue
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

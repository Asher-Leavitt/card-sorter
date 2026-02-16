"""
Minimal test — run this to see if ANY Flask server works on your machine.
  python test_server.py
Then open http://127.0.0.1:8080 in your browser.
"""

from flask import Flask

app = Flask(__name__)

@app.route("/")
def hello():
    return "<h1>IT WORKS!</h1><p>If you can see this, Flask is running correctly.</p>"

@app.route("/ping")
def ping():
    return "pong", 200

if __name__ == "__main__":
    print("\n=== TEST SERVER ===")
    print("Trying port 8080...")
    print("Open http://127.0.0.1:8080 in your browser\n")
    app.run(host="127.0.0.1", port=8080, debug=True)
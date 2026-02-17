"""
Card Sorter Control System
===========================
Runs on laptop (simulated GPIO) or Raspberry Pi Zero 2 W (real GPIO).

Setup:
  pip install flask requests
  python app.py          # laptop simulation
  sudo python app.py     # Raspberry Pi (sudo needed for GPIO)
"""

import threading
import time
import json
import os
import sys
import traceback
import requests as http_requests
from datetime import datetime
from flask import Flask, request, jsonify, render_template

# ---------------------------------------------------------------------------
# GPIO ABSTRACTION
# ---------------------------------------------------------------------------

try:
    import RPi.GPIO as GPIO
    SIMULATED = False
    print("[HW] Running on Raspberry Pi — real GPIO active")
except ImportError:
    SIMULATED = True
    print("[SIM] RPi.GPIO not found — running in simulation mode")

    class _FakeGPIO:
        BCM = 11; OUT = 0; IN = 1; HIGH = 1; LOW = 0; PUD_UP = 22

        def __init__(self):
            self._pins = {}
            self._beams = {}

        def setmode(self, m): pass
        def setwarnings(self, f): pass
        def setup(self, pin, d, pull_up_down=None): self._pins[pin] = 0
        def output(self, pin, val): self._pins[pin] = val

        def input(self, pin):
            if pin in self._beams:
                return self.LOW if self._beams[pin] else self.HIGH
            return self.HIGH

        def cleanup(self): self._pins.clear()

        def sim_set_beam(self, pin, blocked):
            self._beams[pin] = blocked

    GPIO = _FakeGPIO()


# ---------------------------------------------------------------------------
# PIN CONFIG — Change these to match YOUR wiring
# ---------------------------------------------------------------------------

PINS = {
    "stepper1_step": 5,
    "stepper1_dir":  6,
    "stepper2_step": 24,
    "stepper2_dir":  25,
    "beam0":         4,   # home position
    "beam1":         27,   # scan position
}

GPIO.setmode(GPIO.BCM)
if not SIMULATED:
    GPIO.setwarnings(False)

for name, pin in PINS.items():
    if "step" in name or "dir" in name:
        GPIO.setup(pin, GPIO.OUT)
    elif "beam" in name:
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

if SIMULATED:
    for name, pin in PINS.items():
        if "beam" in name:
            GPIO.sim_set_beam(pin, False)

print(f"[GPIO] Pins: {PINS}")


# ---------------------------------------------------------------------------
# STEPPER HELPERS
# ---------------------------------------------------------------------------

def step_motor(step_pin, dir_pin, direction, steps=1, delay=0.001):
    GPIO.output(dir_pin, GPIO.HIGH if direction == 1 else GPIO.LOW)
    taken = 0
    for _ in range(steps):
        GPIO.output(step_pin, GPIO.HIGH)
        time.sleep(delay)
        GPIO.output(step_pin, GPIO.LOW)
        time.sleep(delay)
        taken += 1
    return taken


def run_until_beam(step_pin, dir_pin, beam_pin, direction, delay=0.001, max_steps=50000):
    GPIO.output(dir_pin, GPIO.HIGH if direction == 1 else GPIO.LOW)
    taken = 0
    for _ in range(max_steps):
        if GPIO.input(beam_pin) == GPIO.LOW:
            print(f"[STEPPER] Beam on pin {beam_pin} triggered after {taken} steps")
            return True, taken
        GPIO.output(step_pin, GPIO.HIGH)
        time.sleep(delay)
        GPIO.output(step_pin, GPIO.LOW)
        time.sleep(delay)
        taken += 1
    print(f"[STEPPER] Max steps ({max_steps}) reached without beam trigger!")
    return False, taken


# ---------------------------------------------------------------------------
# SCRYFALL API
# ---------------------------------------------------------------------------

SCRYFALL_CACHE = {}

def fetch_scryfall(scryfall_id):
    if not scryfall_id:
        print("[SCRYFALL] No scryfallId, skipping")
        return None
    if scryfall_id in SCRYFALL_CACHE:
        print(f"[SCRYFALL] Cache hit: {scryfall_id[:12]}...")
        return SCRYFALL_CACHE[scryfall_id]

    url = f"https://api.scryfall.com/cards/{scryfall_id}"
    print(f"[SCRYFALL] Fetching {url}")
    try:
        resp = http_requests.get(url, headers={
            "User-Agent": "CardSorterPi/1.0",
            "Accept": "application/json",
        }, timeout=8)
        print(f"[SCRYFALL] Status: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            result = {
                "cmc": data.get("cmc", 0), "colors": data.get("colors", []),
                "color_identity": data.get("color_identity", []),
                "type_line": data.get("type_line", ""), "mana_cost": data.get("mana_cost", ""),
                "oracle_text": data.get("oracle_text", ""), "power": data.get("power", ""),
                "toughness": data.get("toughness", ""), "keywords": data.get("keywords", []),
                "set_name": data.get("set_name", ""), "rarity": data.get("rarity", ""),
                "image_uri": "", "image_art_crop": "",
            }
            iu = data.get("image_uris")
            if iu:
                result["image_uri"] = iu.get("large", iu.get("normal", ""))
                result["image_art_crop"] = iu.get("art_crop", "")
            elif data.get("card_faces"):
                fi = data["card_faces"][0].get("image_uris", {})
                result["image_uri"] = fi.get("large", fi.get("normal", ""))
                result["image_art_crop"] = fi.get("art_crop", "")
            SCRYFALL_CACHE[scryfall_id] = result
            print(f"[SCRYFALL] OK: {data.get('name','?')} | image={'YES' if result['image_uri'] else 'NO'}")
            return result
        else:
            print(f"[SCRYFALL] Error {resp.status_code}: {resp.text[:200]}")
            return None
    except Exception as e:
        print(f"[SCRYFALL] FAILED: {e}")
        return None


def enrich_card(delver_card):
    scryfall_id = delver_card.get("scryfallId", "")
    sf = fetch_scryfall(scryfall_id)
    enriched = {
        "name": delver_card.get("name", "Unknown"),
        "edition": delver_card.get("edition", ""),
        "editionCode": delver_card.get("editionCode", ""),
        "number": delver_card.get("number", ""),
        "rarity": delver_card.get("rarity", ""),
        "price": delver_card.get("price", 0),
        "fmtPrice": delver_card.get("fmtPrice", ""),
        "finish": delver_card.get("finish", "regular"),
        "cardType": delver_card.get("cardType", ""),
        "scryfallId": scryfall_id,
    }
    if sf:
        enriched.update(sf)
    else:
        enriched.update({
            "cmc": 0, "colors": [], "color_identity": [],
            "type_line": delver_card.get("cardType", ""),
            "mana_cost": "", "oracle_text": "", "power": "", "toughness": "",
            "keywords": [], "image_uri": "", "image_art_crop": "",
        })
    return enriched


# ---------------------------------------------------------------------------
# SCAN LOG + CURRENT CARD
# ---------------------------------------------------------------------------

scan_log = []
scan_log_lock = threading.Lock()
current_card = {"card": None}
current_card_lock = threading.Lock()


# ---------------------------------------------------------------------------
# SORTING RULES
# ---------------------------------------------------------------------------

RULES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules.json")
DEFAULT_RULES = [
    {"name": "High Value",  "field": "price",          "operator": ">",        "value": 5,          "pile": 1},
    {"name": "Mythics",     "field": "rarity",         "operator": "==",       "value": "mythic",   "pile": 2},
    {"name": "Rares",       "field": "rarity",         "operator": "==",       "value": "rare",     "pile": 3},
    {"name": "Blue Cards",  "field": "color_identity", "operator": "contains", "value": "U",        "pile": 4},
    {"name": "Creatures",   "field": "type_line",      "operator": "contains", "value": "Creature", "pile": 5},
]

def load_rules():
    if os.path.exists(RULES_FILE):
        with open(RULES_FILE) as f:
            return json.load(f)
    return list(DEFAULT_RULES)

def save_rules(rules):
    with open(RULES_FILE, "w") as f:
        json.dump(rules, f, indent=2)

def evaluate_rules(card, rules):
    for rule in rules:
        field = rule["field"]; op = rule["operator"]; target = rule["value"]
        field_val = card.get(field)
        if field_val is None: continue
        if isinstance(field_val, list):
            if op == "contains":
                if str(target).upper() in [str(v).upper() for v in field_val]:
                    return rule["pile"]
            elif op == "==":
                tl = sorted(t.strip().upper() for t in str(target).split(","))
                fl = sorted(str(v).upper() for v in field_val)
                if tl == fl: return rule["pile"]
            continue
        try:
            if isinstance(target, str) and target.replace(".", "", 1).replace("-", "", 1).isdigit():
                target = float(target)
            if isinstance(target, (int, float)) and isinstance(field_val, str):
                field_val = float(field_val)
            if isinstance(field_val, (int, float)) and isinstance(target, str):
                target = float(target)
        except (ValueError, TypeError): pass
        match = False
        if op == ">": match = field_val > target
        elif op == "<": match = field_val < target
        elif op == ">=": match = field_val >= target
        elif op == "<=": match = field_val <= target
        elif op == "==": match = str(field_val).lower() == str(target).lower()
        elif op == "!=": match = str(field_val).lower() != str(target).lower()
        elif op == "contains": match = str(target).lower() in str(field_val).lower()
        if match: return rule["pile"]
    return 0


# ---------------------------------------------------------------------------
# SEQUENCE STATE
# ---------------------------------------------------------------------------

class SequenceState:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.stop_requested = False
        self.phase = "idle"       # idle, homing, oscillating, ejecting
        self.status_msg = "Idle"
        self.error = ""
        self.cycle_count = 0      # how many cards processed
        self.osc_count = 0        # oscillation passes this cycle
        self.last_scan_ts = ""    # timestamp of last scan we acted on

seq = SequenceState()


def _should_stop():
    """Check if stop was requested."""
    with seq.lock:
        return seq.stop_requested


def _set_phase(phase, msg):
    """Update sequence phase and status message."""
    with seq.lock:
        seq.phase = phase
        seq.status_msg = msg
    print(f"[SEQ] {msg}")


def _run_until_beam_interruptible(step_pin, dir_pin, beam_pin, direction, delay=0.001, max_steps=50000):
    """Like run_until_beam but checks for stop requests between steps."""
    GPIO.output(dir_pin, GPIO.HIGH if direction == 1 else GPIO.LOW)
    taken = 0
    for _ in range(max_steps):
        if _should_stop():
            return "stopped", taken
        if GPIO.input(beam_pin) == GPIO.LOW:
            return "beam", taken
        GPIO.output(step_pin, GPIO.HIGH)
        time.sleep(delay)
        GPIO.output(step_pin, GPIO.LOW)
        time.sleep(delay)
        taken += 1
    return "max_steps", taken


def _step_interruptible(step_pin, dir_pin, direction, steps, delay=0.001, check_scan=False):
    """Step N times, checking for stop. If check_scan=True, also stop on new card scan."""
    GPIO.output(dir_pin, GPIO.HIGH if direction == 1 else GPIO.LOW)
    taken = 0
    for _ in range(steps):
        if _should_stop():
            return "stopped", taken
        if check_scan:
            with current_card_lock:
                card = current_card["card"]
            if card and card.get("timestamp", "") != seq.last_scan_ts:
                return "scanned", taken
        GPIO.output(step_pin, GPIO.HIGH)
        time.sleep(delay)
        GPIO.output(step_pin, GPIO.LOW)
        time.sleep(delay)
        taken += 1
    return "done", taken


def continuous_sort_loop():
    """
    Main sorting loop:
      1. Home: stepper 1 CCW until beam 0
      2. Oscillate: 800 CW, then CCW back to beam 0, repeat until card scanned
      3. Eject: 2000 steps CW
      4. Repeat 1-3 until stopped
    """
    pin_step = PINS["stepper1_step"]
    pin_dir  = PINS["stepper1_dir"]
    pin_beam = PINS["beam0"]
    delay    = 0.001

    with seq.lock:
        seq.running = True
        seq.stop_requested = False
        seq.cycle_count = 0
        seq.error = ""
        # Snapshot the current scan timestamp so we don't react to old scans
        with current_card_lock:
            card = current_card["card"]
        seq.last_scan_ts = card["timestamp"] if card else ""

    print("[SEQ] ═══ Continuous sort loop started ═══")

    while not _should_stop():
        cycle = seq.cycle_count + 1

        # ── Phase 1: Home ──────────────────────────────────────────
        _set_phase("homing", f"Cycle {cycle}: Homing CCW → beam 0")

        result, steps = _run_until_beam_interruptible(
            pin_step, pin_dir, pin_beam, direction=-1, delay=delay)

        if result == "stopped":
            break
        if result == "max_steps":
            with seq.lock:
                seq.error = f"Cycle {cycle}: Beam 0 never hit during homing!"
                seq.status_msg = "ERROR: Home failed"
            print(f"[SEQ] ERROR: Homing failed after {steps} steps")
            break

        print(f"[SEQ] Cycle {cycle}: Homed after {steps} steps")

        # ── Phase 2: Oscillate until scanned ───────────────────────
        _set_phase("oscillating", f"Cycle {cycle}: Oscillating — waiting for scan")
        with seq.lock:
            seq.osc_count = 0

        scanned = False
        while not _should_stop() and not scanned:
            with seq.lock:
                seq.osc_count += 1
                osc = seq.osc_count

            # Forward 800 steps CW (check for scan during movement)
            _set_phase("oscillating", f"Cycle {cycle}: Osc {osc} — forward 1000 CW")
            result, _ = _step_interruptible(
                pin_step, pin_dir, direction=1, steps=1000,
                delay=delay, check_scan=True)

            if result == "stopped":
                break
            if result == "scanned":
                scanned = True
                break

            # Back to beam 0 CCW (check for scan during movement)
            _set_phase("oscillating", f"Cycle {cycle}: Osc {osc} — returning to beam 0")
            result, _ = _run_until_beam_interruptible(
                pin_step, pin_dir, pin_beam, direction=-1, delay=delay)

            if result == "stopped":
                break
            if result == "max_steps":
                with seq.lock:
                    seq.error = f"Cycle {cycle}: Lost beam 0 during oscillation!"
                print(f"[SEQ] ERROR: Lost beam 0 during oscillation")
                break

            # Quick check for scan at home position too
            with current_card_lock:
                card = current_card["card"]
            if card and card.get("timestamp", "") != seq.last_scan_ts:
                scanned = True
                break

        if _should_stop():
            break

        if not scanned:
            # Error occurred in oscillation
            break

        # Update the scan timestamp so we don't re-trigger on same card
        with current_card_lock:
            card = current_card["card"]
        with seq.lock:
            seq.last_scan_ts = card["timestamp"] if card else ""

        card_name = card["name"] if card else "Unknown"
        print(f"[SEQ] Cycle {cycle}: Card scanned! → {card_name}")

        # ── Phase 3: Eject ─────────────────────────────────────────
        _set_phase("ejecting", f"Cycle {cycle}: Ejecting — 2000 steps CW")

        result, steps = _step_interruptible(
            pin_step, pin_dir, direction=1, steps=2000, delay=delay)

        if result == "stopped":
            break

        print(f"[SEQ] Cycle {cycle}: Ejected ({steps} steps)")

        with seq.lock:
            seq.cycle_count = cycle

        # Small pause between cycles
        time.sleep(0.1)

    # ── Cleanup ────────────────────────────────────────────────────
    with seq.lock:
        seq.running = False
        if seq.stop_requested:
            seq.status_msg = f"Stopped after {seq.cycle_count} cards"
            seq.phase = "idle"
        elif not seq.error:
            seq.status_msg = f"Complete: {seq.cycle_count} cards sorted"
            seq.phase = "idle"

    print(f"[SEQ] ═══ Loop ended: {seq.cycle_count} cards processed ═══")


# ---------------------------------------------------------------------------
# FLASK APP
# ---------------------------------------------------------------------------

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates"))

def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


# -- Webhook handler (shared) -----------------------------------------------

def handle_webhook():
    if request.method == "OPTIONS":
        return add_cors(jsonify({"message": "CORS preflight"})), 200
    data = request.json or {}
    event_type = data.get("type", "")
    print(f"\n[WEBHOOK] Event: {event_type}")

    if event_type == "card_scanned":
        cards = data.get("cards", [])
        if not cards:
            return add_cors(jsonify({"status": "no cards"})), 200
        raw = cards[0]
        print(f"[WEBHOOK] Card: {raw.get('name')}, scryfallId={raw.get('scryfallId','NONE')}")
        enriched = enrich_card(raw)
        rules = load_rules()
        pile = evaluate_rules(enriched, rules)
        entry = {**enriched, "timestamp": datetime.now().isoformat(), "pile": pile}
        with scan_log_lock: scan_log.append(entry)
        with current_card_lock: current_card["card"] = entry
        print(f"[WEBHOOK] ✓ {entry['name']} → Pile {pile} | image={'YES' if entry.get('image_uri') else 'NO'}")
        return add_cors(jsonify({"status": "ok", "pile": pile, "card": entry["name"]})), 200

    elif event_type == "scanner_started": print("[WEBHOOK] Scanner started")
    elif event_type == "scanner_paused": print("[WEBHOOK] Scanner paused")
    else:
        print(f"[WEBHOOK] Unknown: {event_type}")
        print(f"[WEBHOOK] Payload: {json.dumps(data)[:500]}")
    return add_cors(jsonify({"status": "ok"})), 200


# -- Routes ---------------------------------------------------------------

@app.route("/", methods=["GET", "POST", "OPTIONS"])
def index():
    if request.method == "GET":
        return render_template("dashboard.html", simulated=SIMULATED)
    return handle_webhook()

@app.route("/webhook", methods=["POST", "OPTIONS"])
def webhook_route():
    return handle_webhook()

@app.route("/api/status")
def api_status():
    beams = {}
    for name, pin in PINS.items():
        if "beam" in name:
            beams[name] = GPIO.input(pin) == GPIO.LOW
    with seq.lock:
        sd = {"seq_running": seq.running, "seq_phase": seq.phase,
              "seq_status": seq.status_msg, "seq_error": seq.error,
              "seq_cycles": seq.cycle_count, "seq_osc": seq.osc_count}
    with scan_log_lock: total = len(scan_log)
    with current_card_lock: card = current_card["card"]
    return jsonify({"simulated": SIMULATED, "beams": beams,
                     "total_scans": total, "current_card": card, **sd})

@app.route("/api/motor/step", methods=["POST"])
def motor_step_api():
    body = request.json or {}
    stepper = body.get("stepper", 1); direction = body.get("direction", 1)
    steps = body.get("steps", 200); delay = body.get("delay", 0.001)
    pstep = PINS[f"stepper{stepper}_step"]; pdir = PINS[f"stepper{stepper}_dir"]
    print(f"[MOTOR] S{stepper}: {steps}x dir={direction}")
    try:
        taken = step_motor(pstep, pdir, direction, steps, delay)
        return jsonify({"ok": True, "steps_taken": taken})
    except Exception as e:
        print(f"[MOTOR] ERROR: {e}"); traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/motor/run_until_beam", methods=["POST"])
def motor_run_until_beam_api():
    body = request.json or {}
    stepper = body.get("stepper", 1); beam = body.get("beam", "beam0")
    direction = body.get("direction", -1); delay = body.get("delay", 0.001)
    pstep = PINS[f"stepper{stepper}_step"]; pdir = PINS[f"stepper{stepper}_dir"]
    pbeam = PINS[beam]
    print(f"[MOTOR] S{stepper} → {beam}, dir={direction}")
    try:
        success, taken = run_until_beam(pstep, pdir, pbeam, direction, delay)
        return jsonify({"ok": True, "success": success, "steps_taken": taken})
    except Exception as e:
        print(f"[MOTOR] ERROR: {e}"); traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/seq/start", methods=["POST"])
def seq_start():
    with seq.lock:
        if seq.running:
            return jsonify({"ok": False, "error": "Already running"}), 409
    threading.Thread(target=continuous_sort_loop, daemon=True).start()
    return jsonify({"ok": True, "msg": "Continuous sort loop started"})

@app.route("/api/seq/stop", methods=["POST"])
def seq_stop():
    with seq.lock:
        seq.stop_requested = True
    return jsonify({"ok": True, "msg": "Stop requested"})

@app.route("/api/sim/beam", methods=["POST"])
def sim_beam():
    if not SIMULATED:
        return jsonify({"ok": False, "error": "Not sim mode"}), 400
    body = request.json or {}
    beam = body.get("beam", "beam0"); blocked = body.get("blocked", False)
    pin = PINS.get(beam)
    if pin is not None: GPIO.sim_set_beam(pin, blocked)
    return jsonify({"ok": True, "beam": beam, "blocked": blocked})

@app.route("/api/rules", methods=["GET"])
def get_rules(): return jsonify(load_rules())

@app.route("/api/rules", methods=["POST"])
def set_rules():
    rules = request.json
    if not isinstance(rules, list): return jsonify({"error": "Expected array"}), 400
    save_rules(rules); return jsonify({"ok": True})

@app.route("/api/scans", methods=["GET"])
def get_scans():
    with scan_log_lock: return jsonify(list(scan_log))

@app.route("/api/scans/clear", methods=["POST"])
def clear_scans():
    with scan_log_lock: scan_log.clear()
    with current_card_lock: current_card["card"] = None
    return jsonify({"ok": True})

@app.route("/api/scans/export", methods=["GET"])
def export_scans():
    with scan_log_lock:
        lines = ["timestamp,name,edition,rarity,cmc,colors,color_identity,type_line,price,pile"]
        for s in scan_log:
            lines.append(f'{s["timestamp"]},"{s["name"]}","{s.get("edition","")}",{s.get("rarity","")},{s.get("cmc",0)},{"|".join(s.get("colors",[]))},{"|".join(s.get("color_identity",[]))},"{s.get("type_line","")}",{s.get("price",0)},{s["pile"]}')
    return "\n".join(lines), 200, {"Content-Type": "text/csv", "Content-Disposition": "attachment; filename=scans.csv"}

@app.route("/api/sim/scan", methods=["POST"])
def sim_scan():
    body = request.json or {}
    fake = {"name": body.get("name", "Birds of Paradise"), "edition": body.get("edition", ""),
            "editionCode": body.get("editionCode", ""), "number": body.get("number", ""),
            "rarity": body.get("rarity", "R"), "price": body.get("price", 8.36),
            "fmtPrice": "", "finish": "regular", "cardType": body.get("cardType", ""),
            "scryfallId": body.get("scryfallId", "")}
    enriched = enrich_card(fake)
    pile = evaluate_rules(enriched, load_rules())
    entry = {**enriched, "timestamp": datetime.now().isoformat(), "pile": pile}
    with scan_log_lock: scan_log.append(entry)
    with current_card_lock: current_card["card"] = entry
    print(f"[SIM SCAN] {entry['name']} → Pile {pile}")
    return jsonify({"ok": True, "card": entry})

@app.route("/api/scryfall/search", methods=["GET"])
def scryfall_search():
    q = request.args.get("q", "")
    if not q: return jsonify({"error": "Missing ?q="}), 400
    try:
        resp = http_requests.get("https://api.scryfall.com/cards/search",
            params={"q": q, "unique": "prints", "order": "released", "dir": "desc"},
            headers={"User-Agent": "CardSorterPi/1.0"}, timeout=5)
        if resp.status_code == 200:
            cards = []
            for c in resp.json().get("data", [])[:8]:
                img = ""
                if c.get("image_uris"): img = c["image_uris"].get("small", "")
                elif c.get("card_faces") and c["card_faces"][0].get("image_uris"):
                    img = c["card_faces"][0]["image_uris"].get("small", "")
                cards.append({"id": c.get("id",""), "name": c.get("name",""),
                    "set_name": c.get("set_name",""), "set": c.get("set",""),
                    "number": c.get("collector_number",""), "rarity": c.get("rarity",""), "image": img})
            return jsonify(cards)
        elif resp.status_code == 404: return jsonify([])
        else: return jsonify({"error": f"Scryfall {resp.status_code}"}), 502
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/debug")
def debug_page():
    info = {"simulated": SIMULATED, "pins": PINS, "python": sys.version,
            "beams": {}, "scans": len(scan_log), "rules": len(load_rules())}
    for n, p in PINS.items():
        if "beam" in n: info["beams"][n] = "BLOCKED" if GPIO.input(p) == GPIO.LOW else "clear"
    return f"<pre>{json.dumps(info, indent=2)}</pre>"


if __name__ == "__main__":
    host = "0.0.0.0" if not SIMULATED else "127.0.0.1"
    port = 5000 if not SIMULATED else 8080
    print(f"\n🃏 Card Sorter Control System")
    print(f"   Mode:      {'SIMULATION' if SIMULATED else 'RASPBERRY PI'}")
    print(f"   Dashboard: http://{'<pi-ip>' if not SIMULATED else '127.0.0.1'}:{port}")
    print(f"   Webhook:   POST to / or /webhook")
    print(f"   Debug:     http://{'<pi-ip>' if not SIMULATED else '127.0.0.1'}:{port}/debug")
    if not SIMULATED:
        print(f"\n   ⚠️  Motor not working? Try: sudo python app.py")
    print()
    app.run(host=host, port=port, debug=SIMULATED)
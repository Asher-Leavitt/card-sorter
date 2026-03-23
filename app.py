"""
Card Sorter Control System v3
==============================
Full sorting cycle with PCA9685 servo control.

Setup:
  pip install flask requests adafruit-circuitpython-pca9685 adafruit-circuitpython-servokit
  sudo python app.py
"""

import threading, time, json, os, sys, traceback
import requests as http_requests
from datetime import datetime
from flask import Flask, request, jsonify, render_template

# ---------------------------------------------------------------------------
# GPIO ABSTRACTION
# ---------------------------------------------------------------------------
try:
    import RPi.GPIO as GPIO
    SIMULATED = False
    print("[HW] Raspberry Pi GPIO active")
except ImportError:
    SIMULATED = True
    print("[SIM] Simulation mode — no GPIO")

    class _FakeGPIO:
        BCM=11; OUT=0; IN=1; HIGH=1; LOW=0; PUD_UP=22
        def __init__(self): self._pins={}; self._beams={}
        def setmode(self,m): pass
        def setwarnings(self,f): pass
        def setup(self,pin,d,pull_up_down=None): self._pins[pin]=0
        def output(self,pin,val): self._pins[pin]=val
        def input(self,pin):
            if pin in self._beams: return self.LOW if self._beams[pin] else self.HIGH
            return self.HIGH
        def cleanup(self): self._pins.clear()
        def sim_set_beam(self,pin,blocked): self._beams[pin]=blocked
    GPIO = _FakeGPIO()

# ---------------------------------------------------------------------------
# PCA9685 SERVO ABSTRACTION
# ---------------------------------------------------------------------------
try:
    from adafruit_servokit import ServoKit
    servo_kit = ServoKit(channels=16)
    SERVO_AVAILABLE = True
    print("[HW] PCA9685 ServoKit initialized")
except Exception as e:
    SERVO_AVAILABLE = False
    print(f"[SIM] PCA9685 not available ({e}) — servo simulation active")

    class _FakeServo:
        def __init__(self): self.angle = 0
    class _FakeKit:
        def __init__(self):
            self.servo = [_FakeServo() for _ in range(16)]
    servo_kit = _FakeKit()

def set_servo_angle(channel, angle, hold=False):
    """Set a PCA9685 servo to a specific angle (0-180).
    If hold=False (default), releases the servo after it settles — no buzz."""
    angle = max(0, min(180, angle))
    try:
        servo_kit.servo[channel].angle = angle
        print(f"[SERVO] Ch{channel} → {angle}°" + (" (holding)" if hold else ""))
        if not hold:
            time.sleep(0.4)  # let servo reach position
            release_servo(channel)
    except Exception as e:
        print(f"[SERVO] ERROR ch{channel}: {e}")

def release_servo(channel):
    """Stop sending PWM to a servo — releases it so it doesn't buzz."""
    try:
        servo_kit.servo[channel].angle = None
        print(f"[SERVO] Ch{channel} released")
    except Exception as e:
        print(f"[SERVO] Release ERROR ch{channel}: {e}")

def get_servo_angle(channel):
    try:
        a = servo_kit.servo[channel].angle
        return a if a is not None else 0
    except: return 0

# ---------------------------------------------------------------------------
# HARDWARE CONFIG — CHANGE THESE TO MATCH YOUR WIRING
# ---------------------------------------------------------------------------

PINS = {
    "stepper1_step": 22,
    "stepper1_dir":  23,
    "stepper2_step": 17,
    "stepper2_dir":  27,
    "beam0":         10,    # home / intake position
    "beam1":         9,   # after scan zone — pile 1 area
    # Add more pile beams here as needed:
    # "beam2": 19,
    # "beam3": 13,
}

# Pile config: pile_number -> {beam, servo_channel, servo_up, servo_down}
# servo_up = angle when diverter is UP (card drops into pile)
# servo_down = angle when diverter is DOWN (card passes over) 
PILES = {
    1: {"beam": "beam1", "servo_ch": 15, "servo_up": 21, "servo_down": 40},
    2: {"beam": "beam2", "servo_ch": 14, "servo_up": 90, "servo_down": 0},
    3: {"beam": "beam3", "servo_ch": 13, "servo_up": 90, "servo_down": 0},
}

# Error pile — pile 0. Can be set to an actual pile number to share.
# 0 = dedicated error handling (reverse to beam0), or set to a pile number.
ERROR_PILE = 0

# Sort config
SORT_CONFIG = {
    "home_timeout_sec":     20,      # max time for homing before error
    "osc_forward_steps":    800,     # steps CW during oscillation
    "osc_pause_sec":        1.0,     # pause at scan position
    "scan_timeout_osc":     60,      # max oscillations before scan timeout
    "eject_extra_steps":    100,     # extra steps after beam passthrough
    "pile1_shuffle_steps":  300,     # back-and-forth steps for pile 1
    "step_delay":           0.001,   # stepper delay (speed)
}

# ---------------------------------------------------------------------------
# GPIO SETUP
# ---------------------------------------------------------------------------
GPIO.setmode(GPIO.BCM)
if not SIMULATED: GPIO.setwarnings(False)

for name, pin in PINS.items():
    if "step" in name or "dir" in name:
        GPIO.setup(pin, GPIO.OUT)
    elif "beam" in name:
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

if SIMULATED:
    for name, pin in PINS.items():
        if "beam" in name: GPIO.sim_set_beam(pin, False)

# Initialize servos to down position
for pnum, pcfg in PILES.items():
    set_servo_angle(pcfg["servo_ch"], pcfg["servo_down"])

print(f"[GPIO] Pins: {PINS}")
print(f"[PILES] Config: {PILES}")

# ---------------------------------------------------------------------------
# STEPPER HELPERS
# ---------------------------------------------------------------------------
def step_motor(step_pin, dir_pin, direction, steps=1, delay=0.001):
    GPIO.output(dir_pin, GPIO.HIGH if direction == 1 else GPIO.LOW)
    taken = 0
    for _ in range(steps):
        GPIO.output(step_pin, GPIO.HIGH); time.sleep(delay)
        GPIO.output(step_pin, GPIO.LOW);  time.sleep(delay)
        taken += 1
    return taken

def step_dual(s1s, s1d_pin, s1d, s2s, s2d_pin, s2d, steps=1, delay=0.001):
    GPIO.output(s1d_pin, GPIO.HIGH if s1d == 1 else GPIO.LOW)
    GPIO.output(s2d_pin, GPIO.HIGH if s2d == 1 else GPIO.LOW)
    taken = 0
    for _ in range(steps):
        GPIO.output(s1s, GPIO.HIGH); GPIO.output(s2s, GPIO.HIGH)
        time.sleep(delay)
        GPIO.output(s1s, GPIO.LOW);  GPIO.output(s2s, GPIO.LOW)
        time.sleep(delay); taken += 1
    return taken

def run_until_beam(step_pin, dir_pin, beam_pin, direction, delay=0.001, max_steps=50000):
    GPIO.output(dir_pin, GPIO.HIGH if direction == 1 else GPIO.LOW)
    taken = 0
    for _ in range(max_steps):
        if GPIO.input(beam_pin) == GPIO.LOW: return True, taken
        GPIO.output(step_pin, GPIO.HIGH); time.sleep(delay)
        GPIO.output(step_pin, GPIO.LOW);  time.sleep(delay)
        taken += 1
    return False, taken

# ---------------------------------------------------------------------------
# INTERRUPTIBLE HELPERS (check seq.stop_requested)
# ---------------------------------------------------------------------------
def _should_stop():
    with seq.lock: return seq.stop_requested

def _set_phase(phase, msg):
    with seq.lock: seq.phase = phase; seq.status_msg = msg
    print(f"[SEQ] {msg}")

def _step_i(step_pin, dir_pin, direction, steps, delay=0.001):
    """Interruptible step N times."""
    GPIO.output(dir_pin, GPIO.HIGH if direction == 1 else GPIO.LOW)
    taken = 0
    for _ in range(steps):
        if _should_stop(): return "stopped", taken
        GPIO.output(step_pin, GPIO.HIGH); time.sleep(delay)
        GPIO.output(step_pin, GPIO.LOW);  time.sleep(delay)
        taken += 1
    return "done", taken

def _beam_i(step_pin, dir_pin, beam_pin, direction, delay=0.001, max_steps=50000):
    """Interruptible run-until-beam."""
    GPIO.output(dir_pin, GPIO.HIGH if direction == 1 else GPIO.LOW)
    taken = 0
    for _ in range(max_steps):
        if _should_stop(): return "stopped", taken
        if GPIO.input(beam_pin) == GPIO.LOW: return "beam", taken
        GPIO.output(step_pin, GPIO.HIGH); time.sleep(delay)
        GPIO.output(step_pin, GPIO.LOW);  time.sleep(delay)
        taken += 1
    return "max_steps", taken

def _dual_i(s1s, s1d_pin, s1d, s2s, s2d_pin, s2d, steps, delay=0.001):
    """Interruptible dual step."""
    GPIO.output(s1d_pin, GPIO.HIGH if s1d == 1 else GPIO.LOW)
    GPIO.output(s2d_pin, GPIO.HIGH if s2d == 1 else GPIO.LOW)
    taken = 0
    for _ in range(steps):
        if _should_stop(): return "stopped", taken
        GPIO.output(s1s, GPIO.HIGH); GPIO.output(s2s, GPIO.HIGH)
        time.sleep(delay)
        GPIO.output(s1s, GPIO.LOW);  GPIO.output(s2s, GPIO.LOW)
        time.sleep(delay); taken += 1
    return "done", taken

def _dual_beam_i(s1s, s1d_pin, s1d, s2s, s2d_pin, s2d, beam_pin, delay=0.001, max_steps=50000):
    """Interruptible dual step until beam."""
    GPIO.output(s1d_pin, GPIO.HIGH if s1d == 1 else GPIO.LOW)
    GPIO.output(s2d_pin, GPIO.HIGH if s2d == 1 else GPIO.LOW)
    taken = 0
    for _ in range(max_steps):
        if _should_stop(): return "stopped", taken
        if GPIO.input(beam_pin) == GPIO.LOW: return "beam", taken
        GPIO.output(s1s, GPIO.HIGH); GPIO.output(s2s, GPIO.HIGH)
        time.sleep(delay)
        GPIO.output(s1s, GPIO.LOW);  GPIO.output(s2s, GPIO.LOW)
        time.sleep(delay); taken += 1
    return "max_steps", taken

def _dual_passthrough_i(s1s, s1d_pin, s1d, s2s, s2d_pin, s2d, beam_pin, delay=0.001, max_steps=50000):
    """Dual step until beam blocked then cleared (card passes through)."""
    GPIO.output(s1d_pin, GPIO.HIGH if s1d == 1 else GPIO.LOW)
    GPIO.output(s2d_pin, GPIO.HIGH if s2d == 1 else GPIO.LOW)
    taken = 0; phase = "waiting_block"
    for _ in range(max_steps):
        if _should_stop(): return "stopped", taken
        blocked = GPIO.input(beam_pin) == GPIO.LOW
        if phase == "waiting_block" and blocked:
            phase = "waiting_clear"
            print(f"[EJECT] Beam blocked at step {taken}")
        elif phase == "waiting_clear" and not blocked:
            print(f"[EJECT] Beam cleared at step {taken}")
            return "passthrough", taken
        GPIO.output(s1s, GPIO.HIGH); GPIO.output(s2s, GPIO.HIGH)
        time.sleep(delay)
        GPIO.output(s1s, GPIO.LOW);  GPIO.output(s2s, GPIO.LOW)
        time.sleep(delay); taken += 1
    return "max_steps", taken

def _single_passthrough_i(step_pin, dir_pin, direction, beam_pin, delay=0.001, max_steps=50000):
    """Single stepper until beam blocked then cleared."""
    GPIO.output(dir_pin, GPIO.HIGH if direction == 1 else GPIO.LOW)
    taken = 0; phase = "waiting_block"
    for _ in range(max_steps):
        if _should_stop(): return "stopped", taken
        blocked = GPIO.input(beam_pin) == GPIO.LOW
        if phase == "waiting_block" and blocked:
            phase = "waiting_clear"
        elif phase == "waiting_clear" and not blocked:
            return "passthrough", taken
        GPIO.output(step_pin, GPIO.HIGH); time.sleep(delay)
        GPIO.output(step_pin, GPIO.LOW);  time.sleep(delay)
        taken += 1
    return "max_steps", taken

# ---------------------------------------------------------------------------
# SCRYFALL
# ---------------------------------------------------------------------------
SCRYFALL_CACHE = {}

def fetch_scryfall(scryfall_id):
    if not scryfall_id: return None
    if scryfall_id in SCRYFALL_CACHE: return SCRYFALL_CACHE[scryfall_id]
    url = f"https://api.scryfall.com/cards/{scryfall_id}"
    try:
        resp = http_requests.get(url, headers={"User-Agent":"CardSorterPi/1.0"}, timeout=8)
        if resp.status_code == 200:
            d = resp.json()
            r = {"cmc":d.get("cmc",0),"colors":d.get("colors",[]),"color_identity":d.get("color_identity",[]),
                 "type_line":d.get("type_line",""),"mana_cost":d.get("mana_cost",""),"oracle_text":d.get("oracle_text",""),
                 "power":d.get("power",""),"toughness":d.get("toughness",""),"keywords":d.get("keywords",[]),
                 "set_name":d.get("set_name",""),"rarity":d.get("rarity",""),"image_uri":"","image_art_crop":""}
            iu = d.get("image_uris")
            if iu: r["image_uri"]=iu.get("large",iu.get("normal","")); r["image_art_crop"]=iu.get("art_crop","")
            elif d.get("card_faces"):
                fi=d["card_faces"][0].get("image_uris",{})
                r["image_uri"]=fi.get("large",fi.get("normal","")); r["image_art_crop"]=fi.get("art_crop","")
            SCRYFALL_CACHE[scryfall_id]=r; return r
        return None
    except: return None

def enrich_card(dc):
    sf = fetch_scryfall(dc.get("scryfallId",""))
    e = {"name":dc.get("name","Unknown"),"edition":dc.get("edition",""),"editionCode":dc.get("editionCode",""),
         "number":dc.get("number",""),"rarity":dc.get("rarity",""),"price":dc.get("price",0),
         "fmtPrice":dc.get("fmtPrice",""),"finish":dc.get("finish","regular"),
         "cardType":dc.get("cardType",""),"scryfallId":dc.get("scryfallId","")}
    if sf: e.update(sf)
    else: e.update({"cmc":0,"colors":[],"color_identity":[],"type_line":dc.get("cardType",""),
                     "mana_cost":"","oracle_text":"","power":"","toughness":"","keywords":[],
                     "image_uri":"","image_art_crop":""})
    return e

# ---------------------------------------------------------------------------
# STATE
# ---------------------------------------------------------------------------
scan_log = []; scan_log_lock = threading.Lock()
current_card = {"card": None}; current_card_lock = threading.Lock()

# ---------------------------------------------------------------------------
# RULES
# ---------------------------------------------------------------------------
RULES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules.json")
DEFAULT_RULES = [
    {"name":"High Value","field":"price","operator":">","value":5,"pile":1},
    {"name":"Mythics","field":"rarity","operator":"==","value":"mythic","pile":2},
    {"name":"Rares","field":"rarity","operator":"==","value":"rare","pile":3},
    {"name":"Blue Cards","field":"color_identity","operator":"contains","value":"U","pile":4},
    {"name":"Creatures","field":"type_line","operator":"contains","value":"Creature","pile":5},
]

def load_rules():
    if os.path.exists(RULES_FILE):
        with open(RULES_FILE) as f: return json.load(f)
    return list(DEFAULT_RULES)

def save_rules(rules):
    with open(RULES_FILE, "w") as f: json.dump(rules, f, indent=2)

def evaluate_rules(card, rules):
    for rule in rules:
        field=rule["field"]; op=rule["operator"]; target=rule["value"]
        fv=card.get(field)
        if fv is None: continue
        if isinstance(fv, list):
            if op=="contains" and str(target).upper() in [str(v).upper() for v in fv]: return rule["pile"]
            elif op=="==" and sorted(t.strip().upper() for t in str(target).split(","))==sorted(str(v).upper() for v in fv): return rule["pile"]
            continue
        try:
            if isinstance(target,str) and target.replace(".","",1).replace("-","",1).isdigit(): target=float(target)
            if isinstance(target,(int,float)) and isinstance(fv,str): fv=float(fv)
            if isinstance(fv,(int,float)) and isinstance(target,str): target=float(target)
        except: pass
        m=False
        if op==">": m=fv>target
        elif op=="<": m=fv<target
        elif op==">=": m=fv>=target
        elif op=="<=": m=fv<=target
        elif op=="==": m=str(fv).lower()==str(target).lower()
        elif op=="!=": m=str(fv).lower()!=str(target).lower()
        elif op=="contains": m=str(target).lower() in str(fv).lower()
        if m: return rule["pile"]
    return 0

# ---------------------------------------------------------------------------
# SETTINGS (persisted)
# ---------------------------------------------------------------------------
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

def load_settings():
    defaults = {
        "error_pile": ERROR_PILE,
        "sort_config": dict(SORT_CONFIG),
        "piles": {str(k): v for k, v in PILES.items()},
    }
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f:
                saved = json.load(f)
            defaults.update(saved)
        except: pass
    return defaults

def save_settings(s):
    with open(SETTINGS_FILE, "w") as f: json.dump(s, f, indent=2)

def get_pile_config():
    """Return pile config from settings, merged with defaults."""
    s = load_settings()
    piles = {}
    for k, v in s.get("piles", {}).items():
        piles[int(k)] = v
    return piles

def get_error_pile():
    return load_settings().get("error_pile", 0)

def get_sort_config():
    return load_settings().get("sort_config", SORT_CONFIG)

# ---------------------------------------------------------------------------
# SEQUENCE STATE
# ---------------------------------------------------------------------------
class SequenceState:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.stop_requested = False
        self.phase = "idle"
        self.status_msg = "Idle"
        self.error = ""
        self.cycle_count = 0
        self.osc_count = 0
        self.last_scan_ts = ""
        self.last_error_card = None  # card data when pile=0 error

seq = SequenceState()

# ---------------------------------------------------------------------------
# FULL SORTING LOOP
# ---------------------------------------------------------------------------
def continuous_sort_loop():
    """
    Full sorting cycle:
      1. Home: S1 CCW until beam0 (timeout = error)
      2. Oscillate: S1 CW 800 steps, pause, CCW to beam0, repeat until scanned
         - Timeout: reverse card to beam0 passthrough → pile 0 error
      3. Route to pile:
         - pile 0 (or error_pile alias): S1 CCW until beam0 passthrough
         - pile 1: S1+S2 CW to beam1, then shuffle back/forth
         - pile N (>1): servo up, S1+S2 CW until beam_pileN passthrough + extra steps, servo down
      4. Repeat
    """
    s1s = PINS["stepper1_step"]; s1d = PINS["stepper1_dir"]
    s2s = PINS["stepper2_step"]; s2d = PINS["stepper2_dir"]
    beam0 = PINS["beam0"]
    cfg = get_sort_config()
    delay = cfg["step_delay"]

    with seq.lock:
        seq.running = True; seq.stop_requested = False
        seq.cycle_count = 0; seq.error = ""; seq.last_error_card = None
        with current_card_lock: c = current_card["card"]
        seq.last_scan_ts = c["timestamp"] if c else ""

    print("[SEQ] ═══ Sort loop started ═══")

    while not _should_stop():
        cycle = seq.cycle_count + 1
        cfg = get_sort_config()  # reload each cycle
        delay = cfg["step_delay"]
        piles = get_pile_config()
        error_pile = get_error_pile()

        # ── 1. HOME ──────────────────────────────────────────────────
        _set_phase("homing", f"C{cycle}: Homing S1 CCW → beam0")
        timeout_steps = int(cfg["home_timeout_sec"] / (delay * 2))

        result, steps = _beam_i(s1s, s1d, beam0, direction=-1, delay=delay, max_steps=timeout_steps)

        if result == "stopped": break
        if result == "max_steps":
            with seq.lock:
                seq.error = f"C{cycle}: Intake error — failed to extract card from pile (beam0 not hit in {cfg['home_timeout_sec']}s)"
            _set_phase("error", seq.error)
            break

        print(f"[SEQ] C{cycle}: Homed after {steps} steps")

        # ── 2. OSCILLATE UNTIL SCANNED ───────────────────────────────
        _set_phase("oscillating", f"C{cycle}: Oscillating — waiting for scan")
        with seq.lock: seq.osc_count = 0
        # Snapshot scan ts so we only react to NEW scans
        with current_card_lock: c = current_card["card"]
        with seq.lock: seq.last_scan_ts = c["timestamp"] if c else ""

        scanned = False
        max_osc = cfg.get("scan_timeout_osc", 60)

        while not _should_stop() and not scanned:
            with seq.lock:
                seq.osc_count += 1
                osc = seq.osc_count

            if osc > max_osc:
                # Scan timeout — return card to error pile
                print(f"[SEQ] C{cycle}: Scan timeout after {max_osc} oscillations")
                break

            # Forward CW
            _set_phase("oscillating", f"C{cycle}: Osc {osc}/{max_osc} — CW {cfg['osc_forward_steps']}")
            result, _ = _step_i(s1s, s1d, 1, cfg["osc_forward_steps"], delay)
            if result == "stopped": break

            # Pause and check for scan
            _set_phase("oscillating", f"C{cycle}: Osc {osc}/{max_osc} — waiting for scan...")
            t0 = time.time()
            while time.time() - t0 < cfg["osc_pause_sec"]:
                if _should_stop(): break
                with current_card_lock: c = current_card["card"]
                if c and c.get("timestamp","") != seq.last_scan_ts:
                    scanned = True; break
                time.sleep(0.05)
            if _should_stop() or scanned: break

            # Return CCW to beam0
            _set_phase("oscillating", f"C{cycle}: Osc {osc}/{max_osc} — returning to beam0")
            result, _ = _beam_i(s1s, s1d, beam0, -1, delay)
            if result == "stopped": break
            if result == "max_steps":
                with seq.lock: seq.error = f"C{cycle}: Lost beam0 during oscillation"
                break

        if _should_stop(): break

        if not scanned:
            # Scan timeout or error — eject card to error pile via beam0 passthrough
            _set_phase("error_eject", f"C{cycle}: Scan timeout — ejecting to error pile")
            print(f"[SEQ] C{cycle}: No scan — ejecting to error pile (S1 CCW → beam0 passthrough)")

            result, _ = _single_passthrough_i(s1s, s1d, -1, beam0, delay)
            if result == "stopped": break

            with seq.lock:
                seq.error = f"C{cycle}: Unable to scan card — sent to error pile"
                seq.cycle_count = cycle
            # Continue to next card
            time.sleep(0.2)
            continue

        # Update scan ts
        with current_card_lock: card = current_card["card"]
        with seq.lock: seq.last_scan_ts = card["timestamp"] if card else ""
        card_name = card["name"] if card else "Unknown"
        pile = card.get("pile", 0) if card else 0
        print(f"[SEQ] C{cycle}: Scanned → {card_name} → Pile {pile}")

        # ── 3. ROUTE TO PILE ─────────────────────────────────────────

        # Resolve error pile alias
        actual_pile = pile
        if pile == 0:
            actual_pile = error_pile  # might still be 0

        if actual_pile == 0:
            # ── PILE 0: No matching condition — reverse to beam0 passthrough ──
            _set_phase("error_eject", f"C{cycle}: Pile 0 — no match, reversing to error pile")

            # Show error with card properties
            with seq.lock:
                seq.error = f"C{cycle}: No matching condition for '{card_name}'"
                seq.last_error_card = card

            result, _ = _single_passthrough_i(s1s, s1d, -1, beam0, delay)
            if result == "stopped": break

        elif actual_pile == 1:
            # ── PILE 1: Special — run to beam1, then shuffle ──
            pile_cfg = piles.get(1, {})
            beam_name = pile_cfg.get("beam", "beam1")
            beam_pin = PINS.get(beam_name, PINS.get("beam1"))

            _set_phase("ejecting", f"C{cycle}: → Pile 1 — S1+S2 CW to {beam_name}")

            # Run both steppers CW until beam1
            result, steps = _dual_beam_i(s1s, s1d, 1, s2s, s2d, 1, beam_pin, delay)
            if result == "stopped": break
            if result == "max_steps":
                with seq.lock: seq.error = f"C{cycle}: Pile 1 beam never hit"
                break

            # Shuffle back and forth to drop into pile 1
            shuffle = cfg.get("pile1_shuffle_steps", 300)
            _set_phase("ejecting", f"C{cycle}: → Pile 1 — shuffling {shuffle} steps")
            result, _ = _dual_i(s1s, s1d, -1, s2s, s2d, -1, shuffle, delay)
            if result == "stopped": break
            result, _ = _dual_i(s1s, s1d, 1, s2s, s2d, 1, shuffle, delay)
            if result == "stopped": break

        else:
            # ── PILE N (>1): servo up, run to beam passthrough, servo down ──
            pile_cfg = piles.get(actual_pile)
            if not pile_cfg:
                # No config for this pile — treat as error
                with seq.lock: seq.error = f"C{cycle}: No config for pile {actual_pile}"
                _set_phase("error_eject", f"C{cycle}: Pile {actual_pile} not configured — error")
                result, _ = _single_passthrough_i(s1s, s1d, -1, beam0, delay)
                if result == "stopped": break
                continue

            beam_name = pile_cfg.get("beam", "beam1")
            beam_pin = PINS.get(beam_name)
            servo_ch = pile_cfg.get("servo_ch", 15)
            servo_up = pile_cfg.get("servo_up", 90)
            servo_down = pile_cfg.get("servo_down", 0)

            if beam_pin is None:
                with seq.lock: seq.error = f"C{cycle}: Beam '{beam_name}' not found in PINS"
                break

            # Flip servo up (hold=True — keep position while card moves)
            _set_phase("ejecting", f"C{cycle}: → Pile {actual_pile} — servo {servo_ch} UP")
            set_servo_angle(servo_ch, servo_up, hold=True)
            time.sleep(0.3)  # let servo reach position

            # Run both steppers CW until beam passthrough
            _set_phase("ejecting", f"C{cycle}: → Pile {actual_pile} — S1+S2 CW → {beam_name} passthrough")
            result, steps = _dual_passthrough_i(s1s, s1d, 1, s2s, s2d, 1, beam_pin, delay)
            if result == "stopped":
                set_servo_angle(servo_ch, servo_down); break
            if result == "max_steps":
                set_servo_angle(servo_ch, servo_down)
                with seq.lock: seq.error = f"C{cycle}: Card never passed {beam_name}"
                break

            # Extra steps to clear
            extra = cfg.get("eject_extra_steps", 100)
            if extra > 0:
                result, _ = _dual_i(s1s, s1d, 1, s2s, s2d, 1, extra, delay)
                if result == "stopped":
                    set_servo_angle(servo_ch, servo_down); break

            # Flip servo down (auto-releases after settling)
            set_servo_angle(servo_ch, servo_down)

        print(f"[SEQ] C{cycle}: Complete — pile {actual_pile}")
        with seq.lock: seq.cycle_count = cycle
        time.sleep(0.1)

    # Cleanup
    with seq.lock:
        seq.running = False
        if seq.stop_requested:
            seq.status_msg = f"Stopped after {seq.cycle_count} cards"
        elif not seq.error:
            seq.status_msg = f"Done: {seq.cycle_count} cards sorted"
        seq.phase = "idle"

    # All servos down
    for pn, pc in get_pile_config().items():
        set_servo_angle(pc["servo_ch"], pc.get("servo_down", 0))

    print(f"[SEQ] ═══ Loop ended: {seq.cycle_count} cards ═══")

# ---------------------------------------------------------------------------
# FLASK
# ---------------------------------------------------------------------------
app = Flask(__name__, template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates"))

def add_cors(r):
    r.headers["Access-Control-Allow-Origin"]="*"
    r.headers["Access-Control-Allow-Methods"]="POST, GET, OPTIONS"
    r.headers["Access-Control-Allow-Headers"]="Content-Type"
    return r

def handle_webhook():
    if request.method == "OPTIONS": return add_cors(jsonify({"msg":"ok"})), 200
    data = request.json or {}; et = data.get("type","")
    if et == "card_scanned":
        cards = data.get("cards",[])
        if not cards: return add_cors(jsonify({"status":"no cards"})), 200
        enriched = enrich_card(cards[0])
        pile = evaluate_rules(enriched, load_rules())
        entry = {**enriched, "timestamp": datetime.now().isoformat(), "pile": pile}
        with scan_log_lock: scan_log.append(entry)
        with current_card_lock: current_card["card"] = entry
        print(f"[WEBHOOK] ✓ {entry['name']} → Pile {pile}")
        return add_cors(jsonify({"status":"ok","pile":pile})), 200
    return add_cors(jsonify({"status":"ok"})), 200

# -- Routes --
@app.route("/", methods=["GET","POST","OPTIONS"])
def index():
    if request.method == "GET":
        return render_template("dashboard.html", simulated=SIMULATED)
    return handle_webhook()

@app.route("/webhook", methods=["POST","OPTIONS"])
def webhook_route(): return handle_webhook()

@app.route("/api/status")
def api_status():
    beams = {}
    for n, p in PINS.items():
        if "beam" in n: beams[n] = GPIO.input(p) == GPIO.LOW
    servos = {}
    for pn, pc in get_pile_config().items():
        ch = pc.get("servo_ch",0)
        servos[f"pile{pn}_ch{ch}"] = get_servo_angle(ch)
    with seq.lock:
        sd = {"seq_running":seq.running,"seq_phase":seq.phase,"seq_status":seq.status_msg,
              "seq_error":seq.error,"seq_cycles":seq.cycle_count,"seq_osc":seq.osc_count,
              "seq_error_card":seq.last_error_card}
    with scan_log_lock: total = len(scan_log)
    with current_card_lock: card = current_card["card"]
    settings = load_settings()
    return jsonify({"simulated":SIMULATED,"beams":beams,"servos":servos,
                     "total_scans":total,"current_card":card,
                     "pins":PINS,"piles":settings.get("piles",{}),
                     "error_pile":settings.get("error_pile",0),
                     "sort_config":settings.get("sort_config",SORT_CONFIG),
                     "servo_available":SERVO_AVAILABLE, **sd})

# Motor
@app.route("/api/motor/step", methods=["POST"])
def motor_step_api():
    b=request.json or {}; s=b.get("stepper",1); d=b.get("direction",1)
    st=b.get("steps",200); dl=b.get("delay",0.001)
    try: return jsonify({"ok":True,"steps_taken":step_motor(PINS[f"stepper{s}_step"],PINS[f"stepper{s}_dir"],d,st,dl)})
    except Exception as e: return jsonify({"ok":False,"error":str(e)}),500

@app.route("/api/motor/dual", methods=["POST"])
def motor_dual_api():
    b=request.json or {}
    try: return jsonify({"ok":True,"steps_taken":step_dual(
        PINS["stepper1_step"],PINS["stepper1_dir"],b.get("s1_dir",1),
        PINS["stepper2_step"],PINS["stepper2_dir"],b.get("s2_dir",-1),b.get("steps",200),b.get("delay",0.001))})
    except Exception as e: return jsonify({"ok":False,"error":str(e)}),500

# Servo
@app.route("/api/servo/set", methods=["POST"])
def servo_set():
    b=request.json or {}; ch=b.get("channel",15); angle=b.get("angle",90)
    hold=b.get("hold", True)  # manual control holds by default
    set_servo_angle(ch, angle, hold=hold)
    return jsonify({"ok":True,"channel":ch,"angle":angle})

@app.route("/api/servo/nudge", methods=["POST"])
def servo_nudge():
    b=request.json or {}; ch=b.get("channel",15); delta=b.get("delta",5)
    cur = get_servo_angle(ch)
    new_angle = max(0, min(180, cur + delta))
    set_servo_angle(ch, new_angle, hold=True)  # hold during nudging
    return jsonify({"ok":True,"channel":ch,"angle":new_angle})

@app.route("/api/servo/release", methods=["POST"])
def servo_release():
    b=request.json or {}; ch=b.get("channel",15)
    release_servo(ch)
    return jsonify({"ok":True,"channel":ch})

# Sequence
@app.route("/api/seq/start", methods=["POST"])
def seq_start():
    with seq.lock:
        if seq.running: return jsonify({"ok":False,"error":"Already running"}),409
    threading.Thread(target=continuous_sort_loop, daemon=True).start()
    return jsonify({"ok":True})

@app.route("/api/seq/stop", methods=["POST"])
def seq_stop():
    with seq.lock: seq.stop_requested = True
    return jsonify({"ok":True})

# Settings
@app.route("/api/settings", methods=["GET"])
def get_settings(): return jsonify(load_settings())

@app.route("/api/settings", methods=["POST"])
def set_settings():
    save_settings(request.json); return jsonify({"ok":True})

# Sim
@app.route("/api/sim/beam", methods=["POST"])
def sim_beam():
    if not SIMULATED: return jsonify({"ok":False}),400
    b=request.json or {}; pin=PINS.get(b.get("beam","beam0"))
    if pin is not None: GPIO.sim_set_beam(pin, b.get("blocked",False))
    return jsonify({"ok":True})

@app.route("/api/sim/scan", methods=["POST"])
def sim_scan():
    b=request.json or {}
    fake={"name":b.get("name","Birds of Paradise"),"edition":b.get("edition",""),
          "editionCode":b.get("editionCode",""),"number":b.get("number",""),
          "rarity":b.get("rarity","R"),"price":b.get("price",8.36),
          "fmtPrice":"","finish":"regular","cardType":b.get("cardType",""),
          "scryfallId":b.get("scryfallId","")}
    enriched=enrich_card(fake); pile=evaluate_rules(enriched,load_rules())
    entry={**enriched,"timestamp":datetime.now().isoformat(),"pile":pile}
    with scan_log_lock: scan_log.append(entry)
    with current_card_lock: current_card["card"]=entry
    return jsonify({"ok":True,"card":entry})

@app.route("/api/rules", methods=["GET"])
def get_rules(): return jsonify(load_rules())
@app.route("/api/rules", methods=["POST"])
def set_rules():
    r=request.json
    if not isinstance(r,list): return jsonify({"error":"Expected array"}),400
    save_rules(r); return jsonify({"ok":True})

@app.route("/api/scans", methods=["GET"])
def get_scans():
    with scan_log_lock: return jsonify(list(scan_log))
@app.route("/api/scans/clear", methods=["POST"])
def clear_scans():
    with scan_log_lock: scan_log.clear()
    with current_card_lock: current_card["card"]=None
    return jsonify({"ok":True})
@app.route("/api/scans/export", methods=["GET"])
def export_scans():
    with scan_log_lock:
        lines=["timestamp,name,edition,rarity,cmc,colors,type_line,price,pile"]
        for s in scan_log:
            lines.append(f'{s["timestamp"]},"{s["name"]}","{s.get("edition","")}",{s.get("rarity","")},{s.get("cmc",0)},{"|".join(s.get("colors",[]))},"{s.get("type_line","")}",{s.get("price",0)},{s["pile"]}')
    return "\n".join(lines),200,{"Content-Type":"text/csv","Content-Disposition":"attachment; filename=scans.csv"}

@app.route("/api/scryfall/search", methods=["GET"])
def scryfall_search():
    q=request.args.get("q","")
    if not q: return jsonify({"error":"Missing ?q="}),400
    try:
        resp=http_requests.get("https://api.scryfall.com/cards/search",params={"q":q,"unique":"prints","order":"released","dir":"desc"},
            headers={"User-Agent":"CardSorterPi/1.0"},timeout=5)
        if resp.status_code==200:
            cards=[]
            for c in resp.json().get("data",[])[:8]:
                img=""
                if c.get("image_uris"): img=c["image_uris"].get("small","")
                elif c.get("card_faces") and c["card_faces"][0].get("image_uris"): img=c["card_faces"][0]["image_uris"].get("small","")
                cards.append({"id":c.get("id",""),"name":c.get("name",""),"set_name":c.get("set_name",""),
                    "set":c.get("set",""),"number":c.get("collector_number",""),"rarity":c.get("rarity",""),"image":img})
            return jsonify(cards)
        elif resp.status_code==404: return jsonify([])
        else: return jsonify({"error":f"Scryfall {resp.status_code}"}),502
    except Exception as e: return jsonify({"error":str(e)}),500

if __name__ == "__main__":
    host = "0.0.0.0" if not SIMULATED else "127.0.0.1"
    port = 5000 if not SIMULATED else 8080
    print(f"\n🃏 Card Sorter v3")
    print(f"   Mode: {'SIM' if SIMULATED else 'PI'}  |  Servo: {'YES' if SERVO_AVAILABLE else 'SIM'}")
    print(f"   http://{'<pi-ip>' if not SIMULATED else '127.0.0.1'}:{port}")
    if not SIMULATED: print(f"   ⚠️  sudo python app.py for GPIO")
    print()
    app.run(host=host, port=port, debug=SIMULATED)
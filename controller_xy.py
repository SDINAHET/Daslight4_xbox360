import json
import math
import os
import sys
import time
from threading import Thread

# --- Entrées manette ---
from inputs import get_gamepad

# --- Souris & clavier ---
import pyautogui
import keyboard

CONFIG_PATH = "config_xy.json"

DEFAULT_CONFIG = {
    "rect": {  # Zone Daslight à l'écran (sera calibrée avec F6/F7)
        "x1": 1400, "y1": 260,  # coin haut-gauche
        "x2": 1820, "y2": 660   # coin bas-droit
    },
    "settings": {
        "deadzone": 0.12,         # zone morte du stick (0..1)
        "expo": 1.5,              # courbe d'expo (>1 = plus fin au centre)
        "invert_y": True,         # inverser Y (souvent plus naturel)
        "smooth": 0.25,           # lissage 0..1 (0=brut, 0.25 conseillé)
        "autodrag": True,         # clique-maintenu auto dans la zone
        "drag_button": "BTN_TL",  # si autodrag=False : maintenir LB pour glisser
        "enable_toggle_key": "f8", # activer/désactiver le script
        "exit_key": "f12"          # fermer proprement
    },
    "hotkeys": {
        "set_top_left": "f6",     # enregistre x1,y1 avec la souris
        "set_bottom_right": "f7", # enregistre x2,y2 avec la souris
        "save_rect": "f9",        # sauvegarde le rect en JSON
        "load_rect": "f10",       # recharge le rect du JSON
        "center_cursor": "f11"    # recentre le curseur dans la zone
    }
}

# État global
STATE = {
    "enabled": True,
    "dragging": False,
    "last_x": None,
    "last_y": None,
    "lb_held": False
}

# --------- Utils config ----------
def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            # merge permissif
            return deep_merge(DEFAULT_CONFIG, cfg)
        except Exception:
            pass
    return DEFAULT_CONFIG

def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

def deep_merge(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        r = dict(a)
        for k, v in b.items():
            r[k] = deep_merge(a.get(k), v)
        return r
    return b if b is not None else a

CFG = load_config()

# Sécurité pyautogui
pyautogui.FAILSAFE = True   # coin haut-gauche pour stop d'urgence
pyautogui.PAUSE = 0         # pas de pause automatique

def clamp(v, lo, hi):
    return max(lo, min(hi, v))

def lerp(a, b, t):
    return a + (b - a) * t

def expo_curve(v, expo):
    # v in [-1..1] -> applique courbe expo (préserve le signe)
    s = 1 if v >= 0 else -1
    v = abs(v)
    return s * (v ** expo)

def stick_to_unit(axis, deadzone, expo):
    """
    Convertit valeur d'axe int -> float -1..1, applique deadzone & expo.
    inputs / Xbox renvoie:
      - ABS_X / ABS_Y: -32768..32767
    """
    if axis is None:
        return 0.0
    # normalisation
    if axis < 0:
        n = axis / 32768.0
    else:
        n = axis / 32767.0

    # deadzone
    if abs(n) < deadzone:
        return 0.0

    # rescale hors deadzone
    n = (abs(n) - deadzone) / (1 - deadzone) * (1 if n >= 0 else -1)
    n = clamp(n, -1.0, 1.0)

    # expo
    n = expo_curve(n, expo)
    return float(n)

def map_to_rect(x_unit, y_unit, rect, invert_y):
    """
    x_unit,y_unit ∈ [-1..1] → position écran dans rect
    Centre du stick = centre du rect
    """
    cx = (rect["x1"] + rect["x2"]) / 2.0
    cy = (rect["y1"] + rect["y2"]) / 2.0
    half_w = (rect["x2"] - rect["x1"]) / 2.0
    half_h = (rect["y2"] - rect["y1"]) / 2.0

    y_unit = -y_unit if invert_y else y_unit
    x = cx + x_unit * half_w
    y = cy + y_unit * half_h

    return int(clamp(x, rect["x1"], rect["x2"])), int(clamp(y, rect["y1"], rect["y2"]))

def start_drag_if_needed():
    if not STATE["dragging"]:
        pyautogui.mouseDown()
        STATE["dragging"] = True

def stop_drag_if_needed():
    if STATE["dragging"]:
        pyautogui.mouseUp()
        STATE["dragging"] = False

def move_cursor_smooth(nx, ny, smooth):
    lx, ly = STATE["last_x"], STATE["last_y"]
    if lx is None or ly is None or smooth <= 0:
        pyautogui.moveTo(nx, ny)
        STATE["last_x"], STATE["last_y"] = nx, ny
        return

    # interpolation unique par frame
    t = clamp(1.0 - smooth, 0.05, 1.0)  # plus smooth est grand, plus on lisse
    ix = int(lerp(lx, nx, t))
    iy = int(lerp(ly, ny, t))
    pyautogui.moveTo(ix, iy)
    STATE["last_x"], STATE["last_y"] = ix, iy

def center_cursor():
    r = CFG["rect"]
    cx = (r["x1"] + r["x2"]) // 2
    cy = (r["y1"] + r["y2"]) // 2
    pyautogui.moveTo(cx, cy)
    STATE["last_x"], STATE["last_y"] = cx, cy

def info(msg):
    print(f"[INFO] {msg}")

def warn(msg):
    print(f"[WARN] {msg}")

# --------- Threads ---------
def gamepad_thread():
    """
    Boucle de lecture manette → calcul position → souris.
    """
    axes = {"ABS_X": 0, "ABS_Y": 0}
    btn_lb_code = CFG["settings"]["drag_button"]
    autodrag = CFG["settings"]["autodrag"]
    while True:
        try:
            events = get_gamepad()
            if not STATE["enabled"]:
                # libérer le drag si désactivé
                stop_drag_if_needed()
                time.sleep(0.01)
                continue

            for e in events:
                code, val = e.code, e.state

                # Boutons
                if code == btn_lb_code:
                    STATE["lb_held"] = (val == 1)

                # Axes stick gauche
                if code == "ABS_X":
                    axes["ABS_X"] = val
                elif code == "ABS_Y":
                    axes["ABS_Y"] = val

            # Convertir axes → unité
            s = CFG["settings"]
            x_unit = stick_to_unit(axes["ABS_X"], s["deadzone"], s["expo"])
            y_unit = stick_to_unit(axes["ABS_Y"], s["deadzone"], s["expo"])

            # Pas de mouvement → on relâche éventuellement le drag si on n'est pas en autodrag
            if x_unit == 0 and y_unit == 0 and not autodrag and not STATE["lb_held"]:
                stop_drag_if_needed()

            # Calcul position écran
            nx, ny = map_to_rect(x_unit, y_unit, CFG["rect"], s["invert_y"])
            move_cursor_smooth(nx, ny, s["smooth"])

            # Drag auto ou en maintenant LB
            if autodrag or STATE["lb_held"]:
                start_drag_if_needed()
            else:
                stop_drag_if_needed()

        except Exception as ex:
            warn(f"gamepad loop: {ex}")
            time.sleep(0.01)

def hotkeys_thread():
    global CFG   # On le met UNE FOIS au tout début
    hk = CFG["hotkeys"]

    info("Raccourcis:")
    info(f"- {hk['set_top_left']}  : enregistrer coin haut-gauche")
    info(f"- {hk['set_bottom_right']}: enregistrer coin bas-droit")
    info(f"- {hk['save_rect']}   : sauvegarder config")
    info(f"- {hk['load_rect']}   : recharger config")
    info(f"- {hk['center_cursor']}: centrer le curseur dans la zone")
    info(f"- {CFG['settings']['enable_toggle_key']}: activer/désactiver")
    info(f"- {CFG['settings']['exit_key']}: quitter")
    info("Astuce: mets Daslight au premier plan, place la souris sur chaque coin, F6/F7, puis F9.")

    while True:
        try:
            if keyboard.is_pressed(hk["set_top_left"]):
                x, y = pyautogui.position()
                CFG["rect"]["x1"], CFG["rect"]["y1"] = x, y
                info(f"Top-Left = ({x},{y})")
                time.sleep(0.3)

            if keyboard.is_pressed(hk["set_bottom_right"]):
                x, y = pyautogui.position()
                CFG["rect"]["x2"], CFG["rect"]["y2"] = x, y
                info(f"Bottom-Right = ({x},{y})")
                time.sleep(0.3)

            if keyboard.is_pressed(hk["save_rect"]):
                save_config(CFG)
                info(f"Sauvegardé → {CONFIG_PATH}")
                time.sleep(0.3)

            # if keyboard.is_pressed(hk["load_rect"]):
            #     global CFG
            #     CFG = load_config()
            #     info("Config rechargée.")
            #     time.sleep(0.3)

            if keyboard.is_pressed(hk["load_rect"]):
                CFG = load_config()
                info("Config rechargée.")
                time.sleep(0.3)


            if keyboard.is_pressed(hk["center_cursor"]):
                center_cursor()
                time.sleep(0.3)

            if keyboard.is_pressed(CFG["settings"]["enable_toggle_key"]):
                STATE["enabled"] = not STATE["enabled"]
                if not STATE["enabled"]:
                    stop_drag_if_needed()
                info(f"Enabled = {STATE['enabled']}")
                time.sleep(0.3)

            if keyboard.is_pressed(CFG["settings"]["exit_key"]):
                info("Fermeture demandée.")
                stop_drag_if_needed()
                os._exit(0)

            time.sleep(0.02)
        except Exception as ex:
            warn(f"hotkeys loop: {ex}")
            time.sleep(0.1)

def main():
    center_cursor()

    t1 = Thread(target=gamepad_thread, daemon=True)
    t2 = Thread(target=hotkeys_thread, daemon=True)
    t1.start()
    t2.start()

    # boucle vie
    while True:
        time.sleep(1)

if __name__ == "__main__":
    main()

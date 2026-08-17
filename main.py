"""
========================================================================================
BOT DE SIGNAL LIVE : WEBSOCKET (Pragmatic Play) -> MACHINE A ETATS -> TELEGRAM
========================================================================================
🔧 STRATÉGIE "RETOUR APRÈS ABSENCE" : DIZAINES / COLONNES
   - Parie qu'une dizaine/colonne PRÉCISE va enfin réapparaître après avoir
     été absente pendant SEUIL spins d'affilée.
   - Seuil de déclenchement : 23 spins d'absence
   - Échelle de mise : 5 vies [55, 55, 110, 165, 275] (660 DHS requis)
   - Une seule séquence par signal (target_wins=1)
   - La cible NE CHANGE JAMAIS pendant un signal
   - PAYOUT 2:1 (dizaine/colonne, pas 1:1 comme pair/impair)

🔧 FILTRAGE TELEGRAM : seuls les 2 premiers signaux après chaque bust sont
   notifiés sur Telegram. Le bot continue de tout traiter/parier normalement
   en interne (CSV, console) — il devient juste silencieux côté Telegram à
   partir du 3ème signal, jusqu'au prochain bust qui relance le compteur.
========================================================================================
"""

import json
import time
import csv
import os
import requests
import websocket  # pip install websocket-client
from datetime import datetime

# ==========================================================================
# 0. ENREGISTREMENT CSV (persistant si Volume Railway attaché)
# ==========================================================================
VOLUME_PATH = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", ".")
CSV_FILE = os.path.join(VOLUME_PATH, "roulette_data.csv")
CSV_HEADERS = ["Timestamp", "GameID", "Result", "Color"]


def ensure_valid_csv_header():
    if not os.path.exists(CSV_FILE):
        return
    try:
        with open(CSV_FILE, newline='') as f:
            header = next(csv.reader(f), None)
        if header != CSV_HEADERS:
            backup_name = CSV_FILE + ".ancien_format.bak"
            print(f"[CSV] ⚠️ Format existant incompatible (colonnes trouvées : {header}) "
                  f"— sauvegardé sous {backup_name}, nouveau fichier créé.")
            os.replace(CSV_FILE, backup_name)
    except Exception as e:
        print(f"[CSV] Erreur de vérification du header : {e}")


def load_last_game_id_from_csv():
    if not os.path.exists(CSV_FILE):
        return None
    try:
        with open(CSV_FILE, newline='') as f:
            rows = list(csv.reader(f))
        if len(rows) <= 1:
            return None
        return rows[-1][1]
    except Exception as e:
        print(f"[CSV] Impossible de lire le dernier gameId : {e}")
        return None


ensure_valid_csv_header()

if not os.path.exists(CSV_FILE):
    with open(CSV_FILE, mode='w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADERS)


def log_spin_to_csv(game_id, result, color, spin_time=None):
    timestamp = spin_time if spin_time else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = [timestamp, game_id, result, color]
    with open(CSV_FILE, mode='a', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(row)


# ==========================================================================
# 1. CONFIGURATION TELEGRAM
# ==========================================================================
TELEGRAM_BOT_TOKEN = "8623867750:AAEf4-Ke-1hlmMeYEwetx4a4V5WWbs2d8D0"
TELEGRAM_CHAT_ID = "6098394153"


def send_telegram_alert(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"[Telegram] Erreur d'envoi : {e}")


# ==========================================================================
# 2. CONFIGURATION WEBSOCKET (Pragmatic Play) + PROXY DATAIMPULSE
# ==========================================================================
WS_URL = "wss://dga.pragmaticplaylive.net/ws"

TABLE_KEY = "2244"
CURRENCY = "EUR"
CASINO_ID = "il9srgw4dna22222"

WS_HEADERS = [
    "Accept-Encoding: gzip, deflate, br, zstd",
    "Accept-Language: en-US,en;q=0.9,fr-MA;q=0.8,fr;q=0.7",
    "Cache-Control: no-cache",
    "Pragma: no-cache",
    "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
]
WS_ORIGIN = "https://www.bigwinboard.com"

PROXY_HOST = "gw.dataimpulse.com"
PROXY_PORT = 824
PROXY_TYPE = "socks5"
PROXY_LOGIN = "c28464d2322ae2cb5a09"
PROXY_PASSWORD = "afd6703a49960bd1"
USE_PROXY = True

DEBUG_TRACE = False


# ==========================================================================
# 3. MACHINE A ETATS : RETOUR APRÈS ABSENCE (DIZAINES/COLONNES)
# ==========================================================================
def get_dozen(n):
    if 1 <= n <= 12: return 1
    if 13 <= n <= 24: return 2
    if 25 <= n <= 36: return 3
    return 0

def get_column(n):
    if n == 0: return 0
    if n % 3 == 1: return 1
    if n % 3 == 2: return 2
    if n % 3 == 0: return 3
    return 0


class LiveAbsenceEngine:
    def __init__(self, seuil=20):
        self.fib = [55, 55, 110, 165, 275]
        self.capital_requis = sum(self.fib)  # 660
        self.seuil = seuil

        self.capital = self.capital_requis
        self.initial_capital = self.capital
        self.total_real_deposits = self.capital

        self.is_betting = False
        self.target_type = None   # 'dozen' ou 'column'
        self.target_value = None
        self.fib_index = 0
        self.current_sequence_loss = 0

        # Compteurs d'absence pour les 6 catégories, mis à jour à CHAQUE spin
        self.absence = {
            ('dozen', 1): 0, ('dozen', 2): 0, ('dozen', 3): 0,
            ('column', 1): 0, ('column', 2): 0, ('column', 3): 0,
        }

        self.signal_counter = 0

        # 🔧 Filtrage Telegram : seuls les 2 premiers signaux après chaque
        # bust sont notifiés. Le compteur repart à 0 à chaque bust.
        self.signals_since_bust = 0
        self.notify_current_signal = True

    def process_spin(self, number):
        events = []
        c_doz = get_dozen(number)
        c_col = get_column(number)

        # Mise à jour des compteurs d'absence à CHAQUE spin, peu importe l'état
        for val in (1, 2, 3):
            self.absence[('dozen', val)] = 0 if c_doz == val else self.absence[('dozen', val)] + 1
            self.absence[('column', val)] = 0 if c_col == val else self.absence[('column', val)] + 1

        if not self.is_betting:
            # 🔧 Alerte précoce (seuil-1), respecte le même filtrage que le
            # signal réel qui la suivra (prédictif, calculé avant l'ouverture).
            will_be_notified = (self.signals_since_bust + 1) <= 2
            self.notify_current_signal = will_be_notified

            for (cat_type, val), count in self.absence.items():
                if count == self.seuil - 1:
                    events.append(
                        f"⏳ <b>ALERTE PRÉCOCE</b> — {cat_type.upper()} {val} approche du seuil "
                        f"({count}/{self.seuil} spins d'absence). Prépare-toi."
                    )

            triggered = None
            for cat_type, val in [('dozen', 1), ('dozen', 2), ('dozen', 3),
                                   ('column', 1), ('column', 2), ('column', 3)]:
                if self.absence[(cat_type, val)] >= self.seuil:
                    triggered = (cat_type, val)
                    break

            if triggered:
                self.target_type, self.target_value = triggered
                self.is_betting = True
                self.fib_index = 0
                self.current_sequence_loss = 0
                self.signal_counter += 1

                self.signals_since_bust += 1
                self.notify_current_signal = self.signals_since_bust <= 2

                events.append(
                    f"⚡ <b>SIGNAL #{self.signal_counter}</b> — {self.target_type.upper()} {self.target_value} "
                    f"(absent depuis {self.absence[triggered]} spins)\n"
                    f"Mise à placer : <b>{self.fib[0]} DHS</b> sur {self.target_type} {self.target_value}"
                )

            return events

        # PHASE D'ATTAQUE (cible FIXE, ne change jamais, payout 2:1)
        bet_amount = self.fib[self.fib_index]
        actual_val = c_doz if self.target_type == 'dozen' else c_col

        if actual_val == self.target_value:
            net_gain = bet_amount * 2  # 🔧 payout 2:1
            self.capital += net_gain
            profit = net_gain - self.current_sequence_loss

            events.append(
                f"🟢 <b>GAIN</b> — Signal #{self.signal_counter} | Palier {self.fib_index + 1}\n"
                f"Profit : +{profit} DHS | Capital : {self.capital} DHS"
            )
            events.append(f"✅ Signal #{self.signal_counter} terminé — objectif atteint.")

            self.is_betting = False
            self.fib_index = 0
            self.current_sequence_loss = 0
        else:
            self.capital -= bet_amount
            self.current_sequence_loss += bet_amount
            self.fib_index += 1

            events.append(
                f"🔴 Perte niveau {self.fib_index} | Prochaine mise : "
                f"{self.fib[self.fib_index] if self.fib_index < len(self.fib) else 'RECHARGE'} DHS"
            )

            if self.fib_index >= len(self.fib) or self.capital < self.fib[self.fib_index]:
                solde_restant = self.capital
                if solde_restant < self.capital_requis:
                    apport = self.initial_capital - solde_restant
                    self.total_real_deposits += apport
                    self.capital = self.initial_capital
                    events.append(f"🚨 <b>BUST</b> — Recharge de {apport} DHS nécessaire. Capital remis à {self.initial_capital} DHS.")
                else:
                    events.append(f"🚨 Fin de séquence — capital auto-suffisant ({solde_restant} DHS).")

                self.signals_since_bust = 0  # 🔧 relance le compteur pour les 2 prochains signaux
                self.is_betting = False
                self.fib_index = 0
                self.current_sequence_loss = 0

        return events


# ==========================================================================
# 4. CLIENT WEBSOCKET
# ==========================================================================
engine = LiveAbsenceEngine(seuil=20)

last_game_id = load_last_game_id_from_csv()
if last_game_id:
    print(f"[Rattrapage] Dernier gameId connu au démarrage : {last_game_id}")


def handle_new_result(number, table_id):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Nouveau spin (table {table_id}) : {number}")
    events = engine.process_spin(number)
    for msg in events:
        if engine.notify_current_signal:
            send_telegram_alert(msg)
        print(msg)


def process_new_results(results):
    global last_game_id

    if last_game_id is None:
        new_entries = list(reversed(results))
    else:
        idx = next((i for i, r in enumerate(results) if r.get("gameId") == last_game_id), None)
        if idx is None:
            print("[Rattrapage] ⚠️ Coupure trop longue (>20 spins) — reprise au plus récent.")
            new_entries = [results[0]] if results else []
        elif idx == 0:
            new_entries = []
        else:
            new_entries = list(reversed(results[:idx]))
            if len(new_entries) > 1:
                print(f"[Rattrapage] {len(new_entries)} spin(s) manqué(s) détecté(s), traitement en cours...")

    for entry in new_entries:
        game_id = entry.get("gameId")
        if game_id is None:
            continue

        last_game_id = game_id
        log_spin_to_csv(game_id, entry.get("result"), entry.get("color"), entry.get("time"))

        try:
            number = int(entry["result"])
        except (KeyError, ValueError, TypeError):
            continue

        handle_new_result(number, TABLE_KEY)


def on_message(ws, message):
    try:
        data = json.loads(message)
    except json.JSONDecodeError:
        return

    if str(data.get("tableId")) != TABLE_KEY:
        return

    results = data.get("last20Results")
    if not results:
        return

    process_new_results(results)


def on_error(ws, error):
    print(f"[WS] Erreur : {repr(error)} (type: {type(error).__name__})")


def on_close(ws, close_status_code, close_msg):
    print(f"[WS] Connexion fermée (code={close_status_code}, msg={close_msg}). Reconnexion dans 3s...")


def on_open(ws):
    print("[WS] Connexion établie.")
    send_telegram_alert(
        f"🎲 Bot démarré (WebSocket). Stratégie : Retour après absence (DIZAINE/COLONNE) | "
        f"Seuil : {engine.seuil} spins | Capital : {engine.initial_capital} DHS."
    )

    msg1 = json.dumps({"type": "available", "casinoId": CASINO_ID})
    ws.send(msg1)
    print(f"[WS] Message 'available' envoyé : {msg1}")

    time.sleep(1)

    msg2 = json.dumps({"type": "subscribe", "currency": CURRENCY, "key": TABLE_KEY, "casinoId": CASINO_ID})
    ws.send(msg2)
    print(f"[WS] Message 'subscribe' envoyé : {msg2}")


def run_forever_with_reconnect():
    if DEBUG_TRACE:
        import logging
        logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(message)s')
        websocket.enableTrace(True)

    while True:
        ws = websocket.WebSocketApp(
            WS_URL,
            header=WS_HEADERS,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )

        run_kwargs = {"ping_interval": 30, "ping_timeout": 25, "origin": WS_ORIGIN}
        if USE_PROXY:
            run_kwargs.update({
                "http_proxy_host": PROXY_HOST,
                "http_proxy_port": PROXY_PORT,
                "http_proxy_auth": (PROXY_LOGIN, PROXY_PASSWORD),
                "proxy_type": PROXY_TYPE,
            })

        try:
            ws.run_forever(**run_kwargs)
        except Exception as e:
            print(f"[WS] Exception : {repr(e)} (type: {type(e).__name__})")

        print("[WS] Reconnexion dans 3 secondes...")
        time.sleep(3)


if __name__ == "__main__":
    if VOLUME_PATH == ".":
        print(f"⚠️ ATTENTION : RAILWAY_VOLUME_MOUNT_PATH non détecté — CSV éphémère : {os.path.abspath(CSV_FILE)}")
    else:
        print(f"✅ Volume détecté ({VOLUME_PATH}) — CSV persistant : {CSV_FILE}")
    print(f"🎲 Bot démarré | Échelle (5 vies) : {engine.fib} | Capital requis : {engine.capital_requis} DHS")
    run_forever_with_reconnect()

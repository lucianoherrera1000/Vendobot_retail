# =========================
# test_app.py  (COPIAR Y PEGAR TODO)
# FIX: si no está corriendo app.py, lo levanta solo
# =========================
import sys
import time
import subprocess
import requests

BASE = "http://127.0.0.1:5000"

def is_up():
    try:
        r = requests.get(f"{BASE}/health", timeout=1.0)
        return r.status_code == 200
    except Exception:
        return False

def start_server():
    kwargs = {}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
    subprocess.Popen([sys.executable, "app.py"], **kwargs)

def send(user, text):
    r = requests.post(
        f"{BASE}/test_message",
        json={"from": user, "text": text},
        timeout=15
    )
    r.raise_for_status()
    return r.json().get("reply", "")

def main():
    if not is_up():
        start_server()
        for _ in range(30):
            if is_up():
                break
            time.sleep(0.3)

    print("Escribí mensajes. exit para salir\n")
    user = "test"
    while True:
        msg = input("TU: ").strip()
        if msg.lower() == "exit":
            break
        try:
            print("BOT:", send(user, msg), "\n")
        except Exception as e:
            print("ERROR:", e)

if __name__ == "__main__":
    main()




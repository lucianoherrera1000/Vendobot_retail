# ===== app.py =====
import os
import re
import json
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from typing import Dict, List, Tuple, Optional

import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

APP_NAME = os.getenv("APP_NAME", "Vendobot simplified")
PORT = int(os.getenv("PORT", "5000"))

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")

AI_ENABLED = os.getenv("AI_ENABLED", "0").strip() == "1"
AI_BASE_URL = os.getenv("AI_BASE_URL", "http://127.0.0.1:8080/v1").rstrip("/")
AI_MODEL = os.getenv("AI_MODEL", "local-model")
AI_API_KEY = os.getenv("AI_API_KEY", "none")
DEBUG_SHOW_AI_JSON = os.getenv("DEBUG_SHOW_AI_JSON", "0").strip() == "1"

DATA_DIR = os.path.abspath(os.path.dirname(__file__))
PINCHO_DIR = os.path.join(DATA_DIR, "pincho_comandas")
COUNTER_FILE = os.path.join(DATA_DIR, "counter.txt")
COMANDA_PRINT_PATH = os.path.join(DATA_DIR, "comanda.txt")

ETA_MIN = 20
DELIVERY_FEE = 3000

app = Flask(__name__)
lock = Lock()


# ----------------------------
# Utils texto
# ----------------------------
def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = strip_accents(s)
    s = re.sub(r"[^\w\s$]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def clip_words(s: str, max_words: int) -> str:
    words = (s or "").strip().split()
    return " ".join(words[:max_words]).strip()


def now_ts() -> float:
    return time.time()


# ----------------------------
# Menu + Synonyms
# ----------------------------
@dataclass
class MenuItem:
    sku: str
    name: str
    price: int
    keys: List[str] = field(default_factory=list)


def slugify(s: str) -> str:
    s = norm(s)
    s = s.replace(" ", "_")
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "item"


def parse_price(line: str) -> Optional[int]:
    # acepta "$10000", "10000", "$ 10.000"
    m = re.search(r"(\$?\s*\d[\d\.]*)", line)
    if not m:
        return None
    raw = m.group(1)
    raw = raw.replace("$", "").replace(" ", "").replace(".", "")
    try:
        return int(raw)
    except Exception:
        return None


def load_menu(path: str) -> Dict[str, MenuItem]:
    items: Dict[str, MenuItem] = {}
    if not os.path.exists(path):
        raise FileNotFoundError(f"Falta {path}")

    lines = open(path, "r", encoding="utf-8").read().splitlines()
    for ln in lines:
        line = ln.strip()
        if not line or line.startswith("#"):
            continue

        # Formato: Nombre = $precio
        if "=" in line:
            left, right = line.split("=", 1)
            name = left.strip()
            price = parse_price(right)
        else:
            # fallback: "Nombre $10000"
            name = re.sub(r"\$?\s*\d[\d\.]*", "", line).strip()
            price = parse_price(line)

        if not name or price is None:
            continue

        sku = slugify(name)
        keys = [norm(name)]
        items[sku] = MenuItem(sku=sku, name=name, price=price, keys=keys)

    return items


def load_synonyms(path: str) -> Dict[str, List[str]]:
    syn: Dict[str, List[str]] = {}
    if not os.path.exists(path):
        return syn

    for ln in open(path, "r", encoding="utf-8").read().splitlines():
        line = ln.strip()
        if not line or line.startswith("#"):
            continue
        if "|" not in line:
            continue
        sku, rhs = line.split("|", 1)
        sku = sku.strip()
        aliases = [a.strip() for a in rhs.split(",") if a.strip()]
        syn[sku] = [norm(a) for a in aliases]
    return syn


def build_matchers(menu: Dict[str, MenuItem], synonyms: Dict[str, List[str]]) -> Dict[str, List[str]]:
    # matcher strings normalizados
    matchers: Dict[str, List[str]] = {}
    for sku, item in menu.items():
        m = set(item.keys)
        if sku in synonyms:
            for a in synonyms[sku]:
                if a:
                    m.add(a)
        # también agregar variaciones simples
        m2 = set()
        for k in m:
            m2.add(k)
            m2.add(k.replace("sandwich", "sanguche"))
            m2.add(k.replace("sanguche", "sandwich"))
        matchers[sku] = sorted(m2, key=lambda x: (-len(x), x))
    return matchers


# ----------------------------
# Cantidades
# ----------------------------
NUM_WORDS = {
    "un": 1, "una": 1, "uno": 1,
    "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
    "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10,
}


def extract_qty(text: str) -> Optional[int]:
    t = norm(text)
    m = re.search(r"\b(\d+)\b", t)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    for w, n in NUM_WORDS.items():
        if re.search(rf"\b{re.escape(w)}\b", t):
            return n
    return None


def parse_items(text: str, matchers: Dict[str, List[str]]) -> Dict[str, int]:
    """
    Detecta items y cantidades con reglas simples:
    - Si encuentra una frase/alias, asigna qty por:
        1) patrón "2 x item" o "item x2"
        2) número cerca (antes) "2 item"
        3) default 1
    """
    t = norm(text)
    found: Dict[str, int] = {}

    # patrones "2x" "2 x"
    def qty_near(alias: str) -> int:
        # item x2
        m = re.search(rf"{re.escape(alias)}\s*x\s*(\d+)\b", t)
        if m:
            return int(m.group(1))
        # 2 x item
        m = re.search(rf"\b(\d+)\s*x\s*{re.escape(alias)}", t)
        if m:
            return int(m.group(1))
        # 2 item
        m = re.search(rf"\b(\d+)\s+{re.escape(alias)}", t)
        if m:
            return int(m.group(1))
        # palabras "dos item"
        for w, n in NUM_WORDS.items():
            if re.search(rf"\b{re.escape(w)}\s+{re.escape(alias)}", t):
                return n
        return 1

    # buscar aliases largos primero (por sku ya vienen ordenados)
    for sku, aliases in matchers.items():
        for a in aliases:
            if not a:
                continue
            if re.search(rf"\b{re.escape(a)}\b", t):
                q = qty_near(a)
                found[sku] = found.get(sku, 0) + q
                break

    return found


# ----------------------------
# Clasificadores simples
# ----------------------------
def is_yes(text: str) -> bool:
    t = norm(text)
    return re.fullmatch(r"(si|sí|s|dale|ok|oka|okay|confirmo|de_una|deuna|listo)", t) is not None


def is_no(text: str) -> bool:
    t = norm(text)
    return re.fullmatch(r"(no|n|nop|negativo)", t) is not None


def is_no_thanks(text: str) -> bool:
    t = norm(text)
    # respuestas comunes a "querés agregar/modificar?"
    return re.search(r"\b(no|nop|n)\b", t) is not None or re.search(r"\b(no_gracias|gracias_no|no_gra|no_gracia|no\ gracias)\b", t) is not None


def is_cancel(text: str) -> bool:
    t = norm(text)
    return re.search(r"\b(cancel|cancelar|cancelo|cancelalo|cancela|cancelá|anul|anular|anulo|anulalo|anula|anulá)\b", t) is not None


def detect_payment(text: str) -> Optional[str]:
    t = norm(text)
    if re.search(r"\b(efectivo|cash)\b", t):
        return "efectivo"
    if re.search(r"\b(transfer|transferencia|transf|cbu|alias)\b", t):
        return "transferencia"
    # tu caso "transexual" -> transferencia
    if "transexual" in t:
        return "transferencia"
    return None


def detect_delivery(text: str) -> Optional[str]:
    t = norm(text)
    if re.search(r"\b(envio|enviar|a\ domicilio|delivery)\b", t):
        return "envio"
    if re.search(r"\b(retiro|retirar|paso|buscar|voy)\b", t):
        return "retiro"
    return None


BEVERAGE_WORDS = ["coca", "cocacola", "coca_cola", "gaseosa", "cola", "pepsi", "fanta", "sprite", "agua", "jugo", "bebida"]


def menu_has_beverages(menu: Dict[str, MenuItem]) -> bool:
    # heurística: si algún item contiene coca/gaseosa/agua en el nombre
    joined = " ".join(norm(mi.name) for mi in menu.values())
    return any(w in joined for w in ["coca", "gaseosa", "agua", "bebida", "jugo", "pepsi", "sprite", "fanta"])


def asked_for_beverage(text: str) -> bool:
    t = norm(text)
    return any(re.search(rf"\b{re.escape(w)}\b", t) for w in BEVERAGE_WORDS)


STOPWORDS = {
    "hola", "buen", "buenas", "dia", "tarde", "noche", "como", "estas", "todo", "bien",
    "quiero", "quisiera", "querria", "dame", "mandame", "me", "podes", "podrias", "encargar",
    "por", "favor", "para", "un", "una", "uno", "dos", "tres", "cuatro", "cinco", "y", "con",
    "al", "de", "del", "la", "el", "los", "las", "en", "a", "x"
}


def extract_unknown_food_words(text: str, matchers: Dict[str, List[str]]) -> List[str]:
    # NO “no tengo”: solo avisar bebidas si no existen en menú.
    # Otros "unknown" los ignoramos para no romper UX.
    return []


# ----------------------------
# Sesiones
# ----------------------------
@dataclass
class Session:
    user_id: str
    state: str = "START"
    name: Optional[str] = None
    delivery_method: Optional[str] = None   # envio/retiro
    address: Optional[str] = None
    payment_method: Optional[str] = None    # efectivo/transferencia
    items: Dict[str, int] = field(default_factory=dict)
    modifications: List[str] = field(default_factory=list)

    order_id: Optional[int] = None
    modified_flag: bool = False

    awaiting_cancel_confirm: bool = False
    pending_mod_text: Optional[str] = None

    last_confirmed_ts: Optional[float] = None  # para reset 20 min


SESSIONS: Dict[str, Session] = {}


def get_sess(user_id: str) -> Session:
    with lock:
        s = SESSIONS.get(user_id)
        if not s:
            s = Session(user_id=user_id)
            SESSIONS[user_id] = s
        return s


def reset_if_expired(sess: Session) -> None:
    if sess.last_confirmed_ts is None:
        return
    if now_ts() - sess.last_confirmed_ts >= ETA_MIN * 60:
        # nueva venta luego de 20 min
        sess.state = "START"
        sess.name = None
        sess.delivery_method = None
        sess.address = None
        sess.payment_method = None
        sess.items = {}
        sess.modifications = []
        sess.order_id = None
        sess.modified_flag = False
        sess.awaiting_cancel_confirm = False
        sess.pending_mod_text = None
        sess.last_confirmed_ts = None


# ----------------------------
# Comandas
# ----------------------------
def ensure_dirs():
    os.makedirs(PINCHO_DIR, exist_ok=True)


def bump_counter() -> int:
    ensure_dirs()
    if not os.path.exists(COUNTER_FILE):
        open(COUNTER_FILE, "w", encoding="utf-8").write("0")
    n = int(open(COUNTER_FILE, "r", encoding="utf-8").read().strip() or "0")
    n += 1
    open(COUNTER_FILE, "w", encoding="utf-8").write(str(n))
    return n


def calc_total(sess: Session, menu: Dict[str, MenuItem]) -> int:
    total = 0
    for sku, qty in sess.items.items():
        mi = menu.get(sku)
        if mi:
            total += mi.price * int(qty)
    if sess.delivery_method == "envio":
        total += DELIVERY_FEE
    return total


def order_summary_message(sess: Session, menu: Dict[str, MenuItem]) -> str:
    lines = []
    if sess.name:
        lines.append(f"🧾 Pedido a nombre de: {sess.name}")
    for sku, qty in sess.items.items():
        mi = menu.get(sku)
        if mi:
            lines.append(f"• {qty} x {mi.name}")
    if sess.delivery_method == "envio":
        lines.append(f"📍 Dirección: {sess.address or '-'}")
        lines.append(f"🚚 Envío: ${DELIVERY_FEE}")
    else:
        lines.append("🏃 Retiro en local")
    if sess.payment_method:
        lines.append(f"💳 Pago: {sess.payment_method}")
    lines.append(f"💰 Total: ${calc_total(sess, menu)}")
    lines.append(f"⏱️ Demora: {ETA_MIN} min")
    return "\n".join(lines)


def render_comanda_text(sess: Session, menu: Dict[str, MenuItem], title: str) -> str:
    lines = []
    lines.append(title)
    lines.append(f"Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if sess.order_id:
        lines.append(f"Pedido #{sess.order_id}")
    lines.append("")
    lines.append(f"Cliente: {sess.name or '-'}")
    lines.append("")
    lines.append("Items:")
    for sku, qty in sess.items.items():
        mi = menu.get(sku)
        name = mi.name if mi else sku
        price = mi.price if mi else 0
        lines.append(f"- {qty} x {name}  (${price} c/u)")
    lines.append("")
    if sess.delivery_method == "envio":
        lines.append(f"Entrega: ENVÍO")
        lines.append(f"Dirección: {sess.address or '-'}")
        lines.append(f"Envío: ${DELIVERY_FEE}")
    else:
        lines.append("Entrega: RETIRO")
    lines.append(f"Pago: {sess.payment_method or '-'}")
    lines.append(f"Total: ${calc_total(sess, menu)}")
    lines.append(f"Demora: {ETA_MIN} min")

    if sess.modifications:
        lines.append("")
        lines.append("MODIFICACIONES:")
        for m in sess.modifications[-10:]:
            lines.append(f"* {m}")

    lines.append("")
    return "\n".join(lines)


def write_comandas(sess: Session, menu: Dict[str, MenuItem]) -> None:
    ensure_dirs()
    title = "PEDIDO MODIFICADO" if sess.modified_flag else "PEDIDO"
    txt = render_comanda_text(sess, menu, title=title)

    # 1) “pincho” (histórico)
    if sess.order_id:
        fn = f"pedido_{sess.order_id:05d}.txt"
    else:
        fn = f"pedido_{int(now_ts())}.txt"
    path_hist = os.path.join(PINCHO_DIR, fn)
    open(path_hist, "w", encoding="utf-8").write(txt)

    # 2) comanda “para imprimir” (última)
    open(COMANDA_PRINT_PATH, "w", encoding="utf-8").write(txt)


# ----------------------------
# Mensajes
# ----------------------------
def menu_message(menu: Dict[str, MenuItem]) -> str:
    lines = []
    lines.append("👋 *Hola!* Soy *Marietta*")
    lines.append("🧾 *Menú*")
    for mi in menu.values():
        lines.append(f"🍽️ {mi.name} — ${mi.price}")
    lines.append("")
    lines.append(f"🚚 Envío ${DELIVERY_FEE} | ⏱️ {ETA_MIN} min")
    lines.append("👉 Mandame tu pedido con cantidades (ej: “2 milanesas y 1 sandwich”)")
    return "\n".join(lines)


def needs_menu(text: str) -> bool:
    t = norm(text)
    # saludos / consultas genéricas
    if len(t.split()) <= 3 and re.search(r"\b(hola|buenas|buen_dia|buenas_buenas|que_tal|estan|trabajando)\b", t):
        return True
    if re.search(r"\b(menu|que\ hay|que\ tenes|precio|precios)\b", t):
        return True
    return False


def handle_message(user_id: str, text: str, menu: Dict[str, MenuItem], matchers: Dict[str, List[str]]) -> str:
    sess = get_sess(user_id)
    reset_if_expired(sess)

    raw = text or ""
    t = norm(raw)

    # cancel confirm flow (global)
    if sess.awaiting_cancel_confirm:
        if is_yes(raw):
            sess.awaiting_cancel_confirm = False
            sess.state = "START"
            sess.items = {}
            sess.modifications = []
            sess.name = None
            sess.delivery_method = None
            sess.address = None
            sess.payment_method = None
            sess.order_id = None
            sess.modified_flag = False
            sess.last_confirmed_ts = None
            return "❌ Pedido cancelado."
        if is_no(raw) or is_no_thanks(raw):
            sess.awaiting_cancel_confirm = False
            sess.state = "POST_CONFIRMED_WAIT"
            return "👌 Perfecto. Tu pedido sigue en preparación."
        return "❌ ¿Querés cancelar el pedido? (SI / NO)"

    # Si pide menú
    if sess.state == "START" and needs_menu(raw):
        return menu_message(menu)

    # Si está esperando “post confirmado”
    if sess.state == "POST_CONFIRMED_WAIT":
        if is_no_thanks(raw):
            return "👌 Perfecto. Tu pedido está en preparación."

        if is_cancel(raw):
            sess.awaiting_cancel_confirm = True
            return "❌ ¿Querés cancelar el pedido? (SI / NO)"

        # Agregar comida (si detecta items)
        add_items = parse_items(raw, matchers)
        if add_items:
            if not sess.order_id:
                sess.order_id = bump_counter()
            for sku, qty in add_items.items():
                sess.items[sku] = sess.items.get(sku, 0) + qty
            sess.modified_flag = True
            sess.state = "ASK_CONFIRM_MOD"
            return (
                "📝 Perfecto, sumé al pedido. Te paso el resumen:\n"
                + order_summary_message(sess, menu)
                + "\n¿Confirmás el pedido modificado? (SI / NO)"
            )

        # Modificación libre (NO analizar)
        mod_text = clip_words(raw.strip(), 20)
        if not mod_text or is_no(raw):
            return "👌 Perfecto. Tu pedido está en preparación."
        sess.pending_mod_text = mod_text
        sess.state = "POST_MOD_CONFIRM"
        return f"🧾 Modificación:\n“{mod_text}”\n¿Confirmás? (SI / NO)"

    # Confirmar mod libre
    if sess.state == "POST_MOD_CONFIRM":
        if is_yes(raw):
            mod = sess.pending_mod_text or ""
            mod = clip_words(mod, 20)
            if mod:
                sess.modifications.append(mod)
                sess.modified_flag = True
                write_comandas(sess, menu)
            sess.pending_mod_text = None
            sess.state = "POST_CONFIRMED_WAIT"
            return "✅ Modificación aceptada. Tu pedido está en preparación."
        if is_no(raw) or is_no_thanks(raw):
            sess.pending_mod_text = None
            sess.state = "POST_CONFIRMED_WAIT"
            return "👌 Perfecto. Tu pedido sigue en preparación."
        return "¿Confirmás la modificación? (SI / NO)"

    # Confirmar “pedido modificado” (items agregados)
    if sess.state == "ASK_CONFIRM_MOD":
        if is_yes(raw):
            write_comandas(sess, menu)
            sess.last_confirmed_ts = now_ts()
            sess.state = "POST_CONFIRMED_WAIT"
            return "✅ Pedido modificado confirmado. Tu pedido está en preparación."
        if is_no(raw):
            # revert simple: no revertimos para mantener simple, solo pedimos que escriba el pedido como lo quiere
            sess.state = "POST_CONFIRMED_WAIT"
            return "👌 Perfecto. Tu pedido sigue en preparación."
        return "¿Confirmás el pedido modificado? (SI / NO)"

    # 1) Detectar pedido inicial (sin decir “no tengo” por palabras basura)
    if sess.state == "START":
        items = parse_items(raw, matchers)

        # bebidas pedidas y no hay bebidas en el menu
        if asked_for_beverage(raw) and not menu_has_beverages(menu):
            # si además hay comida detectada, seguimos
            if items:
                sess.items = items
                sess.order_id = bump_counter()
                sess.modified_flag = False
                # seguimos flujo normal
            else:
                # no hay comida detectada, mostrar menú
                return "🥤 Cocacola/gaseosa no tenemos para ofrecerte en estos momentos (no hay bebidas hoy).\n" + menu_message(menu)

        if not items:
            # si no detecta items, menú
            return menu_message(menu)

        sess.items = items
        sess.order_id = bump_counter()
        sess.modified_flag = False
        sess.state = "ASK_NAME"
        return "🧾 Perfecto. ¿A nombre de quién es el pedido?"

    # 2) Nombre
    if sess.state == "ASK_NAME":
        name = raw.strip()
        name = re.sub(r"^\s*a\s+nombre\s+de\s+", "", name, flags=re.IGNORECASE)
        name = clip_words(name, 5)
        sess.name = name if name else None
        sess.state = "ASK_DELIVERY"
        return "📦 ¿Envío o retirar?"

    # 3) Delivery
    if sess.state == "ASK_DELIVERY":
        dm = detect_delivery(raw)
        if not dm:
            return "📦 ¿Envío o retirar?"
        sess.delivery_method = dm
        if dm == "envio":
            sess.state = "ASK_ADDRESS"
            return "📍 Perfecto. Decime la dirección por favor."
        sess.state = "ASK_PAYMENT"
        return "💵 ¿Efectivo o transferencia?"

    # 4) Dirección
    if sess.state == "ASK_ADDRESS":
        addr = clip_words(raw.strip(), 12)
        sess.address = addr if addr else None
        sess.state = "ASK_PAYMENT"
        return "💵 ¿Efectivo o transferencia?"

    # 5) Pago
    if sess.state == "ASK_PAYMENT":
        pm = detect_payment(raw)
        if not pm:
            return "💵 ¿Efectivo o transferencia?"
        sess.payment_method = pm
        sess.state = "ASK_CONFIRM"
        return order_summary_message(sess, menu) + "\n¿Confirmás? (SI / NO)"

    # 6) Confirmación inicial
    if sess.state == "ASK_CONFIRM":
        if is_yes(raw):
            write_comandas(sess, menu)
            sess.last_confirmed_ts = now_ts()
            sess.state = "POST_CONFIRMED_WAIT"
            return (
                "✅ Pedido confirmado. ¡Gracias!\n"
                "¿Querés agregar algo más, o modificar algún ingrediente? Escribí lo que quieras y yo se lo paso al cocinero."
            )
        if is_no(raw):
            sess.state = "START"
            sess.items = {}
            sess.modifications = []
            sess.name = None
            sess.delivery_method = None
            sess.address = None
            sess.payment_method = None
            sess.order_id = None
            sess.modified_flag = False
            sess.last_confirmed_ts = None
            return "❌ Pedido cancelado."
        return "¿Confirmás? (SI / NO)"

    return menu_message(menu)


# ----------------------------
# Meta WhatsApp Cloud API
# ----------------------------
def meta_send_text(to_number: str, body: str) -> None:
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        return
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": body},
    }
    try:
        requests.post(url, headers=headers, json=payload, timeout=12)
    except Exception:
        pass


@app.get("/webhook")
def webhook_verify():
    mode = request.args.get("hub.mode", "")
    token = request.args.get("hub.verify_token", "")
    challenge = request.args.get("hub.challenge", "")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "forbidden", 403


@app.post("/webhook")
def webhook_receive():
    data = request.get_json(force=True) or {}
    # WhatsApp payload
    try:
        entry = (data.get("entry") or [])[0]
        changes = (entry.get("changes") or [])[0]
        value = changes.get("value") or {}
        messages = value.get("messages") or []
        if not messages:
            return jsonify({"ok": True})
        msg = messages[0]
        from_number = msg.get("from")
        text = (((msg.get("text") or {})).get("body")) or ""
    except Exception:
        return jsonify({"ok": True})

    menu = load_menu(os.path.join(DATA_DIR, "menu.txt"))
    syn = load_synonyms(os.path.join(DATA_DIR, "synonyms.txt"))
    matchers = build_matchers(menu, syn)

    reply = handle_message(from_number, text, menu, matchers)
    meta_send_text(from_number, reply)
    return jsonify({"ok": True})


# ----------------------------
# Test API (para test_app.py)
# ----------------------------
@app.post("/test_message")
def test_message():
    data = request.get_json(force=True) or {}
    uid = str(data.get("from", "test_user"))
    text = str(data.get("text", ""))

    menu = load_menu(os.path.join(DATA_DIR, "menu.txt"))
    syn = load_synonyms(os.path.join(DATA_DIR, "synonyms.txt"))
    matchers = build_matchers(menu, syn)

    reply = handle_message(uid, text, menu, matchers)
    return jsonify({"reply": reply})


if __name__ == "__main__":
    ensure_dirs()
    app.run(host="0.0.0.0", port=PORT, debug=True)


# ===== README.md =====
# Vendobot simplified

## Run (local test)
1) `pip install -r requirements.txt`
2) `python app.py`
3) In another terminal: `python test_app.py`

## Files
- `menu.txt` editable: `Nombre = $precio`
- `synonyms.txt` editable: `sku|alias1,alias2,...`
- `pincho_comandas/` guarda histórico
- `comanda.txt` siempre la última (para impresora térmica)

## WhatsApp (Meta Cloud API)
- Setear `.env` con `VERIFY_TOKEN`, `WHATSAPP_TOKEN`, `PHONE_NUMBER_ID`
- Endpoint webhook:
  - Verify: `GET /webhook`
  - Receive: `POST /webhook`

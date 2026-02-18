# Vendobot Retail

Vendobot Retail is a production-ready WhatsApp automation bot designed for small food businesses.

It handles full conversational order flows:
- Greeting & menu delivery
- Natural language order detection
- Quantity parsing
- Payment method detection
- Delivery / pickup flow
- Address capture
- Order confirmation
- Post-confirmation modifications
- Safe idle state while order is prepared

## Features
- State-based conversation engine
- Menu driven from editable text files
- Synonym-based item recognition
- Robust confirmation and modification flow
- Local or external AI compatible (OpenAI-style API)
- WhatsApp Cloud API ready
- Clean separation between code, config and runtime data

## Tech Stack
- Python
- Flask
- WhatsApp Cloud API
- Local LLM (tested with Qwen / LLaMA via OpenAI-compatible endpoint)

## Notes
Sensitive files (.env, orders, counters) are excluded from version control.

This project is part of my professional portfolio and represents a real-world automation system in active use.

Enjoy.

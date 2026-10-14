# Lounge Vending Machine

Project overview
----------------
This repository implements the backend for a student-lounge equipment kiosk that loans items (free of charge) such as billiard sticks, table tennis rackets, and pingpong balls. The system is designed for deployment on a Raspberry Pi 5 (4GB recommended) driving a 12V DC gear motor via a motor driver board and an attached microcontroller for LCD/actuator serial commands.

What this repo contains (consolidated)
--------------------------------------
- backend.py — Main backend implementation (hardware abstractions, SQLite persistence, student validation, inventory, dispense flow, return reminders and admin escalation).
- mailer.py — Project-local SMTP helper used by backend.py; reads SMTP configuration from environment variables so it can use your university relay.
- vending_machine.db — Consolidated SQLite database for students, inventory_items, and loans (this is the active DB used by default).
- backend.ipynb — Exploratory notebook / prototype (kept for reference; not required for deployment).
- README.md — This document (updated).


Quick start (development / simulation)
-------------------------------------
1. Create a virtual environment and install dependencies if you plan to run on a Pi or dev machine that has RPi libraries available (for local simulation no special libs are required unless you connect hardware).

2. Configure environment variables (important):
   - VENDING_DB_PATH (optional) — path to SQLite DB; defaults to `vending_machine.db`.
   - SMTP_HOST — mail server hostname (e.g. mail.university.edu)
   - SMTP_PORT — mail server port (e.g. 25, 587)
   - SMTP_USE_SSL — set to true for SMTPS (port 465); default false
   - SMTP_STARTTLS — set to true to upgrade to TLS on ports like 587
   - SMTP_USERNAME — optional (leave unset for unauthenticated campus relays)
   - SMTP_PASSWORD — optional
   - SENDER_EMAIL — optional from-address; otherwise mailer uses SMTP_USERNAME or no-reply
   - ADMIN_EMAIL — (optional) address to escalate unreturned items

3. Run a local debug SMTP server for development (optional):
   - python -m smtpd -c DebuggingServer -n localhost:1025
   - Then set SMTP_HOST=localhost and SMTP_PORT=1025 for the session to see email payloads printed to the console.

4. Run a smoke test locally (simulation mode):
   - python -c "import backend; m=backend.EquipmentVendingMachine(); print(m.inventory_manager.list_items())"
   - The code is safe to import on non-Pi hosts — GPIO and serial operations fall back to simulation if hardware is missing.

Deployment notes for Raspberry Pi 5
----------------------------------
- Hardware: the 12V DC gear motor MUST be driven through a motor driver (L298N, TB6612FNG, MOSFET + H-bridge). Do not connect a 12V motor directly to Pi GPIO pins.
- Connect the motor driver inputs to the BCM pin numbers defined in MotorConfig (defaults: in1=17, in2=27, enable=18) or override via HardwareProfile/gpio_pins.
- Attach the LCD/microcontroller to a serial port that matches one of the supported serial device nodes (e.g., /dev/serial0). The backend will send two command types over serial when present:
  - LCD:<text>\n — display text on LCD (truncated to a default length)
  - DISPENSE:<ITEM_CODE>:<QUANTITY>\n — notify the microcontroller a dispense has occurred
- Environment: set the SMTP_* variables for the university relay and ADMIN_EMAIL for escalations.
- Run as a managed service (systemd) so the kiosk restarts automatically.

Database and persistence
------------------------
- The default file is `vending_machine.db`. The schema contains tables: students, inventory_items, loans.
- Seed data includes example students and items (BILLIARD, RACKET, BALL). Adjust inventory via the InventoryManager or directly via SQLite for admin tasks.

Email and reminders
-------------------
- Email sending is handled by `mailer.py`. It supports unauthenticated campus relays (no SMTP_USERNAME) and authenticated SMTP (username/password).
- Return reminders and escalation are implemented in backend.py: loans have a 2-hour due_by by default; when a loan becomes overdue a reminder is sent to the student, and if not returned by end-of-day an admin email is sent.

Safety and operational safeguards
--------------------------------
- SecurityGuard prevents invalid IDs, bad item codes, unreasonable quantities, and blocks operations while machine status is 'maintenance' or 'failed'.
- Motor actions are wrapped in MotorController and guarded so importing the module on development machines won't raise import errors.
- Use sensors or encoder feedback for jam detection in a production deployment — current implementation uses timed motor activation only.

Administrative tasks and next steps
----------------------------------
- Add an admin CLI or small web UI to manage students, restock inventory, and view loan logs.
- Add email retry/backoff and a queued delivery system so transient SMTP errors don't block kiosk operations.
- Calibrate dispense timings and add sensor-based confirmation for reliable dispensing.
- Create a systemd unit file for production deployment on the Pi.

Updated on: 2026-08-18T18:18:22+04:00

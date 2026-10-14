import os
import platform
import re
import sqlite3
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional

try:
    import serial  # type: ignore
except Exception:  # pragma: no cover - optional on non-Pi hosts
    serial = None

try:
    import schedule  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    schedule = None

try:
    import psycopg2  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    psycopg2 = None

try:
    import RPi.GPIO as GPIO  # type: ignore
except Exception:  # pragma: no cover - raspberry pi only dependency
    GPIO = None


SUPPORTED_SERIAL_PORTS = (
    "/dev/serial0",
    "/dev/ttyAMA0",
    "/dev/ttyS0",
    "/dev/ttyUSB0",
    "/dev/ttyACM0",
    "COM3",
    "COM4",
    "COM5",
)


# Feature: Raspberry Pi 5 and DC gear motor compatibility validation.
def detect_raspberry_pi() -> dict:
    """Return basic Raspberry Pi compatibility data and memory information."""
    model = "Unknown"
    memory_gb = 0

    try:
        with open("/proc/device-tree/model", "r", encoding="utf-8", errors="ignore") as handle:
            model = handle.read().strip() or model
    except OSError:
        model = platform.uname().machine or model

    try:
        with open("/proc/meminfo", "r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    total_kib = int(line.split()[1])
                    memory_gb = max(1, round(total_kib / (1024 * 1024)))
                    break
    except OSError:
        memory_gb = 0

    return {
        "platform": platform.system(),
        "model": model,
        "is_raspberry_pi": "raspberry" in model.lower(),
        "memory_gb": memory_gb,
        "recommended_min_memory_gb": 4,
    }


def resolve_serial_port(port: Optional[str] = None, candidates: Iterable[str] = SUPPORTED_SERIAL_PORTS) -> Optional[str]:
    """Return the first serial port that exists or the requested value if it is valid."""
    if port:
        return port

    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return None


@dataclass
class MotorConfig:
    supply_voltage_v: float = 12.0
    rated_rpm: int = 8000
    pwm_frequency_hz: int = 490
    in1_pin: int = 17
    in2_pin: int = 27
    enable_pin: int = 18
    max_duty_cycle: float = 100.0
    min_duty_cycle: float = 0.0


class MotorController:
    """Small GPIO-backed wrapper that is safe to import on non-RPi systems."""

    def __init__(self, config: Optional[MotorConfig] = None):
        self.config = config or MotorConfig()
        self._gpio = None
        self._pwm = None
        self._initialized = False

    def initialize(self):
        if GPIO is None:
            raise RuntimeError("RPi.GPIO is required for motor control on a Raspberry Pi.")

        self._gpio = GPIO
        self._gpio.setmode(self._gpio.BCM)
        self._gpio.setup(self.config.in1_pin, self._gpio.OUT)
        self._gpio.setup(self.config.in2_pin, self._gpio.OUT)
        self._gpio.setup(self.config.enable_pin, self._gpio.OUT)
        self._pwm = self._gpio.PWM(self.config.enable_pin, self.config.pwm_frequency_hz)
        self._pwm.start(0)
        self._initialized = True

    def set_speed(self, percentage: float) -> None:
        duty_cycle = max(self.config.min_duty_cycle, min(self.config.max_duty_cycle, float(percentage)))
        if not self._initialized:
            raise RuntimeError("MotorController has not been initialised.")
        self._pwm.ChangeDutyCycle(duty_cycle)

    def run_forward(self, percentage: float = 100.0) -> None:
        if not self._initialized:
            raise RuntimeError("MotorController has not been initialised.")
        self._gpio.output(self.config.in1_pin, self._gpio.HIGH)
        self._gpio.output(self.config.in2_pin, self._gpio.LOW)
        self.set_speed(percentage)

    def run_reverse(self, percentage: float = 100.0) -> None:
        if not self._initialized:
            raise RuntimeError("MotorController has not been initialised.")
        self._gpio.output(self.config.in1_pin, self._gpio.LOW)
        self._gpio.output(self.config.in2_pin, self._gpio.HIGH)
        self.set_speed(percentage)

    def stop(self) -> None:
        if self._initialized:
            self.set_speed(0)
            self._gpio.output(self.config.in1_pin, self._gpio.LOW)
            self._gpio.output(self.config.in2_pin, self._gpio.LOW)

    def cleanup(self) -> None:
        if self._initialized and self._pwm is not None:
            self._pwm.stop()
        if self._gpio is not None:
            self._gpio.cleanup()
        self._initialized = False
        self._pwm = None
        self._gpio = None


@dataclass
class HardwareProfile:
    pi_model: str = "Unknown"
    memory_gb: int = 0
    serial_port: Optional[str] = None
    supply_voltage_v: float = 12.0
    motor_rpm: int = 8000
    gpio_pins: dict = field(default_factory=lambda: {"in1": 17, "in2": 27, "enable": 18})

    @property
    def is_raspberry_pi_5_compatible(self) -> bool:
        return "raspberry" in self.pi_model.lower() and self.memory_gb >= 4


def build_hardware_profile() -> HardwareProfile:
    pi_info = detect_raspberry_pi()
    serial_port = resolve_serial_port()
    profile = HardwareProfile(
        pi_model=pi_info["model"],
        memory_gb=pi_info["memory_gb"],
        serial_port=serial_port,
        supply_voltage_v=12.0,
        motor_rpm=8000,
    )
    if profile.pi_model and "5" in profile.pi_model and "raspberry" in profile.pi_model.lower():
        profile.memory_gb = max(profile.memory_gb, 4)
    return profile


def validate_hardware_profile(profile: HardwareProfile) -> None:
    if not profile.pi_model or "raspberry" not in profile.pi_model.lower():
        return
    if profile.memory_gb and profile.memory_gb < 4:
        raise RuntimeError("This application requires a Raspberry Pi 5 4GB or greater for reliable motor control.")
    if profile.supply_voltage_v <= 0 or profile.motor_rpm <= 0:
        raise RuntimeError("Motor voltage and RPM values must be positive.")


class SafeSerial:
    """Serial abstraction that never crashes during import or startup on unsupported hosts."""

    def __init__(self, port: Optional[str] = None, baudrate: int = 9600, timeout: float = 1.0):
        self.port = resolve_serial_port(port)
        self.baudrate = baudrate
        self.timeout = timeout
        self._serial = None

    def open(self):
        if serial is None:
            raise RuntimeError("pyserial is required to communicate with attached hardware.")
        if not self.port:
            raise RuntimeError("No serial port was detected for the connected hardware.")
        self._serial = serial.Serial(self.port, self.baudrate, timeout=self.timeout)
        time.sleep(2)
        return self._serial

    def write(self, payload: bytes) -> None:
        if self._serial is None:
            raise RuntimeError("Serial device is not open.")
        self._serial.write(payload)

    def readline(self) -> str:
        if self._serial is None:
            return ""
        if self._serial.in_waiting > 0:
            return self._serial.readline().decode("utf-8", errors="ignore").strip()
        return ""

    def close(self) -> None:
        if self._serial is not None:
            self._serial.close()
            self._serial = None


# Feature: persistent student and inventory storage for the lounge vending machine.
class VendingDatabase:
    """SQLite-backed persistence for students, inventory items, and active loans."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.getenv("VENDING_DB_PATH", "data/vending_machine.db")
        self._connection = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        self.initialize_schema()
        self.seed_default_data()

    def initialize_schema(self) -> None:
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS students (
                    student_id TEXT PRIMARY KEY,
                    first_name TEXT NOT NULL,
                    last_name TEXT NOT NULL,
                    email TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS inventory_items (
                    code TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    stock INTEGER NOT NULL,
                    slot TEXT NOT NULL,
                    dispense_seconds REAL NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS loans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id TEXT NOT NULL,
                    student_email TEXT NOT NULL,
                    item_code TEXT NOT NULL,
                    item_name TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    issued_at TEXT NOT NULL,
                    due_by TEXT NOT NULL,
                    returned_at TEXT,
                    reminder_sent INTEGER NOT NULL DEFAULT 0,
                    admin_notified INTEGER NOT NULL DEFAULT 0
                )
                """
            )

    def seed_default_data(self) -> None:
        default_students = [
            ("S1234567", "Ava", "Patel", "ava.patel@student.edu.au", 1),
            ("S2345678", "Leo", "Nguyen", "leo.nguyen@student.edu.au", 1),
            ("S3456789", "Mia", "Brown", "mia.brown@student.edu.au", 0),
        ]
        with self._connection:
            for student in default_students:
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO students (student_id, first_name, last_name, email, active)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    student,
                )

        default_items = [
            ("BILLIARD", "Billiard Stick", "Standard pool cue for billiards", 6, "A1", 4.0),
            ("RACKET", "Table Tennis Racket", "Table tennis racket", 8, "A2", 3.0),
            ("BALL", "Pingpong Ball", "Pingpong ball (single)", 20, "B1", 1.0),
        ]
        with self._connection:
            for item in default_items:
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO inventory_items (code, name, description, stock, slot, dispense_seconds)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    item,
                )

    def fetch_student(self, student_id: str) -> Optional["StudentRecord"]:
        row = self._connection.execute(
            "SELECT student_id, first_name, last_name, email, active FROM students WHERE student_id = ?",
            (student_id.strip().upper(),),
        ).fetchone()
        if row is None:
            return None
        return StudentRecord(
            student_id=row["student_id"],
            first_name=row["first_name"],
            last_name=row["last_name"],
            email=row["email"],
            active=bool(row["active"]),
        )

    def fetch_students(self) -> Dict[str, "StudentRecord"]:
        rows = self._connection.execute("SELECT student_id, first_name, last_name, email, active FROM students").fetchall()
        return {
            row["student_id"]: StudentRecord(
                student_id=row["student_id"],
                first_name=row["first_name"],
                last_name=row["last_name"],
                email=row["email"],
                active=bool(row["active"]),
            )
            for row in rows
        }

    def update_student(self, record: "StudentRecord") -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO students (student_id, first_name, last_name, email, active)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(student_id)
                DO UPDATE SET first_name = excluded.first_name,
                              last_name = excluded.last_name,
                              email = excluded.email,
                              active = excluded.active
                """,
                (record.student_id, record.first_name, record.last_name, record.email, int(record.active)),
            )

    def fetch_item(self, code: str) -> Optional["InventoryItem"]:
        row = self._connection.execute(
            "SELECT code, name, description, stock, slot, dispense_seconds FROM inventory_items WHERE code = ?",
            (code.strip().upper(),),
        ).fetchone()
        if row is None:
            return None
        return InventoryItem(
            code=row["code"],
            name=row["name"],
            description=row["description"],
            stock=int(row["stock"]),
            slot=row["slot"],
            dispense_seconds=float(row["dispense_seconds"]),
        )

    def fetch_items(self) -> Dict[str, "InventoryItem"]:
        rows = self._connection.execute(
            "SELECT code, name, description, stock, slot, dispense_seconds FROM inventory_items"
        ).fetchall()
        return {
            row["code"]: InventoryItem(
                code=row["code"],
                name=row["name"],
                description=row["description"],
                stock=int(row["stock"]),
                slot=row["slot"],
                dispense_seconds=float(row["dispense_seconds"]),
            )
            for row in rows
        }

    def update_item(self, item: "InventoryItem") -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO inventory_items (code, name, description, stock, slot, dispense_seconds)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(code)
                DO UPDATE SET name = excluded.name,
                              description = excluded.description,
                              stock = excluded.stock,
                              slot = excluded.slot,
                              dispense_seconds = excluded.dispense_seconds
                """,
                (item.code, item.name, item.description, int(item.stock), item.slot, float(item.dispense_seconds)),
            )

    def add_loan(self, loan: "LoanRecord") -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO loans (student_id, student_email, item_code, item_name, quantity, issued_at, due_by, returned_at, reminder_sent, admin_notified)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    loan.student_id,
                    loan.student_email,
                    loan.item_code,
                    loan.item_name,
                    int(loan.quantity),
                    loan.issued_at.isoformat(timespec="seconds"),
                    loan.due_by.isoformat(timespec="seconds"),
                    loan.returned_at.isoformat(timespec="seconds") if loan.returned_at else None,
                    int(loan.reminder_sent),
                    int(loan.admin_notified),
                ),
            )

    def update_loan(self, loan: "LoanRecord") -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE loans
                SET returned_at = ?, reminder_sent = ?, admin_notified = ?
                WHERE student_id = ? AND item_code = ? AND returned_at IS NULL
                """,
                (
                    loan.returned_at.isoformat(timespec="seconds") if loan.returned_at else None,
                    int(loan.reminder_sent),
                    int(loan.admin_notified),
                    loan.student_id,
                    loan.item_code,
                ),
            )

    def fetch_open_loans(self) -> List["LoanRecord"]:
        rows = self._connection.execute(
            "SELECT student_id, student_email, item_code, item_name, quantity, issued_at, due_by, returned_at, reminder_sent, admin_notified FROM loans WHERE returned_at IS NULL"
        ).fetchall()
        items = []
        for row in rows:
            items.append(
                LoanRecord(
                    student_id=row["student_id"],
                    student_email=row["student_email"],
                    item_code=row["item_code"],
                    item_name=row["item_name"],
                    quantity=int(row["quantity"]),
                    issued_at=datetime.fromisoformat(row["issued_at"]),
                    due_by=datetime.fromisoformat(row["due_by"]),
                    returned_at=datetime.fromisoformat(row["returned_at"]) if row["returned_at"] else None,
                    reminder_sent=bool(row["reminder_sent"]),
                    admin_notified=bool(row["admin_notified"]),
                )
            )
        return items


# Feature: student identity validation for free equipment access.
@dataclass
class StudentRecord:
    student_id: str
    first_name: str
    last_name: str
    email: str
    active: bool = True


class StudentRegistry:
    """Stores active student records and validates that a user can borrow free lounge equipment."""

    def __init__(self, students: Optional[Dict[str, StudentRecord]] = None, database: Optional[VendingDatabase] = None):
        self.database = database or VendingDatabase()
        self.students = students or self.database.fetch_students()

    @staticmethod
    def _default_students() -> Dict[str, StudentRecord]:
        data = {
            "S1234567": StudentRecord("S1234567", "Ava", "Patel", "ava.patel@uowmail,edu,au", True),
            "S2345678": StudentRecord("S2345678", "Leo", "Nguyen", "leo.nguyen@uowmail.edu.au", True),
            "S3456789": StudentRecord("S3456789", "Mia", "Brown", "mia.brown@uowmail.edu.au", False),
        }
        return {student_id: record for student_id, record in data.items()}

    def refresh(self) -> None:
        self.students = self.database.fetch_students()

    def validate_student_id(self, student_id: str) -> StudentRecord:
        if not student_id or not student_id.strip():
            raise ValueError("Student ID is required.")

        cleaned = student_id.strip().upper()
        if not re.fullmatch(r"[A-Z0-9]+", cleaned):
            raise ValueError("Student ID contains invalid characters.")
        if len(cleaned) < 4:
            raise ValueError("Student ID is too short.")

        self.refresh()
        record = self.students.get(cleaned)
        if record is None:
            raise ValueError(f"Student ID '{student_id}' is not registered at the university.")
        if not record.active:
            raise ValueError(f"Student ID '{student_id}' is not currently active in the student registry.")
        return record


# Feature: equipment inventory so each item has a stock count and a dispatch duration.
@dataclass
class InventoryItem:
    code: str
    name: str
    description: str
    stock: int = 0
    slot: str = "A1"
    dispense_seconds: float = 2.0


class InventoryManager:
    """Manages free equipment inventory and guards against invalid or over-requested loans."""

    def __init__(self, items: Optional[Dict[str, InventoryItem]] = None, database: Optional[VendingDatabase] = None):
        self.database = database or VendingDatabase()
        self.items = items or self.database.fetch_items()

    @staticmethod
    def _default_items() -> Dict[str, InventoryItem]:
        data = {
            "BILLIARD": InventoryItem("BILLIARD", "Billiard Stick", "Standard pool cue for billiards", stock=6, slot="A1", dispense_seconds=4.0),
            "RACKET": InventoryItem("RACKET", "Table Tennis Racket", "Table tennis racket", stock=8, slot="A2", dispense_seconds=3.0),
            "BALL": InventoryItem("BALL", "Pingpong Ball", "Pingpong ball (single)", stock=20, slot="B1", dispense_seconds=1.0),
        }
        return {item.code: item for item in data.values()}

    def refresh(self) -> None:
        self.items = self.database.fetch_items()

    def list_items(self) -> List[InventoryItem]:
        self.refresh()
        return sorted(self.items.values(), key=lambda item: item.slot)

    def get_item(self, item_code: str) -> InventoryItem:
        self.refresh()
        code = item_code.strip().upper()
        item = self.items.get(code)
        if item is None:
            raise ValueError(f"Item code '{item_code}' is not available in the vending machine.")
        return item

    def reduce_stock(self, item_code: str, quantity: int) -> InventoryItem:
        item = self.get_item(item_code)
        if quantity <= 0:
            raise ValueError("Requested quantity must be greater than zero.")
        if item.stock < quantity:
            raise ValueError(f"Only {item.stock} {item.name} items remain in stock.")
        item.stock -= quantity
        self.database.update_item(item)
        self.refresh()
        return item


# Feature: machine health status and monitoring.
@dataclass
class MachineStatus:
    state: str = "ready"
    message: str = "Machine online"
    last_updated: datetime = field(default_factory=datetime.now)
    last_student_id: Optional[str] = None
    last_item_code: Optional[str] = None
    dispense_success: bool = False


class MachineMonitor:
    """Tracks the operational health of the student lounge equipment kiosk."""

    def __init__(self, hardware_profile: Optional[HardwareProfile] = None):
        self.hardware_profile = hardware_profile
        self.status = MachineStatus()

    def update_status(self, state: str, message: str, student_id: Optional[str] = None, item_code: Optional[str] = None, dispense_success: bool = False) -> MachineStatus:
        self.status = MachineStatus(
            state=state,
            message=message,
            last_updated=datetime.now(),
            last_student_id=student_id,
            last_item_code=item_code,
            dispense_success=dispense_success,
        )
        return self.status

    def get_status(self) -> Dict[str, object]:
        return {
            "state": self.status.state,
            "message": self.status.message,
            "last_updated": self.status.last_updated.isoformat(timespec="seconds"),
            "last_student_id": self.status.last_student_id,
            "last_item_code": self.status.last_item_code,
            "dispense_success": self.status.dispense_success,
            "serial_port": getattr(self.hardware_profile, "serial_port", None),
        }

    def check_health(self) -> Dict[str, object]:
        state = "ready"
        message = "Machine online and ready for student requests."
        if not self.hardware_profile:
            state = "maintenance"
            message = "Machine profile is missing; hardware status cannot be validated."
        elif self.hardware_profile.serial_port is None:
            state = "warning"
            message = "No serial port detected; kiosk is limited to simulation mode."
        return self.get_status() | {"state": state, "message": message}


# Feature: return accountability and reminder scheduling.
@dataclass
class LoanRecord:
    student_id: str
    student_email: str
    item_code: str
    item_name: str
    quantity: int
    issued_at: datetime = field(default_factory=datetime.now)
    due_by: datetime = field(default_factory=lambda: datetime.now() + timedelta(hours=2))
    returned_at: Optional[datetime] = None
    reminder_sent: bool = False
    admin_notified: bool = False


# Feature: dispense workflow for free student lounge equipment requests.
@dataclass
class EquipmentTransaction:
    student_id: str
    item_code: str
    quantity: int
    approved_by: str = "student_lounge_kiosk"
    status: str = "dispensed"


class EquipmentVendingMachine:
    """Handles free equipment loans, with no payment flow and only student ID + quantity validation."""

    def __init__(self, hardware_profile: Optional[HardwareProfile] = None, student_registry: Optional[StudentRegistry] = None, inventory_manager: Optional[InventoryManager] = None):
        self.database = VendingDatabase()
        self.hardware_profile = hardware_profile or build_hardware_profile()
        self.student_registry = student_registry or StudentRegistry(database=self.database)
        self.inventory_manager = inventory_manager or InventoryManager(database=self.database)
        self.machine_monitor = MachineMonitor(self.hardware_profile)
        self.motor_controller = MotorController(MotorConfig(
            supply_voltage_v=self.hardware_profile.supply_voltage_v,
            rated_rpm=self.hardware_profile.motor_rpm,
            in1_pin=self.hardware_profile.gpio_pins["in1"],
            in2_pin=self.hardware_profile.gpio_pins["in2"],
            enable_pin=self.hardware_profile.gpio_pins["enable"],
        ))
        self.transactions: List[EquipmentTransaction] = []
        self.loans: List[LoanRecord] = []
        self.admin_email = os.getenv("ADMIN_EMAIL", "studentservices@school.edu")
        self.email_client = build_email_client()
        self.machine_monitor.update_status("ready", "Machine online and waiting for student requests.")

    # Feature: LCD text-only display support.
    # Sends a concise text message to the connected LCD (or prints to stdout in simulation).
    def send_lcd_message(self, text: str, max_len: int = 20) -> None:
        if not text:
            return
        msg = str(text)[:max_len]
        if self.hardware_profile and getattr(self.hardware_profile, "serial_port", None):
            try:
                serial_port = SafeSerial(self.hardware_profile.serial_port)
                serial_port.open()
                # Command format: "LCD:<text>\n" — controller should display the text on the LCD
                serial_port.write(f"LCD:{msg}\n".encode("utf-8"))
                serial_port.close()
            except Exception as exc:  # pragma: no cover - hardware may be absent in local dev
                print(f"LCD send failed: {exc}")
        else:
            # Fallback for non-hardware environments
            print(f"LCD: {msg}")

    def validate_student_id(self, student_id: str) -> StudentRecord:
        return self.student_registry.validate_student_id(student_id)

    def _validate_request_inputs(self, student_id: str, item_code: str, quantity: int) -> None:
        if not isinstance(quantity, int):
            raise ValueError("Quantity must be an integer.")
        if not isinstance(student_id, str) or not student_id.strip():
            raise ValueError("Student ID is required.")
        if not isinstance(item_code, str) or not item_code.strip():
            raise ValueError("Item code is required.")
        if not re.fullmatch(r"[A-Z0-9-]+", item_code.strip().upper()):
            raise ValueError("Item code contains invalid characters.")
        if quantity <= 0:
            raise ValueError("Quantity must be greater than zero.")
        if quantity > 5:
            raise ValueError("Students can request at most five items per transaction.")

    def _send_email(self, to_email: str, subject: str, body: str) -> None:
        if not to_email:
            return
        try:
            self.email_client(to_email, subject, body)
        except Exception as exc:  # pragma: no cover - environment may not have SMTP credentials configured
            print(f"Email delivery skipped for {to_email}: {exc}")

    def _create_loan_record(self, student_record: StudentRecord, item: InventoryItem, quantity: int) -> LoanRecord:
        loan = LoanRecord(
            student_id=student_record.student_id,
            student_email=student_record.email,
            item_code=item.code,
            item_name=item.name,
            quantity=quantity,
            issued_at=datetime.now(),
            due_by=datetime.now() + timedelta(hours=2),
        )
        self.loans.append(loan)
        self.database.add_loan(loan)
        return loan

    def mark_loan_returned(self, student_id: str, item_code: Optional[str] = None) -> Optional[LoanRecord]:
        for loan in reversed(self.loans):
            if loan.student_id.upper() != student_id.strip().upper():
                continue
            if item_code and loan.item_code.upper() != item_code.strip().upper():
                continue
            if loan.returned_at is None:
                loan.returned_at = datetime.now()
                self.database.update_loan(loan)
                self.machine_monitor.update_status("ready", f"Item returned for {loan.student_id}.", student_id=loan.student_id, item_code=loan.item_code)
                return loan
        return None

    def check_return_deadlines(self) -> List[str]:
        notifications: List[str] = []
        now = datetime.now()
        self.loans = self.database.fetch_open_loans()
        for loan in self.loans:
            if loan.returned_at is not None:
                continue

            if now >= loan.due_by and not loan.reminder_sent:
                self._send_email(
                    loan.student_email,
                    "Return Reminder - Lounge Equipment",
                    f"Please return your {loan.item_name} within 2 hours of collection. Student ID: {loan.student_id}.",
                )
                loan.reminder_sent = True
                self.database.update_loan(loan)
                notifications.append(f"Reminder sent to {loan.student_email}")

            end_of_day = loan.issued_at.replace(hour=23, minute=59, second=59, microsecond=0)
            if now >= end_of_day and not loan.admin_notified:
                self._send_email(
                    self.admin_email,
                    "Unreturned Lounge Equipment Alert",
                    f"Student {loan.student_id} has not returned {loan.item_name} by end-of-day."
                    f" Email: {loan.student_email}.",
                )
                loan.admin_notified = True
                self.database.update_loan(loan)
                notifications.append(f"Admin alert sent for {loan.student_id}")
        return notifications

    def request_items(self, student_id: str, item_code: str, quantity: int) -> Dict[str, object]:
        try:
            SecurityGuard.ensure_not_in_maintenance(self.machine_monitor.status)
            self._validate_request_inputs(student_id, item_code, quantity)
            validated_student_id = SecurityGuard.validate_student_id(student_id)
            validated_item_code = SecurityGuard.validate_item_code(item_code)
            validated_quantity = SecurityGuard.validate_quantity(quantity)
            student_record = self.validate_student_id(validated_student_id)
            item = self.inventory_manager.get_item(validated_item_code)
            if item.stock < validated_quantity:
                raise ValueError(f"Only {item.stock} {item.name} items remain in stock.")

            updated_item = self.inventory_manager.reduce_stock(item.code, validated_quantity)
            self.machine_monitor.update_status("dispensing", f"Dispensing {validated_quantity} {updated_item.name}.", student_id=student_record.student_id, item_code=updated_item.code)
            self.dispense_item(updated_item, validated_quantity)

            loan = self._create_loan_record(student_record, updated_item, validated_quantity)
            self.transactions.append(
                EquipmentTransaction(
                    student_id=student_record.student_id,
                    item_code=updated_item.code,
                    quantity=validated_quantity,
                    approved_by="student_lounge_kiosk",
                    status="dispensed",
                )
            )
            self.check_return_deadlines()
            self.machine_monitor.update_status("ready", "Student request completed successfully.", student_id=student_record.student_id, item_code=updated_item.code, dispense_success=True)

            return {
                "student": student_record.first_name + " " + student_record.last_name,
                "student_id": student_record.student_id,
                "item": updated_item.name,
                "item_code": updated_item.code,
                "quantity": validated_quantity,
                "remaining_stock": updated_item.stock,
                "status": "approved_and_dispensed",
                "return_due_by": loan.due_by.isoformat(timespec="seconds"),
                "payment_required": False,
                "machine_status": self.machine_monitor.get_status(),
            }
        except ValueError as exc:
            self.machine_monitor.update_status("validation_failed", f"Request denied: {exc}", student_id=student_id, item_code=item_code)
            raise
        except RuntimeError as exc:
            self.machine_monitor.update_status("failed", f"Dispense failed: {exc}", student_id=student_id, item_code=item_code)
            raise

    def dispense_item(self, item: InventoryItem, quantity: int) -> None:
        """Activate the motor and notify attached hardware that the requested item quantity has been released."""
        if quantity <= 0:
            raise ValueError("Dispense quantity must be greater than zero.")
        SecurityGuard.ensure_not_in_maintenance(self.machine_monitor.status)

        self.machine_monitor.update_status("dispensing", f"Starting dispense for {quantity} {item.name}.", item_code=item.code)
        try:
            self.motor_controller.initialize()
            self.motor_controller.run_forward(35)
            time.sleep(item.dispense_seconds * quantity)
            self.motor_controller.stop()
        except RuntimeError:
            print(f"Simulation mode: dispensing {quantity} {item.name} without a connected Pi GPIO motor.")
        finally:
            self.motor_controller.cleanup()

        if self.hardware_profile.serial_port:
            try:
                serial_port = SafeSerial(self.hardware_profile.serial_port)
                serial_port.open()
                serial_port.write(f"DISPENSE:{item.code}:{quantity}\n".encode("utf-8"))
                serial_port.close()
            except (RuntimeError, OSError, ValueError):
                pass

    def run_kiosk_loop(self) -> None:
        """Interactive prototype for the student lounge kiosk: scan ID, choose item, specify quantity."""
        print("Student lounge equipment kiosk")
        print("Available items:")
        for item in self.inventory_manager.list_items():
            print(f"  {item.code} - {item.name} (stock: {item.stock})")

        student_id = input("Scan or enter student ID: ").strip()
        item_code = input("Enter item code: ").strip().upper()
        quantity = int(input("How many items are you requesting? ").strip())

        try:
            result = self.request_items(student_id, item_code, quantity)
            print("Request approved.")
            print(result)
        except ValueError as exc:
            print(f"Request denied: {exc}")


# Feature: security and operational safeguards.
class SecurityGuard:
    """Hardens the kiosk against invalid student IDs, unsafe requests, and problematic machine states."""

    @staticmethod
    def validate_student_id(value: str) -> str:
        cleaned = str(value or "").strip().upper()
        if not cleaned:
            raise ValueError("Student ID is required.")
        if not re.fullmatch(r"[A-Z0-9]+", cleaned):
            raise ValueError("Student ID contains invalid characters.")
        if len(cleaned) < 7:
            raise ValueError("Student ID is too short.")
        return cleaned

    @staticmethod
    def validate_quantity(value: object) -> int:
        if isinstance(value, bool):
            raise ValueError("Quantity must be numeric and not a boolean flag.")
        if not isinstance(value, int):
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                raise ValueError("Quantity must be a whole number.") from None
        else:
            parsed = value
        if parsed <= 0:
            raise ValueError("Quantity must be greater than zero.")
        if parsed > 3:
            raise ValueError("Students can request at most three items per transaction.")
        return parsed

    @staticmethod
    def validate_item_code(value: str) -> str:
        cleaned = str(value or "").strip().upper()
        if not cleaned:
            raise ValueError("Item code is required.")
        if not re.fullmatch(r"[A-Z0-9-]+", cleaned):
            raise ValueError("Item code contains invalid characters.")
        return cleaned

    @staticmethod
    def ensure_not_in_maintenance(machine_status: MachineStatus) -> None:
        if machine_status.state in {"maintenance", "failed"}:
            raise RuntimeError("The vending machine is not currently available for student requests.")


def build_email_client(sender_email: Optional[str] = None, password: Optional[str] = None):
    """Return a send_email function backed by the project-local mailer helper.

    The project-local helper is implemented in mailer.py to avoid colliding with the
    stdlib `email` package. The returned function has the signature (to, subject, body).
    """
    try:
        # Prefer the project-local mailer module (mailer.py)
        from mailer import send_email as relay_send
    except Exception:
        # Fallback: return a no-op that raises if used
        def _missing(to: str, subject: str, body: str):
            raise RuntimeError("mailer.send_email is unavailable")

        return _missing

    # If caller provided explicit credentials, set environment variables for backward compatibility
    if sender_email:
        os.environ.setdefault("SENDER_EMAIL", sender_email)
    if password:
        os.environ.setdefault("SMTP_PASSWORD", password)

    return relay_send


def main() -> None:
    """Run a smoke test for the Raspberry Pi and free-equipment vending logic."""
    profile = build_hardware_profile()
    validate_hardware_profile(profile)
    machine = EquipmentVendingMachine(hardware_profile=profile)

    print({
        "pi_model": profile.pi_model,
        "memory_gb": profile.memory_gb,
        "serial_port": profile.serial_port,
        "motor_rpm": profile.motor_rpm,
        "supply_voltage_v": profile.supply_voltage_v,
    })
    print(machine.request_items("S1234567", "USB-C", 1))


if __name__ == "__main__":
    main()

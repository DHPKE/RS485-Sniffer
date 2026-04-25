#!/usr/bin/env python3
"""
RS485 Bus Sniffer
-----------------
Connects to a Waveshare USB-to-RS485 adapter (FT232RNL based) and captures
all bus traffic, grouping bytes into frames by inter-frame silence gaps.

Features:
  - Auto-detects FTDI / Waveshare adapter
  - Configurable baud rate, parity, stop bits, frame-gap timeout
  - Modbus RTU CRC validation on every frame
  - Colour-coded terminal output with hex + ASCII dump
  - Every frame flushed to a timestamped log file immediately (crash-safe)
  - MARKER system: press Enter while running to insert a named bookmark
    in both the terminal and the log file for easy navigation later

Requirements:  pip install pyserial
Usage:         python rs485_sniffer.py
"""

import serial
import serial.tools.list_ports
import sys
import os
import time
import threading
import argparse
from datetime import datetime

# ── ANSI colours ─────────────────────────────────────────────────────────────
RESET   = "\033[0m"
BOLD    = "\033[1m"
RED     = "\033[91m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
CYAN    = "\033[96m"
GREY    = "\033[90m"
MAGENTA = "\033[95m"
BLUE    = "\033[94m"

def c(text, colour): return f"{colour}{text}{RESET}"

def strip_ansi(text: str) -> str:
    for code in (RESET, BOLD, RED, GREEN, YELLOW, CYAN, GREY, MAGENTA, BLUE):
        text = text.replace(code, "")
    return text

# ── Modbus RTU CRC-16 ─────────────────────────────────────────────────────────
def modbus_crc(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc

def check_modbus_crc(frame: bytes) -> tuple[bool, str]:
    if len(frame) < 4:
        return False, "too short for CRC"
    payload      = frame[:-2]
    received_crc = frame[-2] | (frame[-1] << 8)
    expected_crc = modbus_crc(payload)
    if received_crc == expected_crc:
        return True, f"CRC OK (0x{expected_crc:04X})"
    return False, f"CRC FAIL (got 0x{received_crc:04X}, expected 0x{expected_crc:04X})"

# ── Modbus function code names ────────────────────────────────────────────────
FC_NAMES = {
    0x01: "Read Coils",
    0x02: "Read Discrete Inputs",
    0x03: "Read Holding Registers",
    0x04: "Read Input Registers",
    0x05: "Write Single Coil",
    0x06: "Write Single Register",
    0x0F: "Write Multiple Coils",
    0x10: "Write Multiple Registers",
    0x11: "Report Server ID",
    0x17: "Read/Write Multiple Registers",
}

def describe_modbus(frame: bytes) -> str:
    if len(frame) < 4:
        return ""
    addr = frame[0]
    fc   = frame[1]
    name = FC_NAMES.get(fc & 0x7F, "Unknown FC")
    exc  = " [EXCEPTION]" if fc & 0x80 else ""
    return f"Addr={addr}  FC=0x{fc:02X} ({name}){exc}"

# ── Hex + ASCII dump ──────────────────────────────────────────────────────────
def hex_dump(data: bytes, indent: str = "    ") -> list[str]:
    lines = []
    for i in range(0, len(data), 16):
        chunk    = data[i:i+16]
        hex_part = " ".join(f"{b:02X}" for b in chunk)
        asc_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{indent}{c(f'{i:04X}', GREY)}  {hex_part:<47}  {c(asc_part, CYAN)}")
    return lines

# ── Port auto-detection ───────────────────────────────────────────────────────
FTDI_VID   = 0x0403
KNOWN_PIDS = {0x6001, 0x6010, 0x6011, 0x6014, 0x6015}

def find_waveshare_port() -> list[str]:
    hits = []
    for p in serial.tools.list_ports.comports():
        if p.vid == FTDI_VID and p.pid in KNOWN_PIDS:
            hits.append(p.device)
        elif p.description and any(
            k in p.description.upper()
            for k in ("FT232", "FTDI", "USB SERIAL", "RS485", "WAVESHARE")
        ):
            hits.append(p.device)
    return hits

def list_all_ports() -> list:
    return sorted(serial.tools.list_ports.comports(), key=lambda p: p.device)

# ── Sniffer ───────────────────────────────────────────────────────────────────
class RS485Sniffer:
    def __init__(self, port: str, baud: int, parity: str, stopbits: int,
                 gap_ms: float, log_path: str | None):
        self.port       = port
        self.baud       = baud
        self.parity     = parity
        self.stopbits   = stopbits
        self.gap_ms     = gap_ms
        self.log_path   = log_path
        self.ser        = None
        self.log_fh     = None
        self.frame_no   = 0
        self.marker_no  = 0
        self.byte_total = 0
        self.running    = False
        self._lock      = threading.Lock()

    # ── Open ──────────────────────────────────────────────────────────────────
    def open(self):
        parity_map = {"N": serial.PARITY_NONE,
                      "E": serial.PARITY_EVEN,
                      "O": serial.PARITY_ODD}
        stop_map   = {1: serial.STOPBITS_ONE, 2: serial.STOPBITS_TWO}
        self.ser = serial.Serial(
            port     = self.port,
            baudrate = self.baud,
            bytesize = serial.EIGHTBITS,
            parity   = parity_map[self.parity],
            stopbits = stop_map[self.stopbits],
            timeout  = 0.05,
        )
        if self.log_path:
            self.log_fh = open(self.log_path, "a", encoding="utf-8", buffering=1)
            self._log(f"\n{'='*72}")
            self._log(f"SESSION START  {datetime.now().isoformat()}")
            self._log(f"Port={self.port}  Baud={self.baud}  "
                      f"Parity={self.parity}  Stop={self.stopbits}  Gap={self.gap_ms}ms")
            self._log(f"Log file: {self.log_path}")
            self._log(f"{'='*72}\n")

    def close(self):
        self.running = False
        if self.ser and self.ser.is_open:
            self.ser.close()
        if self.log_fh:
            self._log(f"\n{'='*72}")
            self._log(f"SESSION END  {datetime.now().isoformat()}  "
                      f"{self.frame_no} frames  {self.byte_total} bytes")
            self._log(f"{'='*72}\n")
            self.log_fh.flush()
            self.log_fh.close()

    # ── Logging helpers ───────────────────────────────────────────────────────
    def _log(self, text: str):
        """Write one line to the log file (ANSI stripped, flushed immediately)."""
        if self.log_fh:
            self.log_fh.write(strip_ansi(text) + "\n")
            self.log_fh.flush()   # flush after every line → crash-safe

    def _print_and_log(self, lines: list[str]):
        """Print to terminal (with colour) and write to log (stripped)."""
        for line in lines:
            print(line)
            self._log(line)

    # ── Frame emitter ─────────────────────────────────────────────────────────
    def _emit_frame(self, frame: bytes, ts: datetime):
        with self._lock:
            self.frame_no   += 1
            self.byte_total += len(frame)
            n = self.frame_no

        crc_ok, crc_msg = check_modbus_crc(frame)
        modbus_info     = describe_modbus(frame) if len(frame) >= 4 else ""
        hex_line        = " ".join(f"{b:02X}" for b in frame)
        crc_col         = GREEN if crc_ok else RED

        lines = [
            "",
            (f"{c('▶', MAGENTA)} Frame {c(f'#{n:04d}', BOLD)}  "
             f"{c(ts.strftime('%H:%M:%S.%f')[:-3], GREY)}  "
             f"{c(str(len(frame)), YELLOW)} bytes  "
             f"{c(crc_msg, crc_col)}"),
        ]
        if modbus_info:
            lines.append(f"    {c(modbus_info, CYAN)}")
        lines.append(f"    HEX: {hex_line}")
        lines += hex_dump(frame)
        self._print_and_log(lines)

    # ── Marker emitter ────────────────────────────────────────────────────────
    def emit_marker(self, label: str = ""):
        with self._lock:
            self.marker_no += 1
            n = self.marker_no

        ts    = datetime.now()
        title = f"MARK #{n:03d}"
        if label:
            title += f": {label}"
        bar   = "─" * 72
        ts_s  = ts.strftime("%H:%M:%S.%f")[:-3]

        # Terminal: bright yellow banner
        lines = [
            "",
            c(f"┌{bar}┐", YELLOW),
            c(f"│  {BOLD}{title:<38}{RESET}{YELLOW}  @  {ts_s}                    │", YELLOW),
            c(f"└{bar}┘", YELLOW),
        ]
        # Log file: plain ASCII box
        log_lines = [
            "",
            f"+{bar}+",
            f"|  {title:<38}  @  {ts_s}                    |",
            f"+{bar}+",
            "",
        ]
        # Print to terminal
        for line in lines:
            print(line)
        # Write to log (already stripped, skip _print_and_log)
        for line in log_lines:
            self._log(line)

    # ── Marker input thread ───────────────────────────────────────────────────
    def _marker_thread(self):
        """
        Runs in background. Each time the user presses Enter, inserts a marker.
        If they typed text before Enter, that becomes the marker label.
        Type 'q' + Enter to quit cleanly.
        """
        while self.running:
            try:
                line = sys.stdin.readline()
                if not self.running:
                    break
                label = line.rstrip("\n").strip()
                if label.lower() == "q":
                    self.running = False
                    break
                self.emit_marker(label)
            except Exception:
                break

    # ── Main capture loop ─────────────────────────────────────────────────────
    def run(self):
        self.running = True
        buf          = bytearray()
        last_rx      = time.monotonic()
        gap_s        = self.gap_ms / 1000.0

        print(c(f"\nListening on {self.port}  "
                f"{self.baud} {self.parity}8{self.stopbits}  "
                f"frame-gap={self.gap_ms}ms", BOLD))
        if self.log_path:
            print(c(f"Logging to:  {self.log_path}", GREY))
        print(c("\nPress [Enter]         → insert an auto-numbered marker", YELLOW))
        print(c("Type a label [Enter]  → marker with your text", YELLOW))
        print(c("Type q [Enter]        → quit\n", YELLOW))

        # Start marker thread as daemon so it doesn't block process exit
        mt = threading.Thread(target=self._marker_thread, daemon=True)
        mt.start()

        try:
            while self.running:
                chunk = self.ser.read(256)
                now   = time.monotonic()

                if chunk:
                    if buf and (now - last_rx) >= gap_s:
                        self._emit_frame(bytes(buf), datetime.now())
                        buf.clear()
                    buf     += chunk
                    last_rx  = now
                else:
                    if buf and (now - last_rx) >= gap_s:
                        self._emit_frame(bytes(buf), datetime.now())
                        buf.clear()

        except KeyboardInterrupt:
            if buf:
                self._emit_frame(bytes(buf), datetime.now())
        finally:
            self.running = False
            print(c(f"\nStopped.  "
                    f"{self.frame_no} frames  "
                    f"{self.byte_total} bytes  "
                    f"{self.marker_no} markers\n", BOLD))
            self.close()

# ── Interactive setup ─────────────────────────────────────────────────────────
def choose_port() -> str:
    auto      = find_waveshare_port()
    all_ports = list_all_ports()
    print(c("\n── Available serial ports ──────────────────────────────", BOLD))
    for i, p in enumerate(all_ports):
        tag = c(" ← Waveshare/FTDI detected", GREEN) if p.device in auto else ""
        print(f"  [{i}]  {p.device:<20} {p.description}{tag}")
    if not all_ports:
        print(c("  No serial ports found. Is the adapter plugged in?", RED))
        sys.exit(1)
    default = next((i for i, p in enumerate(all_ports) if p.device in auto), 0)
    try:
        choice = input(f"\nSelect port [{default}]: ").strip()
        return all_ports[int(choice) if choice else default].device
    except (ValueError, IndexError):
        print(c("Invalid selection.", RED)); sys.exit(1)

def choose_baud() -> int:
    common = [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400, 460800]
    print(c("\n── Baud rate ────────────────────────────────────────────", BOLD))
    for i, b in enumerate(common):
        print(f"  [{i}]  {b}")
    try:
        choice = input("\nSelect baud [3 = 9600]: ").strip()
        return common[int(choice) if choice else 3]
    except (ValueError, IndexError):
        return 9600

def choose_parity() -> str:
    print(c("\n── Parity ───────────────────────────────────────────────", BOLD))
    print("  [0]  None (most common)")
    print("  [1]  Even")
    print("  [2]  Odd")
    return {"0": "N", "1": "E", "2": "O"}.get(input("\nSelect parity [0]: ").strip(), "N")

def choose_stopbits() -> int:
    print(c("\n── Stop bits ────────────────────────────────────────────", BOLD))
    print("  [1]  1 stop bit (most common)")
    print("  [2]  2 stop bits")
    return 2 if input("\nSelect stop bits [1]: ").strip() == "2" else 1

def choose_gap() -> float:
    print(c("\n── Inter-frame gap (ms) ─────────────────────────────────", BOLD))
    print("  Modbus RTU: 3.5 × char time  (~4ms at 9600 baud)")
    print("  20ms is safe for slow buses; raise if frames get split")
    try:
        val = input("\nGap timeout ms [20]: ").strip()
        return float(val) if val else 20.0
    except ValueError:
        return 20.0

def make_log_path() -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        f"rs485_capture_{ts}.log")

def choose_log_path() -> str | None:
    default = make_log_path()
    print(c("\n── Log file ─────────────────────────────────────────────", BOLD))
    print(f"  Default: {default}")
    print("  [Enter]      → use default name")
    print("  custom name  → saved next to this script  (e.g. mytest.log)")
    print("  full path    → saved exactly there         (e.g. /tmp/bus.log)")
    print("  n            → disable logging")
    choice = input("\nLog file: ").strip()
    if choice.lower() == "n":
        return None
    if not choice:
        return default
    # If no path separators, save next to script
    if os.sep not in choice and "/" not in choice:
        choice = os.path.join(os.path.dirname(os.path.abspath(__file__)), choice)
    # Ensure .log extension
    if not choice.endswith(".log"):
        choice += ".log"
    return choice

def show_summary(port, baud, parity, stop, gap, log_path):
    """Print a confirmation table before starting capture."""
    bar = "─" * 50
    print(c(f"\n┌{bar}┐", BOLD))
    print(c(f"│{'  Configuration summary':^50}│", BOLD))
    print(c(f"├{bar}┤", BOLD))
    rows = [
        ("Interface",  port),
        ("Baud rate",  str(baud)),
        ("Parity",     {"N":"None","E":"Even","O":"Odd"}[parity]),
        ("Stop bits",  str(stop)),
        ("Frame gap",  f"{gap} ms"),
        ("Log file",   log_path if log_path else c("disabled", RED)),
    ]
    for label, value in rows:
        print(c(f"│  {label:<14}", BOLD) + f"  {value:<32}" + c("│", BOLD))
    print(c(f"└{bar}┘", BOLD))
    answer = input(c("\nStart capture? [Y/n]: ", YELLOW)).strip().lower()
    return answer not in ("n", "no")

# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="RS485 Bus Sniffer")
    parser.add_argument("--port",   help="Serial port (e.g. /dev/ttyUSB0 or COM3)")
    parser.add_argument("--baud",   type=int, help="Baud rate")
    parser.add_argument("--parity", choices=["N","E","O"], help="Parity (default N)")
    parser.add_argument("--stop",   type=int, choices=[1,2], help="Stop bits (default 1)")
    parser.add_argument("--gap",    type=float, help="Inter-frame gap in ms (default 20)")
    parser.add_argument("--log",    help="Log file path (auto-named if omitted)")
    parser.add_argument("--no-log", action="store_true", help="Disable file logging")
    args = parser.parse_args()

    print(c("""
  ██████  ███████  ██  ██  ██████     ███████  ███  ██  ███  ███████  ████████  ██████
  ██  ██  ██       ██  ██  ██  ██     ██       ████ ██  ██   ██         ██      ██
  ██████  ███████  ██████  ██████     ███████  ██ ████  ██   ███████    ██      ██████
  ██  ██      ██     ██    ██  ██         ██  ██  ███   ██   ██         ██      ██
  ██  ██  ███████    ██    ██  ██     ███████  ██   ██  ███  ██         ██      ██████

  RS485 Bus Sniffer  ·  Waveshare USB-to-RS485 (FT232RNL)
""", CYAN))

    # If all flags supplied skip interactive menus; otherwise walk through them
    port   = args.port   or choose_port()
    baud   = args.baud   or choose_baud()
    parity = args.parity or choose_parity()
    stop   = args.stop   or choose_stopbits()
    gap    = args.gap    or choose_gap()

    if args.no_log:
        log_path = None
    elif args.log:
        log_path = args.log
    else:
        log_path = choose_log_path()

    if not show_summary(port, baud, parity, stop, gap, log_path):
        print("Aborted.")
        sys.exit(0)

    sniffer = RS485Sniffer(
        port=port, baud=baud, parity=parity,
        stopbits=stop, gap_ms=gap, log_path=log_path,
    )
    sniffer.open()
    sniffer.run()

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
RS485 Bus Sniffer
-----------------
Connects to a Waveshare USB-to-RS485 adapter (FT232RNL based) and captures
all bus traffic, grouping bytes into frames by inter-frame silence gaps.

Features:
  - Auto-detects FTDI / Waveshare adapter
  - Configurable baud rate, parity, stop bits
  - Frame grouping with configurable gap timeout
  - Modbus RTU CRC validation on every frame
  - Colour-coded terminal output
  - Timestamped log file (hex + decoded)

Requirements:  pip install pyserial
Usage:         python rs485_sniffer.py
"""

import serial
import serial.tools.list_ports
import struct
import sys
import os
import time
import threading
import argparse
from datetime import datetime

# ── ANSI colours ────────────────────────────────────────────────────────────
RESET   = "\033[0m"
BOLD    = "\033[1m"
RED     = "\033[91m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
CYAN    = "\033[96m"
GREY    = "\033[90m"
MAGENTA = "\033[95m"

def c(text, colour): return f"{colour}{text}{RESET}"

# ── Modbus RTU CRC-16 ────────────────────────────────────────────────────────
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
    """Returns (valid, detail_string). Frame must be >= 4 bytes."""
    if len(frame) < 4:
        return False, "too short"
    payload = frame[:-2]
    received_crc = frame[-2] | (frame[-1] << 8)   # little-endian
    expected_crc = modbus_crc(payload)
    if received_crc == expected_crc:
        return True, f"CRC OK  (0x{expected_crc:04X})"
    return False, f"CRC FAIL (got 0x{received_crc:04X}, expected 0x{expected_crc:04X})"

# ── Modbus function code names ───────────────────────────────────────────────
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
    """Best-effort Modbus RTU frame decode (addr + function code only)."""
    if len(frame) < 4:
        return ""
    addr = frame[0]
    fc   = frame[1]
    name = FC_NAMES.get(fc & 0x7F, "Unknown FC")
    exc  = " [EXCEPTION]" if fc & 0x80 else ""
    return f"Addr={addr}  FC=0x{fc:02X} ({name}){exc}"

# ── Hex + ASCII dump ─────────────────────────────────────────────────────────
def hex_dump(data: bytes, indent: str = "    ") -> list[str]:
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i:i+16]
        hex_part = " ".join(f"{b:02X}" for b in chunk)
        asc_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        offset   = f"{i:04X}"
        lines.append(f"{indent}{c(offset, GREY)}  {hex_part:<47}  {c(asc_part, CYAN)}")
    return lines

# ── Port auto-detection ──────────────────────────────────────────────────────
FTDI_VID  = 0x0403
KNOWN_PIDS = {0x6001, 0x6010, 0x6011, 0x6014, 0x6015}  # FT232, FT2232, FT4232, FT232H, FT231X

def find_waveshare_port() -> list[str]:
    """Return list of likely Waveshare / FTDI port names."""
    hits = []
    for p in serial.tools.list_ports.comports():
        if p.vid == FTDI_VID and p.pid in KNOWN_PIDS:
            hits.append(p.device)
        elif p.description and any(k in p.description.upper() for k in ("FT232", "FTDI", "USB SERIAL", "RS485", "WAVESHARE")):
            hits.append(p.device)
    return hits

def list_all_ports() -> list:
    return sorted(serial.tools.list_ports.comports(), key=lambda p: p.device)

# ── Sniffer core ─────────────────────────────────────────────────────────────
class RS485Sniffer:
    def __init__(self, port: str, baud: int, parity: str, stopbits: float,
                 gap_ms: float, log_path: str | None):
        self.port      = port
        self.baud      = baud
        self.parity    = parity
        self.stopbits  = stopbits
        self.gap_ms    = gap_ms          # inter-frame silence to split frames
        self.log_path  = log_path
        self.ser       = None
        self.log_fh    = None
        self.frame_no  = 0
        self.byte_total= 0
        self.running   = False
        self._lock     = threading.Lock()

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
            stopbits = stop_map[int(self.stopbits)],
            timeout  = 0.05,   # short read timeout for responsive gap detection
        )
        if self.log_path:
            self.log_fh = open(self.log_path, "a", encoding="utf-8")
            self._log_raw(f"\n{'='*72}")
            self._log_raw(f"Session start  {datetime.now().isoformat()}")
            self._log_raw(f"Port={self.port}  Baud={self.baud}  Parity={self.parity}  Stop={self.stopbits}  Gap={self.gap_ms}ms")
            self._log_raw(f"{'='*72}\n")

    def close(self):
        self.running = False
        if self.ser and self.ser.is_open:
            self.ser.close()
        if self.log_fh:
            self.log_fh.flush()
            self.log_fh.close()

    def _log_raw(self, text: str):
        if self.log_fh:
            self.log_fh.write(text + "\n")

    def _emit_frame(self, frame: bytes, ts: datetime):
        with self._lock:
            self.frame_no  += 1
            self.byte_total += len(frame)
            n = self.frame_no

        # ── CRC check ──
        crc_ok, crc_msg = check_modbus_crc(frame)
        crc_colour = GREEN if crc_ok else RED

        # ── Modbus decode attempt ──
        modbus_info = describe_modbus(frame) if len(frame) >= 4 else ""

        # ── Terminal output ──
        hdr = (f"\n{c('▶', MAGENTA)} Frame {c(f'#{n:04d}', BOLD)}  "
               f"{c(ts.strftime('%H:%M:%S.%f')[:-3], GREY)}  "
               f"{c(str(len(frame)), YELLOW)} bytes  "
               f"{c(crc_msg, crc_colour)}")
        print(hdr)
        if modbus_info:
            print(f"    {c(modbus_info, CYAN)}")
        for line in hex_dump(frame):
            print(line)

        # ── Raw hex one-liner for quick grep ──
        hex_line = " ".join(f"{b:02X}" for b in frame)

        # ── Log file ──
        log_lines = [
            f"--- Frame #{n:04d}  {ts.isoformat()}  {len(frame)} bytes  {crc_msg}",
        ]
        if modbus_info:
            log_lines.append(f"    {modbus_info}")
        log_lines.append(f"    HEX: {hex_line}")
        log_lines += hex_dump(frame, indent="    ")
        for ll in log_lines:
            # strip ANSI for log file
            clean = ll.replace(RESET,"").replace(BOLD,"").replace(RED,"") \
                      .replace(GREEN,"").replace(YELLOW,"").replace(CYAN,"") \
                      .replace(GREY,"").replace(MAGENTA,"")
            self._log_raw(clean)

    def run(self):
        self.running = True
        buf          = bytearray()
        last_rx      = time.monotonic()
        gap_s        = self.gap_ms / 1000.0

        print(c(f"\nListening on {self.port}  {self.baud} {self.parity}8{int(self.stopbits)}  "
                f"frame-gap={self.gap_ms}ms  —  Ctrl+C to stop\n", BOLD))
        if self.log_path:
            print(c(f"Logging to: {self.log_path}\n", GREY))

        try:
            while self.running:
                chunk = self.ser.read(256)
                now   = time.monotonic()

                if chunk:
                    # If silence gap exceeded before new bytes, flush old frame
                    if buf and (now - last_rx) >= gap_s:
                        self._emit_frame(bytes(buf), datetime.now())
                        buf.clear()
                    buf      += chunk
                    last_rx   = now
                else:
                    # No bytes — check if buffered data has aged past gap
                    if buf and (now - last_rx) >= gap_s:
                        self._emit_frame(bytes(buf), datetime.now())
                        buf.clear()

        except KeyboardInterrupt:
            if buf:
                self._emit_frame(bytes(buf), datetime.now())
            print(c(f"\n\nStopped.  {self.frame_no} frames  {self.byte_total} bytes total.\n", BOLD))
        finally:
            self.close()

# ── Interactive setup ────────────────────────────────────────────────────────
def choose_port() -> str:
    auto = find_waveshare_port()
    all_ports = list_all_ports()

    print(c("\n── Available serial ports ──────────────────────────────", BOLD))
    for i, p in enumerate(all_ports):
        tag = c(" ← Waveshare/FTDI detected", GREEN) if p.device in auto else ""
        print(f"  [{i}]  {p.device:<20} {p.description}{tag}")

    if not all_ports:
        print(c("  No serial ports found. Is the adapter plugged in?", RED))
        sys.exit(1)

    if auto:
        default_idx = next(i for i, p in enumerate(all_ports) if p.device == auto[0])
    else:
        default_idx = 0

    try:
        choice = input(f"\nSelect port [{default_idx}]: ").strip()
        idx = int(choice) if choice else default_idx
        return all_ports[idx].device
    except (ValueError, IndexError):
        print(c("Invalid selection.", RED))
        sys.exit(1)

def choose_baud() -> int:
    common = [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400, 460800]
    print(c("\n── Baud rate ────────────────────────────────────────────", BOLD))
    for i, b in enumerate(common):
        print(f"  [{i}]  {b}")
    choice = input("\nSelect baud [3 = 9600]: ").strip()
    try:
        idx = int(choice) if choice else 3
        return common[idx]
    except (ValueError, IndexError):
        return 9600

def choose_parity() -> str:
    print(c("\n── Parity ───────────────────────────────────────────────", BOLD))
    print("  [0]  None (most common)")
    print("  [1]  Even")
    print("  [2]  Odd")
    choice = input("\nSelect parity [0]: ").strip()
    return {"0": "N", "1": "E", "2": "O"}.get(choice, "N")

def choose_stopbits() -> int:
    print(c("\n── Stop bits ────────────────────────────────────────────", BOLD))
    print("  [1]  1 stop bit (most common)")
    print("  [2]  2 stop bits")
    choice = input("\nSelect stop bits [1]: ").strip()
    return 2 if choice == "2" else 1

def choose_gap() -> float:
    print(c("\n── Inter-frame gap (ms) ─────────────────────────────────", BOLD))
    print("  Modbus RTU spec: 3.5 × char time")
    print("  At 9600 baud that's ~4ms. 20ms is safe for slow buses.")
    choice = input("\nGap timeout ms [20]: ").strip()
    try:
        return float(choice) if choice else 20.0
    except ValueError:
        return 20.0

def make_log_path() -> str:
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"rs485_capture_{ts}.log"
    # save next to this script
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)

# ── Entry point ──────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="RS485 Bus Sniffer")
    parser.add_argument("--port",    help="Serial port (e.g. /dev/ttyUSB0 or COM3)")
    parser.add_argument("--baud",    type=int,   help="Baud rate (default: interactive)")
    parser.add_argument("--parity",  choices=["N","E","O"], help="Parity (default: N)")
    parser.add_argument("--stop",    type=int,   choices=[1,2], help="Stop bits (default: 1)")
    parser.add_argument("--gap",     type=float, help="Inter-frame gap in ms (default: 20)")
    parser.add_argument("--log",     help="Log file path (auto-named if omitted)")
    parser.add_argument("--no-log",  action="store_true", help="Disable logging")
    args = parser.parse_args()

    print(c("""
  ██████  ███████  ██  ██  ██████     ███████  ███  ██  ███  ███████  ████████  ██████
  ██  ██  ██       ██  ██  ██  ██     ██       ████ ██  ██   ██         ██      ██
  ██████  ███████  ██████  ██████     ███████  ██ ████  ██   ███████    ██      ██████
  ██  ██      ██     ██    ██  ██         ██  ██  ███   ██   ██         ██      ██
  ██  ██  ███████    ██    ██  ██     ███████  ██   ██  ███  ██         ██      ██████

  RS485 Bus Sniffer  ·  Waveshare USB-to-RS485 (FT232RNL)
""", CYAN))

    port    = args.port    or choose_port()
    baud    = args.baud    or choose_baud()
    parity  = args.parity  or choose_parity()
    stop    = args.stop    or choose_stopbits()
    gap     = args.gap     or choose_gap()

    if args.no_log:
        log_path = None
    else:
        log_path = args.log or make_log_path()

    sniffer = RS485Sniffer(
        port     = port,
        baud     = baud,
        parity   = parity,
        stopbits = stop,
        gap_ms   = gap,
        log_path = log_path,
    )
    sniffer.open()
    sniffer.run()

if __name__ == "__main__":
    main()

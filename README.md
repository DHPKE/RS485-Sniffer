# RS485 Sniffer

Passive RS485 bus sniffer for the **Waveshare USB-to-RS485** adapter (FT232RNL chip).  
Built for reverse-engineering live RS485 systems — captures every frame, validates Modbus CRC, and lets you drop named markers into the log while the bus is running.

---

## Requirements

```bash
pipx install pyserial
```

Python 3.10 or newer.

---

## Quick Start

```bash
git clone https://github.com/DHPKE/RS485-Sniffer.git
cd RS485-Sniffer
pip install pyserial
python rs485_sniffer.py
```

The tool walks you through a short setup menu, shows a confirmation summary, then starts capturing.

---

## Interactive Setup Menus

When you run the tool without flags it steps through six menus:

### 1 — Interface (serial port)

```
── Available serial ports ──────────────────────────────
  [0]  /dev/ttyUSB0         USB Serial   ← Waveshare/FTDI detected
  [1]  /dev/ttyS0           ttyS0

Select port [0]:
```

FTDI / Waveshare adapters are highlighted and pre-selected automatically.  
Press **Enter** to accept the default, or type the number of another port.

### 2 — Baud Rate

```
── Baud rate ────────────────────────────────────────────
  [0]  1200
  [1]  2400
  [2]  4800
  [3]  9600       ← default
  [4]  19200
  ...

Select baud [3 = 9600]:
```

If you don't know the baud rate, start with **9600** — the most common setting for industrial devices.  
A good sign you have the wrong baud: every frame shows `CRC FAIL`.

### 3 — Parity

```
  [0]  None (most common)
  [1]  Even
  [2]  Odd
```

### 4 — Stop Bits

```
  [1]  1 stop bit (most common)
  [2]  2 stop bits
```

### 5 — Inter-Frame Gap

The gap in milliseconds of silence that marks the boundary between frames.  
Modbus RTU specifies 3.5 × character time (~4 ms at 9600 baud).  
**20 ms** is a safe default for slow or noisy buses. Increase it if frames appear split; decrease it if distinct commands are being merged together.

### 6 — Log File

```
── Log file ─────────────────────────────────────────────
  Default: /path/to/rs485_capture_20240101_120000.log
  [Enter]      → use default name
  custom name  → saved next to this script  (e.g. mytest.log)
  full path    → saved exactly there         (e.g. /tmp/bus.log)
  n            → disable logging
```

### Confirmation Summary

Before capture starts you see a summary and can abort:

```
┌──────────────────────────────────────────────────────┐
│               Configuration summary                  │
├──────────────────────────────────────────────────────┤
│  Interface       /dev/ttyUSB0                        │
│  Baud rate       9600                                │
│  Parity          None                                │
│  Stop bits       1                                   │
│  Frame gap       20.0 ms                             │
│  Log file        rs485_capture_20240101_120000.log   │
└──────────────────────────────────────────────────────┘

Start capture? [Y/n]:
```

---

## Command-Line Flags (skip the menus)

All settings can be passed as flags to bypass interactive mode entirely:

```bash
python rs485_sniffer.py \
  --port   /dev/ttyUSB0 \
  --baud   9600 \
  --parity N \
  --stop   1 \
  --gap    20 \
  --log    my_capture.log
```

| Flag | Description |
|------|-------------|
| `--port PORT` | Serial port (`/dev/ttyUSB0`, `COM3`, …) |
| `--baud RATE` | Baud rate (e.g. `9600`) |
| `--parity N\|E\|O` | Parity: None / Even / Odd |
| `--stop 1\|2` | Stop bits |
| `--gap MS` | Inter-frame gap in milliseconds |
| `--log PATH` | Log file path |
| `--no-log` | Disable file logging |

---

## Live Capture Output

Each received frame is printed immediately with:

```
▶ Frame #0001  12:34:56.789  8 bytes  CRC OK (0x1234)
    Addr=1  FC=0x03 (Read Holding Registers)
    HEX: 01 03 00 00 00 0A C5 CD
    0000  01 03 00 00 00 0A C5 CD                           ........
```

- **Frame number** — sequential, never reset during a session  
- **Timestamp** — millisecond precision  
- **Byte count**  
- **CRC result** — green = valid Modbus CRC, red = invalid or wrong baud/parity  
- **Modbus decode** — address and function code name (best-effort, shown when frame ≥ 4 bytes)  
- **HEX one-liner** — easy to copy or grep  
- **Hex + ASCII dump** — 16 bytes per row with printable ASCII on the right  

---

## Markers

While the sniffer is running, the terminal still accepts input.  
Use this to drop named bookmarks into both the terminal and the log file — very useful when you trigger specific actions on the device under test.

| Action | Result |
|--------|--------|
| Press **Enter** (empty) | Auto-numbered marker: `MARK #001` |
| Type text + **Enter** | Named marker: `MARK #001: relay 1 on` |
| Type **q** + **Enter** | Clean shutdown |

Markers appear in the terminal as a bright yellow banner and in the log as a plain ASCII box:

```
+------------------------------------------------------------------------+
|  MARK #001: relay 1 on                  @  12:35:10.042               |
+------------------------------------------------------------------------+
```

---

## Log File

Every frame and marker is written to the log file **immediately** (flushed after each line) — you won't lose data even if the tool crashes or you kill the terminal.

The log is plain UTF-8 text with no ANSI colour codes, readable in any editor.  
Useful grep patterns:

```bash
# List all frames
grep "^--- Frame" capture.log

# List all markers
grep "^|  MARK" capture.log

# Find frames with CRC failures
grep "CRC FAIL" capture.log

# Extract all raw hex lines
grep "HEX:" capture.log

# Find frames from Modbus address 3
grep "Addr=3" capture.log
```

The session header and footer record the exact settings and total frame/byte counts:

```
========================================================================
SESSION START  2024-01-01T12:34:56.789
Port=/dev/ttyUSB0  Baud=9600  Parity=N  Stop=1  Gap=20.0ms
========================================================================

...frames and markers...

========================================================================
SESSION END  2024-01-01T12:45:00.000  247 frames  1832 bytes
========================================================================
```

---

## Wiring

Connect the Waveshare adapter **in parallel** with the existing bus — no need to break the wiring:

```
Existing device A ──┬── Waveshare A
Existing device B ──┴── Waveshare B
               GND ───── Waveshare GND
```

No extra termination resistor needed for the sniffer tap. Keep the tap leads short.

---

## Tips for Reverse Engineering

1. **Unknown baud rate?** Start at 9600. If all frames show `CRC FAIL`, try 19200, then 38400, 115200.
2. **Trigger one action at a time** on the source device, then drop a marker before and after — e.g. `before relay ON` / `after relay ON`. The frames between those markers are what you need.
3. **Markers make searching easy** — your log becomes a labelled timeline rather than a wall of hex.
4. **Frame splitting?** If one logical command shows up as two frames, increase the gap timeout.
5. **Common grep**: `grep "HEX:" capture.log` gives you a clean list of all raw frames to compare.

---

## License

MIT

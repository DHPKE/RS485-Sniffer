# RS485 Sniffer

Passive RS485 bus sniffer for the **Waveshare USB-to-RS485** adapter (FT232RNL chip), built for reverse-engineering existing RS485 systems.

## Features

- **Auto-detects** Waveshare / FTDI adapters on all platforms
- **Frame grouping** — bytes are assembled into frames using a configurable inter-frame silence gap (default 20 ms)
- **Modbus RTU CRC validation** on every captured frame (green = valid, red = invalid)
- **Modbus function code decoding** — shows address, FC name, exception flag
- **Colour-coded terminal output** with full hex + ASCII dump
- **Timestamped log file** saved automatically next to the script

## Requirements

```bash
pip install pyserial
```

## Usage

### Interactive (recommended for first run)

```bash
python rs485_sniffer.py
```

The tool will:
1. List all serial ports and highlight the Waveshare adapter
2. Ask for baud rate, parity, stop bits, and frame gap
3. Start capturing and print every frame to the terminal
4. Save a timestamped `.log` file alongside the script

### Command-line flags

```bash
python rs485_sniffer.py \
  --port /dev/ttyUSB0 \   # or COM3 on Windows
  --baud 9600 \
  --parity N \            # N, E, or O
  --stop 1 \              # 1 or 2
  --gap 20                # inter-frame gap in ms
```

Other flags:
- `--log /path/to/file.log` — custom log path
- `--no-log` — disable file logging

## Wiring

Connect the Waveshare adapter A/B terminals in **parallel** with the existing bus wires. No termination resistor needed for the sniffer tap — just T-junction the A and B lines and share a common ground.

```
Existing device A ──┬── Waveshare A
Existing device B ──┴── Waveshare B
                GND ──── Waveshare GND
```

## Tips for Reverse Engineering

- If you don't know the baud rate, start at **9600** (most common in industrial equipment)
- Watch the CRC column — if all frames show `CRC FAIL`, you likely have the wrong baud rate or parity
- Trigger one input/action at a time on the source device and note which frame appears
- The log file is plain text — grep it easily: `grep "HEX:" rs485_capture.log`

## License

MIT

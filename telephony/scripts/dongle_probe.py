#!/usr/bin/env python3
"""Day-1 hardware check for the SIM7600G-H. Read-only AT queries; places no call, changes nothing.

Run on the Linux box the dongle is plugged into, with Asterisk STOPPED (it owns the AT port):
    uv run --with pyserial python dongle_probe.py /dev/sim7600-at

Prints a table of what the module reports, and a verdict per item that decides whether the plan
holds. Anything it cannot decide is printed as UNKNOWN, never guessed.
"""
import sys
import time

import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB2"

QUERIES = [
    ("ATI", "module identity"),
    ("AT+CGMR", "firmware revision"),
    ("AT+CPIN?", "SIM ready?"),
    ("AT+CIMI", "IMSI (operator = first 5-6 digits; 405/404 = India)"),
    ("AT+CSQ", "signal 0-31 (99 = none)"),
    ("AT+CREG?", "2G/3G (circuit-switched) registration"),
    ("AT+CEREG?", "LTE registration"),
    ("AT+COPS?", "operator name"),
    ("AT+CPSI?", "serving system and LTE band"),
    ("AT+CNMP?", "network mode preference (2 auto, 38 LTE only, 13 GSM only)"),
    ("AT+CLIP?", "caller-ID presentation enabled?"),
    ("AT+VOLTESETTING?", "VoLTE setting (name from the Waveshare FAQ; query form unverified, ERROR is fine)"),
    ("AT+CPCMREG?", "PCM-over-USB-serial audio state"),
    ("AT+DDET?", "module-side DTMF detection state"),
    ("AT+CLCC", "current calls (should be empty)"),
]


def send(ser, cmd, wait=1.5):
    ser.reset_input_buffer()
    ser.write((cmd + "\r").encode())
    end = time.time() + wait
    buf = b""
    while time.time() < end:
        chunk = ser.read(256)
        if chunk:
            buf += chunk
            if b"OK\r\n" in buf or b"ERROR" in buf:
                break
    return buf.decode("utf-8", "replace").replace("\r", "").strip()


def main():
    with serial.Serial(PORT, 115200, timeout=0.3) as ser:
        send(ser, "ATE0")
        for cmd, why in QUERIES:
            out = send(ser, cmd)
            status = "ERROR" if "ERROR" in out else "ok"
            print(f"--- {cmd:12s} [{status}]  {why}")
            for line in out.splitlines():
                if line and line != "OK":
                    print("      " + line)
    print("\nDecide by reading the output:")
    print(" * +CPIN: READY, +CEREG: 0,1 or 0,5, and a plausible +CSQ  -> radio is fine")
    print(" * Jio SIM: voice needs VoLTE. If +CEREG is registered but calls fail, try VoLTE on:")
    print("     AT+VOLTESETTING=1 (Waveshare FAQ) and reboot the module; re-run this script.")
    print(" * Airtel/Vi/BSNL SIM: voice can fall back to 2G (CSFB). +CREG should show registered")
    print("     after AT+CNMP=2 or after a call attempt.")
    print(" * AT+CPCMREG? / AT+DDET? returning ERROR means a different firmware command name:")
    print("     read the SIM7600 AT manual for your firmware revision (see +CGMR above).")


if __name__ == "__main__":
    main()

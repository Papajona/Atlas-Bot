"""Capture rendered Android screens on an emulator in CI (no real account is logged in)."""
from __future__ import annotations

import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

OUT = Path("ui-audit-artifacts/android")
OUT.mkdir(parents=True, exist_ok=True)


def run(args: list[str], *, timeout: int = 90, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=check, timeout=timeout, text=True, capture_output=True)


def dump_ui(name: str) -> ET.Element:
    run(["adb", "shell", "uiautomator", "dump", "/sdcard/atlas-ui.xml"], timeout=60)
    raw = run(["adb", "exec-out", "cat", "/sdcard/atlas-ui.xml"], timeout=15).stdout
    (OUT / f"{name}-hierarchy.xml").write_text(raw, encoding="utf-8")
    return ET.fromstring(raw)


def capture(name: str) -> None:
    result = subprocess.run(["adb", "exec-out", "screencap", "-p"], check=True, timeout=20, capture_output=True)
    if not result.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError(f"Android screenshot {name} was not a valid PNG")
    (OUT / f"{name}.png").write_bytes(result.stdout)


def find_text(root: ET.Element, text: str) -> tuple[int, int] | None:
    # Find the smallest displayed node matching visible text or accessibility description.
    candidates = []
    for node in root.iter("node"):
        actual = (node.attrib.get("text", "") + " " + node.attrib.get("content-desc", "")).strip().lower()
        if text.lower() not in actual:
            continue
        bounds = node.attrib.get("bounds", "")
        try:
            first, second = bounds.strip("[]").split("][")
            x1, y1 = (int(v) for v in first.split(","))
            x2, y2 = (int(v) for v in second.split(","))
        except Exception:
            continue
        candidates.append(((x2 - x1) * (y2 - y1), (x1 + x2) // 2, (y1 + y2) // 2))
    if not candidates:
        return None
    _, x, y = min(candidates)
    return x, y


def tap_visible_text(name: str, *, attempts: int = 8) -> None:
    for attempt in range(attempts):
        root = dump_ui(f"before-tap-{name.replace(' ', '-')}-{attempt}")
        point = find_text(root, name)
        if point:
            run(["adb", "shell", "input", "tap", str(point[0]), str(point[1])], timeout=15)
            time.sleep(1.3)
            return
        # Settings is a long scrollable screen; reveal lower items without using fixed coordinates.
        run(["adb", "shell", "input", "swipe", "800", "1900", "800", "620", "400"], timeout=15)
        time.sleep(0.5)
    raise RuntimeError(f"Could not find visible Android control text after scrolling: {name!r}")


def main() -> None:
    run(["adb", "wait-for-device"], timeout=120)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        result = run(["adb", "shell", "getprop", "sys.boot_completed"], timeout=15, check=False)
        if result.stdout.strip() == "1":
            break
        time.sleep(2)
    else:
        raise RuntimeError("Android emulator did not finish booting within 5 minutes")

    run(["adb", "shell", "settings", "put", "system", "font_scale", "1.0"])
    run(["adb", "shell", "wm", "size", "1080x2400"])
    run(["adb", "shell", "wm", "density", "420"])
    run(["adb", "install", "-r", "app/build/outputs/apk/debug/app-debug.apk"], timeout=120)
    run(["adb", "shell", "am", "start", "-n", "com.atlas.trading/.MainActivity"])
    time.sleep(5)
    capture("customer-home")
    tap_visible_text("Settings")
    capture("customer-settings")
    tap_visible_text("Operator Console")
    capture("admin-login")

    (OUT / "README.txt").write_text(
        "Rendered screenshots from the PR branch's debug APK on an Android emulator. "
        "No customer or admin credentials were used; customer data is unauthenticated/empty. "
        "This is a device-render pass, not a production backend/integration test.\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()

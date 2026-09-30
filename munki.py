#!/usr/bin/env python3
"""munki -- measure printed wedges and plaques with an ArgyllCMS instrument
(built for a ColorMunki Photo) and turn the numbers into things the rest of
this repo consumes.

Three jobs:

  measure-wedge   reflectance of each step of a printed wedge, printed as the
                  hex list `calibrate.py fit --measured` takes (and saved with
                  the spectra, for a later Kubelka-Munk fit).
  transmission    how much light gets through standalone chips of 1..N layers
                  (calibrate.py chips), using a *laptop screen* as the
                  backlight: measure the screen bare, then with a chip laid on
                  it. T = with / bare, and td = -thickness / ln(T).
  verify-plaque   measure patches of a finished stackforge plaque and report dE
                  against what the simulation predicted.

Not verified against hardware yet: this was written from spotread's documented
output and the transcript in `tests/test_munki.py`, not from a run on the
instrument. In particular the `-s` spectrum layout and emissive-mode contact
with a screen through a sample are assumptions -- see `docs/measuring.md`.

ColorMunki with a stale dial report: `--nospos` runs the patched ArgyllCMS
(`argyll-nospos`, dial check off). Nothing then checks the dial, so munki.py
checks white paper after calibration, flags repeated readings and wedge
reversals, and tells you when to turn the dial back.

    munki.py measure-wedge --steps 12 -o teal_white.json
    munki.py --nospos measure-wedge --steps 12 -o teal_white.json
    munki.py transmission --thickness 0.25,0.33,0.41,0.49 -o teal_chips.json
    munki.py verify-plaque --predicted "#D8E6E4,#54A09E"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

import numpy as np

import tdcolor

try:
    import pty
except ImportError:  # Windows
    pty = None

STARTUP_TIMEOUT_S = 30.0
MEASURE_TIMEOUT_S = 60.0

READY_PROMPT = "any other key to take a reading"
ACTION_PROMPT_RE = re.compile(r"any key to continue,\s*or hit Esc or Q to abort:\s*$")
_NUM = r"([+-]?[\d.]+(?:[eE][+-]?\d+)?)"
RESULT_RE = re.compile(rf"Result is XYZ:\s*{_NUM}[,\s]+{_NUM}[,\s]+{_NUM}")
LAB_RE = re.compile(rf"D50 Lab:\s*{_NUM}[,\s]+{_NUM}[,\s]+{_NUM}")
SPECTRUM_RE = re.compile(r"Spectrum from\s*([\d.]+)\s*to\s*([\d.]+)\s*nm\s*in\s*(\d+)\s*steps")
MAX_ACTION_PROMPTS = 6

# The patched ArgyllCMS (dial-position check compiled out) is run through its
# wrapper, which also keeps its calibrations in a separate cache. The wrapper
# exports ARGYLL_NOSPOS=1; the patched binary itself never reads it.
NOSPOS_WRAPPER = "argyll-nospos"
NOSPOS_NOTE = ("  (patched Argyll: the dial position is NOT checked. Make sure it really is "
               "where the prompt says.)")
NOSPOS_DIAL_BACK = ("  >> Calibrated. Turn the dial back to the measuring position, then Enter "
                    "(q + Enter to quit): ")
WHITE_Y_RANGE = (70.0, 110.0)   # plain white paper, D50-relative Y, UV-cut illuminant
SAME_READING_DE = 0.5           # two different patches closer than this look like a stuck dial
WEDGE_REVERSAL_L = 1.5          # L* going the wrong way by more than this breaks a wedge


class MunkiError(RuntimeError):
    pass


class SessionAborted(MunkiError):
    """The human chose to quit at a prompt."""


class NeedsRecalibration(MunkiError):
    """A reading came back but the instrument then asked for a calibration
    (bumped dial, or the ColorMunki's own switch fired a second trigger)."""


class DeadReading(MunkiError):
    """Exactly-zero XYZ. spotread cannot tell when a ColorMunki's dial sits at
    the calibration position (its position report can be stale) and prints the
    zeros as a normal result, so refuse them rather than fit garbage."""


def _clean(text: str) -> str:
    lines = [ln.rstrip() for ln in text.replace("\r", "").split("\n")]
    return "\n".join(ln for ln in lines if ln.strip())


def parse_reading(text: str) -> dict:
    """Pull XYZ, D50 Lab and (if `-s` was passed) the spectrum out of spotread's
    output for one reading. Raises MunkiError when there is no result."""
    m = RESULT_RE.search(text)
    if not m:
        raise MunkiError(f"spotread gave no reading. Its output:\n{_clean(text)[-600:]}")
    xyz = tuple(float(g) for g in m.groups())
    if not any(xyz):
        raise DeadReading(
            "the instrument returned an all-zero reading (X=Y=Z=0). Its dial is probably at "
            "the calibration position, or the sensor is blocked. Turn it to the measure "
            "position (power-cycle it there) and start again.")
    out = {"xyz": xyz}
    lab = LAB_RE.search(text)
    if lab:
        out["lab_d50"] = tuple(float(g) for g in lab.groups())
    sp = SPECTRUM_RE.search(text)
    if sp:
        lo, hi, n = float(sp.group(1)), float(sp.group(2)), int(sp.group(3))
        tail = text[sp.end():]
        vals = [float(v) for v in re.findall(r"[+-]?\d+\.?\d*(?:[eE][+-]?\d+)?", tail)][:n]
        if len(vals) == n:
            out["spectrum"] = {"nm_from": lo, "nm_to": hi, "values": vals}
    return out


# --------------------------------------------------------------------------
# colour conversions
# --------------------------------------------------------------------------

# Bradford D50 -> D65, then XYZ -> linear sRGB (tdcolor's own matrix, so the
# result lands in the same space as everything else in this repo).
_BRADFORD_D50_D65 = np.array([
    [0.9555766, -0.0230393, 0.0631636],
    [-0.0282895, 1.0099416, 0.0210077],
    [0.0122982, -0.0204830, 1.3299098],
])


def xyz_d50_to_srgb(xyz, scale: float = 100.0) -> np.ndarray:
    """spotread's D50-relative XYZ (white ~ 100 in reflective mode) -> sRGB 0..255."""
    x = np.asarray(xyz, dtype=np.float64) / scale
    lin = tdcolor._M_XYZ2RGB @ (_BRADFORD_D50_D65 @ x)
    return tdcolor.linear_to_srgb(lin)


def xyz_d50_to_lab(xyz, scale: float = 100.0) -> np.ndarray:
    """spotread's D50-relative XYZ -> CIE Lab (D50), no gamut clipping."""
    white = np.array([96.422, 100.0, 82.521]) / 100.0
    r = np.asarray(xyz, dtype=np.float64) / scale / white
    f = np.where(r > (6 / 29) ** 3, np.cbrt(r), r / (3 * (6 / 29) ** 2) + 4 / 29)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]),
                     200 * (f[..., 1] - f[..., 2])], axis=-1)


# --------------------------------------------------------------------------
# wrong-dial guards (the patched Argyll no longer checks the dial itself)
# --------------------------------------------------------------------------


def check_white(reading):
    """A reading of plain white paper must land near Y = 100. Far off means the
    calibration was taken with the dial away from the calibration tile, which
    rescales every reading after it."""
    y = reading["xyz"][1]
    lo, hi = WHITE_Y_RANGE
    if not lo <= y <= hi:
        raise MunkiError(
            f"white paper read Y = {y:.1f} (expected {lo:.0f}-{hi:.0f}). The calibration was "
            "probably taken with the dial away from the calibration position. Start again.")


def same_reading(a, b) -> float | None:
    """dE between two readings when it is small enough to look like the same
    surface (dial left at the calibration position, or meter not moved)."""
    de = float(np.linalg.norm(xyz_d50_to_lab(a["xyz"]) - xyz_d50_to_lab(b["xyz"])))
    return de if de < SAME_READING_DE else None


def wedge_reversals(lab_l, tol: float = WEDGE_REVERSAL_L) -> list[int]:
    """1-based steps where L* moves against the wedge's overall trend by more
    than `tol`. Each extra layer moves the colour further from the base, so a
    wedge runs one way; a reversal is a misread (wrong patch or wrong dial)."""
    L = np.asarray(lab_l, float)
    trend = L[-1] - L[0] if len(L) > 1 else 0.0
    if abs(trend) < tol:
        return []
    d = np.diff(L) * np.sign(trend)
    return [i + 2 for i in np.flatnonzero(d < -tol)]


# --------------------------------------------------------------------------
# transmission maths (pure, so it is testable without an instrument)
# --------------------------------------------------------------------------


def transmittance(with_sample, bare):
    """Per-channel T = reading through the sample / reading of the bare source,
    clipped to (0, 1]. A value above 1 means drift or a leak, not gain."""
    t = np.asarray(with_sample, float) / np.maximum(np.asarray(bare, float), 1e-12)
    return np.clip(t, 1e-6, 1.0)


def td_from_transmittance(thickness_mm, T):
    """Fit td per channel from T = exp(-t/td), by least squares through the
    origin on ln T = -t/td. thickness (n,), T (n,3) -> td (3,), residual (3,).

    Uses only chips with 0.02 < T < 0.98: near 1 the log is all noise, and near
    0 the meter is at its floor (stray edge light dominates), so both ends
    bias td. Returns NaN when fewer than two chips qualify."""
    t = np.asarray(thickness_mm, float)
    T = np.asarray(T, float)
    td, rms = np.full(3, np.nan), np.full(3, np.nan)
    for c in range(3):
        ok = (T[:, c] > 0.02) & (T[:, c] < 0.98)
        if ok.sum() < 2:
            continue
        y = -np.log(T[ok, c])
        den = float((t[ok] ** 2).sum())
        if den <= 0:
            continue
        k = float((t[ok] * y).sum() / den)  # slope = 1/td
        if k <= 0:
            continue
        td[c] = 1.0 / k
        rms[c] = float(np.sqrt(np.mean((y - k * t[ok]) ** 2)))
    return td, rms


def delta_e(a_srgb, b_srgb) -> np.ndarray:
    return np.linalg.norm(tdcolor.srgb_to_lab(np.asarray(a_srgb, float))
                          - tdcolor.srgb_to_lab(np.asarray(b_srgb, float)), axis=-1)


# --------------------------------------------------------------------------
# the spotread session
# --------------------------------------------------------------------------


class SpotreadSession:
    """One long-lived interactive `spotread`, driven a key at a time.

    Why a session and not one spotread per patch: spotread must calibrate before
    its first reading (on a ColorMunki the human turns the dial to the
    calibration position and confirms), and does so again for every fresh
    process. The only key sent is a space ("any other key") plus `k`
    (recalibrate) and `q`; letters are commands at spotread's prompt.

    Adapted from the same idea in the calibration-suite project, deliberately
    copied rather than imported so the two repos stay independent. It runs under
    a pty on POSIX because spotread reads keys as a terminal would.
    """

    def __init__(self, args=(), nospos=False):
        self.args = [str(a) for a in args]
        under_wrapper = os.environ.get("ARGYLL_NOSPOS") == "1"
        self.nospos = nospos or under_wrapper
        self._wrap = nospos and not under_wrapper   # already inside the wrapper: PATH is set
        self.command = None
        self._proc = None
        self._buf = ""
        self._lock = threading.Lock()
        self._changed = threading.Event()
        self._eof = False

    def start(self):
        if self._proc is not None:
            return
        if self._wrap:
            wrapper = shutil.which(NOSPOS_WRAPPER)
            if wrapper is None:
                raise MunkiError(f"'{NOSPOS_WRAPPER}' is not on PATH (the patched ArgyllCMS)")
            self.command = [wrapper, "spotread"]
        else:
            exe = shutil.which("spotread")
            if exe is None:
                raise MunkiError("'spotread' is not on PATH (install ArgyllCMS)")
            self.command = [exe]
        argv = [*self.command, *self.args]
        if pty is not None:
            master, slave = pty.openpty()
            self._proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave,
                                          close_fds=True)
            os.close(slave)
            self._rfd = self._wfd = master
        else:
            self._proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT, bufsize=0)
            self._rfd, self._wfd = self._proc.stdout.fileno(), self._proc.stdin.fileno()
        threading.Thread(target=self._reader, daemon=True).start()

    def close(self):
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.poll() is None:
            try:
                os.write(self._wfd, b"q")
                proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
        if pty is not None:
            try:
                os.close(self._rfd)
            except OSError:
                pass
        else:
            for f in (proc.stdin, proc.stdout):
                try:
                    f.close()
                except OSError:
                    pass

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    def _reader(self):
        while True:
            try:
                data = os.read(self._rfd, 4096)
            except OSError:
                data = b""
            if not data:
                with self._lock:
                    self._eof = True
                self._changed.set()
                return
            with self._lock:
                self._buf += data.decode("utf-8", "replace")
            self._changed.set()

    def _take(self) -> str:
        with self._lock:
            text, self._buf = self._buf, ""
        return text

    def _wait_for(self, pred, timeout, poll=None) -> str:
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                buf, eof = self._buf, self._eof
            if pred(buf):
                return buf
            if eof:
                raise MunkiError(f"spotread exited unexpectedly. Its output:\n{_clean(buf)[-800:]}")
            if time.monotonic() > deadline:
                raise MunkiError(f"spotread did not respond within {timeout:.0f}s. "
                                 f"Its output:\n{_clean(buf)[-800:]}")
            if poll is not None:
                poll()
            self._changed.wait(0.05)
            self._changed.clear()

    def prepare(self, say=print, ask=input, timeout=STARTUP_TIMEOUT_S) -> int:
        """Get to the idle "take a reading" prompt, relaying every prompt that
        needs the human (dial position). Returns how many were relayed."""
        actions = 0
        while True:
            buf = self._wait_for(
                lambda b: READY_PROMPT in b or ACTION_PROMPT_RE.search(b) is not None, timeout)
            text = self._take()
            if READY_PROMPT in buf and not ACTION_PROMPT_RE.search(buf):
                # spotread no longer insists on the measuring position; say it ourselves.
                if self.nospos and actions and ask(NOSPOS_DIAL_BACK).strip().lower().startswith("q"):
                    raise SessionAborted("quit at an instrument prompt")
                return actions
            actions += 1
            if actions > MAX_ACTION_PROMPTS:
                raise MunkiError(
                    f"spotread still refusing after {MAX_ACTION_PROMPTS} attempts; the dial "
                    f"never reached the position it asked for. Last prompt:\n{_clean(text)[-400:]}")
            say(_clean(text)[-400:])
            if self.nospos:
                say(NOSPOS_NOTE)
            if ask("  >> Press Enter when done (q + Enter to quit): ").strip().lower().startswith("q"):
                raise SessionAborted("quit at an instrument prompt")
            os.write(self._wfd, b" ")

    def recalibrate(self, say=print, ask=input):
        self._take()
        os.write(self._wfd, b"k")
        self.prepare(say, ask)

    def measure(self, poll=None, timeout=MEASURE_TIMEOUT_S) -> dict:
        """One reading. Waits for spotread's *next* idle prompt before returning
        so the next key cannot be swallowed mid-reading."""
        self._take()
        os.write(self._wfd, b" ")

        def settled(b):
            m = RESULT_RE.search(b)
            tail = b[m.end():] if m else b
            return READY_PROMPT in tail or ACTION_PROMPT_RE.search(tail) is not None

        buf = self._wait_for(settled, timeout, poll)
        m = RESULT_RE.search(buf)
        if m and ACTION_PROMPT_RE.search(buf[m.end():]):
            raise NeedsRecalibration(
                "the instrument gave a reading, then asked to be recalibrated. Recalibrate and "
                f"start the run again. Its output:\n{_clean(buf)[-600:]}")
        return parse_reading(buf)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

REFLECT_ARGS = ["-s", "-i", "D50", "-Q", "1931_2"]   # reflective is spotread's default mode
EMISSIVE_ARGS = ["-e", "-s", "-Q", "1931_2"]


def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _meta(mode, s):
    # No serial number: this goes into a public repo.
    return {"tool": "munki.py", "mode": mode, "nospos": s.nospos,
            "spotread": [os.path.basename(s.command[0]), *s.command[1:]],   # no home path
            "spotread_args": s.args, "at": _stamp(),
            "note": "ColorMunki is UV-cut only (white-LED illuminant, no M0/M1): fluorescent "
                    "whiteners in PLA are not excited, so a white can read duller/yellower here "
                    "than under a UV-rich light."}


def _white_check(s):
    """On the patched Argyll, prove the calibration before trusting any patch."""
    if not s.nospos:
        return None
    input("\nCheck: meter on plain white paper (tests the calibration), Enter to measure ")
    r = s.measure()
    check_white(r)
    print(f"   white paper Y = {r['xyz'][1]:.1f}: calibration looks right")
    return r


def _read_patch(s, prompt, prev):
    """One patch, re-measured on request when it matches the previous reading."""
    while True:
        input(prompt)
        r = s.measure()
        de = same_reading(r, prev) if prev is not None else None
        if de is None:
            return r
        print(f"   ! same as the previous reading (dE {de:.2f}): is the dial still at the "
              "calibration position, or the meter on the same patch?")
        if not input("   Enter to keep it, r + Enter to measure again: ").strip().lower().startswith("r"):
            return r


def cmd_measure_wedge(args):
    sargs = REFLECT_ARGS + args.spotread_arg
    readings = []
    with SpotreadSession(sargs, nospos=args.nospos) as s:
        s.prepare()
        white = _white_check(s)
        for i in range(args.steps):
            r = _read_patch(s, f"\nstep {i + 1}/{args.steps} ({i + 1} layers): centre the aperture "
                               f"on the patch, then press Enter to measure ",
                            readings[-1] if readings else None)
            r["srgb"] = [round(float(v), 1) for v in xyz_d50_to_srgb(r["xyz"])]
            readings.append(r)
            print(f"   {tdcolor.to_hex(r['srgb'])}  Lab D50 {r.get('lab_d50')}")
        meta = _meta("reflective", s)
    bad = wedge_reversals([xyz_d50_to_lab(r["xyz"])[0] for r in readings])
    if bad:
        print(f"\n  ! L* runs against the wedge at step(s) {bad}: re-measure those before fitting")
    hexes = ",".join(tdcolor.to_hex(r["srgb"]) for r in readings)
    if args.output:
        out = {**meta, "readings": readings, "hex": hexes, "reversed_steps": bad}
        if white is not None:
            out["white_check"] = white
        with open(args.output, "w") as f:
            json.dump(out, f, indent=1)
        print(f"\nsaved {args.output}")
    print(f"\ncalibrate.py fit --filament <id> --base <hex or id> --measured \"{hexes}\"")


def cmd_transmission(args):
    thick = [float(t) for t in args.thickness.split(",")]
    sargs = EMISSIVE_ARGS + args.spotread_arg
    root = None
    try:
        import tkinter as tk
        root = tk.Tk()
        root.attributes("-fullscreen", True)
        root.configure(cursor="none")
    except Exception as e:  # no display: the user must show the patch themselves
        print(f"  ! no window ({e}); show a full-screen white/red/green/blue patch yourself.")

    def show(rgb):
        if root is not None:
            root.configure(bg="#%02x%02x%02x" % rgb)
            root.update()
            time.sleep(args.settle)

    primaries = {"W": (255, 255, 255), "R": (255, 0, 0), "G": (0, 255, 0), "B": (0, 0, 255)}

    def read_all(s, label):
        out = {}
        for name, rgb in primaries.items():
            show(rgb)
            out[name] = s.measure(poll=(root.update if root else None))["xyz"][1]  # Y, cd/m^2
        print(f"   {label}: " + "  ".join(f"{k}={v:.2f}" for k, v in out.items()))
        return out

    try:
        with SpotreadSession(sargs, nospos=args.nospos) as s:
            print("Meter in emissive/display position, flat on the screen. Black card or foam\n"
                  "around the chip so no screen light reaches the aperture except through it.")
            s.prepare()
            input("\nNo sample: press Enter to read the bare screen ")
            bare0 = read_all(s, "bare")
            rows = []
            for t in thick:
                input(f"\nchip {t:.2f} mm under the meter, then Enter ")
                rows.append(read_all(s, f"{t:.2f} mm"))
            input("\nRemove the chip: Enter to re-read the bare screen (drift check) ")
            bare1 = read_all(s, "bare again")
            meta = _meta("emissive-transmission", s)
    finally:
        if root is not None:
            root.destroy()

    drift = max(abs(bare1[k] / max(bare0[k], 1e-12) - 1) for k in primaries)
    bare = {k: (bare0[k] + bare1[k]) / 2 for k in primaries}
    T = np.array([[transmittance(r[k], bare[k]) for k in ("R", "G", "B")] for r in rows])
    Tw = np.array([float(transmittance(r["W"], bare["W"])) for r in rows])
    td, rms = td_from_transmittance(thick, T)
    print(f"\nscreen drift over the run: {100 * drift:.1f}%"
          + ("   ! over 3%: repeat with a warmer screen" if drift > 0.03 else ""))
    print(f"{'mm':>6} {'T_R':>7} {'T_G':>7} {'T_B':>7} {'T_white':>8}")
    for t, row, tw in zip(thick, T, Tw):
        print(f"{t:6.2f} {row[0]:7.3f} {row[1]:7.3f} {row[2]:7.3f} {tw:8.3f}")
    print(f"\ntd (mm) R/G/B = {np.round(td, 3).tolist()}   (fit rms in ln T {np.round(rms, 3).tolist()})")
    print("A screen is a narrow-band, polarised source: treat this as a cross-check on\n"
          "the reflectance-fit td, not a replacement for it.\n"
          "This td is SINGLE-PASS. calibrate.py and stackforge use a reflectance-fit td,\n"
          "where light crosses each layer twice; for a clear absorber that is about half\n"
          "this value. Do not paste it into td_rgb as-is.")
    if args.output:
        with open(args.output, "w") as f:
            json.dump({**meta, "thickness_mm": thick,
                       "bare": bare, "drift": drift, "T": T.tolist(), "T_white": Tw.tolist(),
                       "td_transmission_rgb": [None if np.isnan(v) else float(v) for v in td]}, f, indent=1)
        print(f"saved {args.output}")


def cmd_verify(args):
    pred = np.array([tdcolor.parse_hex(t) for t in args.predicted.split(",")], float)
    sargs = REFLECT_ARGS + args.spotread_arg
    readings = []
    with SpotreadSession(sargs, nospos=args.nospos) as s:
        s.prepare()
        _white_check(s)
        for i, p in enumerate(pred):
            readings.append(_read_patch(
                s, f"\npatch {i + 1}/{len(pred)} (predicted {tdcolor.to_hex(p)}): Enter to measure ",
                readings[-1] if readings else None))
    got = np.array([xyz_d50_to_srgb(r["xyz"]) for r in readings])
    de = delta_e(pred, got)
    print(f"\n{'patch':>5} {'predicted':>10} {'measured':>10} {'dE':>6}")
    for i, (p, g, d) in enumerate(zip(pred, got, de), 1):
        print(f"{i:5d} {tdcolor.to_hex(p):>10} {tdcolor.to_hex(g):>10} {d:6.1f}")
    print(f"\ndE mean {de.mean():.1f}, max {de.max():.1f}")
    if de.mean() > 5:
        print("  ! above 5: the td/colour data behind this prediction are probably not measured yet")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spotread-arg", action="append", default=[], metavar="ARG",
                    help="extra argument passed through to spotread (repeatable)")
    ap.add_argument("--nospos", action="store_true",
                    help=f"run the patched ArgyllCMS through '{NOSPOS_WRAPPER}' (ColorMunki dial "
                         "check off); adds a white-paper calibration check. Implied when already "
                         "run under the wrapper")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("measure-wedge", help="reflectance of each step of a printed wedge")
    p.add_argument("--steps", type=int, default=12)
    p.add_argument("-o", "--output", help="JSON with readings and spectra")
    p.set_defaults(fn=cmd_measure_wedge)

    p = sub.add_parser("transmission", help="chip transmission using the screen as backlight")
    p.add_argument("--thickness", required=True,
                   help="comma-separated chip thicknesses in mm (measure with calipers)")
    p.add_argument("--settle", type=float, default=1.0, help="seconds after each patch change")
    p.add_argument("-o", "--output")
    p.set_defaults(fn=cmd_transmission)

    p = sub.add_parser("verify-plaque", help="dE of measured patches vs predicted colours")
    p.add_argument("--predicted", required=True, help="comma-separated hex, one per patch")
    p.set_defaults(fn=cmd_verify)

    args = ap.parse_args(argv)
    try:
        args.fn(args)
    except (MunkiError, KeyboardInterrupt) as e:
        raise SystemExit(f"\nmunki: {e}")


if __name__ == "__main__":
    main()

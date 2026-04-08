import subprocess
import curses
import queue
import threading
import time
import os
import argparse
from pathlib import Path
from collections import deque
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
TIMEOUT_SECONDS = 10          # Max seconds to wait for msdos.exe before killing it
CRC_ERROR_STRING = "File CRC error."  # Exact string to watch for in TDCHECK output
MAX_WORKERS = min(8, os.cpu_count() or 4)  # Parallel workers, capped at 8

# Thread-safety for moving timeout folders (parallel workers may share a parent dir)
_timeout_lock    = threading.Lock()
_moved_folders   = set()          # resolved paths of folders already moved to _Timeouts

# Thread-safety for moving successfully converted folders
_converted_lock    = threading.Lock()
_converted_folders = set()        # resolved paths of folders already moved to _Converted


# ---------------------------------------------------------------------------
# Curses TUI
# ---------------------------------------------------------------------------

class CursesUI:
    """
    Full-screen terminal dashboard using curses.

    Layout (top → bottom):
        Title bar
        ─────────────────────────────
        Status  : <current operation>
        Current : <filename>
        Progress: [████░░░░░░] n/total (pct%)
        ETA     : ~Xm Ys  (avg N.Ns/file, N samples)
        ─────────────────────────────
        Total  n   OK        n   Queued     n
        Errors n   CRC Errs  n   Timeouts   n
        Converted  n   Conv Failed  n
        ─────────────────────────────
        Validation Log:      │ Conversion Log:
          … scrolling …      │   … scrolling …
    """

    COLOR_HEADER  = 1
    COLOR_SUCCESS = 2
    COLOR_ERROR   = 3
    COLOR_WARNING = 4
    COLOR_NORMAL  = 5
    COLOR_DIM     = 6
    COLOR_LIVE    = 7  # plain white (no bold) — softer than COLOR_NORMAL

    def __init__(self, stdscr):
        self.stdscr = stdscr
        self._status       = "Initialising…"
        self._flags        = []              # active CLI flags shown in status block
        self._current_file = ""
        self._prog_cur     = 0
        self._prog_total   = 0
        self._log_lines    = deque(maxlen=200)  # (text, color_pair_int) — validation panel
        self._live_queue   = queue.Queue()       # thread-safe feed for conversion panel
        self._live_lines   = deque(maxlen=200)   # display buffer for conversion panel
        self._eta_window   = deque(maxlen=10)    # rolling last-10 file durations (seconds)
        self._live_log     = None                # optional file handle for --log mode
        self.stats = dict(
            total=0, ok=0, crc_errors=0, timeouts=0, errors=0,
            queued=0,
            converted=0, convert_failed=0,
        )

        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(self.COLOR_HEADER,  curses.COLOR_CYAN,   -1)
        curses.init_pair(self.COLOR_SUCCESS, curses.COLOR_GREEN,  -1)
        curses.init_pair(self.COLOR_ERROR,   curses.COLOR_RED,    -1)
        curses.init_pair(self.COLOR_WARNING, curses.COLOR_YELLOW, -1)
        curses.init_pair(self.COLOR_NORMAL,  curses.COLOR_WHITE,  -1)
        curses.init_pair(self.COLOR_DIM,     curses.COLOR_BLACK,  -1)  # bold black = dark grey
        curses.init_pair(self.COLOR_LIVE,    curses.COLOR_WHITE,  -1)  # plain white, no bold

        curses.curs_set(0)
        self.stdscr.nodelay(True)
        self.draw()

    # ── public API ──────────────────────────────────────────────────────────

    def record_eta(self, elapsed_seconds: float):
        """Record one file's elapsed time into the rolling ETA window."""
        if elapsed_seconds > 0:
            self._eta_window.append(elapsed_seconds)

    @staticmethod
    def _fmt_eta(seconds: float) -> str:
        s = int(seconds)
        if s < 60:
            return f"{s}s"
        m, s = divmod(s, 60)
        if m < 60:
            return f"{m}m {s:02d}s"
        h, m = divmod(m, 60)
        return f"{h}h {m:02d}m"

    def set_live_log(self, file_handle):
        """Attach an open file handle to mirror all panel output."""
        self._live_log = file_handle

    def set_flags(self, flags: list):
        """Set the list of active CLI flag strings shown in the status block."""
        self._flags = flags
        self.draw()

    def set_status(self, msg: str):
        self._status = msg
        self.draw()

    def set_current_file(self, fname: str):
        self._current_file = fname
        self.draw()

    def set_progress(self, current: int, total: int):
        self._prog_cur   = current
        self._prog_total = total
        self.draw()

    def update_stats(self, **kwargs):
        self.stats.update(kwargs)
        self.draw()

    def log(self, msg: str, level: str = "normal"):
        """Add a line to the left Validation Log panel."""
        color_map = {
            "success": self.COLOR_SUCCESS,
            "error":   self.COLOR_ERROR,
            "warning": self.COLOR_WARNING,
            "header":  self.COLOR_HEADER,
            "normal":  self.COLOR_NORMAL,
            "dim":     self.COLOR_DIM,
        }
        pair = color_map.get(level, self.COLOR_NORMAL)
        self._log_lines.append((msg, pair))
        if self._live_log:
            self._live_log.write(f"[VALID  ] [{level.upper():<7}] {msg}\n")
            self._live_log.flush()
        self.draw()

    def push_live(self, msg: str, level: str = "live"):
        """Thread-safe: enqueue a line for the right Conversion Log panel."""
        color_map = {
            "success": self.COLOR_SUCCESS,
            "error":   self.COLOR_ERROR,
            "warning": self.COLOR_WARNING,
            "normal":  self.COLOR_NORMAL,
            "dim":     self.COLOR_DIM,
            "live":    self.COLOR_LIVE,
        }
        self._live_queue.put((msg, color_map.get(level, self.COLOR_LIVE)))
        if self._live_log:
            self._live_log.write(f"[CONVERT]   {msg}\n")
            self._live_log.flush()

    def wait_for_key(self, prompt: str = "  Press any key to exit…"):
        h, w = self.stdscr.getmaxyx()
        self.stdscr.nodelay(False)
        try:
            self.stdscr.attron(curses.color_pair(self.COLOR_WARNING) | curses.A_BOLD)
            self.stdscr.addstr(h - 1, 0, prompt[: w - 1])
            self.stdscr.attroff(curses.color_pair(self.COLOR_WARNING) | curses.A_BOLD)
        except curses.error:
            pass
        self.stdscr.refresh()
        self.stdscr.getch()

    # ── internal drawing ─────────────────────────────────────────────────────

    def _safe_addstr(self, row: int, col: int, text: str, attr: int = 0, max_w: int = None):
        h, w = self.stdscr.getmaxyx()
        if row < 0 or row >= h - 1:
            return
        max_len = (max_w if max_w is not None else w) - col - 1
        if max_len <= 0:
            return
        try:
            self.stdscr.addstr(row, col, text[:max_len], attr)
        except curses.error:
            pass

    def _hline(self, row: int):
        h, w = self.stdscr.getmaxyx()
        if 0 <= row < h - 1:
            try:
                self.stdscr.hline(row, 0, curses.ACS_HLINE, w - 1)
            except curses.error:
                pass

    def draw(self):
        try:
            h, w = self.stdscr.getmaxyx()
            self.stdscr.erase()
            row = 0
            col_split = w // 2
            s = self.stats

            # ── Title bar ──────────────────────────────────────────────────
            title = "  TD0 Validator & Converter  "
            attr  = curses.color_pair(self.COLOR_HEADER) | curses.A_BOLD | curses.A_REVERSE
            self._safe_addstr(row, 0, title.center(w - 1), attr)
            row += 1
            self._hline(row); row += 1

            # ── Status / current file ──────────────────────────────────────
            self._safe_addstr(row, 0, f" Status  : {self._status}",
                              curses.color_pair(self.COLOR_NORMAL))
            row += 1
            if self._flags:
                flags_str = "   ".join(self._flags)
                self._safe_addstr(row, 0, f" Flags   : {flags_str}",
                                  curses.color_pair(self.COLOR_WARNING) | curses.A_BOLD)
                row += 1
            cf = self._current_file or "—"
            self._safe_addstr(row, 0, f" Current : {cf}",
                              curses.color_pair(self.COLOR_DIM) | curses.A_BOLD)
            row += 1

            # ── Progress bar ───────────────────────────────────────────────
            if self._prog_total > 0:
                pct    = self._prog_cur / self._prog_total
                bar_w  = max(10, w - 32)
                filled = int(bar_w * pct)
                bar    = "█" * filled + "░" * (bar_w - filled)
                prog_ln = f" [{bar}] {self._prog_cur}/{self._prog_total} ({pct:.0%})"
                bar_colour = (curses.color_pair(self.COLOR_SUCCESS)
                              if pct >= 1.0 else
                              curses.color_pair(self.COLOR_WARNING))
                self._safe_addstr(row, 0, prog_ln, bar_colour)
                row += 1

                # ── ETA line ───────────────────────────────────────────────
                remaining = max(0, s["queued"] - s["total"])
                if self._eta_window and remaining > 0:
                    avg      = sum(self._eta_window) / len(self._eta_window)
                    eta_secs = avg * remaining
                    samples  = len(self._eta_window)
                    eta_str  = self._fmt_eta(eta_secs)
                    eta_ln   = f" ETA ~{eta_str}  (avg {avg:.1f}s/file, {samples} sample{'s' if samples != 1 else ''})"
                    self._safe_addstr(row, 0, eta_ln,
                                      curses.color_pair(self.COLOR_NORMAL) | curses.A_BOLD)
                elif remaining == 0 and s["total"] > 0:
                    self._safe_addstr(row, 0, " ETA —  complete",
                                      curses.color_pair(self.COLOR_DIM) | curses.A_BOLD)
                else:
                    self._safe_addstr(row, 0, " ETA —  calculating…",
                                      curses.color_pair(self.COLOR_DIM) | curses.A_BOLD)
                row += 1
            else:
                self._safe_addstr(row, 0, " [waiting for files…]",
                                  curses.color_pair(self.COLOR_DIM) | curses.A_BOLD)
                row += 1

            self._hline(row); row += 1

            # ── Stats grid ─────────────────────────────────────────────────
            def stat_line(pairs, attr):
                col_w = (w - 2) // 3
                parts = [f"{label}: {val:<{col_w - len(label) - 2}}" for label, val in pairs]
                self._safe_addstr(row, 0, "  " + "".join(parts), attr)

            # Phase 1 — validation counters
            stat_line(
                [("Total", s["total"]), ("OK", s["ok"]),
                 ("Queued", max(0, s["queued"] - s["total"]))],
                curses.color_pair(self.COLOR_NORMAL)
            ); row += 1
            stat_line(
                [("Errors", s["errors"]), ("CRC Errs", s["crc_errors"]),
                 ("Timeouts", s["timeouts"])],
                curses.color_pair(self.COLOR_WARNING)
            ); row += 1

            # Phase 2 — conversion counters
            stat_line(
                [("Converted", s["converted"]), ("Conv Failed", s["convert_failed"]),
                 ("", "")],
                curses.color_pair(self.COLOR_SUCCESS)
            ); row += 1

            self._hline(row)
            # T-junction where the horizontal rule meets the vertical panel divider
            try:
                self.stdscr.addch(row, col_split, curses.ACS_TTEE,
                                  curses.color_pair(self.COLOR_DIM) | curses.A_BOLD)
            except curses.error:
                pass
            row += 1

            # Drain thread-safe live queue into the display buffer
            try:
                while True:
                    self._live_lines.append(self._live_queue.get_nowait())
            except queue.Empty:
                pass

            # ── Two-panel log area ─────────────────────────────────────────
            self._safe_addstr(row, 0, " Validation Log:",
                              curses.color_pair(self.COLOR_HEADER) | curses.A_BOLD,
                              max_w=col_split)
            self._safe_addstr(row, col_split + 1, " Conversion Log:",
                              curses.color_pair(self.COLOR_HEADER) | curses.A_BOLD)
            row += 1

            # Vertical divider
            for r in range(row, h - 1):
                try:
                    self.stdscr.addch(r, col_split, curses.ACS_VLINE,
                                      curses.color_pair(self.COLOR_DIM))
                except curses.error:
                    pass

            # Render both panels
            log_rows_available = h - row - 1
            visible_log  = list(self._log_lines)[-log_rows_available:] if log_rows_available > 0 else []
            visible_live = list(self._live_lines)[-log_rows_available:] if log_rows_available > 0 else []

            for i in range(log_rows_available):
                r = row + i
                if r >= h - 1:
                    break
                if i < len(visible_log):
                    msg, pair = visible_log[i]
                    self._safe_addstr(r, 0, f"  {msg}",
                                      curses.color_pair(pair), max_w=col_split)
                if i < len(visible_live):
                    msg, pair = visible_live[i]
                    self._safe_addstr(r, col_split + 1, f" {msg}",
                                      curses.color_pair(pair))

            self.stdscr.refresh()
        except curses.error:
            pass


# ---------------------------------------------------------------------------
# Processing logic
# ---------------------------------------------------------------------------

def process_file(td0_file, ui=None):
    """
    Run TDCHECK.EXE on a single .td0 file via msdos.exe.
    Returns (file_path_string, status_string).
    Status: "OK", "CRC ERROR", "ERROR (code X)", or "TIMEOUT (>Xs)"
    """
    directory   = td0_file.parent
    filename    = td0_file.stem
    output_file = directory / f"{filename}_tdcheck.txt"

    _t0 = time.monotonic()
    try:
        result = subprocess.run(
            ['msdos.exe', 'TDCHECK.EXE', str(td0_file)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            timeout=TIMEOUT_SECONDS
        )

        with open(output_file, 'w') as f:
            f.write(result.stdout)

        elapsed = time.monotonic() - _t0

        if CRC_ERROR_STRING in result.stdout or CRC_ERROR_STRING in result.stderr:
            crc_output_file = directory / f"{filename}_tdcheck_CRC-Error.txt"
            output_file.rename(crc_output_file)
            return (str(td0_file), "CRC ERROR", elapsed)

        elif result.returncode != 0:
            return (str(td0_file), f"ERROR (code {result.returncode})", elapsed)

        else:
            return (str(td0_file), "OK", elapsed)

    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - _t0

        # Rename the file so --retry mode can locate it later
        timeout_file = directory / f"{filename}_tdcheck_Timeout-Error.td0"
        td0_file.rename(timeout_file)

        # Move the parent folder (and all its contents) into _Timeouts/,
        # unless the parent *is* the current working directory.
        cwd = Path('.').resolve()
        if directory.resolve() != cwd:
            timeouts_root = Path('_Timeouts')
            timeouts_root.mkdir(exist_ok=True)
            with _timeout_lock:
                folder_key = str(directory.resolve())
                if folder_key not in _moved_folders:
                    dest = timeouts_root / directory.name
                    # Avoid name collisions: append a counter if dest already exists
                    if dest.exists():
                        counter = 1
                        while dest.exists():
                            dest = timeouts_root / f"{directory.name}_{counter}"
                            counter += 1
                    for _attempt in range(10):
                        try:
                            directory.rename(dest)
                            _moved_folders.add(folder_key)
                            break
                        except PermissionError:
                            if _attempt < 9:
                                time.sleep(0.3)
                            else:
                                raise

        return (str(td0_file), f"TIMEOUT (>{TIMEOUT_SECONDS}s)", elapsed)


def convert_file(td0_file, ui=None):
    """
    Convert a validated .td0 file to .ima via wteledsk.exe.
    Returns (file_path_string, status_string, output_lines).
    output_lines is a list of non-blank lines from wteledsk.exe stdout+stderr.
    """
    directory = td0_file.parent
    filename  = td0_file.stem
    ima_file  = directory / f"{filename}.ima"

    try:
        td0_relative = os.path.relpath(td0_file)
        ima_relative = os.path.relpath(ima_file)

        result = subprocess.run(
            ['msdos.exe', 'wteledsk.exe', f'.\\{td0_relative}', f'-o.\\{ima_relative}'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            timeout=TIMEOUT_SECONDS
        )

        combined = (result.stdout + "\n" + result.stderr).strip()
        output_lines = [ln.rstrip() for ln in combined.splitlines() if ln.strip()]

        if ima_file.exists():
            # Move the parent folder (and all its contents) into _Converted/,
            # unless the parent *is* the current working directory.
            cwd = Path('.').resolve()
            if directory.resolve() != cwd:
                converted_root = Path('_Converted')
                converted_root.mkdir(exist_ok=True)
                with _converted_lock:
                    folder_key = str(directory.resolve())
                    if folder_key not in _converted_folders:
                        dest = converted_root / directory.name
                        # Avoid name collisions: append a counter if dest already exists
                        if dest.exists():
                            counter = 1
                            while dest.exists():
                                dest = converted_root / f"{directory.name}_{counter}"
                                counter += 1
                        for _attempt in range(10):
                            try:
                                directory.rename(dest)
                                _converted_folders.add(folder_key)
                                break
                            except PermissionError:
                                if _attempt < 9:
                                    time.sleep(0.3)
                                else:
                                    raise
            return (str(td0_file), "CONVERTED", output_lines)
        else:
            return (str(td0_file), f"CONVERT ERROR (code {result.returncode})", output_lines)

    except subprocess.TimeoutExpired:
        return (str(td0_file), f"CONVERT TIMEOUT (>{TIMEOUT_SECONDS}s)", [])


# ---------------------------------------------------------------------------
# Main processing loop — driven by the UI
# ---------------------------------------------------------------------------

def run(ui: CursesUI, retry: bool):
    start_time = datetime.now()

    # ── Executable check ────────────────────────────────────────────────────
    REQUIRED = ['msdos.exe', 'TDCHECK.EXE', 'wteledsk.exe']
    missing  = [exe for exe in REQUIRED if not Path(exe).exists()]
    if missing:
        for exe in missing:
            ui.log(f"Missing executable: {exe}", "error")
        ui.set_status("Aborted — required executables not found.")
        return

    # ── File discovery ───────────────────────────────────────────────────────
    skipped = []  # files bypassed in normal mode due to _tdcheck_Timeout-Error suffix
    if retry:
        td0_files = list(Path('.').rglob('*_tdcheck_Timeout-Error.td0'))
        ui.set_status(f"Retry mode: found {len(td0_files)} timed-out file(s)")
        renamed = []
        for f in td0_files:
            original = f.parent / f.name.replace('_tdcheck_Timeout-Error', '')
            f.rename(original)
            renamed.append(original)
        td0_files = renamed
    else:
        all_td0   = Path('.').rglob('*.td0')
        td0_files = []
        for f in all_td0:
            if '_tdcheck_Timeout-Error' in f.name:
                skipped.append(f)
            else:
                td0_files.append(f)
        if skipped:
            ui.log(f"Skipped {len(skipped)} Timeout-Error file(s) — use --retry to reprocess", "warning")
            for f in skipped:
                ui.log(f"  \u21b7 {f.name}", "dim")

    if not td0_files:
        ui.set_status("No .td0 files found.")
        return

    ui.update_stats(queued=len(td0_files))
    ui.set_progress(0, len(td0_files))
    ui.set_status(f"Phase 1 — Validating {len(td0_files)} file(s) with {MAX_WORKERS} worker(s)…")

    # ── Phase 1: TDCHECK validation ─────────────────────────────────────────
    results    = []
    completed  = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_file, f, ui): f for f in td0_files}

        for future in as_completed(futures):
            path, status, elapsed = future.result()
            results.append((path, status))
            completed += 1

            ui.record_eta(elapsed)
            ui.set_current_file(Path(path).name)
            ui.set_progress(completed, len(td0_files))

            if status == "OK":
                ui.log(f"[OK]        {Path(path).name}", "success")
                ui.update_stats(total=completed, ok=ui.stats["ok"] + 1)
            elif status == "CRC ERROR":
                ui.log(f"[CRC ERR]   {Path(path).name}", "error")
                ui.update_stats(total=completed, crc_errors=ui.stats["crc_errors"] + 1)
            elif status.startswith("TIMEOUT"):
                ui.log(f"[TIMEOUT]   {Path(path).name}", "warning")
                ui.update_stats(total=completed, timeouts=ui.stats["timeouts"] + 1)
            else:
                ui.log(f"[ERROR]     {Path(path).name}  {status}", "error")
                ui.update_stats(total=completed, errors=ui.stats["errors"] + 1)

    # ── Phase 2: .ima conversion ─────────────────────────────────────────────
    ok_files       = [Path(p) for p, s in results if s == "OK"]
    convert_results = []

    if ok_files:
        ui.set_status(f"Phase 2 — Converting {len(ok_files)} validated file(s) to .ima…")
        ui.set_progress(0, len(ok_files))
        completed = 0

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(convert_file, f, ui): f for f in ok_files}

            for future in as_completed(futures):
                path, status, output_lines = future.result()
                convert_results.append((path, status))
                completed += 1

                ui.set_current_file(Path(path).name)
                ui.set_progress(completed, len(ok_files))

                if status == "CONVERTED":
                    ui.push_live(f"[OK]     {Path(path).name}", "success")
                    for ln in output_lines:
                        ui.push_live(f"         {ln}", "live")
                    ui.update_stats(converted=ui.stats["converted"] + 1)
                else:
                    ui.push_live(f"[FAIL]   {Path(path).name}  {status}", "error")
                    for ln in output_lines:
                        ui.push_live(f"         {ln}", "warning")
                    ui.update_stats(convert_failed=ui.stats["convert_failed"] + 1)

                # Force a draw so the live panel drains its queue
                ui.draw()
    else:
        ui.set_status("Phase 2 — No files passed validation, skipping conversion.")

    # ── Phase 3: Remove empty directories ───────────────────────────────────────
    ui.set_status("Phase 3 — Removing empty directories…")
    cwd = Path('.').resolve()
    skip_roots = {'_Converted', '_Timeouts'}
    removed_dirs = 0
    # Walk bottom-up so deepest dirs are tried first
    for dirpath, dirnames, filenames in os.walk('.', topdown=False):
        p = Path(dirpath).resolve()
        if p == cwd:
            continue
        # Never touch the special output folders themselves
        if p.name in skip_roots:
            continue
        try:
            p.rmdir()   # only succeeds if the directory is truly empty
            ui.log(f"[RMDIR]     {Path(dirpath)}", "dim")
            removed_dirs += 1
        except OSError:
            pass  # not empty, or access issue — leave it alone
    if removed_dirs:
        ui.log(f"Removed {removed_dirs} empty director{'y' if removed_dirs == 1 else 'ies'}.", "normal")

    # ── Summary counts ───────────────────────────────────────────────────────
    end_time      = datetime.now()
    duration      = end_time - start_time
    s             = ui.stats
    ok_count      = s["ok"]
    crc_count     = s["crc_errors"]
    timeout_count = s["timeouts"]
    err_count     = s["errors"]
    converted     = s["converted"]
    conv_failed   = s["convert_failed"]

    # ── Report ───────────────────────────────────────────────────────────────
    ui.set_status(f"Writing report…")
    report_path = Path("tdcheck_report.txt")

    with open(report_path, 'w') as f:
        f.write("TDCHECK PROCESSING REPORT\n")
        f.write("=" * 60 + "\n")
        f.write(f"Date/Time      : {start_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Duration       : {duration}\n")
        f.write(f"Workers        : {MAX_WORKERS}\n")
        f.write(f"Mode           : {'Retry Timeouts' if retry else 'Normal'}\n")
        f.write(f"Total Files    : {len(results)}\n")
        f.write(f"Successful     : {ok_count}\n")
        f.write(f"CRC Errors     : {crc_count}\n")
        f.write(f"Timeouts       : {timeout_count} (Timeout-Error, use --retry to reprocess)\n")
        f.write(f"Errors         : {err_count}\n")
        f.write(f"Skipped        : {len(skipped)}\n")
        f.write("-" * 60 + "\n")
        f.write(f".ima Converted : {converted}\n")
        f.write(f".ima Failed    : {conv_failed}\n")
        f.write("=" * 60 + "\n\n")

        if skipped:
            f.write("SKIPPED FILES\n")
            f.write("-" * 60 + "\n")
            for path in sorted(skipped):
                f.write(f"[SKIPPED] {path}\n")
            f.write("\n")

        failed = [(p, s) for p, s in results if s != "OK"]
        if failed:
            f.write("FAILED FILES\n")
            f.write("-" * 60 + "\n")
            for path, status in sorted(failed):
                f.write(f"[{status}] {path}\n")
            f.write("\n")

        conv_failed_list = [(p, s) for p, s in convert_results if s != "CONVERTED"]
        if conv_failed_list:
            f.write("CONVERSION FAILURES\n")
            f.write("-" * 60 + "\n")
            for path, status in sorted(conv_failed_list):
                f.write(f"[{status}] {path}\n")
            f.write("\n")

        f.write("FILE RESULTS\n")
        f.write("-" * 60 + "\n")
        for path, status in sorted(results):
            if status == "OK":
                f.write(f"[{status}] {path}\n")

    ui.set_status(f"Done — {duration}  |  Report: {report_path.resolve()}")
    ui.set_current_file("")
    ui.log(f"Report saved → {report_path}", "normal")

# ───────────────────────────────────────────────
# ARGUMENT PARSING
# Handles command line flags passed to the script
# Usage:
#   Normal run:        python tdcheck.py
#   Retry timeouts:    python tdcheck.py --retry
#   Live Log:          python tdcheck.py --log
# ────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Validate and convert .td0 files via TDCHECK.EXE and wteledsk.exe")
    parser.add_argument(
        '--retry',
        action='store_true',
        help="Retry previously timed-out files (*_tdcheck_Timeout-Error.td0)"
    )
    parser.add_argument(
        '--log', metavar='FILE', default=None,
        help="Mirror both panel streams to a text file in real time"
    )
    args = parser.parse_args()

    # Check executables before entering curses so errors print normally
    REQUIRED = ['msdos.exe', 'TDCHECK.EXE', 'wteledsk.exe']
    missing  = [exe for exe in REQUIRED if not Path(exe).exists()]
    if missing:
        print("ERROR: The following required executables were not found:")
        for exe in missing:
            print(f"  - {exe}")
        print("\nPlease ensure all executables are in the current working directory.")
        exit(1)

    def _curses_main(stdscr):
        ui = CursesUI(stdscr)

        # ── Notify status block of any active CLI flags ──────────────────────
        active_flags = []
        if args.retry:
            active_flags.append("--retry")
        if args.log:
            active_flags.append(f"--log → {args.log}")
        if active_flags:
            ui.set_flags(active_flags)
            for flag in active_flags:
                ui.log(f"Active flag: {flag}", "warning")

        if args.log:
            live_log_handle = open(args.log, 'w', encoding='utf-8', errors='replace')
            live_log_handle.write(f"TD0 Live Log — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            live_log_handle.write("=" * 60 + "\n")
            live_log_handle.flush()
            ui.set_live_log(live_log_handle)
        else:
            live_log_handle = None

        try:
            run(ui, retry=args.retry)
            ui.wait_for_key("  Done — press any key to exit…")
        finally:
            if live_log_handle:
                live_log_handle.write("=" * 60 + "\n")
                live_log_handle.write("Log complete.\n")
                live_log_handle.close()

    curses.wrapper(_curses_main)

    print(f"\nScan complete. Report: tdcheck_report.txt")

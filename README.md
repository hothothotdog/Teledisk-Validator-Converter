# TD0 Validator & Converter

Batch-validates `.td0` floppy disk images using `TDCHECK.EXE` and converts files that passed  to `.ima` format via `wteledsk.exe`, with a full-screen terminal UI.

## External executables


Place these three executables in the **same folder as the script**:

| File           | Purpose                                                |
| -------------- | ------------------------------------------------------ |
| `msdos.exe`    | DOS emulator for running legacy tools                  |
| `TDCHECK.EXE`  | 16-bit Legacy TD0 file checker                         |
| `wteledsk.exe` | 16-bit Legacy TD0 to IMA Converter                     |

---

## Obtaining Prerequisites

Alternatively, you ca skip the individual steps and download the `prerequisitesTD0.7z` from the releases page.

---


### MS-DOS Player (i8086) for Win32-x64

**Provides:** `msdos.exe`

There are many versions to choose from inside the download. I went with the simplest x86 for speed.

|                   |                                                      |
| ----------------- | ---------------------------------------------------- |
| **Download file** | `msdos.7z`                                           |
| **Topic**         | https://takeda-toshiya.my.coocan.jp/msdos/index.html |
| **Download**      | https://takeda-toshiya.my.coocan.jp/msdos/msdos.7z   |

**Extraction steps:**

1. Extract `msdos.7z` with 7-Zip
2. Navigate to `\msdos\binary\i86_x86\`
3. Copy `msdos.exe` to the script folder

---

### TeleDisk v2.23 1995

**Provides:** `TDCHECK.EXE`

|                   |                                                                  |
| ----------------- | -----------------------------------------------------------------|
| **Download file** | `TDISK223.ZIP`                                                   |
| **Topic**         | https://www.andysarcade.net/personal/tech/fluke9100/index.htm    |
| **Download**      | https://www.andysarcade.net/personal/tech/fluke9100/TDISK223.ZIP |

**Extraction steps:**

1. Extract `TDCHECK.EXE` from 'TDISK223.ZIP' with 7-Zip
2. Copy `TDCHECK.EXE` to the script folder

---
### wteledsk v1.01 2002

**Provides:** `wteledsk.exe`

|                   |                                                                     |
| ----------------- | ------------------------------------------------------------------- |
| **Download file** | `wteledsk.exe`                                                      |
| **Topic**         | https://hwiegman.home.xs4all.nl/fileformats/T/teledisk/wteledsk.htm |
| **Download**      | https://github.com/jmechnich/wteledsk/tree/master/dos               |

1. Save `wteledsk.exe` to the script folder

---

Python 3.8+ required. No third-party packages needed.

## Usage

```bash
# Normal run — validate and convert all .td0 files
python TD0-CC.py

# Retry previously timed-out files
python TD0-CC.py --retry

# Mirror both log panels to a text file in real time
python TD0-CC.py --log output.log
```

## Processing Phases

| Phase | Description |
|-------|-------------|
| **1 — Validate** | Runs `TDCHECK.EXE` on every `.td0` file found recursively under the current directory |
| **2 — Convert** | Converts all validated files to `.ima` using `wteledsk.exe` |
| **3 — Cleanup** | Removes any directories left empty after processing |


## Terminal Dashboard

The script runs inside a full-screen curses TUI.

![](<Clipboard-20260408.png>)


## Output Folders

| Folder | Contents |
|--------|----------|
| `_Converted/` | Parent folders of successfully converted files |
| `_Timeouts/` | Parent folders of files that timed out during validation |

Folders are moved atomically. Name collisions are resolved by appending `_1`, `_2`, etc.

## File Outcomes

| Result | What happens |
|--------|-------------|
| **OK** | File proceeds to conversion |
| **CRC Error** | `.txt` output renamed to `*_tdcheck_CRC-Error.txt` |
| **Timeout** | File renamed to `*_tdcheck_Timeout-Error.td0`, parent folder moved to `_Timeouts/` |
| **Converted** | `.ima` file written alongside source, parent folder moved to `_Converted/` |

## Configuration

Edit these constants at the top of the script:

```python
TIMEOUT_SECONDS = 10   # Per-file timeout for msdos.exe
MAX_WORKERS     = min(8, os.cpu_count() or 4)  # Parallel worker threads
```

## Report

A `tdcheck_report.txt` is written to the current directory after every run, summarising counts and listing all failed or skipped files.

## Retry Mode

Files that time out are renamed with a `_tdcheck_Timeout-Error` suffix so they are skipped on subsequent normal runs. Use `--retry` to rename them back and reprocess them.

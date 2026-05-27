# ORF Explorer (Python + Qt)

Desktop version of the ORF file browser, built with PySide6.

## Features

- Native desktop UI with dark theme
- Left folder tree (expandable)
- Main view: toggle between **Table** and **Preview grid**
- Three preview sizes: Small / Big / Large
- Double-click to open full-size preview dialog
- ORF preview extraction (embedded JPEG from Olympus RAW files)
- Works with .orf, .jpg, .jpeg

## Setup (Windows)

1. Run `init_venv.bat` (creates venv and installs PySide6)
2. Run `run_venv.bat` (or `run_venv.bat --root "D:\Photos"`)

## Run manually

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python main.py --root "D:\Photos"
```

## ORF Preview

The app extracts the embedded JPEG preview from `.orf` files using a pure-Python TIFF/IFD parser (supports SubIFD, ExifIFD, and type-13 pointers common in Olympus files). Falls back to JPEG marker scanning if needed.

If a file has no embedded preview, the card shows a placeholder.

## Differences from Go web version

- Native Qt desktop (no browser)
- Faster local interaction
- Same robust ORF extraction logic (improved)
- No web server overhead

## Requirements

- Python 3.10+
- PySide6

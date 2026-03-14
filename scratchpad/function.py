"""
ilovepdf_convert_to_pdf
-----------------------
Upload a local file to iLovePDF and download the converted PDF.

Supported input formats (auto-detected by extension):
    Word   : .doc, .docx  -> word_to_pdf
    Excel  : .xls, .xlsx  -> excel_to_pdf
    PPT    : .ppt, .pptx  -> powerpoint_to_pdf
    Image  : .jpg .jpeg .png .bmp .gif .tiff .tif -> jpg_to_pdf

Usage:
    python3 function.py <input_file> [output_dir]
"""

import re
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# Map file extension -> ilovepdf tool slug and human-readable type label
EXT_TO_TOOL = {
    ".doc":  ("word_to_pdf",        "WORD"),
    ".docx": ("word_to_pdf",        "WORD"),
    ".xls":  ("excel_to_pdf",       "EXCEL"),
    ".xlsx": ("excel_to_pdf",       "EXCEL"),
    ".ppt":  ("powerpoint_to_pdf",  "POWERPOINT"),
    ".pptx": ("powerpoint_to_pdf",  "POWERPOINT"),
    ".jpg":  ("jpg_to_pdf",         "JPG"),
    ".jpeg": ("jpg_to_pdf",         "JPG"),
    ".png":  ("jpg_to_pdf",         "JPG"),
    ".bmp":  ("jpg_to_pdf",         "JPG"),
    ".gif":  ("jpg_to_pdf",         "JPG"),
    ".tiff": ("jpg_to_pdf",         "JPG"),
    ".tif":  ("jpg_to_pdf",         "JPG"),
}

BASE_URL = "https://www.ilovepdf.com"


def convert_to_pdf(input_file: str, output_dir: str = "/tmp") -> str:
    """
    Convert *input_file* to PDF using iLovePDF and save the result to *output_dir*.

    Parameters
    ----------
    input_file : str
        Absolute or relative path to the source file.
    output_dir : str
        Directory where the downloaded PDF will be saved (default: /tmp).

    Returns
    -------
    str
        Absolute path to the downloaded PDF file.

    Raises
    ------
    FileNotFoundError
        If *input_file* does not exist.
    ValueError
        If the file extension is not supported.
    RuntimeError
        If the conversion or download fails.
    """
    input_path = Path(input_file).resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    ext = input_path.suffix.lower()
    entry = EXT_TO_TOOL.get(ext)
    if entry is None:
        raise ValueError(
            f"Unsupported extension '{ext}'. Supported: {sorted(EXT_TO_TOOL)}"
        )
    tool_slug, _ = entry
    tool_url = f"{BASE_URL}/{tool_slug}"

    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[ilovepdf] Converting {input_path.name} -> PDF via {tool_url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        # ── 1. Open the tool page ──────────────────────────────────────────
        page.goto(tool_url, wait_until="networkidle")

        # ── 2. Upload via the hidden <input type="file"> ───────────────────
        # iLovePDF renders a standard file input hidden beneath the drop-zone.
        # Setting files on it directly is more reliable than clicking the link.
        file_input = page.locator('input[type="file"]').first
        file_input.set_input_files(str(input_path))
        print("[ilovepdf] File set, waiting for upload to finish…")

        # Wait until the filename appears in the file-list (confirms upload)
        page.wait_for_selector(
            f'text="{input_path.name}"', timeout=30_000
        )

        # ── 3. Click the Convert button ────────────────────────────────────
        convert_btn = page.get_by_role(
            "button", name=re.compile(r"convert", re.IGNORECASE)
        )
        convert_btn.wait_for(state="visible", timeout=15_000)
        convert_btn.click()
        print("[ilovepdf] Conversion started…")

        # ── 4. Wait for the Download link and trigger download ─────────────
        download_link = page.get_by_role(
            "link", name=re.compile(r"download", re.IGNORECASE)
        )
        try:
            download_link.wait_for(state="visible", timeout=120_000)
        except PWTimeout:
            raise RuntimeError(
                "Download link never appeared – conversion may have failed."
            )

        with page.expect_download(timeout=60_000) as dl_info:
            download_link.click()

        download = dl_info.value
        suggested = download.suggested_filename or (input_path.stem + ".pdf")
        dest = out_dir / suggested
        download.save_as(str(dest))
        print(f"[ilovepdf] Saved -> {dest}")

        context.close()
        browser.close()

    return str(dest)


def convert_to_docx(input_file: str, output_dir: str = "/tmp") -> str:
    """
    Convert a PDF file to DOCX using iLovePDF and save the result to *output_dir*.

    Parameters
    ----------
    input_file : str
        Absolute or relative path to the source PDF file.
    output_dir : str
        Directory where the downloaded DOCX will be saved (default: /tmp).

    Returns
    -------
    str
        Absolute path to the downloaded DOCX file.

    Raises
    ------
    FileNotFoundError
        If *input_file* does not exist.
    ValueError
        If the file is not a PDF.
    RuntimeError
        If the conversion or download fails.
    """
    input_path = Path(input_file).resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    if input_path.suffix.lower() != ".pdf":
        raise ValueError(f"Expected a .pdf file, got '{input_path.suffix}'")

    tool_url = f"{BASE_URL}/pdf_to_word"
    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[ilovepdf] Converting {input_path.name} -> DOCX via {tool_url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        # ── 1. Open the tool page ──────────────────────────────────────────
        page.goto(tool_url, wait_until="networkidle")

        # ── 2. Upload via the hidden <input type="file"> ───────────────────
        file_input = page.locator('input[type="file"]').first
        file_input.set_input_files(str(input_path))
        print("[ilovepdf] File set, waiting for upload to finish…")

        page.wait_for_selector(
            f'text="{input_path.name}"', timeout=30_000
        )

        # ── 3. Click the Convert button ────────────────────────────────────
        convert_btn = page.get_by_role(
            "button", name=re.compile(r"convert", re.IGNORECASE)
        )
        convert_btn.wait_for(state="visible", timeout=15_000)
        convert_btn.click()
        print("[ilovepdf] Conversion started…")

        # ── 4. Wait for the Download link and trigger download ─────────────
        download_link = page.get_by_role(
            "link", name=re.compile(r"download", re.IGNORECASE)
        )
        try:
            download_link.wait_for(state="visible", timeout=120_000)
        except PWTimeout:
            raise RuntimeError(
                "Download link never appeared – conversion may have failed."
            )

        with page.expect_download(timeout=60_000) as dl_info:
            download_link.click()

        download = dl_info.value
        suggested = download.suggested_filename or (input_path.stem + ".docx")
        dest = out_dir / suggested
        download.save_as(str(dest))
        print(f"[ilovepdf] Saved -> {dest}")

        context.close()
        browser.close()

    return str(dest)


# ── CLI entry-point ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python3 function.py <input_file> [output_dir]")
        sys.exit(1)

    src = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else "/tmp"
    result = convert_to_pdf(src, out)
    print(f"Done: {result}")

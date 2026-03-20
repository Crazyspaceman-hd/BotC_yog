"""
ChatGPT Browser Bridge
======================
Automates ChatGPT via Playwright so you can send prompts and receive
responses without copy-pasting.

Usage
-----
# Interactive mode (type prompts at the terminal):
    python chatgpt_bridge.py

# Single prompt via argument:
    python chatgpt_bridge.py "Write a Python function that..."

# Pipe a prompt from a file or another command:
    cat my_prompt.txt | python chatgpt_bridge.py
    echo "Explain this code" | python chatgpt_bridge.py

# Keep the browser open between prompts (faster):
    python chatgpt_bridge.py --keep-open

Setup (one-time)
----------------
    pip install -r requirements_chatgpt.txt
    playwright install chromium

The first run opens a real browser window so you can log in to ChatGPT.
Your session is saved to chatgpt_session/ and reused on future runs.
"""

import argparse
import json
import sys
import time
from pathlib import Path

SESSION_DIR = Path(__file__).parent / "chatgpt_session"
CHATGPT_URL = "https://chatgpt.com/"
RESPONSE_TIMEOUT = 120  # seconds to wait for a complete response


def get_browser_and_context(playwright, headless: bool):
    """Launch Chromium and load (or create) a persistent session."""
    SESSION_DIR.mkdir(exist_ok=True)
    context = playwright.chromium.launch_persistent_context(
        str(SESSION_DIR),
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
        no_viewport=False,
        viewport={"width": 1280, "height": 900},
    )
    return context


def wait_for_response(page) -> str:
    """
    Wait until ChatGPT finishes generating a response and return its text.
    Detects completion by watching the Stop button disappear.
    """
    # Wait for the Stop / regenerate button to appear (generation started)
    try:
        page.wait_for_selector(
            '[data-testid="stop-button"], button[aria-label="Stop generating"]',
            timeout=30_000,
        )
    except Exception:
        pass  # might have been very fast

    # Wait for it to disappear (generation finished)
    deadline = time.time() + RESPONSE_TIMEOUT
    while time.time() < deadline:
        stop_visible = page.query_selector(
            '[data-testid="stop-button"], button[aria-label="Stop generating"]'
        )
        if stop_visible is None:
            break
        time.sleep(0.5)
    else:
        print("[bridge] Warning: timed out waiting for response to complete.", file=sys.stderr)

    # Small settle delay so the DOM is fully written
    time.sleep(0.5)

    # Grab the last assistant message
    messages = page.query_selector_all('[data-message-author-role="assistant"]')
    if not messages:
        return ""
    last = messages[-1]
    return last.inner_text().strip()


def ensure_logged_in(page):
    """If the page shows a login wall, pause and ask the user to log in."""
    page.goto(CHATGPT_URL, wait_until="domcontentloaded")
    time.sleep(2)

    if "auth" in page.url or page.query_selector('button:has-text("Log in")'):
        print(
            "\n[bridge] ChatGPT login required.\n"
            "  → Please log in inside the browser window that just opened.\n"
            "  → Press Enter here when you are logged in and see the chat interface.",
            file=sys.stderr,
        )
        input()
        page.goto(CHATGPT_URL, wait_until="domcontentloaded")
        time.sleep(2)


def send_prompt(page, prompt: str) -> str:
    """Navigate to a fresh chat, send *prompt*, and return the response text."""
    # Start a new chat each time to avoid context bleed
    page.goto(CHATGPT_URL, wait_until="domcontentloaded")
    time.sleep(1)

    # Find the prompt textarea
    textarea = page.wait_for_selector(
        '#prompt-textarea, textarea[placeholder], [contenteditable="true"][data-id]',
        timeout=15_000,
    )

    textarea.click()
    # Use clipboard paste to handle multi-line / special characters safely
    page.evaluate(
        """(text) => {
            const el = document.querySelector(
                '#prompt-textarea, [contenteditable="true"][data-id]'
            );
            if (!el) return;
            // For contenteditable divs
            if (el.tagName !== 'TEXTAREA') {
                el.innerText = text;
                el.dispatchEvent(new InputEvent('input', {bubbles: true}));
            }
        }""",
        prompt,
    )
    # For plain textareas fall back to type
    tag = textarea.evaluate("el => el.tagName")
    if tag == "TEXTAREA":
        textarea.fill(prompt)

    # Submit
    page.keyboard.press("Enter")

    response = wait_for_response(page)
    return response


def run_interactive(page):
    """REPL: read prompt from stdin, print response, repeat."""
    print("[bridge] Interactive mode. Type your prompt and press Enter twice (blank line) to send.")
    print("[bridge] Ctrl+C or type 'exit' to quit.\n")
    while True:
        lines = []
        try:
            while True:
                line = input()
                if line.lower() == "exit":
                    return
                if line == "" and lines:
                    break
                lines.append(line)
        except EOFError:
            break

        prompt = "\n".join(lines).strip()
        if not prompt:
            continue

        print("\n[bridge] Sending to ChatGPT…", file=sys.stderr)
        response = send_prompt(page, prompt)
        print("\n" + response + "\n")


def main():
    parser = argparse.ArgumentParser(description="ChatGPT browser bridge")
    parser.add_argument("prompt", nargs="?", help="Prompt to send (optional)")
    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="Keep the browser open for multiple prompts (interactive mode)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode (only works after session is saved)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=RESPONSE_TIMEOUT,
        help=f"Seconds to wait for a response (default: {RESPONSE_TIMEOUT})",
    )
    args = parser.parse_args()

    global RESPONSE_TIMEOUT
    RESPONSE_TIMEOUT = args.timeout

    # Read prompt from stdin if piped
    stdin_prompt = None
    if not sys.stdin.isatty():
        stdin_prompt = sys.stdin.read().strip()

    prompt = args.prompt or stdin_prompt

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "[bridge] Playwright not installed.\n"
            "  Run:  pip install -r requirements_chatgpt.txt && playwright install chromium",
            file=sys.stderr,
        )
        sys.exit(1)

    with sync_playwright() as pw:
        headless = args.headless and SESSION_DIR.exists()
        context = get_browser_and_context(pw, headless=headless)
        page = context.pages[0] if context.pages else context.new_page()

        try:
            ensure_logged_in(page)

            if prompt:
                print("[bridge] Sending to ChatGPT…", file=sys.stderr)
                response = send_prompt(page, prompt)
                print(response)
            else:
                run_interactive(page)
        finally:
            context.close()


if __name__ == "__main__":
    main()

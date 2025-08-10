import asyncio
import os
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TimeElapsedColumn, TextColumn
from rich.table import Table
from tenacity import retry, stop_after_delay, wait_fixed
from fingerprints import random_profile
import contextlib
import subprocess
import shutil
import stat

console = Console()

MAX_WAIT = 8000  # ms, per step
POLL_MS = 100  # element polling interval
MAX_FIND_SEC = 60  # per element
HEADLESS = True
INSTANCES = 3  # default; will be overridden by user prompt
VERBOSE = False  # suppress all prints except the live dashboard


@dataclass
class StepResult:
    step: str
    status: str
    detail: str = ""


@dataclass
class InstanceState:
    id: int
    runs: int = 0
    successes: int = 0
    failures: int = 0
    current_step: str = "idle"
    last_url: str = ""
    last_detail: str = ""
    status: str = "idle"
    started_at: float = field(default_factory=time.time)
    last_duration: float = 0.0


def nice_table(title: str, rows: list[tuple[str, str]]):
    table = Table(title=title, show_header=True, header_style="bold magenta")
    table.add_column("Key", style="cyan", no_wrap=True)
    table.add_column("Value", style="white")
    for k, v in rows:
        table.add_row(k, v)
    console.print(table)


async def hard_click(page, selector: str, description: str, many_selectors: list[str] | None = None, double: bool = False) -> None:
    """Hard, real-like click with many strategies and live logs.

    - Poll every 100ms up to 60s using multiple selectors
    - Scroll into view, focus, mouse move, down/up, force click
    - JS click + dispatch pointer events as fallback
    - Optional double click for stubborn buttons
    """
    selectors = [selector] + (many_selectors or [])
    deadline = time.time() + MAX_FIND_SEC
    last_err: Optional[Exception] = None
    last_log = 0.0

    while time.time() < deadline:
        for sel in selectors:
            now = time.time()
            if VERBOSE and now - last_log > 1.0:
                console.print(f"[yellow]Searching[/yellow] for [cyan]{description}[/cyan] using selector: [white]{sel}[/white] t={now:.0f}")
                last_log = now
            try:
                el = await page.wait_for_selector(sel, timeout=POLL_MS, state="visible")
                if not el:
                    continue
                try:
                    await el.scroll_into_view_if_needed()
                except Exception:
                    pass
                # Try element.click with different forces
                for force in (True, False):
                    try:
                        await el.click(force=force, timeout=MAX_WAIT)
                        if VERBOSE:
                            console.print(f"[green]Clicked[/green] {description} via element.click(force={force})")
                        return
                    except Exception as e:
                        last_err = e
                # Try double click if requested
                if double:
                    try:
                        await el.dblclick(timeout=MAX_WAIT)
                        if VERBOSE:
                            console.print(f"[green]Double-clicked[/green] {description} via element.dblclick()")
                        return
                    except Exception as e:
                        last_err = e
                # Try mouse interaction at element center with human-like behavior
                try:
                    box = await el.bounding_box()
                except Exception:
                    box = None
                if box:
                    try:
                        # Add small random offset for more human-like clicking
                        x = box["x"] + box["width"] * (0.3 + random.random() * 0.4)
                        y = box["y"] + box["height"] * (0.3 + random.random() * 0.4)
                        
                        # Human-like mouse movement and click timing
                        await page.mouse.move(x, y)
                        await asyncio.sleep(random.uniform(0.05, 0.15))  # Brief pause
                        await page.mouse.down()
                        await asyncio.sleep(random.uniform(0.05, 0.12))  # Hold time
                        await page.mouse.up()
                        
                        if VERBOSE:
                            console.print(f"[green]Human-like mouse click[/green] on {description} at ({x:.0f},{y:.0f})")
                        return
                    except Exception as e:
                        last_err = e
                # Try page.click selector directly
                try:
                    await page.click(sel, timeout=MAX_WAIT, force=True)
                    if VERBOSE:
                        console.print(f"[green]Clicked[/green] {description} via page.click(force=True)")
                    return
                except Exception as e:
                    last_err = e
                # Try JS click and dispatch events
                try:
                    await page.evaluate(
                        "el => { el.dispatchEvent(new MouseEvent('mousedown',{bubbles:true})); el.dispatchEvent(new MouseEvent('mouseup',{bubbles:true})); el.click(); }",
                        el,
                    )
                    if VERBOSE:
                        console.print(f"[green]Clicked[/green] {description} via JS dispatch")
                    return
                except Exception as e:
                    last_err = e
            except PlaywrightTimeout as e:
                last_err = e
                await asyncio.sleep(POLL_MS / 1000)
                continue
            except Exception as e:
                last_err = e
                await asyncio.sleep(POLL_MS / 1000)
                continue
        await asyncio.sleep(POLL_MS / 1000)
    raise last_err or RuntimeError(f"Element not found/clickable: {description}")


async def wait_for_load(page, max_ms: int = MAX_WAIT):
    # Wait for network mostly idle but cap by max_ms
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=max_ms)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=500)
    except Exception:
        pass

async def wait_for_any_visible(page, selectors: list[str], max_seconds: int = MAX_FIND_SEC) -> Optional[str]:
    """Poll multiple selectors every POLL_MS until one becomes visible; return that selector or None."""
    deadline = time.time() + max_seconds
    while time.time() < deadline:
        for sel in selectors:
            try:
                el = await page.wait_for_selector(sel, timeout=POLL_MS, state="visible")
                if el:
                    return sel
            except PlaywrightTimeout:
                pass
            except Exception:
                pass
        await asyncio.sleep(POLL_MS / 1000)
    return None


def _on_rm_error(func, path, exc_info):
    # On Windows, remove read-only flag and retry
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


def wipe_dir(path: Path, attempts: int = 3, delay: float = 0.3):
    for _ in range(attempts):
        if not path.exists():
            return
        try:
            shutil.rmtree(path, onerror=_on_rm_error)
            return
        except Exception:
            time.sleep(delay)
    # Fallback: best-effort empty contents
    if path.exists():
        for p in sorted(path.rglob('*'), reverse=True):
            try:
                if p.is_file() or p.is_symlink():
                    os.chmod(p, stat.S_IWRITE)
                    p.unlink(missing_ok=True)
                elif p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
            except Exception:
                pass

def read_random_url(file_path: str) -> str:
    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"Input file not found: {file_path}")
    lines = [ln.strip() for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()]
    if not lines:
        raise ValueError("No URLs found in the file")
    return random.choice(lines)


async def read_new_page(page, state: InstanceState, results: list) -> None:
    """Read and scroll a new page after clicking a link, but don't click any more links."""
    try:
        # Wait for the new page to fully load
        await wait_for_load(page)
        
        # Human-like pause to process the new page
        state.current_step = "processing new page"
        await asyncio.sleep(random.uniform(1.5, 3.0))
        
        # Get new page dimensions
        page_height = await page.evaluate("document.body.scrollHeight")
        viewport_height = await page.evaluate("window.innerHeight")
        
        if page_height > viewport_height:
            # Calculate how much to read (50-90% since we already read one page)
            target_percentage = random.uniform(0.50, 0.90)
            target_scroll_position = page_height * target_percentage
            
            state.current_step = f"reading new page (target: {target_percentage*100:.0f}%)"
            
            current_position = 0
            scroll_sessions = random.randint(5, 12)  # Fewer sessions for second page
            
            for session in range(scroll_sessions):
                if current_position >= target_scroll_position:
                    break
                
                # Human-like scroll behavior for new page
                if random.random() < 0.8:  # 80% chance for normal scroll down
                    scroll_amount = random.uniform(150, 500)
                    direction = 1  # down
                else:  # 20% chance for small scroll up (re-reading)
                    scroll_amount = random.uniform(50, 200)
                    direction = -1  # up
                
                new_position = max(0, min(page_height, current_position + (scroll_amount * direction)))
                
                # Smooth scroll
                await page.evaluate(f"window.scrollTo({{top: {new_position}, behavior: 'smooth'}})")
                current_position = new_position
                
                # Reading time for new page (slightly faster since user is more focused)
                reading_time = random.uniform(1.0, 3.5)
                if session % 4 == 0:  # Occasional longer pauses
                    reading_time = random.uniform(2.5, 6.0)
                
                state.current_step = f"reading new page content (session {session+1}/{scroll_sessions})"
                await asyncio.sleep(reading_time)
            
            final_position = await page.evaluate("window.pageYOffset")
            final_percentage = (final_position / page_height) * 100 if page_height > 0 else 0
            results.append(StepResult("New Page Reading", "OK", f"Read {final_percentage:.1f}% of new page"))
        else:
            # New page is short, read for a moderate amount of time
            reading_time = random.uniform(8.0, 20.0)
            state.current_step = f"reading short new page ({reading_time:.1f}s)"
            await asyncio.sleep(reading_time)
            results.append(StepResult("New Page Reading", "OK", "Read short new page content"))
            
        # Final pause before closing
        state.current_step = "finishing reading new page"
        await asyncio.sleep(random.uniform(1.0, 2.5))
        
    except Exception as e:
        # If anything fails, just continue
        results.append(StepResult("New Page Reading", "PARTIAL", f"Error reading new page: {str(e)[:50]}"))


async def run_once(url: str, instance_id: int, browser, state: InstanceState) -> list[StepResult]:
    results: list[StepResult] = []
    start_time = time.time()
    context = None
    page = None
    try:
        fp = random_profile()
        context = await browser.new_context(
            user_agent=fp["user_agent"],
            viewport=fp["viewport"],
            device_scale_factor=fp["device_scale_factor"],
            is_mobile=fp["is_mobile"],
            has_touch=fp["has_touch"],
            locale=fp["locale"],
            timezone_id=fp["timezone_id"],
            color_scheme=fp["color_scheme"],
            reduced_motion=fp["reduced_motion"],
            extra_http_headers=fp["headers"],
            java_script_enabled=True,
            ignore_https_errors=True,
            permissions=['geolocation', 'notifications'],
            geolocation={
                "latitude": random.uniform(25.0, 49.0), 
                "longitude": random.uniform(-125.0, -66.0)
            }
        )
        
        # Inject comprehensive stealth script on every page
        await context.add_init_script(fp["stealth_script"])
        
        # Add enhanced human-like behavior simulation
        await context.add_init_script(f"""
            // Enhanced realistic mouse movement simulation
            let mouseX = {random.randint(100, 800)};
            let mouseY = {random.randint(100, 600)};
            let lastMouseTime = Date.now();
            
            function simulateHumanMouse() {{
                const now = Date.now();
                const timeDelta = now - lastMouseTime;
                lastMouseTime = now;
                
                // More realistic movement based on time
                const maxDelta = Math.min(50, timeDelta / 20);
                const deltaX = (Math.random() - 0.5) * maxDelta;
                const deltaY = (Math.random() - 0.5) * maxDelta;
                mouseX = Math.max(0, Math.min(window.innerWidth, mouseX + deltaX));
                mouseY = Math.max(0, Math.min(window.innerHeight, mouseY + deltaY));
                
                const moveEvent = new MouseEvent('mousemove', {{
                    clientX: mouseX,
                    clientY: mouseY,
                    bubbles: true
                }});
                document.dispatchEvent(moveEvent);
            }}
            
            // Random mouse movements with varying intervals
            setInterval(simulateHumanMouse, {random.randint(800, 2500)});
            
            // Simulate human-like reading pauses
            let readingPauses = 0;
            function simulateReadingBehavior() {{
                // Sometimes pause to "read"
                if (Math.random() < 0.15) {{ // 15% chance to pause
                    readingPauses++;
                    // Longer pause for reading
                    setTimeout(() => {{
                        console.log('📖 Reading pause completed');
                    }}, {random.randint(2000, 8000)});
                }}
            }}
            
            setInterval(simulateReadingBehavior, {random.randint(5000, 15000)});
            
            console.log('🤖 Enhanced human behavior simulation active');
        """)
        
        page = await context.new_page()
        
        # Block images, fonts, and media to save bandwidth
        await page.route("**/*", lambda route: (
            route.abort() if route.request.resource_type in [
                "image", "font", "media", "stylesheet", 
                "manifest", "other"
            ] or any(ext in route.request.url.lower() for ext in [
                ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp", ".tiff",
                ".woff", ".woff2", ".ttf", ".otf", ".eot",
                ".mp4", ".mp3", ".avi", ".mov", ".wmv", ".flv", ".webm", ".ogg",
                ".css", ".less", ".sass", ".scss"
            ]) else route.continue_()
        ))
        
        # Additional page-level stealth enhancements
        await page.evaluate("""
            // Override common automation detection points
            delete window.navigator.webdriver;
            
            // Safely handle chrome.runtime
            if (window.chrome && window.chrome.runtime) {
                delete window.chrome.runtime.onConnect;
            }
            
            // Simulate realistic timing with more variation
            const originalSetTimeout = window.setTimeout;
            window.setTimeout = function(fn, delay, ...args) {
                const variation = Math.random() * 100 - 50; // ±50ms variation
                const humanDelay = Math.max(0, delay + variation);
                return originalSetTimeout(fn, humanDelay, ...args);
            };
        """)

        # Open URL and wait for full load like a human would
        state.current_step = "opening url"
        await page.goto(url, wait_until="domcontentloaded", timeout=MAX_WAIT)
        await wait_for_load(page)
        
        # Human-like initial page load waiting
        initial_wait = random.uniform(2.0, 5.0)
        state.current_step = f"initial load wait ({initial_wait:.1f}s)"
        await asyncio.sleep(initial_wait)
        results.append(StepResult("Open URL", "OK", url))

        # Get page dimensions for scrolling calculations
        page_height = await page.evaluate("document.body.scrollHeight")
        viewport_height = await page.evaluate("window.innerHeight")
        
        if page_height > viewport_height:
            # Calculate target scroll percentage (70-100%)
            target_percentage = random.uniform(0.70, 1.0)
            target_scroll_position = page_height * target_percentage
            
            state.current_step = f"human reading & scrolling (target: {target_percentage*100:.0f}%)"
            
            current_position = 0
            scroll_sessions = random.randint(8, 20)  # Multiple scroll sessions like a human
            
            # Track time for the 20-30 second rule
            start_reading_time = time.time()
            link_click_time = start_reading_time + random.uniform(20.0, 30.0)  # 20-30 seconds later
            link_clicked = False
            
            for session in range(scroll_sessions):
                if current_position >= target_scroll_position:
                    break
                
                # Human-like scroll behavior - varies by session
                if random.random() < 0.7:  # 70% chance for normal scroll down
                    scroll_amount = random.uniform(100, 400)
                    direction = 1  # down
                elif random.random() < 0.8:  # 20% chance for small scroll up (re-reading)
                    scroll_amount = random.uniform(50, 150)
                    direction = -1  # up
                else:  # 10% chance for big jump (impatient user)
                    scroll_amount = random.uniform(300, 800)
                    direction = 1  # down
                
                new_position = max(0, min(page_height, current_position + (scroll_amount * direction)))
                
                # Smooth scroll like a human
                await page.evaluate(f"window.scrollTo({{top: {new_position}, behavior: 'smooth'}})")
                current_position = new_position
                
                # Human reading/scanning time between scrolls
                reading_time = random.uniform(0.8, 4.5)
                if session % 3 == 0:  # Longer pauses occasionally (deeper reading)
                    reading_time = random.uniform(3.0, 8.0)
                
                state.current_step = f"reading content (session {session+1}/{scroll_sessions})"
                await asyncio.sleep(reading_time)
                
                # Check if it's time to click a link (after 20-30 seconds)
                current_time = time.time()
                if not link_clicked and current_time >= link_click_time:
                    state.current_step = "looking for financecity.me links to click"
                    try:
                        # Find all links containing financecity.me
                        finance_links = await page.query_selector_all('a[href*="financecity.me"]')
                        if finance_links:
                            # Pick a random link from available ones
                            link_to_click = random.choice(finance_links)
                            
                            # Human-like link clicking behavior
                            box = await link_to_click.bounding_box()
                            if box:
                                # Scroll link into view first
                                await link_to_click.scroll_into_view_if_needed()
                                await asyncio.sleep(random.uniform(0.5, 1.5))  # Pause to "notice" the link
                                
                                # Add random offset for human-like clicking
                                x = box["x"] + box["width"] * (0.2 + random.random() * 0.6)
                                y = box["y"] + box["height"] * (0.2 + random.random() * 0.6)
                                
                                # Human-like mouse movement and click
                                await page.mouse.move(x, y)
                                await asyncio.sleep(random.uniform(0.3, 0.8))  # Hover time
                                await page.mouse.down()
                                await asyncio.sleep(random.uniform(0.08, 0.2))  # Click duration
                                await page.mouse.up()
                                
                                state.current_step = "clicked financecity.me link - waiting for new page"
                                results.append(StepResult("Clicked FinanceCity Link", "OK", "Human-like click behavior"))
                                
                                # Wait for the new page to load
                                await asyncio.sleep(random.uniform(2.0, 4.0))  # Wait for page transition
                                await wait_for_load(page)
                                
                                # Read and scroll the new page (but don't click any links)
                                await read_new_page(page, state, results)
                                
                                link_clicked = True
                                break  # Exit the scrolling loop after clicking and reading new page
                        else:
                            state.current_step = "no financecity.me links found - continuing reading"
                            link_clicked = True  # Mark as clicked so we don't keep looking
                    except Exception as e:
                        # Silently continue if link clicking fails
                        state.current_step = "link click failed - continuing reading"
                        link_clicked = True
            
            if not link_clicked:
                # If we didn't click a link, finish reading the current page
                final_position = await page.evaluate("window.pageYOffset")
                final_percentage = (final_position / page_height) * 100 if page_height > 0 else 0
                results.append(StepResult("Human Reading", "OK", f"Scrolled to {final_percentage:.1f}% of page"))
        else:
            # Page is short, just read for a while and then try to click a link
            reading_time = random.uniform(20.0, 30.0)  # Read for 20-30 seconds
            state.current_step = f"reading short page ({reading_time:.1f}s)"
            await asyncio.sleep(reading_time)
            
            # Try to click a financecity.me link after reading
            state.current_step = "looking for financecity.me links to click"
            try:
                finance_links = await page.query_selector_all('a[href*="financecity.me"]')
                if finance_links:
                    link_to_click = random.choice(finance_links)
                    box = await link_to_click.bounding_box()
                    if box:
                        await link_to_click.scroll_into_view_if_needed()
                        await asyncio.sleep(random.uniform(0.5, 1.5))
                        
                        x = box["x"] + box["width"] * (0.2 + random.random() * 0.6)
                        y = box["y"] + box["height"] * (0.2 + random.random() * 0.6)
                        
                        await page.mouse.move(x, y)
                        await asyncio.sleep(random.uniform(0.3, 0.8))
                        await page.mouse.down()
                        await asyncio.sleep(random.uniform(0.08, 0.2))
                        await page.mouse.up()
                        
                        state.current_step = "clicked financecity.me link - waiting for new page"
                        results.append(StepResult("Clicked FinanceCity Link", "OK", "Human-like click behavior"))
                        
                        await asyncio.sleep(random.uniform(2.0, 4.0))
                        await wait_for_load(page)
                        await read_new_page(page, state, results)
                else:
                    results.append(StepResult("Human Reading", "OK", "Read short page content - no financecity.me links found"))
            except Exception:
                results.append(StepResult("Human Reading", "OK", "Read short page content"))

        # Final human-like behavior before closing
        state.current_step = "finishing reading"
        final_pause = random.uniform(1.0, 3.0)
        await asyncio.sleep(final_pause)
        
        state.current_step = "closing page"
        results.append(StepResult("Reading Complete", "OK", "Human-like blog reading completed"))
        return results
        
    finally:
        with contextlib.suppress(Exception):
            if context is not None:
                await context.close()
        elapsed = time.time() - start_time
        results.append(StepResult("Done", "OK", f"Elapsed: {elapsed:.1f}s"))


async def main():
    file_path = r"urls.txt"
    try:
        urls_list = [ln.strip() for ln in Path(file_path).read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()]
    except Exception as e:
        console.print(f"[bold red]Failed to read URL file:[/bold red] {e}")
        sys.exit(1)
    if not urls_list:
        console.print("[bold red]No URLs found in the file[/bold red]")
        sys.exit(1)

    # Ask for number of instances (1-50)
    try:
        user_in = console.input("[bold cyan]How many instances to run (1-50)? [/bold cyan]")
        n = int(user_in.strip())
        if n < 1 or n > 50:
            raise ValueError
        global INSTANCES
        INSTANCES = n
    except Exception:
        INSTANCES = 1

    states: list[InstanceState] = [InstanceState(id=i+1) for i in range(INSTANCES)]
    total_done = 0
    browser = None

    def pick_url() -> str:
        return random.choice(urls_list)

    def make_dashboard() -> Table:
        table = Table(title="Human-like Blog Reader Dashboard", header_style="bold magenta")
        table.add_column("Inst", style="cyan", no_wrap=True)
        table.add_column("Status", style="white")
        table.add_column("Current Step", style="yellow")
        table.add_column("Runs", style="white")
        table.add_column("OK", style="green")
        table.add_column("Fail", style="red")
        table.add_column("Last URL", style="bright_blue")
        table.add_column("Last Detail", style="white")
        for s in states:
            table.add_row(
                str(s.id), s.status, s.current_step, str(s.runs), str(s.successes), str(s.failures), s.last_url[:40] + ("…" if len(s.last_url) > 40 else ""), s.last_detail
            )
        # Totals row
        table.add_row(
            "Totals",
            "",
            "",
            str(sum(s.runs for s in states)),
            str(sum(s.successes for s in states)),
            str(sum(s.failures for s in states)),
            "Ongoing: " + str(sum(1 for s in states if s.status == 'running')),
            "",
        )
        return table

    async def runner_task(state: InstanceState):
        nonlocal total_done
        # Add small delay between instance starts to avoid connection conflicts
        await asyncio.sleep(state.id * 0.5)
        
        while True:
            state.status = "running"
            state.current_step = "pick url"
            url = pick_url()
            state.last_url = url
            state.started_at = time.time()
            try:
                results = await run_once(url, state.id, browser, state)
                state.runs += 1
                state.successes += 1
                total_done += 1
                state.last_detail = results[-1].detail if results else ""
                state.status = "idle"
                state.current_step = "sleep 1s"
                await asyncio.sleep(1)
            except Exception as e:
                state.runs += 1
                state.failures += 1
                state.status = "error/restarting"
                state.last_detail = str(e)[:100]  # Truncate error message
                state.current_step = "error recovery"
                # Wait longer on error to avoid rapid retries
                await asyncio.sleep(2.0)

    # Initialize single shared browser instance
    async with async_playwright() as p:
        try:
            console.print("[yellow]Launching browser...[/yellow]")
            browser = await p.chromium.launch(
                headless=HEADLESS, 
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-extensions",
                    "--disable-plugins",
                    "--disable-javascript-harmony-shipping",
                    "--disable-background-timer-throttling",
                    "--disable-renderer-backgrounding",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-ipc-flooding-protection",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--no-pings",
                    "--password-store=basic",
                    "--use-mock-keychain",
                    "--disable-component-extensions-with-background-pages",
                    "--disable-default-apps",
                    "--mute-audio",
                    "--no-zygote",
                    "--disable-background-networking",
                    "--disable-web-security",
                    "--disable-features=TranslateUI,BlinkGenPropertyTrees",
                    "--hide-scrollbars",
                    "--disable-gpu"
                ]
            )
            console.print("[green]Browser launched successfully![/green]")
        except Exception as e:
            console.print(f"[yellow]Installing Playwright browsers...[/yellow]")
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)
            browser = await p.chromium.launch(
                headless=HEADLESS, 
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-extensions",
                    "--disable-plugins",
                    "--disable-javascript-harmony-shipping",
                    "--disable-background-timer-throttling",
                    "--disable-renderer-backgrounding",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-ipc-flooding-protection",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--no-pings",
                    "--password-store=basic",
                    "--use-mock-keychain",
                    "--disable-component-extensions-with-background-pages",
                    "--disable-default-apps",
                    "--mute-audio",
                    "--no-zygote",
                    "--disable-background-networking",
                    "--disable-web-security",
                    "--disable-features=TranslateUI,BlinkGenPropertyTrees",
                    "--hide-scrollbars",
                    "--disable-gpu"
                ]
            )

        # Start runners
        runners = [asyncio.create_task(runner_task(s)) for s in states]

        # Live dashboard loop only (no extra prints)
        from rich.live import Live
        try:
            with Live(make_dashboard(), console=console, refresh_per_second=4) as live:
                while True:
                    live.update(make_dashboard(), refresh=True)
                    await asyncio.sleep(0.25)
        except (KeyboardInterrupt, SystemExit):
            # Graceful shutdown: cancel runners
            for t in runners:
                t.cancel()
        finally:
            with contextlib.suppress(Exception):
                await asyncio.gather(*runners, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Graceful shutdown without stack traces
        pass

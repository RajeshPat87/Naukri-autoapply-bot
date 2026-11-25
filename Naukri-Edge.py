"""
Naukri Auto-Apply Bot - Microsoft Edge Version (Updated for Naukri.com 2026)
================================================================================
Automates job applications on Naukri.com using Selenium with Edge browser.

- Only applications CONFIRMED by Naukri are counted as "applied".
- "Apply on company site" jobs are skipped (nothing is submitted on Naukri).
- Recruiter questionnaires (chatbot) are answered from application_answers.csv.
- Every result is written immediately to naukri_results.csv.
- Questions the bot could not answer go to unanswered_questions.csv.

Browser modes:
    - REMOTE_DRIVER_URL set -> Windows Edge via msedgedriver.exe running on Windows
    - otherwise             -> Linux Edge inside WSL

Usage:
    1. Fill in .env and application_answers.csv
    2. (Windows Edge) on Windows: C:\\tools\\msedgedriver.exe --port=9515
    3. python Naukri-Edge.py
"""

import os
import re
import csv
import sys
import time
import logging
from datetime import datetime
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException, WebDriverException, SessionNotCreatedException,
)
from bs4 import BeautifulSoup
from edge_driver_check import DriverVersionMismatch, format_mismatch_help

# Try to use webdriver-manager for automatic driver management
try:
    from webdriver_manager.microsoft import EdgeChromiumDriverManager
    WEBDRIVER_MANAGER_AVAILABLE = True
except ImportError:
    WEBDRIVER_MANAGER_AVAILABLE = False

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------
load_dotenv()

# --- Naukri Credentials ---
NAUKRI_EMAIL = os.getenv('NAUKRI_EMAIL', '')
NAUKRI_PASSWORD = os.getenv('NAUKRI_PASSWORD', '')

# --- Personal Details ---
FIRSTNAME = os.getenv('FIRSTNAME', '')
LASTNAME = os.getenv('LASTNAME', '')

# --- Job Search ---
KEYWORDS = [kw.strip() for kw in os.getenv('KEYWORDS', '').split(',') if kw.strip()]
LOCATION = os.getenv('LOCATION', '').strip()

# --- Limits ---
MAX_APPLICATIONS = int(os.getenv('MAX_APPLICATIONS', '50'))
PAGES_PER_KEYWORD = int(os.getenv('PAGES_PER_KEYWORD', '2'))
MAX_CHAT_QUESTIONS = int(os.getenv('MAX_CHAT_QUESTIONS', '20'))

# --- Browser Options ---
REMOTE_DRIVER_URL = os.getenv('REMOTE_DRIVER_URL', '').strip()  # e.g. http://localhost:9515 (Windows Edge)
EDGE_DRIVER_PATH = os.getenv('EDGE_DRIVER_PATH', '')            # optional, only if NOT using webdriver-manager
EDGE_BINARY = os.getenv('EDGE_BINARY', '')                      # optional, e.g. /usr/bin/microsoft-edge
HEADLESS = os.getenv('HEADLESS', 'false').strip().lower() in ('1', 'true', 'yes')

# --- Files ---
ANSWERS_FILE = os.getenv('ANSWERS_FILE', 'application_answers.csv')
RESULTS_FILE = os.getenv('RESULTS_FILE', 'naukri_results.csv')
UNANSWERED_FILE = os.getenv('UNANSWERED_FILE', 'unanswered_questions.csv')

# --- Apply result detection ---
SUCCESS_TEXTS = [
    "you have successfully applied",
    "applied successfully",
    "application sent",
    "successfully applied",
]
RESULT_KEYS = ("applied", "already", "external", "manual", "unconfirmed", "not_found", "error")

# --- Questionnaire (chatbot) selectors ---
CHATBOT_SELECTOR = (
    "[class*='chatbot_Drawer'], "
    "[class*='chatbot_DrawerContentWrapper'], "
    "[class*='chatBotContainer']"
)
BOT_MSG_SELECTORS = ["li.botItem .botMsg", "[class*='botMsg']", "[class*='botItem']"]
TEXT_INPUT_SELECTORS = [
    "div.textArea[contenteditable='true']",
    "[contenteditable='true']",
    "textarea",
    "input[type='text']",
    "input[type='number']",
]
SEND_BUTTON_SELECTORS = ["div.sendMsg", "[class*='sendMsg']", "[class*='sendBtn']", "button[class*='send']"]
CHIP_SELECTORS = ["[class*='chatbot_Chip']", "[class*='chipItem']", "[class*='chip-item']"]

# Loaded at startup from ANSWERS_FILE: list of (keyword, answer), file order = priority
ANSWERS = []
_unanswered_seen = None

# ------------------------------------------------------------
# Logging Setup
# ------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


def validate_config():
    """Check that required configuration values are set."""
    errors = []
    if not NAUKRI_EMAIL:
        errors.append("NAUKRI_EMAIL is not set in .env")
    if not NAUKRI_PASSWORD:
        errors.append("NAUKRI_PASSWORD is not set in .env")
    if not KEYWORDS:
        errors.append("KEYWORDS is not set in .env (comma-separated job roles)")
    if not FIRSTNAME:
        errors.append("FIRSTNAME is not set in .env")
    if not LASTNAME:
        errors.append("LASTNAME is not set in .env")
    if errors:
        for e in errors:
            logger.error(e)
        logger.error("Please copy .env.example to .env and fill in your details.")
        return False
    return True


# ------------------------------------------------------------
# Files: answers, results, unanswered questions
# ------------------------------------------------------------
def load_answers():
    """
    Load keyword -> answer pairs from ANSWERS_FILE.
    Column 1 = keyword (matched case-insensitively inside the question text)
    Column 2 = answer  (use 'a|b|c' to give alternatives for option buttons)
    The FIRST matching row wins, so put specific keywords above generic ones.
    """
    if not os.path.exists(ANSWERS_FILE):
        logger.warning(f"{ANSWERS_FILE} not found. Questionnaires will be marked 'manual'.")
        return []

    answers = []
    with open(ANSWERS_FILE, newline='', encoding='utf-8-sig') as f:
        for row in csv.reader(f):
            if len(row) < 2:
                continue
            keyword = ' '.join(row[0].lower().split())
            answer = row[1].strip()
            if not keyword or keyword.startswith('#') or keyword in ('keyword', 'question_keyword'):
                continue
            if answer:
                answers.append((keyword, answer))

    logger.info(f"Loaded {len(answers)} answers from {ANSWERS_FILE}")
    return answers


def find_answer(question):
    """Return the answer for the first keyword found in the question, or None."""
    q = ' '.join(question.lower().split())
    for keyword, answer in ANSWERS:
        if keyword in q:
            return answer
    return None


def record_result(link, status):
    """Append one job result to RESULTS_FILE immediately."""
    new_file = not os.path.exists(RESULTS_FILE)
    with open(RESULTS_FILE, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(['timestamp', 'status', 'job_url'])
        writer.writerow([datetime.now().strftime('%Y-%m-%d %H:%M:%S'), status, link])


def load_done_links():
    """Job URLs already applied in earlier runs (so they are skipped)."""
    done = set()
    if os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE, newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                if row.get('status') in ('applied', 'already'):
                    done.add(row.get('job_url'))
    return done


def load_unanswered_links():
    """Job URLs from UNANSWERED_FILE, in file order, for a --retry run."""
    links = []
    if os.path.exists(UNANSWERED_FILE):
        with open(UNANSWERED_FILE, newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                link = (row.get('job_url') or '').strip()
                if link and link not in links:
                    links.append(link)
    return links


def record_unanswered(question, link, reason):
    """Save a question the bot could not answer (deduplicated)."""
    global _unanswered_seen
    if _unanswered_seen is None:
        _unanswered_seen = set()
        if os.path.exists(UNANSWERED_FILE):
            with open(UNANSWERED_FILE, newline='', encoding='utf-8') as f:
                for row in csv.DictReader(f):
                    _unanswered_seen.add((row.get('question') or '').strip().lower())

    key = question.strip().lower()
    if not key or key in _unanswered_seen:
        return
    _unanswered_seen.add(key)

    new_file = not os.path.exists(UNANSWERED_FILE)
    with open(UNANSWERED_FILE, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(['question', 'reason', 'job_url'])
        writer.writerow([question.strip(), reason, link])


def save_screenshot(driver, prefix):
    os.makedirs("screenshots", exist_ok=True)
    path = f"screenshots/{prefix}_{int(time.time())}.png"
    try:
        driver.save_screenshot(path)
        logger.info(f"  Screenshot saved: {path}")
    except WebDriverException:
        pass


# ------------------------------------------------------------
# Browser
# ------------------------------------------------------------
def create_edge_driver():
    """Create and return an Edge WebDriver instance."""
    options = webdriver.EdgeOptions()
    options.add_argument("--start-maximized")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-notifications")

    if HEADLESS:
        logger.info("Running Edge in headless mode")
        options.add_argument("--headless=new")

    # --- Use Windows Edge via msedgedriver.exe running on Windows ---
    if REMOTE_DRIVER_URL:
        logger.info(f"Using Windows Edge via {REMOTE_DRIVER_URL}")
        try:
            return webdriver.Remote(command_executor=REMOTE_DRIVER_URL, options=options)
        except SessionNotCreatedException as e:
            help_text = format_mismatch_help(e.msg)
            if help_text:
                raise DriverVersionMismatch(help_text) from None
            raise

    # --- Otherwise use Linux Edge inside WSL ---
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    if EDGE_BINARY:
        logger.info(f"Using Edge binary at: {EDGE_BINARY}")
        options.binary_location = EDGE_BINARY

    if WEBDRIVER_MANAGER_AVAILABLE:
        logger.info("Using webdriver-manager to auto-download EdgeDriver...")
        service = EdgeService(EdgeChromiumDriverManager().install())
    elif EDGE_DRIVER_PATH:
        logger.info(f"Using EdgeDriver at: {EDGE_DRIVER_PATH}")
        service = EdgeService(executable_path=EDGE_DRIVER_PATH)
    else:
        logger.info("No driver path specified; letting Selenium Manager resolve EdgeDriver...")
        service = EdgeService()

    driver = webdriver.Edge(service=service, options=options)
    return driver


def login_naukri(driver):
    """Log in to Naukri.com."""
    logger.info("Logging in to Naukri.com...")
    driver.get('https://login.naukri.com/')
    time.sleep(3)

    WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.ID, 'usernameField'))
    )

    uname = driver.find_element(By.ID, 'usernameField')
    uname.send_keys(NAUKRI_EMAIL)

    passwd = driver.find_element(By.ID, 'passwordField')
    passwd.send_keys(NAUKRI_PASSWORD)
    passwd.send_keys(Keys.ENTER)

    time.sleep(8)

    if "login" in driver.current_url.lower():
        logger.warning("Still on login page. Check credentials, OTP or captcha in the browser window.")
    else:
        logger.info("Login completed.")


# ------------------------------------------------------------
# Job collection
# ------------------------------------------------------------
def build_search_urls():
    """
    Build all search URLs for every keyword and page combination.
    Naukri.com URL format: /{keyword}-jobs-in-{location}
    Returns a list of (keyword, url) tuples.
    """
    urls = []
    for keyword in KEYWORDS:
        keyword_slug = keyword.lower().replace(' ', '-')
        for page_num in range(1, PAGES_PER_KEYWORD + 1):
            if not LOCATION:
                if page_num == 1:
                    url = f"https://www.naukri.com/{keyword_slug}-jobs"
                else:
                    url = f"https://www.naukri.com/{keyword_slug}-jobs-{page_num}"
            else:
                location_slug = LOCATION.lower().replace(' ', '-')
                if page_num == 1:
                    url = f"https://www.naukri.com/{keyword_slug}-jobs-in-{location_slug}"
                else:
                    url = f"https://www.naukri.com/{keyword_slug}-jobs-in-{location_slug}-{page_num}"
            urls.append((keyword, url))
    return urls


def open_tabs_parallel(driver, search_urls):
    """Open all search URLs in parallel browser tabs. Returns the original window handle."""
    original_window = driver.current_window_handle

    if not search_urls:
        return original_window

    keyword, first_url = search_urls[0]
    logger.info(f"[Tab: Main] Opening: {first_url}")
    driver.get(first_url)
    time.sleep(3)

    for keyword, url in search_urls[1:]:
        logger.info(f"[Tab: New] Opening: {url}")
        driver.switch_to.new_window('tab')
        driver.get(url)
        time.sleep(2)

    driver.switch_to.window(original_window)
    return original_window


def collect_job_links_from_tab(driver, window_handle):
    """Switch to a specific tab and scrape job links from the page."""
    links = []
    try:
        driver.switch_to.window(window_handle)
        time.sleep(3)

        soup = BeautifulSoup(driver.page_source, 'html5lib')

        job_wrappers = soup.find_all('div', class_='srp-jobtuple-wrapper')
        logger.info(f"[Tab: {driver.title[:50]}] Found {len(job_wrappers)} job cards (srp-jobtuple-wrapper)")

        if not job_wrappers:
            job_wrappers = soup.find_all('div', class_='cust-job-tuple')
            logger.info(f"[Tab: {driver.title[:50]}] Found {len(job_wrappers)} job cards (cust-job-tuple fallback)")

        for job_wrapper in job_wrappers:
            title_link = job_wrapper.find('a', class_='title')
            if title_link and title_link.get('href'):
                href = title_link.get('href')
                if href.startswith('/'):
                    href = 'https://www.naukri.com' + href
                links.append(href)

    except WebDriverException as e:
        logger.warning(f"[Tab] Error reading tab: {e}")

    return links


def close_extra_tabs(driver, main_handle):
    """Close every tab except main_handle (e.g. company-site redirects)."""
    for handle in driver.window_handles:
        if handle != main_handle:
            try:
                driver.switch_to.window(handle)
                driver.close()
            except WebDriverException:
                pass
    driver.switch_to.window(main_handle)


def collect_all_jobs_parallel(driver, search_urls):
    """Open all keyword search pages in tabs and collect deduplicated job links."""
    logger.info("=" * 50)
    logger.info(f"Opening {len(search_urls)} search pages in parallel tabs...")
    logger.info("=" * 50)

    original_window = open_tabs_parallel(driver, search_urls)

    all_links = []
    window_handles = driver.window_handles
    logger.info(f"Collecting job links from {len(window_handles)} tabs...")

    for handle in window_handles:
        all_links.extend(collect_job_links_from_tab(driver, handle))

    logger.info("Closing search tabs...")
    close_extra_tabs(driver, original_window)

    seen = set()
    unique_links = []
    for link in all_links:
        if link not in seen:
            seen.add(link)
            unique_links.append(link)

    logger.info(f"Total unique job links collected: {len(unique_links)}")
    return unique_links


# ------------------------------------------------------------
# Questionnaire (chatbot) handling
# ------------------------------------------------------------
def _visible(parent, selectors):
    """Return visible elements for the first selector that matches anything."""
    for css in selectors:
        try:
            els = [e for e in parent.find_elements(By.CSS_SELECTOR, css) if e.is_displayed()]
        except WebDriverException:
            continue
        if els:
            return els
    return []


def _safe_click(driver, element):
    try:
        element.click()
    except WebDriverException:
        driver.execute_script("arguments[0].click();", element)


def _find_drawer(driver):
    drawers = _visible(driver, [CHATBOT_SELECTOR])
    return drawers[0] if drawers else None


def _is_success(driver):
    src = driver.page_source.lower()
    return any(text in src for text in SUCCESS_TEXTS)


def _option_elements(drawer):
    """Return [(label_text, clickable_element)] for radio/checkbox options and chips."""
    options = []
    try:
        for inp in drawer.find_elements(By.CSS_SELECTOR, "input[type='radio'], input[type='checkbox']"):
            label = None
            inp_id = inp.get_attribute('id')
            if inp_id:
                try:
                    found = drawer.find_elements(By.CSS_SELECTOR, f"label[for='{inp_id}']")
                    label = found[0] if found else None
                except WebDriverException:
                    label = None
            if label is None:
                label = inp.find_element(By.XPATH, "./..")
            if not label.is_displayed():
                continue
            text = label.text.strip() or (inp.get_attribute('value') or '').strip()
            if text:
                options.append((text, label))

        for chip in _visible(drawer, CHIP_SELECTORS):
            text = chip.text.strip()
            if text:
                options.append((text, chip))
    except WebDriverException:
        pass
    return options


def _match_option(options, choice):
    """Exact (case-insensitive) match first, then partial match."""
    c = choice.strip().lower()
    for text, el in options:
        if text.lower() == c:
            return el
    for text, el in options:
        t = text.lower()
        if c in t or t in c:
            return el
    return None


def _click_send(driver, drawer):
    buttons = _visible(drawer, SEND_BUTTON_SELECTORS)
    if not buttons:
        try:
            buttons = [b for b in drawer.find_elements(
                By.XPATH, ".//*[normalize-space()='Save' or normalize-space()='Submit' or normalize-space()='Send']")
                if b.is_displayed()]
        except WebDriverException:
            buttons = []
    if buttons:
        _safe_click(driver, buttons[-1])
        return True
    return False


def submit_answer(driver, drawer, answer):
    """
    Submit one answer into the chatbot.
    Option buttons: tries each 'a|b|c' alternative until one matches.
    Text box: types the first alternative.
    Returns (ok, reason).
    """
    choices = [a.strip() for a in answer.split('|') if a.strip()]

    options = _option_elements(drawer)
    # A lone 'Skip this question' chip sits next to a text box: type instead
    if options and all('skip this question' in t.lower() for t, _ in options) \
            and _visible(drawer, TEXT_INPUT_SELECTORS):
        options = []
    if options:
        for choice in choices:
            el = _match_option(options, choice)
            if el is not None:
                _safe_click(driver, el)
                time.sleep(0.5)
                _click_send(driver, drawer)
                return True, ""
        # Optional question with no matching option: skip it rather than abandon the job
        skip = [el for t, el in options if 'skip this question' in t.lower()]
        if skip:
            _safe_click(driver, skip[0])
            time.sleep(0.5)
            _click_send(driver, drawer)
            return True, ""
        option_texts = ', '.join(t for t, _ in options)[:200]
        return False, f"no option matches '{answer}' (options: {option_texts})"

    boxes = _visible(drawer, TEXT_INPUT_SELECTORS)
    if boxes:
        box = boxes[-1]
        _safe_click(driver, box)
        box.send_keys(Keys.CONTROL, 'a')
        box.send_keys(Keys.DELETE)
        box.send_keys(choices[0])
        time.sleep(0.5)
        if not _click_send(driver, drawer):
            box.send_keys(Keys.ENTER)
        return True, ""

    return False, "no input box or options found"


def wait_for_question(driver, prev_question, timeout=10):
    """
    Wait for the next chatbot question.
    Returns (state, drawer, question) where state is:
      applied  - Naukri confirmed the application
      closed   - chatbot disappeared without confirmation
      question - a new question is shown
      stuck    - no new question within timeout
    """
    end = time.time() + timeout
    while time.time() < end:
        try:
            if _is_success(driver):
                return "applied", None, ""
            drawer = _find_drawer(driver)
            if drawer is None:
                time.sleep(1)
                if _is_success(driver):
                    return "applied", None, ""
                if _find_drawer(driver) is None:
                    return "closed", None, ""
                continue
            msgs = _visible(drawer, BOT_MSG_SELECTORS)
            question = msgs[-1].text.strip() if msgs else ""
            if question and question != prev_question:
                return "question", drawer, question
        except WebDriverException:
            pass  # stale element while chat re-renders; retry
        time.sleep(1)
    return "stuck", None, prev_question


def answer_questionnaire(driver, link):
    """
    Answer the recruiter questionnaire using ANSWERS.
    Returns: applied | manual | unconfirmed
    """
    logger.info("  Questionnaire opened, answering from answers file...")
    prev_question = ""

    for _ in range(MAX_CHAT_QUESTIONS):
        state, drawer, question = wait_for_question(driver, prev_question)

        if state == "applied":
            return "applied"
        if state == "closed":
            return "unconfirmed"
        if state == "stuck":
            if prev_question:
                record_unanswered(prev_question, link, "answer was not accepted")
            else:
                logger.warning("    Could not read the question text (selectors may need updating).")
            return "manual"

        logger.info(f"    Q: {question[:150]}")
        if 'thank you for your responses' in question.lower():
            # Final screen: just press Save to submit the application
            _click_send(driver, drawer)
            prev_question = question
            time.sleep(2)
            continue

        answer = find_answer(question)
        if answer is None:
            # Default 'Yes' only for yes/no style questions; free-text ones go to manual
            if not re.match(r"\s*(are|do|does|did|have|has|will|would|can|could|is|should|"
                            r"comfortable|availability|available|ready|willing|open)\b",
                            question, re.I):
                logger.warning("    A: (no matching keyword in answers file)")
                record_unanswered(question, link, "no matching keyword")
                return "manual"
            logger.warning("    A: (no matching keyword, defaulting to 'Yes')")
            record_unanswered(question, link, "no matching keyword, answered 'Yes'")
            answer = "Yes"

        ok, reason = submit_answer(driver, drawer, answer)
        if not ok:
            logger.warning(f"    A: could not submit '{answer}': {reason}")
            record_unanswered(question, link, reason)
            return "manual"

        logger.info(f"    A: {answer}")
        prev_question = question
        time.sleep(2)

    return "manual"


# ------------------------------------------------------------
# Applying
# ------------------------------------------------------------
def fill_custom_name_fields(driver):
    """Fill the optional first/last name fields and submit, if Naukri shows them."""
    try:
        firstname_el = driver.find_element(By.ID, 'CUSTOM-FIRSTNAME')
        firstname_el.clear()
        firstname_el.send_keys(FIRSTNAME)
        logger.info("  Filled custom first name field")
    except NoSuchElementException:
        pass

    try:
        lastname_el = driver.find_element(By.ID, 'CUSTOM-LASTNAME')
        lastname_el.clear()
        lastname_el.send_keys(LASTNAME)
        logger.info("  Filled custom last name field")
    except NoSuchElementException:
        pass

    try:
        submit_btn = driver.find_element(By.XPATH, "//*[normalize-space()='Submit and Apply']")
        submit_btn.click()
        logger.info("  Clicked 'Submit and Apply'")
        time.sleep(2)
    except NoSuchElementException:
        pass


def wait_for_apply_result(driver, timeout=10):
    """
    After clicking Apply, wait for Naukri's response.
    Returns: applied | questionnaire | quota | unconfirmed
    """
    end = time.time() + timeout
    while time.time() < end:
        if _is_success(driver) or 'saveapply' in driver.current_url.lower():
            return "applied"
        src = driver.page_source.lower()
        # Naukri shows this generic toast when the daily apply quota is used up
        if "error while processing your request" in src or "daily quota" in src:
            return "quota"
        if _find_drawer(driver) is not None:
            return "questionnaire"
        if driver.find_elements(By.XPATH, "//button[normalize-space()='Applied']"):
            return "applied"
        if driver.find_elements(By.ID, 'CUSTOM-FIRSTNAME'):
            fill_custom_name_fields(driver)
        time.sleep(1)
    return "unconfirmed"


def click_apply_button(driver, link):
    """
    Apply using Naukri's internal "Apply" button only, answering any questionnaire.
    Returns one of: applied | already | external | manual | unconfirmed | not_found
    """
    time.sleep(4)
    main_handle = driver.current_window_handle

    # 1. Already applied?
    if driver.find_elements(By.XPATH,
            "//*[@id='already-applied'] | //button[normalize-space()='Applied']"):
        return "already"

    # 2. Internal Naukri Apply button (exact text 'Apply', NOT 'Apply on company site')
    apply_btn = None
    for xpath in ("//button[@id='apply-button']",
                  "//button[normalize-space()='Apply']"):
        try:
            apply_btn = WebDriverWait(driver, 3).until(
                EC.element_to_be_clickable((By.XPATH, xpath))
            )
            break
        except TimeoutException:
            continue

    if apply_btn is None:
        if driver.find_elements(By.XPATH,
                "//*[@id='company-site-button'] | //button[contains(normalize-space(),'company site')]"):
            return "external"
        return "not_found"

    # 3. Click and handle the result
    _safe_click(driver, apply_btn)
    status = wait_for_apply_result(driver)

    if status == "questionnaire":
        status = answer_questionnaire(driver, link)

    close_extra_tabs(driver, main_handle)

    if status in ("unconfirmed", "manual"):
        save_screenshot(driver, status)

    return status


def apply_to_jobs(driver, job_links):
    """
    Visit each job link and attempt to apply.
    Each result is written to RESULTS_FILE immediately.
    Returns (applied_count, not_applied_count, results_dict).
    """
    results = {key: [] for key in RESULT_KEYS}
    applied = 0
    done_links = load_done_links()

    logger.info("=" * 50)
    logger.info(f"Starting job applications (max: {MAX_APPLICATIONS})...")
    logger.info("=" * 50)

    for i, link in enumerate(job_links, 1):
        if applied >= MAX_APPLICATIONS:
            logger.info(f"Reached max application limit ({MAX_APPLICATIONS}). Stopping.")
            break

        if link in done_links:
            logger.info(f"[{i}/{len(job_links)}] Skipping (applied in an earlier run): {link}")
            continue

        logger.info(f"[{i}/{len(job_links)}] Visiting job: {link}")
        try:
            driver.get(link)
        except WebDriverException as e:
            logger.warning(f"  ✗ Failed to load page: {e}")
            results["error"].append(link)
            record_result(link, "error")
            continue

        if "daily quota" in driver.page_source.lower():
            logger.info("Daily apply quota reached. Stopping.")
            break

        try:
            status = click_apply_button(driver, link)
        except WebDriverException as e:
            logger.warning(f"  ✗ Error while applying: {e}")
            status = "error"

        if status == "quota":
            logger.info("Daily apply quota reached (Naukri rejected Apply). Stopping.")
            break

        results[status].append(link)
        record_result(link, status)

        if status == "applied":
            applied += 1
            logger.info(f"  ✓ Applied (confirmed by Naukri). Total: {applied}")
        elif status == "already":
            logger.info("  • Already applied earlier. Skipped.")
        elif status == "external":
            logger.info("  → 'Apply on company site' only. Skipped.")
        elif status == "manual":
            logger.warning(f"  ? Questionnaire needs your input. Question saved to {UNANSWERED_FILE}.")
        elif status == "unconfirmed":
            logger.warning("  ? Clicked Apply but Naukri did not confirm.")
        elif status == "not_found":
            logger.warning("  ✗ No Apply button found.")

        time.sleep(2)

    not_applied = sum(len(v) for k, v in results.items() if k != "applied")
    return applied, not_applied, results


def main():
    """Main entry point for the bot."""
    global ANSWERS

    logger.info("=" * 50)
    logger.info("Naukri Auto-Apply Bot (Edge Edition - 2026 Update)")
    logger.info("=" * 50)

    if not validate_config():
        return

    ANSWERS = load_answers()

    logger.info(f"Keywords: {KEYWORDS}")
    logger.info(f"Location: {LOCATION or 'Anywhere'}")
    logger.info(f"Max applications: {MAX_APPLICATIONS}")
    logger.info(f"Pages per keyword: {PAGES_PER_KEYWORD}")
    logger.info(f"Browser: {'Windows Edge (' + REMOTE_DRIVER_URL + ')' if REMOTE_DRIVER_URL else 'Linux Edge (WSL)'}")

    driver = None
    try:
        driver = create_edge_driver()
        login_naukri(driver)

        if '--retry' in sys.argv:
            # Re-apply only to jobs whose questionnaire was left unanswered
            job_links = load_unanswered_links()
            logger.info(f"Retry mode: {len(job_links)} jobs from {UNANSWERED_FILE}")
        else:
            search_urls = build_search_urls()
            logger.info(f"Total search pages to open: {len(search_urls)}")
            job_links = collect_all_jobs_parallel(driver, search_urls)
        if not job_links:
            logger.warning("No job links found. Check your keywords and location.")
            return

        applied, not_applied, results = apply_to_jobs(driver, job_links)

        logger.info("=" * 50)
        logger.info("APPLICATION SUMMARY")
        logger.info(f"  Applied (confirmed):         {len(results['applied'])}")
        logger.info(f"  Already applied:             {len(results['already'])}")
        logger.info(f"  Company-site only (skipped): {len(results['external'])}")
        logger.info(f"  Needs manual questionnaire:  {len(results['manual'])}")
        logger.info(f"  Unconfirmed:                 {len(results['unconfirmed'])}")
        logger.info(f"  No apply button:             {len(results['not_found'])}")
        logger.info(f"  Errors:                      {len(results['error'])}")
        logger.info(f"  Total processed:             {applied + not_applied}")
        logger.info(f"  Results file:                {RESULTS_FILE}")
        logger.info(f"  Unanswered questions:        {UNANSWERED_FILE}")
        logger.info("=" * 50)

    except KeyboardInterrupt:
        logger.info("Stopped by user. Results so far are in " + RESULTS_FILE)
    except DriverVersionMismatch as e:
        logger.error(str(e))
    except Exception as e:
        logger.exception(f"An unexpected error occurred: {e}")
    finally:
        if driver:
            try:
                driver.quit()
                logger.info("Browser closed.")
            except Exception:
                pass


if __name__ == "__main__":
    main()
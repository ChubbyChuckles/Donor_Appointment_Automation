import requests
from bs4 import BeautifulSoup
import os
import re
import base64
import urllib.parse
from datetime import datetime, timedelta
import argparse
from google.oauth2 import service_account
from googleapiclient.discovery import build
import json
import tempfile

# Load sensitive data from environment variables (GitHub Secrets)
SURNAME = os.getenv("BLUTSPENDE_SURNAME")
DONOR_ID = os.getenv("BLUTSPENDE_DONOR_ID")
RESERVATION_EMAIL = os.getenv("BLUTSPENDE_EMAIL")
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
APPOINTMENT_LOCATION = os.getenv("APPOINTMENT_LOCATION")

# Validate environment variables
if not all([SURNAME, DONOR_ID, RESERVATION_EMAIL, CALENDAR_ID, SERVICE_ACCOUNT_JSON]):
    raise Exception("Missing required environment variables")

# Validate email format
if not re.match(r"[^@]+@[^@]+\.[^@]+", RESERVATION_EMAIL):
    raise Exception("Invalid RESERVATION_EMAIL format")

# Create temporary file for service account JSON
with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as temp_file:
    json.dump(json.loads(SERVICE_ACCOUNT_JSON), temp_file)
    SERVICE_ACCOUNT_FILE = temp_file.name

# Google Calendar setup
SCOPES = ['https://www.googleapis.com/auth/calendar']


# Base URL
BASE_URL = "https://terminreservierung.blutspende-nordost.de"

# Initialize session
session = requests.Session()
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": f"{BASE_URL}/",
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
}

def authenticate_google_calendar():
    """Authenticate using service account and return the Google Calendar service."""
    credentials = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    service = build('calendar', 'v3', credentials=credentials)
    return service

def add_appointment_to_calendar(service, date_str, time_str):
    """Add a plasma donation appointment to the Google Calendar with custom reminders."""
    try:
        appointment_date = datetime.strptime(date_str, "%Y-%m-%d")
        time_match = re.match(r"(\d+):(\d+)\sUhr", time_str)
        if not time_match:
            raise ValueError(f"Invalid time format: {time_str}")
        hours, minutes = map(int, time_match.groups())
        start_time = appointment_date.replace(hour=hours, minute=minutes)
        end_time = start_time + timedelta(hours=1)
        evening_before = (start_time - timedelta(days=1)).replace(hour=22, minute=0, second=0)
    except Exception as e:
        raise Exception(f"Error parsing date/time: {e}")

    event = {
        'summary': 'Plasma Donation Appointment',
        'description': f'Plasma donation at Leipzig. Donor ID: {DONOR_ID}. Email: {RESERVATION_EMAIL}',
        'location': APPOINTMENT_LOCATION,
        'start': {
            'dateTime': start_time.isoformat(),
            'timeZone': 'Europe/Amsterdam',
        },
        'end': {
            'dateTime': end_time.isoformat(),
            'timeZone': 'Europe/Amsterdam',
        },
        'reminders': {
            'useDefault': False,
            'overrides': [
                {'method': 'popup', 'minutes': 120},
                {'method': 'popup', 'minutes': int((start_time - evening_before).total_seconds() / 60)},
            ],
        },
    }

    try:
        event = service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
        print(f"Event created: {event.get('htmlLink')}")
    except Exception as e:
        raise Exception(f"Error creating calendar event: {e}")

def decode_reservation_context(context):
    """Decode reservation_context JSON from URL-encoded base64."""
    try:
        decoded = base64.b64decode(context).decode("utf-8")
        return json.loads(decoded)
    except Exception as e:
        print(f"Error decoding reservation context: {e}")
        return {}

def make_absolute_url(relative_url):
    """Convert relative URL to absolute by prepending BASE_URL."""
    if relative_url.startswith("http"):
        return relative_url
    return f"{BASE_URL}{relative_url}"

def parse_time(time_str):
    """Convert time string like '13:30 Uhr' to hours and minutes."""
    match = re.match(r"(\d+):(\d+)\sUhr", time_str)
    if not match:
        return None
    hours, minutes = map(int, match.groups())
    return hours * 60 + minutes

def get_target_date():
    """Calculate the target date based on current day and time."""
    now = datetime.now()
    current_hour = now.hour
    current_weekday = now.weekday()
    three_weeks_later = now + timedelta(weeks=3)

    if current_weekday == 0 and current_hour >= 17:
        target_weekday = 0  # Monday
    elif current_weekday == 3 and current_hour >= 17:
        target_weekday = 3  # Thursday
    else:
        target_weekday = 3  # Thursday

    days_until_target = (target_weekday - three_weeks_later.weekday()) % 7
    target_date = three_weeks_later + timedelta(days=days_until_target)
    return target_date.strftime("%Y-%m-%d")

# Parse command-line arguments
parser = argparse.ArgumentParser(description="Book plasma donation appointment and add to Google Calendar")
parser.add_argument('--skip-booking', action='store_true', help="Skip the actual booking submission for testing")
args = parser.parse_args()

try:
    # Calculate preferred date
    preferred_date = get_target_date()
    print(f"Calculated preferred date: {preferred_date}")

    # Step 1: GET login page
    login_page_url = f"{BASE_URL}/"
    print(f"Fetching login page: {login_page_url}")
    response = session.get(login_page_url, headers=headers)
    response.raise_for_status()
    print(f"Login page status code: {response.status_code}")

    # Parse for CSRF token and form fields
    soup = BeautifulSoup(response.text, "html.parser")
    form = soup.find("form", {"class": "simple_form donor_login"})
    if not form:
        raise Exception("Login form not found on login page")

    # Extract CSRF token
    csrf_input = (
        soup.find("input", {"name": "_csrf"}) or
        soup.find("input", {"name": "_token"}) or
        soup.find("input", {"name": "csrf_token"}) or
        soup.find("input", {"name": "authenticity_token"})
    )
    csrf_token = csrf_input["value"] if csrf_input else None
    if not csrf_token:
        meta_csrf = soup.find("meta", {"name": "csrf-token"})
        csrf_token = meta_csrf["content"] if meta_csrf else None
    if not csrf_token:
        script_tags = soup.find_all("script")
        for script in script_tags:
            if script.string:
                match = re.search(r'csrfToken\s*=\s*[\'"]([^\'"]+)[\'"]', script.string)
                if match:
                    csrf_token = match.group(1)
                    break
    if not csrf_token:
        raise Exception("No CSRF token found")
    print(f"CSRF token found: {csrf_token}")

    # Extract form action URL
    form_action = form["action"] if form else "/donor_login/trs_logins?reservation_context=eyJhdXRoZW50aWNhdGlvbl9zdHJhdGVneSI6IiJ9"
    login_url = make_absolute_url(form_action)
    print(f"Login form action URL: {login_url}")

    # Build login data
    login_data = {}
    for input_tag in form.find_all("input", {"name": True}):
        name = input_tag["name"]
        if name in ["donor_login[last_name]", "donor_login[donor_number]"]:
            value = SURNAME if "last_name" in name else DONOR_ID
        else:
            value = input_tag.get("value", "")
        login_data[name] = value
    login_data["button"] = ""
    login_data["authenticity_token"] = csrf_token

    print(f"Sending login POST to: {login_url}")
    login_response = session.post(login_url, data=login_data, headers=headers, allow_redirects=False)
    print(f"Login response status code: {login_response.status_code}")

    # Check for login failure
    login_soup = BeautifulSoup(login_response.text, "html.parser")
    error_messages = login_soup.find_all(["div", "p", "span"], class_=re.compile(r"error|alert|notice|flash"))
    if error_messages:
        errors = [msg.text.strip() for msg in error_messages if msg.text.strip()]
        if errors:
            raise Exception(f"Login failed with errors: {', '.join(errors)}")

    if login_soup.find("form", {"class": "simple_form donor_login"}):
        raise Exception("Login failed: Response contains login form, credentials may be incorrect")

    # Get redirect URL
    redirect_url = login_response.headers.get("X-Xhr-Redirect") or login_response.headers.get("Location")
    if not redirect_url:
        try:
            login_json = login_response.json()
            redirect_url = login_json.get("redirect_url")
            if not login_json.get("success", False):
                raise Exception(f"Login failed: {login_json.get('message', 'Unknown error')}")
        except ValueError:
            script_tags = login_soup.find_all("script")
            for script in script_tags:
                if script.string and "window.location" in script.string:
                    match = re.search(r'window\.location\s*=\s*[\'"]([^\'"]+)[\'"]', script.string)
                    if match:
                        redirect_url = match.group(1)
                        break
            if login_soup.find("a", href=re.compile(r"/spendezentren/\d+/termine")):
                redirect_url = login_url
            elif login_response.status_code in (301, 302, 303):
                redirect_url = login_response.headers.get("Location")
            if not redirect_url:
                raise Exception("Login failed: No redirect URL found")
    print(f"Login successful. Redirect URL: {redirect_url}")

    # Step 3: GET donation centers page
    spendezentren_url = make_absolute_url(redirect_url)
    print(f"Fetching donation centers page: {spendezentren_url}")
    headers["Referer"] = login_url
    spendezentren_response = session.get(spendezentren_url, headers=headers)
    spendezentren_response.raise_for_status()
    print(f"Donation centers page status code: {spendezentren_response.status_code}")

    # Parse donation center options
    spendezentren_soup = BeautifulSoup(spendezentren_response.text, "html.parser")
    center_links = spendezentren_soup.find_all("a", href=re.compile(r"/spendezentren/\d+/termine"))
    centers = []
    for link in center_links:
        center_name = link.text.strip()
        center_href = link["href"]
        center_id = re.search(r"/spendezentren/(\d+)/termine", center_href).group(1)
        centers.append({"name": center_name, "href": center_href, "id": center_id})
    if not centers:
        raise Exception("No donation centers found")

    # Step 4: Select Leipzig donation center
    preferred_center_id = "0427711"
    selected_center = next((c for c in centers if c["id"] == preferred_center_id), None)
    if not selected_center:
        raise Exception(f"Leipzig center (ID {preferred_center_id}) not found")
    print(f"Selected center: {selected_center['name']} (ID: {selected_center['id']})")

    # GET appointment page for Leipzig
    termine_url = make_absolute_url(selected_center["href"])
    print(f"Fetching appointment page: {termine_url}")
    headers["Referer"] = spendezentren_url
    termine_response = session.get(termine_url, headers=headers)
    termine_response.raise_for_status()
    print(f"Appointment page status code: {termine_response.status_code}")

    # Parse donation type options
    termine_soup = BeautifulSoup(termine_response.text, "html.parser")
    select_div = termine_soup.find("div", {"id": "select_donation_type"})
    donation_links = select_div.find_all("a") if select_div else []
    donation_types = []
    for link in donation_links:
        donation_name = link.text.strip()
        donation_href = link["href"]
        parsed_url = urllib.parse.urlparse(donation_href)
        query_params = urllib.parse.parse_qs(parsed_url.query)
        reservation_context = query_params.get("reservation_context", [""])[0]
        context_data = decode_reservation_context(reservation_context)
        donation_type = context_data.get("donation_type", "unknown").lower()
        disabled = "disabled" in link.get("class", [])
        if donation_type in ["pp", "bd"]:
            donation_types.append({"name": donation_name, "href": donation_href, "type": donation_type, "disabled": disabled})
    if not donation_types:
        raise Exception("No donation types found")

    # Step 5: Select Plasmaspende
    preferred_donation_type = "pp"
    selected_donation = next((d for d in donation_types if d["type"] == preferred_donation_type and not d["disabled"]), None)
    if not selected_donation:
        raise Exception(f"Plasmaspende (type {preferred_donation_type}) not found or disabled")
    print(f"Selected donation type: {selected_donation['name']} (Type: {selected_donation['type']})")

    # GET plasma appointment page
    plasmaspende_url = make_absolute_url(selected_donation["href"])
    print(f"Fetching plasma appointment page: {plasmaspende_url}")
    headers["Referer"] = termine_url
    plasmaspende_response = session.get(plasmaspende_url, headers=headers)
    plasmaspende_response.raise_for_status()
    print(f"Plasma appointment page status code: {plasmaspende_response.status_code}")

    # Parse calendar for the preferred date
    plasmaspende_soup = BeautifulSoup(plasmaspende_response.text, "html.parser")
    calendar_div = plasmaspende_soup.find("div", class_="tab-content abstand calendar")
    if not calendar_div:
        raise Exception("No calendar found")

    available_dates = []
    date_links = calendar_div.find_all("a", class_=re.compile(r"calendar-day-open"))
    for link in date_links:
        href = link["href"]
        context = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("reservation_context", [""])[0]
        context_data = decode_reservation_context(context)
        date = context_data.get("date")
        if date:
            available_dates.append({"date": date, "href": href})

    def fetch_slots(date, date_url, headers, session):
        absolute_url = make_absolute_url(date_url)
        print(f"Fetching slots for {date}: {absolute_url}")
        headers["Referer"] = plasmaspende_url
        response = session.get(absolute_url, headers=headers, allow_redirects=True)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        slot_elements = soup.find_all('a', href=True, string=re.compile(r'\d+:\d+\sUhr\s\(\d+\)'))
        slots = []
        for element in slot_elements:
            text = element.get_text(strip=True)
            time_match = re.match(r'(\d+:\d+\sUhr)\s\((\d+)\)', text)
            if time_match:
                free_time = time_match.group(1)
                available_slots = int(time_match.group(2))
                href = element['href']
                slots.append({'time': free_time, 'available_slots': available_slots, 'url': href})
        return slots

    selected_date = next((d for d in available_dates if d["date"] == preferred_date), None)
    if not selected_date:
        raise Exception(f"Preferred date {preferred_date} not found")
    available_slots = fetch_slots(selected_date["date"], selected_date["href"], headers, session)

    if not available_slots:
        raise Exception(f"No available timeslots found for {preferred_date}")

    # Step 6: Select the earliest timeslot after 13:00
    min_time_minutes = 13 * 60
    valid_slots = [slot for slot in available_slots if parse_time(slot["time"]) and parse_time(slot["time"]) > min_time_minutes]
    if not valid_slots:
        raise Exception(f"No timeslots after 13:00 found for {preferred_date}")
    selected_slot = min(valid_slots, key=lambda x: parse_time(x["time"]))
    print(f"Selected timeslot: {selected_slot['time']}")

    # Fetch the booking page
    booking_url = make_absolute_url(selected_slot["url"])
    print(f"Fetching booking page for {selected_slot['time']} on {preferred_date}: {booking_url}")
    headers["Referer"] = make_absolute_url(selected_date["href"])
    booking_response = session.get(booking_url, headers=headers, allow_redirects=True)
    booking_response.raise_for_status()
    print(f"Booking page status code: {booking_response.status_code}")

    # Step 7: Parse booking page and prepare reservation form
    booking_soup = BeautifulSoup(booking_response.text, "html.parser")
    reservation_form = (
        booking_soup.find("form", {"class": re.compile(r"new_reservation|edit_reservation|reservation|form")}) or
        booking_soup.find("form", {"id": re.compile(r"new_reservation|edit_reservation|reservation")}) or
        booking_soup.find("form", {"action": re.compile(r"reservation|book|submit")}) or
        booking_soup.find("form", lambda tag: tag.find("input", {"name": re.compile(r"reservation\[.*\]")}))
    )

    if not reservation_form:
        raise Exception("Reservation form not found on booking page")

    form_action = reservation_form.get("action")
    if not form_action:
        raise Exception("No action attribute found in reservation form")
    reservation_url = make_absolute_url(form_action)
    print(f"Reservation form action URL: {reservation_url}")

    csrf_input = (
        reservation_form.find("input", {"name": "_csrf"}) or
        reservation_form.find("input", {"name": "_token"}) or
        reservation_form.find("input", {"name": "csrf_token"}) or
        reservation_form.find("input", {"name": "authenticity_token"})
    )
    csrf_token = csrf_input["value"] if csrf_input else None
    if not csrf_token:
        meta_csrf = booking_soup.find("meta", {"name": "csrf-token"})
        csrf_token = meta_csrf["content"] if meta_csrf else None
    if not csrf_token:
        raise Exception("No CSRF token found in reservation form")
    print(f"Reservation CSRF token: {csrf_token}")

    email_input = reservation_form.find("input", {"id": "reservation_email"})
    email_confirmation_input = reservation_form.find("input", {"id": "reservation_email_confirmation"})

    if not email_input or not email_confirmation_input:
        raise Exception("Email input fields not found in form")

    email_field_name = email_input.get("name")
    email_confirmation_field_name = email_confirmation_input.get("name")

    if not email_field_name or not email_confirmation_field_name:
        raise Exception("Name attributes missing for email input fields")

    print(f"Email field name: {email_field_name}")
    print(f"Email confirmation field name: {email_confirmation_field_name}")

    reservation_data = {
        email_field_name: RESERVATION_EMAIL,
        email_confirmation_field_name: RESERVATION_EMAIL,
        "reservation[receive_email_reminder]": "1",
        "authenticity_token": csrf_token,
        "button": ""
    }

    reserved_fields = [email_field_name, email_confirmation_field_name, "reservation[receive_email_reminder]", "authenticity_token", "button"]
    for input_tag in reservation_form.find_all("input", {"name": True}):
        name = input_tag["name"]
        if name in reserved_fields:
            continue
        input_type = input_tag.get("type", "").lower()
        if input_type == "checkbox":
            value = "1" if input_tag.get("checked") else ""
        elif input_type == "radio":
            value = input_tag.get("value", "") if input_tag.get("checked") else ""
        else:
            value = input_tag.get("value", "")
        if value:
            reservation_data[name] = value

    for select_tag in reservation_form.find_all("select", {"name": True}):
        name = select_tag["name"]
        if name in reserved_fields:
            continue
        selected_option = select_tag.find("option", selected=True) or select_tag.find("option")
        reservation_data[name] = selected_option["value"] if selected_option else ""

    print(f"Reservation form data: {reservation_data}")

    if not args.skip_booking:
        headers["Referer"] = booking_url
        print(f"Submitting reservation form to: {reservation_url}")
        reservation_response = session.post(reservation_url, data=reservation_data, headers=headers, allow_redirects=True)
        reservation_response.raise_for_status()
        print(f"Reservation submission status code: {reservation_response.status_code}")

        reservation_soup = BeautifulSoup(reservation_response.text, "html.parser")
        error_messages = reservation_soup.find_all(["div", "p", "span"], class_=re.compile(r"error|alert|notice|flash"))
        if error_messages:
            errors = [msg.text.strip() for msg in error_messages if msg.text.strip()]
            if errors:
                raise Exception(f"Reservation submission failed with errors: {', '.join(errors)}")
        success_messages = reservation_soup.find_all(["div", "p", "span"], class_=re.compile(r"success|confirmation"))
        if success_messages:
            successes = [msg.text.strip() for msg in success_messages if msg.text.strip()]
            print(f"Reservation successful: {', '.join(successes)}")
        else:
            print("Reservation submitted, no explicit success message found")
    else:
        print("Skipping booking submission due to --skip-booking flag")

    # Step 8: Add appointment to Google Calendar
    print("Adding appointment to Google Calendar...")
    calendar_service = authenticate_google_calendar()
    add_appointment_to_calendar(calendar_service, preferred_date, selected_slot['time'])

except Exception as e:
    print(f"Error: {str(e)}")
    raise

finally:
    session.close()
    if os.path.exists(SERVICE_ACCOUNT_FILE):
        os.remove(SERVICE_ACCOUNT_FILE)
    print("Session closed")
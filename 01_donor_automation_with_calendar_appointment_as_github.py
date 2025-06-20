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

# Load sensitive data from environment variables
SURNAME = os.getenv("BLUTSPENDE_SURNAME")
DONOR_ID = os.getenv("BLUTSPENDE_DONOR_ID")
RESERVATION_EMAIL = os.getenv("BLUTSPENDE_EMAIL")
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
APPOINTMENT_LOCATION = os.getenv("APPOINTMENT_LOCATION")

# Validate environment variables
if not all([SURNAME, DONOR_ID, RESERVATION_EMAIL, CALENDAR_ID, SERVICE_ACCOUNT_JSON, APPOINTMENT_LOCATION]):
    raise ValueError("Missing required environment variables")

# Validate email format
if not re.match(r"[^@]+@[^@]+\.[^@]+", RESERVATION_EMAIL):
    raise ValueError("Invalid RESERVATION_EMAIL format")

# Decode base64-encoded service account credentials
try:
    decoded_json = base64.b64decode(SERVICE_ACCOUNT_JSON).decode("utf-8")
    service_account_data = json.loads(decoded_json)
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', encoding='utf-8', delete=False) as temp_file:
        json.dump(service_account_data, temp_file)
        temp_file.flush()
        SERVICE_ACCOUNT_FILE = temp_file.name
except Exception as e:
    raise ValueError(f"Failed to decode or parse base64-encoded SERVICE_ACCOUNT_JSON: {e}")

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
    try:
        credentials = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES
        )
        service = build('calendar', 'v3', credentials=credentials)
        return service
    except Exception as e:
        raise ValueError(f"Failed to authenticate Google Calendar: {e}")

def count_plasma_appointments(service, future_only=False):
    """Count plasma donation appointments in the past 365 days or future."""
    try:
        now = datetime.now()
        if future_only:
            time_min = now.isoformat() + 'Z'
            time_max = (now + timedelta(days=365)).isoformat() + 'Z'
        else:
            time_min = (now - timedelta(days=365)).isoformat() + 'Z'
            time_max = now.isoformat() + 'Z'

        events_result = service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        events = events_result.get('items', [])

        plasma_appointments = []
        for event in events:
            summary = event.get('summary', '').lower()
            description = event.get('description', '').lower()
            if 'plasma' in summary or 'plasma' in description:
                start_time = datetime.fromisoformat(event['start']['dateTime'].replace('Z', ''))
                plasma_appointments.append(start_time)

        return len(plasma_appointments), plasma_appointments
    except Exception as e:
        raise ValueError(f"Failed counting plasma appointments: {e}")

def check_minimum_gap(service, target_date_str):
    """Check if there are any plasma appointments within 2 days before the target date."""
    try:
        target_date = datetime.strptime(target_date_str, '%Y-%m-%d').date()
        time_min = (datetime.combine(target_date, datetime.min.time()) - timedelta(days=3)).isoformat() + 'Z'
        time_max = datetime.combine(target_date, datetime.min.time()).isoformat() + 'Z'

        events_result = service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        events = events_result.get('items', [])

        for event in events:
            summary = event.get('summary', '').lower()
            description = event.get('description', '').lower()
            if 'plasma' in summary or 'plasma' in description:
                event_date = datetime.fromisoformat(event['start']['dateTime'].replace('Z', '')).date()
                delta = (target_date - event_date).days
                if delta <= 2:
                    return False, event_date.strftime('%Y-%m-%d')
        return True, None
    except Exception as e:
        raise ValueError(f"Failed checking minimum gap: {e}")

def add_appointment_to_calendar(service, date_str, time_str):
    """Add a plasma donation appointment to the Google Calendar with custom reminders."""
    try:
        appointment_date = datetime.strptime(date_str, '%Y-%m-%d')
        time_match = re.match(r'(\d+):(\d+)\sUhr', time_str)
        if not time_match:
            raise ValueError(f"Invalid time format: {time_str}")
        hours, minutes = map(int, time_match.groups())
        start_time = appointment_date.replace(hour=hours, minute=minutes)
        end_time = start_time + timedelta(hours=1)
        evening_before = (start_time - timedelta(days=1)).replace(hour=22, minute=0, second=0)

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

        event = service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
        print(f"Event created: {event.get('htmlLink')}")
    except Exception as e:
        raise ValueError(f"Failed creating calendar event: {e}")

def decode_reservation_context(context):
    """Decode reservation_context JSON from URL-encoded base64."""
    try:
        decoded = base64.b64decode(context).decode("utf-8")
        return json.loads(decoded)
    except Exception as e:
        print(f"Failed decoding reservation context: {e}")
        return {}

def make_absolute_url(relative_url):
    """Convert relative URL to absolute by prepending BASE_URL."""
    if relative_url.startswith("http"):
        return relative_url
    return f"{BASE_URL}{relative_url}"

def parse_time(time_str):
    """Convert time string like '13:30 Uhr' to minutes since midnight."""
    match = re.match(r"(\d+):(\d+)\sUhr", time_str)
    if not match:
        return None
    hours, minutes = map(int, match.groups())
    return hours * 60 + minutes

def get_target_dates():
    """Generate target dates for 8 weeks between May 1 and August 31, 2025, after current date, alternating 2 and 1 appointments per week."""
    target_dates = []
    year = datetime.now().year
    now = datetime.now()
    may_start = datetime(year, 5, 1)
    aug_end = datetime(year, 8, 31)
    
    # Start from the current date or May 1, whichever is later
    start_date = max(now, may_start)
    # Find the first Monday on or after start_date
    current_date = start_date
    if current_date.weekday() != 0:
        current_date += timedelta(days=(7 - current_date.weekday()) % 7)
    
    weeks_processed = 0
    is_two_appointment_week = True  # Start with a 2-appointment week
    
    while current_date <= aug_end and weeks_processed < 8:
        week_dates = []
        monday = current_date
        
        # Add Monday (or Tuesday if Monday is unavailable)
        if monday.weekday() not in [5, 6]:
            week_dates.append(monday)
        else:
            tuesday = monday + timedelta(days=1)
            if tuesday.weekday() not in [5, 6] and tuesday <= aug_end:
                week_dates.append(tuesday)
        
        # For 2-appointment weeks, add Thursday (or Friday if Tuesday was used)
        if is_two_appointment_week and week_dates:
            first_date = week_dates[0]
            if first_date.weekday() == 1:  # Tuesday
                thursday = first_date + timedelta(days=3)
            else:  # Monday
                thursday = first_date + timedelta(days=(3 - first_date.weekday()) % 7)
            if thursday.weekday() not in [5, 6] and thursday <= aug_end:
                week_dates.append(thursday)
        
        # Only include dates after current date
        target_dates.extend([d.strftime("%Y-%m-%d") for d in week_dates if d > now])
        
        weeks_processed += 1
        is_two_appointment_week = not is_two_appointment_week  # Toggle between 2 and 1 appointment weeks
        current_date += timedelta(weeks=1)
    
    return target_dates[:16]

def try_nearby_dates(target_date_str, available_dates, now):
    """Try dates within ±5 days if the target date is unavailable, ensuring future dates."""
    target_date = datetime.strptime(target_date_str, '%Y-%m-%d').date()
    for offset in [-5, -4, -3, -2, -1, 1, 2, 3, 4, 5]:
        nearby_date = target_date + timedelta(days=offset)
        if nearby_date <= now.date():
            continue
        nearby_date_str = nearby_date.strftime('%Y-%m-%d')
        if any(d['date'] == nearby_date_str for d in available_dates):
            return nearby_date_str
    return None

# Parse command-line arguments
parser = argparse.ArgumentParser(description="Book plasma donation appointments and add to Google Calendar")
parser.add_argument('--skip-booking', action='store_true', help="Skip everything after determining free slots for testing")
parser.add_argument('--testing', action='store_true', help="Test mode: Print existing and proposed appointments without booking")
args = parser.parse_args()

try:
    # Get list of target dates
    target_dates = get_target_dates()
    if not target_dates:
        raise ValueError("No valid target dates generated")
    print(f"Target dates for booking: {target_dates}")

    # Check plasma appointment limits
    print("Checking plasma appointment limits...")
    calendar_service = authenticate_google_calendar()
    plasma_count, plasma_appointments = count_plasma_appointments(calendar_service, future_only=False)
    future_count, future_appointments = count_plasma_appointments(calendar_service, future_only=True)
    
    if args.testing:
        print("Existing plasma donation appointments in past 365 days:")
        if plasma_appointments:
            for appt in plasma_appointments:
                print(f"- {appt.strftime('%Y-%m-%d %H:%M')}")
        else:
            print("- None found")
        print(f"Future plasma appointments (after {datetime.now().strftime('%Y-%m-%d %H:%M')}):")
        if future_appointments:
            for appt in future_appointments:
                print(f"- {appt.strftime('%Y-%m-%d %H:%M')}")
        else:
            print("- None found")

    max_new_bookings_annual = min(16, 60 - plasma_count)
    max_new_bookings_future = 5 - future_count  # Updated to 5
    max_new_bookings = min(max_new_bookings_annual, max_new_bookings_future)
    if max_new_bookings <= 0:
        if plasma_count >= 60:
            raise ValueError(f"Cannot book: Already have {plasma_count} plasma appointments in past 365 days, exceeding limit of 60")
        else:
            raise ValueError(f"Cannot book: Already have {future_count} future appointments, exceeding limit of 5 active bookings")
    print(f"Proceeding with up to {max_new_bookings} new bookings. Past 365-day appointments: {plasma_count}/60, Future appointments: {future_count}/5")

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
        raise ValueError("Login form not found on login page")

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
        raise ValueError("No CSRF token found")
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
            raise ValueError(f"Login failed with errors: {', '.join(errors)}")

    if login_soup.find("form", {"class": "simple_form donor_login"}):
        raise ValueError("Login failed: Response contains login form, credentials may be incorrect")

    # Get redirect URL
    redirect_url = login_response.headers.get("X-Xhr-Redirect") or login_response.headers.get("Location")
    if not redirect_url:
        try:
            login_json = login_response.json()
            redirect_url = login_json.get("redirect_url")
            if not login_json.get("success", False):
                raise ValueError(f"Login failed: {login_json.get('message', 'Unknown error')}")
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
                raise ValueError("Login failed: No redirect URL found")
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
        center_id = re.search(r"/spendezentren/(\d+)/", center_href).group(1)
        centers.append({"name": center_name, "href": center_href, "id": center_id})
    if not centers:
        raise ValueError("No donation centers found")

    # Step 4: Select Leipzig donation center
    preferred_center_id = "0427711"
    selected_center = next((c for c in centers if c["id"] == preferred_center_id), None)
    if not selected_center:
        raise ValueError(f"Leipzig center (ID {preferred_center_id}) not found")
    print(f"Selected center: {selected_center['name']} (ID: {selected_center['id']})")

    # Get appointment page for Leipzig
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
        raise ValueError("No donation types found")

    # Step 5: Select Plasmaspende
    preferred_donation_type = "pp"
    selected_donation = next((d for d in donation_types if d["type"] == preferred_donation_type and not d["disabled"]), None)
    if not selected_donation:
        raise ValueError(f"Plasmaspende (type {preferred_donation_type}) not found or disabled")
    print(f"Selected donation type: {selected_donation['name']} (Type: {selected_donation['type']})")

    # GET plasma appointment page
    plasmaspende_url = make_absolute_url(selected_donation["href"])
    print(f"Fetching plasma appointment page: {plasmaspende_url}")
    headers["Referer"] = termine_url
    plasmaspende_response = session.get(plasmaspende_url, headers=headers)
    plasmaspende_response.raise_for_status()
    print(f"Plasma appointment page status code: {plasmaspende_response.status_code}")

    # Parse calendar for available dates
    plasmaspende_soup = BeautifulSoup(plasmaspende_response.text, "html.parser")
    calendar_div = plasmaspende_soup.find("div", class_="tab-content abstand calendar")
    if not calendar_div:
        raise ValueError("No calendar found")

    available_dates = []
    date_links = calendar_div.find_all("a", class_=re.compile(r"calendar-day-open"))
    for link in date_links:
        href = link["href"]
        context = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("reservation_context", [""])[0]
        context_data = decode_reservation_context(context)
        date = context_data.get("date")
        if date:
            date_obj = datetime.strptime(date, '%Y-%m-%d').date()
            if date_obj > datetime.now().date():
                available_dates.append({"date": date, "href": href})
    print(f"Available dates found: {[d['date'] for d in available_dates]}")

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
        print(f"Found slots for {date}: {[(s['time'], s['available_slots']) for s in slots]}")
        return slots

    # Attempt to book appointments for target dates
    bookings_made = 0
    proposed_bookings = []
    now = datetime.now()
    for preferred_date in target_dates:
        if bookings_made >= max_new_bookings:
            print(f"Reached maximum new bookings ({max_new_bookings}). Stopping.")
            break

        # Check 2-day gap rule
        can_book, conflict_date = check_minimum_gap(calendar_service, preferred_date)
        if not can_book:
            print(f"Skipping {preferred_date}: Conflicts with appointment on {conflict_date} (2-day gap rule)")
            continue

        # Try target date or nearby dates
        date_to_try = preferred_date
        date_obj = datetime.strptime(date_to_try, '%Y-%m-%d').date()
        if date_obj <= now.date():
            print(f"Skipping {preferred_date}: Date is in the past")
            continue
        selected_date = next((d for d in available_dates if d["date"] == date_to_try), None)
        if not selected_date:
            date_to_try = try_nearby_dates(preferred_date, available_dates, now)
            if date_to_try:
                selected_date = next((d for d in available_dates if d["date"] == date_to_try), None)
                print(f"Target date {preferred_date} not available, trying nearby date {date_to_try}")
            else:
                print(f"Preferred date {preferred_date} and nearby dates not available")
                continue

        # Fetch slots
        slots = fetch_slots(date_to_try, selected_date["href"], headers, session)
        if not slots:
            print(f"No available timeslots found for {date_to_try}")
            continue

        # If skip-booking is enabled, print slots and continue
        if args.skip_booking:
            print(f"Available timeslots for {date_to_try}:")
            for slot in slots:
                print(f"Time: {slot['time']}, Available Slots: {slot['available_slots']}, URL: {slot['url']}")
            continue

        # Select the earliest timeslot after 13:00
        min_time_minutes = 13 * 60
        valid_slots = [slot for slot in slots if parse_time(slot["time"]) and parse_time(slot["time"]) > min_time_minutes]
        if not valid_slots:
            print(f"No timeslots after 13:00 found for {date_to_try}")
            continue
        selected_slot = min(valid_slots, key=lambda x: parse_time(x["time"]))
        print(f"Selected timeslot: {selected_slot['time']} for {date_to_try}")

        if args.testing:
            proposed_bookings.append((date_to_try, selected_slot['time']))
            bookings_made += 1
            continue

        # Fetch the booking page
        booking_url = make_absolute_url(selected_slot["url"])
        print(f"Fetching booking page for {selected_slot['time']} on {date_to_try}: {booking_url}")
        headers["Referer"] = make_absolute_url(selected_date["href"])
        booking_response = session.get(booking_url, headers=headers, allow_redirects=True)
        booking_response.raise_for_status()
        print(f"Booking page status code: {booking_response.status_code}")

        # Parse booking page and prepare reservation form
        booking_soup = BeautifulSoup(booking_response.text, "html.parser")
        reservation_form = (
            booking_soup.find("form", {"class": re.compile(r"new_reservation|edit_reservation|reservation|form")}) or
            booking_soup.find("form", {"id": re.compile(r"new_reservation|edit_reservation|reservation")}) or
            booking_soup.find("form", {"action": re.compile(r"reservation|book|submit")}) or
            booking_soup.find("form", lambda tag: tag.find("input", {"name": re.compile(r"reservation\[.*\]")}))
        )

        if not reservation_form:
            raise ValueError(f"Reservation form not found on booking page for {date_to_try}")

        form_action = reservation_form.get("action")
        if not form_action:
            raise ValueError(f"No action attribute found in reservation form for {date_to_try}")
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
            raise ValueError(f"No CSRF token found in reservation form for {date_to_try}")
        print(f"Reservation CSRF token: {csrf_token}")

        email_input = reservation_form.find("input", {"id": "reservation_email"})
        email_confirmation_input = reservation_form.find("input", {"id": "reservation_email_confirmation"})

        if not email_input or not email_confirmation_input:
            raise ValueError(f"Email input fields not found in form for {date_to_try}")

        email_field_name = email_input.get("name")
        email_confirmation_field_name = email_confirmation_input.get("name")

        if not email_field_name or not email_confirmation_field_name:
            raise ValueError(f"Name attributes missing for email input fields for {date_to_try}")

        print(f"Email field name: {email_field_name}")
        print(f"Email confirmation field name: {email_confirmation_field_name}")

        reservation_data = {
            email_field_name: RESERVATION_EMAIL,
            email_confirmation_field_name: RESERVATION_EMAIL,
            "reservation[receive_email_reminder]": "1",
            "authenticity_token": csrf_token,
            "button": ""
        }

        reserved_fields = [
            email_field_name,
            email_confirmation_field_name,
            "reservation[receive_email_reminder]",
            "authenticity_token",
            "button"
        ]

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
            if selected_option:
                reservation_data[name] = selected_option["value"]

        print(f"Reservation form data: {reservation_data}")

        # Submit reservation form
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
                raise ValueError(f"Reservation submission failed for {date_to_try} with errors: {', '.join(errors)}")
        success_messages = reservation_soup.find_all(["div", "p", "span"], class_=re.compile(r"success|confirmation"))
        if success_messages:
            successes = [msg.text.strip() for msg in success_messages if msg.text.strip()]
            print(f"Reservation successful for {date_to_try}: {', '.join(successes)}")
        else:
            print(f"Reservation submitted for {date_to_try}, no explicit success message found")

        # Add appointment to Google Calendar
        print(f"Adding appointment to Google Calendar for {date_to_try}...")
        add_appointment_to_calendar(calendar_service, date_to_try, selected_slot['time'])
        bookings_made += 1
        print(f"Bookings made: {bookings_made}/{max_new_bookings}")

    if args.testing and proposed_bookings:
        print("Proposed bookings:")
        for date, time in proposed_bookings:
            print(f"- {date} at {time}")
    elif args.testing:
        print("No proposed bookings due to availability or constraints")

except Exception as e:
    print(f"Error: {str(e)}")
    raise

finally:
    session.close()
    if os.path.exists(SERVICE_ACCOUNT_FILE):
        os.remove(SERVICE_ACCOUNT_FILE)
    print("Session closed")

#Copyright @Arslan-MD
#Updates Channel t.me/arslanmd
from flask import Flask, request, jsonify
from datetime import datetime
import cloudscraper
import json
from bs4 import BeautifulSoup
import logging
import os
import gzip
from io import BytesIO
from pathlib import Path
import re
import brotli

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

API_DATE_FORMAT = '%d/%m/%Y'
IVAS_DATE_FORMAT = '%Y-%m-%d'
SUPPORTED_INPUT_DATE_FORMATS = (API_DATE_FORMAT, IVAS_DATE_FORMAT)
BASE_DIR = Path(__file__).resolve().parent
try:
    IVAS_REQUEST_TIMEOUT = int(os.getenv("IVAS_REQUEST_TIMEOUT", "30"))
except ValueError:
    IVAS_REQUEST_TIMEOUT = 30


def parse_supported_date(date_str):
    cleaned_date = (date_str or '').strip()
    if not cleaned_date:
        raise ValueError('Date parameter is required')

    for date_format in SUPPORTED_INPUT_DATE_FORMATS:
        try:
            return datetime.strptime(cleaned_date, date_format)
        except ValueError:
            continue

    raise ValueError('Invalid date format. Use DD/MM/YYYY or YYYY-MM-DD')

class IVASSMSClient:
    def __init__(self):
        self.scraper = cloudscraper.create_scraper()
        self.base_url = "https://www.ivasms.com"
        self.logged_in = False
        self.csrf_token = None
        self.auth_error = None
        
        self.scraper.headers.update({
            'User-Agent': 'Mozilla/5.0 (Linux; Android 8.0.0; SM-G955U Build/R16NW) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Mobile Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Cache-Control': 'max-age=0',
        })

    def set_auth_failure(self, message):
        self.logged_in = False
        self.csrf_token = None
        self.auth_error = message
        logger.error(message)

    def decompress_response(self, response):
        """Decompress response content if encoded with gzip or brotli."""
        encoding = response.headers.get('Content-Encoding', '').lower()
        content = response.content
        try:
            if encoding == 'gzip' and content[:2] == b'\x1f\x8b':
                logger.debug("Decompressing gzip response")
                content = gzip.decompress(content)
            elif encoding == 'br':
                if content.startswith(b'<') or content.startswith(b'\n<'):
                    return response.text
                logger.debug("Decompressing brotli response")
                content = brotli.decompress(content)
            return content.decode('utf-8', errors='replace')
        except Exception as e:
            logger.debug(f"Falling back to response.text after decompression check: {e}")
            return response.text

    def _extract_script_html_value(self, html_content, element_id):
        pattern = re.compile(
            rf'\$\("#{re.escape(element_id)}"\)\.html\(["\']([^"\']*)["\']\);'
        )
        match = pattern.search(html_content)
        return match.group(1).strip() if match else None

    def _clean_text(self, node, separator=' ', default=''):
        if not node:
            return default
        return node.get_text(separator, strip=True)

    def _clean_currency(self, value, default='0'):
        cleaned = (value or '').replace('USD', '').replace('$', '').strip()
        return cleaned or default

    def _extract_onclick_args(self, onclick_value):
        if not onclick_value:
            return []
        return re.findall(r"'([^']*)'", onclick_value)

    def _parse_summary_html(self, html_content):
        soup = BeautifulSoup(html_content, 'html.parser')

        count_sms = self._clean_text(soup.select_one("#CountSMS")) or self._extract_script_html_value(
            html_content, 'CountSMS'
        ) or '0'
        paid_sms = self._clean_text(soup.select_one("#PaidSMS")) or self._extract_script_html_value(
            html_content, 'PaidSMS'
        ) or '0'
        unpaid_sms = self._clean_text(soup.select_one("#UnpaidSMS")) or self._extract_script_html_value(
            html_content, 'UnpaidSMS'
        ) or '0'
        revenue_sms = self._clean_currency(
            self._clean_text(soup.select_one("#RevenueSMS")) or self._extract_script_html_value(
                html_content, 'RevenueSMS'
            )
        )

        sms_details = []

        legacy_items = soup.select("div.item")
        for item in legacy_items:
            country_number = self._clean_text(item.select_one(".col-sm-4"))
            count = self._clean_text(item.select_one(".col-3:nth-child(2) p"), default='0')
            paid = self._clean_text(item.select_one(".col-3:nth-child(3) p"), default='0')
            unpaid = self._clean_text(item.select_one(".col-3:nth-child(4) p"), default='0')
            revenue = self._clean_currency(
                self._clean_text(item.select_one(".col-3:nth-child(5) p span.currency_cdr"))
            )

            sms_details.append({
                'country_number': country_number,
                'count': count,
                'paid': paid,
                'unpaid': unpaid,
                'revenue': revenue
            })

        if not sms_details:
            modern_items = soup.select("div.rng")
            for item in modern_items:
                country_number = self._clean_text(item.select_one(".rname"))
                count = self._clean_text(item.select_one(".v-count"), default='0')
                paid = self._clean_text(item.select_one(".v-paid"), default='0')
                unpaid = self._clean_text(item.select_one(".v-unpaid"), default='0')
                revenue = self._clean_currency(self._clean_text(item.select_one(".v-rev")))

                sms_details.append({
                    'country_number': country_number,
                    'count': count,
                    'paid': paid,
                    'unpaid': unpaid,
                    'revenue': revenue
                })

        return {
            'count_sms': count_sms,
            'paid_sms': paid_sms,
            'unpaid_sms': unpaid_sms,
            'revenue': revenue_sms,
            'sms_details': sms_details
        }

    def _parse_number_details_html(self, html_content):
        soup = BeautifulSoup(html_content, 'html.parser')
        number_details = []

        legacy_items = soup.select("div.card.card-body")
        for item in legacy_items:
            phone_number = self._clean_text(item.select_one(".col-sm-4"))
            count = self._clean_text(item.select_one(".col-3:nth-child(2) p"), default='0')
            paid = self._clean_text(item.select_one(".col-3:nth-child(3) p"), default='0')
            unpaid = self._clean_text(item.select_one(".col-3:nth-child(4) p"), default='0')
            revenue = self._clean_currency(
                self._clean_text(item.select_one(".col-3:nth-child(5) p span.currency_cdr"))
            )
            onclick_args = self._extract_onclick_args(item.select_one(".col-sm-4").get('onclick', '') if item.select_one(".col-sm-4") else '')
            id_number = onclick_args[3] if len(onclick_args) > 3 else ''

            number_details.append({
                'phone_number': phone_number,
                'count': count,
                'paid': paid,
                'unpaid': unpaid,
                'revenue': revenue,
                'id_number': id_number
            })

        if not number_details:
            modern_items = soup.select("div.nrow")
            for item in modern_items:
                onclick_args = self._extract_onclick_args(item.get('onclick', ''))
                phone_number = self._clean_text(item.select_one(".nnum"))
                if onclick_args:
                    phone_number = onclick_args[0]
                count = self._clean_text(item.select_one(".v-count"), default='0')
                paid = self._clean_text(item.select_one(".v-paid"), default='0')
                unpaid = self._clean_text(item.select_one(".v-unpaid"), default='0')
                revenue = self._clean_currency(self._clean_text(item.select_one(".v-rev")))
                id_number = onclick_args[1] if len(onclick_args) > 1 else ''

                number_details.append({
                    'phone_number': phone_number,
                    'count': count,
                    'paid': paid,
                    'unpaid': unpaid,
                    'revenue': revenue,
                    'id_number': id_number
                })

        return number_details

    def _parse_otp_message_html(self, html_content):
        soup = BeautifulSoup(html_content, 'html.parser')

        legacy_message = soup.select_one(".col-9.col-sm-6 p")
        if legacy_message:
            return self._clean_text(legacy_message, separator='\n')

        message_nodes = soup.select("div.msg-text")
        messages = [self._clean_text(node, separator='\n') for node in message_nodes if self._clean_text(node, separator='\n')]
        if messages:
            return "\n\n".join(messages)

        return None

    def _is_login_page(self, response, html_content):
        final_url = (getattr(response, 'url', '') or '').lower()
        if '/login' in final_url:
            return True

        soup = BeautifulSoup(html_content, 'html.parser')
        return bool(
            soup.select_one("form[action*='/login']")
            or soup.select_one("input[name='email']")
            or soup.select_one("input[name='password']")
        )

    def load_cookies(self, file_path="cookies.json"):
        try:
            if os.getenv("COOKIES_JSON"):
                cookies_raw = json.loads(os.getenv("COOKIES_JSON"))
                logger.debug("Loaded cookies from environment variable")
            else:
                cookie_path = Path(file_path)
                if not cookie_path.is_absolute():
                    cookie_path = BASE_DIR / cookie_path

                with cookie_path.open('r', encoding='utf-8') as file:
                    cookies_raw = json.load(file)
                    logger.debug(f"Loaded cookies from file: {cookie_path}")
            
            if isinstance(cookies_raw, dict):
                logger.debug("Cookies loaded as dictionary")
                return cookies_raw
            elif isinstance(cookies_raw, list):
                cookies = {}
                for cookie in cookies_raw:
                    if 'name' in cookie and 'value' in cookie:
                        cookies[cookie['name']] = cookie['value']
                logger.debug("Cookies loaded as list")
                return cookies
            else:
                self.set_auth_failure("Cookies are in an unsupported format.")
                raise ValueError("Cookies are in an unsupported format.")
        except FileNotFoundError:
            cookie_path = Path(file_path)
            if not cookie_path.is_absolute():
                cookie_path = BASE_DIR / cookie_path
            self.set_auth_failure(
                f"Cookie file not found at {cookie_path}. Set COOKIES_JSON or add cookies.json next to app.py."
            )
            return None
        except json.JSONDecodeError:
            self.set_auth_failure("Invalid JSON format in cookies.json or COOKIES_JSON")
            return None
        except Exception as e:
            self.set_auth_failure(f"Error loading cookies: {e}")
            return None

    def login_with_cookies(self, cookies_file="cookies.json"):
        logger.debug("Attempting to login with cookies")
        self.logged_in = False
        self.csrf_token = None
        self.auth_error = None
        cookies = self.load_cookies(cookies_file)
        if not cookies:
            if not self.auth_error:
                self.set_auth_failure(
                    "No valid cookies loaded. Set COOKIES_JSON or add cookies.json next to app.py."
                )
            return False
        
        self.scraper.cookies.clear()
        for name, value in cookies.items():
            self.scraper.cookies.set(name, value, domain="www.ivasms.com")
        
        try:
            response = self.scraper.get(f"{self.base_url}/portal/sms/received", timeout=IVAS_REQUEST_TIMEOUT)
            logger.debug(f"Response headers: {response.headers}")
            if response.status_code == 200:
                html_content = self.decompress_response(response)
                if self._is_login_page(response, html_content):
                    self.set_auth_failure(
                        "IVAS cookies are expired or no longer authenticated. Refresh the IVAS session cookies."
                    )
                    return False
                soup = BeautifulSoup(html_content, 'html.parser')
                csrf_input = soup.find('input', {'name': '_token'})
                if csrf_input:
                    self.csrf_token = csrf_input.get('value')
                    self.logged_in = True
                    self.auth_error = None
                    logger.debug(f"Logged in successfully with CSRF token: {self.csrf_token}")
                    return True
                else:
                    self.set_auth_failure(
                        "IVAS login did not return the expected CSRF token. Refresh the IVAS session cookies."
                    )
                    logger.error("Response HTML (first 2000 chars): %s", html_content[:2000])
                    logger.error(f"Full response length: {len(html_content)}")
                    return False
            self.set_auth_failure(f"IVAS login failed with status code {response.status_code}")
            return False
        except Exception as e:
            self.set_auth_failure(f"IVAS login error: {e}")
            return False

    def ensure_authenticated(self, cookies_file="cookies.json"):
        if self.logged_in and self.csrf_token:
            return True

        return self.login_with_cookies(cookies_file)

    def check_otps(self, from_date="", to_date=""):
        if not self.logged_in:
            logger.error("Not logged in")
            return None
        
        if not self.csrf_token:
            logger.error("No CSRF token available")
            return None
        
        logger.debug(f"Checking OTPs from {from_date} to {to_date}")
        try:
            payload = {
                'from': from_date,
                'to': to_date,
                '_token': self.csrf_token
            }
            
            headers = {
                'Accept': 'text/html, */*; q=0.01',
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'X-Requested-With': 'XMLHttpRequest',
                'Origin': self.base_url,
                'Referer': f"{self.base_url}/portal/sms/received"
            }
            
            response = self.scraper.post(
                f"{self.base_url}/portal/sms/received/getsms",
                data=payload,
                headers=headers,
                timeout=IVAS_REQUEST_TIMEOUT
            )
            
            if response.status_code == 200:
                logger.debug("Successfully retrieved SMS data")
                html_content = self.decompress_response(response)
                if self._is_login_page(response, html_content):
                    self.set_auth_failure(
                        "IVAS redirected the SMS summary request to login. Refresh the IVAS session cookies."
                    )
                    return None
                result = self._parse_summary_html(html_content)
                result['raw_response'] = html_content
                logger.debug(f"Retrieved {len(result['sms_details'])} SMS detail records: {result['sms_details']}")
                return result
            logger.error(f"Failed to check OTPs. Status code: {response.status_code}, Response: {self.decompress_response(response)[:2000]}")
            return None
        except Exception as e:
            logger.error(f"Error checking OTPs: {e}")
            return None

    def get_sms_details(self, phone_range, from_date="", to_date=""):
        if not self.logged_in:
            logger.error("Not logged in")
            return None
        
        logger.debug(f"Fetching SMS details for range: {phone_range}, from {from_date} to {to_date}")
        try:
            payload = {
                '_token': self.csrf_token,
                'start': from_date,
                'end': to_date,
                'range': phone_range
            }
            
            headers = {
                'Accept': 'text/html, */*; q=0.01',
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'X-Requested-With': 'XMLHttpRequest',
                'Origin': self.base_url,
                'Referer': f"{self.base_url}/portal/sms/received"
            }
            
            response = self.scraper.post(
                f"{self.base_url}/portal/sms/received/getsms/number",
                data=payload,
                headers=headers,
                timeout=IVAS_REQUEST_TIMEOUT
            )
            
            if response.status_code == 200:
                html_content = self.decompress_response(response)
                if self._is_login_page(response, html_content):
                    self.set_auth_failure(
                        f"IVAS redirected the number detail request for {phone_range} to login."
                    )
                    return None
                number_details = self._parse_number_details_html(html_content)
                logger.debug(f"Retrieved {len(number_details)} number details for range {phone_range}: {number_details}")
                return number_details
            logger.error(f"Failed to get SMS details for {phone_range}. Status code: {response.status_code}, Response: {self.decompress_response(response)[:2000]}")
            return None
        except Exception as e:
            logger.error(f"Error getting SMS details for {phone_range}: {e}")
            return None

    def get_otp_message(self, phone_number, phone_range, from_date="", to_date=""):
        if not self.logged_in:
            logger.error("Not logged in")
            return None
        
        logger.debug(f"Fetching OTP message for phone: {phone_number}, range: {phone_range}, from {from_date} to {to_date}")
        try:
            payload = {
                '_token': self.csrf_token,
                'start': from_date,
                'end': to_date,
                'Number': phone_number,
                'Range': phone_range
            }
            
            headers = {
                'Accept': 'text/html, */*; q=0.01',
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'X-Requested-With': 'XMLHttpRequest',
                'Origin': self.base_url,
                'Referer': f"{self.base_url}/portal/sms/received"
            }
            
            response = self.scraper.post(
                f"{self.base_url}/portal/sms/received/getsms/number/sms",
                data=payload,
                headers=headers,
                timeout=IVAS_REQUEST_TIMEOUT
            )
            
            if response.status_code == 200:
                html_content = self.decompress_response(response)
                if self._is_login_page(response, html_content):
                    self.set_auth_failure(
                        f"IVAS redirected the OTP message request for {phone_number} to login."
                    )
                    return None
                message = self._parse_otp_message_html(html_content)
                logger.debug(f"Retrieved OTP message for {phone_number}: {message}")
                return message
            logger.error(f"Failed to get OTP message for {phone_number}. Status code: {response.status_code}, Response: {self.decompress_response(response)[:2000]}")
            return None
        except Exception as e:
            logger.error(f"Error getting OTP message for {phone_number}: {e}")
            return None

    def get_all_otp_messages(self, sms_details, from_date="", to_date="", limit=None):
        all_otp_messages = []
        
        logger.debug(f"Processing {len(sms_details)} SMS details for OTP messages with limit {limit}")
        for detail in sms_details:
            phone_range = detail['country_number']
            number_details = self.get_sms_details(phone_range, from_date, to_date)
            
            if number_details:
                for number_detail in number_details:
                    if limit is not None and len(all_otp_messages) >= limit:
                        logger.debug(f"Reached limit of {limit} OTP messages, stopping")
                        return all_otp_messages
                    phone_number = number_detail['phone_number']
                    otp_message = self.get_otp_message(phone_number, phone_range, from_date, to_date)
                    if otp_message:
                        all_otp_messages.append({
                            'range': phone_range,
                            'phone_number': phone_number,
                            'otp_message': otp_message
                        })
                        logger.debug(f"Added OTP message for {phone_number}: {otp_message}")
            else:
                logger.warning(f"No number details found for range: {phone_range}")
        
        logger.debug(f"Collected {len(all_otp_messages)} OTP messages")
        return all_otp_messages

app = Flask(__name__)
client = IVASSMSClient()

with app.app_context():
    if os.getenv("SKIP_IVAS_LOGIN") == "1":
        logger.debug("Skipping IVAS login because SKIP_IVAS_LOGIN=1")
    elif not client.login_with_cookies():
        logger.error("Failed to initialize client with cookies")

@app.route('/')
def welcome():
    return jsonify({
        'message': 'Welcome to the IVAS SMS API',
        'status': 'API is alive',
        'endpoints': {
            '/sms': 'Get OTP messages for a specific date (format: DD/MM/YYYY or YYYY-MM-DD) with optional limit. Example: /sms?date=01/05/2025&limit=10'
        }
    })

@app.route('/sms')
def get_sms():
    date_str = (request.args.get('date') or '').strip()
    limit = request.args.get('limit')
    
    if not date_str:
        return jsonify({
            'error': 'Date parameter is required in DD/MM/YYYY or YYYY-MM-DD format'
        }), 400
    
    try:
        from_date_dt = parse_supported_date(date_str)
        to_date_raw = (request.args.get('to_date') or '').strip()
        # The IVAS panel submits both start and end dates, even for a single day.
        to_date_dt = parse_supported_date(to_date_raw) if to_date_raw else from_date_dt
        if to_date_dt < from_date_dt:
            return jsonify({
                'error': 'to_date must be the same day or after date'
            }), 400
    except ValueError:
        return jsonify({
            'error': 'Invalid date format. Use DD/MM/YYYY or YYYY-MM-DD'
        }), 400

    from_date = from_date_dt.strftime(IVAS_DATE_FORMAT)
    to_date = to_date_dt.strftime(IVAS_DATE_FORMAT)
    response_from_date = from_date_dt.strftime(API_DATE_FORMAT)
    response_to_date = to_date_dt.strftime(API_DATE_FORMAT) if to_date_raw else 'Not specified'

    if limit:
        try:
            limit = int(limit)
            if limit <= 0:
                return jsonify({
                    'error': 'Limit must be a positive integer'
                }), 400
        except ValueError:
            return jsonify({
                'error': 'Limit must be a valid integer'
            }), 400
    else:
        limit = None

    if not client.ensure_authenticated():
        return jsonify({
            'error': 'Client not authenticated',
            'details': client.auth_error or 'Unable to authenticate with IVAS using the configured cookies.',
            'hint': 'Refresh the IVAS cookies and set them via COOKIES_JSON or place cookies.json next to app.py.'
        }), 401
    
    logger.debug(f"Fetching SMS for date range: {from_date} to {to_date or 'empty'} with limit {limit}")
    result = client.check_otps(from_date=from_date, to_date=to_date)
    
    if not result:
        return jsonify({
            'error': 'Failed to fetch OTP data'
        }), 500

    otp_messages = client.get_all_otp_messages(result.get('sms_details', []), from_date=from_date, to_date=to_date, limit=limit)
    
    return jsonify({
        'status': 'success',
        'from_date': response_from_date,
        'to_date': response_to_date,
        'limit': limit if limit is not None else 'Not specified',
        'sms_stats': {
            'count_sms': result['count_sms'],
            'paid_sms': result['paid_sms'],
            'unpaid_sms': result['unpaid_sms'],
            'revenue': result['revenue']
        },
        'otp_messages': otp_messages
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)

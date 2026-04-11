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
import brotli

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

API_DATE_FORMAT = '%d/%m/%Y'
IVAS_DATE_FORMAT = '%Y-%m-%d'
SUPPORTED_INPUT_DATE_FORMATS = (API_DATE_FORMAT, IVAS_DATE_FORMAT)


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
        
        self.scraper.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36',
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

    def decompress_response(self, response):
        """Decompress response content if encoded with gzip or brotli."""
        encoding = response.headers.get('Content-Encoding', '').lower()
        content = response.content
        try:
            if encoding == 'gzip':
                logger.debug("Decompressing gzip response")
                content = gzip.decompress(content)
            elif encoding == 'br':
                logger.debug("Decompressing brotli response")
                content = brotli.decompress(content)
            return content.decode('utf-8', errors='replace')
        except Exception as e:
            logger.error(f"Error decompressing response: {e}")
            return response.text

    def load_cookies(self, file_path="cookies.json"):
        try:
            if os.getenv("COOKIES_JSON"):
                cookies_raw = json.loads(os.getenv("COOKIES_JSON"))
                logger.debug("Loaded cookies from environment variable")
            else:
                with open(file_path, 'r') as file:
                    cookies_raw = json.load(file)
                    logger.debug("Loaded cookies from file")
            
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
                logger.error("Cookies are in an unsupported format")
                raise ValueError("Cookies are in an unsupported format.")
        except FileNotFoundError:
            logger.error("cookies.json file not found")
            return None
        except json.JSONDecodeError:
            logger.error("Invalid JSON format in cookies.json")
            return None
        except Exception as e:
            logger.error(f"Error loading cookies: {e}")
            return None

    def login_with_cookies(self, cookies_file="cookies.json"):
        logger.debug("Attempting to login with cookies")
        cookies = self.load_cookies(cookies_file)
        if not cookies:
            logger.error("No valid cookies loaded")
            return False
        
        for name, value in cookies.items():
            self.scraper.cookies.set(name, value, domain="www.ivasms.com")
        
        try:
            response = self.scraper.get(f"{self.base_url}/portal/sms/received", timeout=10)
            logger.debug(f"Response headers: {response.headers}")
            if response.status_code == 200:
                html_content = self.decompress_response(response)
                soup = BeautifulSoup(html_content, 'html.parser')
                csrf_input = soup.find('input', {'name': '_token'})
                if csrf_input:
                    self.csrf_token = csrf_input.get('value')
                    self.logged_in = True
                    logger.debug(f"Logged in successfully with CSRF token: {self.csrf_token}")
                    return True
                else:
                    logger.error("Could not find CSRF token. Dumping response HTML for debugging:")
                    logger.error(f"Response HTML (first 2000 chars): {html_content[:2000]}")
                    logger.error(f"Full response length: {len(html_content)}")
                    return False
            logger.error(f"Login failed with status code: {response.status_code}")
            return False
        except Exception as e:
            logger.error(f"Login error: {e}")
            return False

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
                timeout=10
            )
            
            if response.status_code == 200:
                logger.debug("Successfully retrieved SMS data")
                html_content = self.decompress_response(response)
                soup = BeautifulSoup(html_content, 'html.parser')
                
                count_sms = soup.select_one("#CountSMS").text if soup.select_one("#CountSMS") else '0'
                paid_sms = soup.select_one("#PaidSMS").text if soup.select_one("#PaidSMS") else '0'
                unpaid_sms = soup.select_one("#UnpaidSMS").text if soup.select_one("#UnpaidSMS") else '0'
                revenue_sms = soup.select_one("#RevenueSMS").text.replace(' USD', '') if soup.select_one("#RevenueSMS") else '0'
                
                sms_details = []
                items = soup.select("div.item")
                for item in items:
                    country_number = item.select_one(".col-sm-4").text.strip()
                    count = item.select_one(".col-3:nth-child(2) p").text.strip()
                    paid = item.select_one(".col-3:nth-child(3) p").text.strip()
                    unpaid = item.select_one(".col-3:nth-child(4) p").text.strip()
                    revenue = item.select_one(".col-3:nth-child(5) p span.currency_cdr").text.strip()
                    
                    sms_details.append({
                        'country_number': country_number,
                        'count': count,
                        'paid': paid,
                        'unpaid': unpaid,
                        'revenue': revenue
                    })
                
                result = {
                    'count_sms': count_sms,
                    'paid_sms': paid_sms,
                    'unpaid_sms': unpaid_sms,
                    'revenue': revenue_sms,
                    'sms_details': sms_details
                }
                result['raw_response'] = html_content
                logger.debug(f"Retrieved {len(sms_details)} SMS detail records: {sms_details}")
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
                timeout=10
            )
            
            if response.status_code == 200:
                html_content = self.decompress_response(response)
                soup = BeautifulSoup(html_content, 'html.parser')
                number_details = []
                items = soup.select("div.card.card-body")
                for item in items:
                    phone_number = item.select_one(".col-sm-4").text.strip()
                    count = item.select_one(".col-3:nth-child(2) p").text.strip()
                    paid = item.select_one(".col-3:nth-child(3) p").text.strip()
                    unpaid = item.select_one(".col-3:nth-child(4) p").text.strip()
                    revenue = item.select_one(".col-3:nth-child(5) p span.currency_cdr").text.strip()
                    onclick = item.select_one(".col-sm-4").get('onclick', '')
                    id_number = onclick.split("'")[3] if onclick else ''
                    
                    number_details.append({
                        'phone_number': phone_number,
                        'count': count,
                        'paid': paid,
                        'unpaid': unpaid,
                        'revenue': revenue,
                        'id_number': id_number
                    })
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
                timeout=10
            )
            
            if response.status_code == 200:
                html_content = self.decompress_response(response)
                soup = BeautifulSoup(html_content, 'html.parser')
                message = soup.select_one(".col-9.col-sm-6 p").text.strip() if soup.select_one(".col-9.col-sm-6 p") else None
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

    if not client.logged_in:
        return jsonify({
            'error': 'Client not authenticated'
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

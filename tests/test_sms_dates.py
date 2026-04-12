import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch


os.environ["SKIP_IVAS_LOGIN"] = "1"

import app as app_module


class DummyClient:
    def __init__(self, logged_in=True, ensure_authenticated_result=True, auth_error=None):
        self.logged_in = logged_in
        self.ensure_authenticated_result = ensure_authenticated_result
        self.auth_error = auth_error
        self.ensure_authenticated_calls = 0
        self.check_otps_calls = []
        self.get_all_otp_messages_calls = []

    def ensure_authenticated(self):
        self.ensure_authenticated_calls += 1
        if self.ensure_authenticated_result:
            self.logged_in = True
        return self.ensure_authenticated_result

    def check_otps(self, from_date="", to_date=""):
        self.check_otps_calls.append((from_date, to_date))
        return {
            'count_sms': '40',
            'paid_sms': '40',
            'unpaid_sms': '0',
            'revenue': '0.48',
            'sms_details': [{'country_number': 'SENEGAL 5966'}],
        }

    def get_all_otp_messages(self, sms_details, from_date="", to_date="", limit=None):
        self.get_all_otp_messages_calls.append((sms_details, from_date, to_date, limit))
        return []


class SmsDateHandlingTests(unittest.TestCase):
    def setUp(self):
        self.original_client = app_module.client
        self.dummy_client = DummyClient()
        app_module.client = self.dummy_client
        app_module.app.config['TESTING'] = True
        self.http_client = app_module.app.test_client()

    def tearDown(self):
        app_module.client = self.original_client

    def test_single_day_queries_send_iso_dates_to_ivas(self):
        response = self.http_client.get('/sms?date=11/04/2026')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.dummy_client.check_otps_calls, [('2026-04-11', '2026-04-11')])
        self.assertEqual(
            self.dummy_client.get_all_otp_messages_calls,
            [([{'country_number': 'SENEGAL 5966'}], '2026-04-11', '2026-04-11', None)],
        )

        payload = response.get_json()
        self.assertEqual(payload['from_date'], '11/04/2026')
        self.assertEqual(payload['to_date'], 'Not specified')
        self.assertEqual(payload['sms_stats']['count_sms'], '40')
        self.assertEqual(self.dummy_client.ensure_authenticated_calls, 1)

    def test_iso_input_dates_are_accepted(self):
        response = self.http_client.get('/sms?date=2026-04-11&to_date=2026-04-12&limit=5')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.dummy_client.check_otps_calls, [('2026-04-11', '2026-04-12')])
        self.assertEqual(
            self.dummy_client.get_all_otp_messages_calls,
            [([{'country_number': 'SENEGAL 5966'}], '2026-04-11', '2026-04-12', 5)],
        )

        payload = response.get_json()
        self.assertEqual(payload['from_date'], '11/04/2026')
        self.assertEqual(payload['to_date'], '12/04/2026')
        self.assertEqual(payload['limit'], 5)
        self.assertEqual(self.dummy_client.ensure_authenticated_calls, 1)

    def test_rejects_ranges_where_to_date_is_before_from_date(self):
        response = self.http_client.get('/sms?date=12/04/2026&to_date=11/04/2026')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json(), {'error': 'to_date must be the same day or after date'})

    def test_reauthenticates_when_client_starts_logged_out(self):
        self.dummy_client = DummyClient(logged_in=False, ensure_authenticated_result=True)
        app_module.client = self.dummy_client

        response = self.http_client.get('/sms?date=11/04/2026')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.dummy_client.ensure_authenticated_calls, 1)
        self.assertEqual(self.dummy_client.check_otps_calls, [('2026-04-11', '2026-04-11')])

    def test_returns_actionable_401_when_authentication_fails(self):
        self.dummy_client = DummyClient(
            logged_in=False,
            ensure_authenticated_result=False,
            auth_error='Cookie file not found at /var/task/cookies.json',
        )
        app_module.client = self.dummy_client

        response = self.http_client.get('/sms?date=11/04/2026')

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.get_json(),
            {
                'error': 'Client not authenticated',
                'details': 'Cookie file not found at /var/task/cookies.json',
                'hint': 'Refresh the IVAS cookies and set them via COOKIES_JSON or place cookies.json next to app.py.',
            },
        )


class CookieLoadingTests(unittest.TestCase):
    def test_load_cookies_resolves_relative_paths_from_app_directory(self):
        scratch_root = app_module.BASE_DIR / 'tests' / '__tmp_cookie_loading__'
        cookie_root = scratch_root / 'cookies'
        other_cwd = scratch_root / 'cwd'
        cookie_path = cookie_root / 'cookies.json'
        shutil.rmtree(scratch_root, ignore_errors=True)
        cookie_root.mkdir(parents=True, exist_ok=True)
        other_cwd.mkdir(parents=True, exist_ok=True)
        cookie_path.write_text(
            json.dumps([{'name': 'ivas_sms_session', 'value': 'session-value'}]),
            encoding='utf-8',
        )
        client = app_module.IVASSMSClient()

        with patch.object(app_module, 'BASE_DIR', cookie_root):
            original_cwd = os.getcwd()
            try:
                os.chdir(other_cwd)
                self.assertEqual(
                    client.load_cookies(),
                    {'ivas_sms_session': 'session-value'},
                )
            finally:
                os.chdir(original_cwd)
                shutil.rmtree(scratch_root, ignore_errors=True)


class IvasHtmlParsingTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.IVASSMSClient()

    def test_parses_modern_summary_markup_with_script_totals(self):
        html = """
        <div class="rng" onclick="toggleRange('SENEGAL 5767','SENEGAL_5767')">
            <div class="inner">
                <div class="c-name"><span class="rname">SENEGAL 5767</span></div>
                <div class="c-val v-count">8</div>
                <div class="c-val v-paid">8</div>
                <div class="c-val v-unpaid">0</div>
                <div class="c-val v-rev">0.10 <small>USD</small></div>
            </div>
        </div>
        <div class="rng" onclick="toggleRange('SENEGAL 5966','SENEGAL_5966')">
            <div class="inner">
                <div class="c-name"><span class="rname">SENEGAL 5966</span></div>
                <div class="c-val v-count">23</div>
                <div class="c-val v-paid">23</div>
                <div class="c-val v-unpaid">0</div>
                <div class="c-val v-rev">0.28 <small>USD</small></div>
            </div>
        </div>
        <script>
        $("#CountSMS").html("40");
        $("#PaidSMS").html("40");
        $("#UnpaidSMS").html("0");
        $("#RevenueSMS").html("$0.48");
        </script>
        """

        parsed = self.client._parse_summary_html(html)

        self.assertEqual(parsed['count_sms'], '40')
        self.assertEqual(parsed['paid_sms'], '40')
        self.assertEqual(parsed['unpaid_sms'], '0')
        self.assertEqual(parsed['revenue'], '0.48')
        self.assertEqual(
            parsed['sms_details'],
            [
                {
                    'country_number': 'SENEGAL 5767',
                    'count': '8',
                    'paid': '8',
                    'unpaid': '0',
                    'revenue': '0.10',
                },
                {
                    'country_number': 'SENEGAL 5966',
                    'count': '23',
                    'paid': '23',
                    'unpaid': '0',
                    'revenue': '0.28',
                },
            ],
        )

    def test_parses_modern_number_detail_markup(self):
        html = """
        <div class="nrow" onclick="toggleNumMrJWr('221761821066','221761821066_206578574')">
            <div class="c-name">
                <span class="nnum"><span class="ph"></span> 221761821066</span>
            </div>
            <div class="c-val v-count">1</div>
            <div class="c-val v-paid">1</div>
            <div class="c-val v-unpaid">0</div>
            <div class="c-val v-rev">0.01 <small>USD</small></div>
        </div>
        """

        parsed = self.client._parse_number_details_html(html)

        self.assertEqual(
            parsed,
            [
                {
                    'phone_number': '221761821066',
                    'count': '1',
                    'paid': '1',
                    'unpaid': '0',
                    'revenue': '0.01',
                    'id_number': '221761821066_206578574',
                }
            ],
        )

    def test_parses_modern_message_markup(self):
        html = """
        <table>
            <tbody>
                <tr>
                    <td><div class="msg-text">Votre code WhatsApp : 368-257</div></td>
                </tr>
            </tbody>
        </table>
        """

        parsed = self.client._parse_otp_message_html(html)

        self.assertEqual(parsed, 'Votre code WhatsApp : 368-257')

    def test_detects_login_page_html(self):
        class ResponseStub:
            url = 'https://www.ivasms.com/login'

        self.assertTrue(
            self.client._is_login_page(
                ResponseStub(),
                '<form action="https://www.ivasms.com/login"><input name="email"><input name="password"></form>',
            )
        )


if __name__ == '__main__':
    unittest.main()

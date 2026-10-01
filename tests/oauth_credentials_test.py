# Copyright 2026 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for the Google Ads OAuth credentials file and its authorize helper.

Only fake credentials are used, and no request reaches Google: the token
endpoint and the consent flow are mocked.
"""

import contextlib
import datetime
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import traceback
import unittest
import urllib.parse
from unittest import mock

from google.auth.exceptions import DefaultCredentialsError
from google.oauth2.credentials import Credentials

from ads_mcp import oauth_credentials
from ads_mcp import utils

ADS_SCOPE = "https://www.googleapis.com/auth/adwords"
TOKEN_URI = "https://oauth2.googleapis.com/token"
ADS_API_URL = "https://googleads.googleapis.com/"
ENV_VAR = "GOOGLE_ADS_OAUTH_CREDENTIALS"

FAKE_CLIENT_ID = "fake-client-id.apps.googleusercontent.com"
FAKE_CLIENT_SECRET = "fake-client-secret"
FAKE_REFRESH_TOKEN = "fake-refresh-token"
FAKE_ACCESS_TOKEN = "fake-access-token"


def _authorized_user_info(refresh_token=FAKE_REFRESH_TOKEN):
    return {
        "type": "authorized_user",
        "client_id": FAKE_CLIENT_ID,
        "client_secret": FAKE_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "token_uri": TOKEN_URI,
        "scopes": [ADS_SCOPE],
    }


def _write_file(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        if isinstance(content, str):
            file.write(content)
        else:
            json.dump(content, file)


def _acl_entries(path):
    """Returns the macOS ACL entries of path, as listed by ls -le."""
    listing = subprocess.run(
        ["/bin/ls", "-led", path], capture_output=True, text=True, check=True
    ).stdout
    return [line for line in listing.splitlines()[1:] if line.strip()]


def _token_response(access_token):
    """A successful response of the OAuth token endpoint."""
    response = mock.Mock()
    response.status = 200
    response.data = json.dumps(
        {
            "access_token": access_token,
            "expires_in": 3600,
            "scope": ADS_SCOPE,
            "token_type": "Bearer",
        }
    ).encode("utf-8")
    return response


class _IsolatedTestCase(unittest.TestCase):
    """Runs with an empty home directory and no credentials configured."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.default_path = os.path.join(
            self.home, ".config", "google-ads-mcp", "oauth_credentials.json"
        )

        # USERPROFILE is the home directory on Windows.
        self._patch(
            mock.patch.dict(
                os.environ, {"HOME": self.home, "USERPROFILE": self.home}
            )
        )
        os.environ.pop(ENV_VAR, None)
        os.environ.pop("GOOGLE_ADS_DEVELOPER_TOKEN", None)
        os.environ.pop("GOOGLE_ADS_LOGIN_CUSTOMER_ID", None)
        self._patch(mock.patch.object(oauth_credentials, "_cache", None))
        self._patch(mock.patch.object(utils, "_credentials_source", None))
        # No MCP client token: the server runs over stdio.
        self._patch(
            mock.patch(
                "fastmcp.server.dependencies.get_access_token",
                return_value=None,
            )
        )
        self.adc_credentials = mock.Mock(name="adc_credentials")
        self.auth_default = self._patch(
            mock.patch(
                "google.auth.default",
                return_value=(self.adc_credentials, None),
            )
        )

    def _patch(self, patcher):
        patched = patcher.start()
        self.addCleanup(patcher.stop)
        return patched

    def _assert_error_hides(self, error, *values):
        """Checks that the error and its traceback show none of the values."""
        text = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
        for value in values:
            self.assertNotIn(value, text)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)


class CreateCredentialsTest(_IsolatedTestCase):
    """Tests how utils._create_credentials picks the credentials."""

    def test_default_path(self):
        self.assertEqual(
            oauth_credentials.credentials_path(), (self.default_path, False)
        )

    def test_uses_credentials_file_when_present(self):
        info = _authorized_user_info()
        # The server asks for the Google Ads scope whatever the file says.
        del info["scopes"]
        _write_file(self.default_path, info)

        credentials = utils._create_credentials()

        self.assertIsInstance(credentials, Credentials)
        self.assertEqual(credentials.scopes, [ADS_SCOPE])
        self.assertEqual(credentials.refresh_token, FAKE_REFRESH_TOKEN)
        self.assertEqual(credentials.client_id, FAKE_CLIENT_ID)
        self.auth_default.assert_not_called()

    def test_falls_back_to_application_default_credentials(self):
        credentials = utils._create_credentials()

        self.assertIs(credentials, self.adc_credentials)
        self.auth_default.assert_called_once_with(scopes=[ADS_SCOPE])

    def test_env_var_overrides_default_path(self):
        _write_file(self.default_path, _authorized_user_info("default-token"))
        custom_path = os.path.join(self.home, "elsewhere", "ads.json")
        _write_file(custom_path, _authorized_user_info("custom-token"))
        os.environ[ENV_VAR] = custom_path

        credentials = utils._create_credentials()

        self.assertEqual(credentials.refresh_token, "custom-token")
        self.assertEqual(credentials.scopes, [ADS_SCOPE])
        self.auth_default.assert_not_called()

    def test_env_var_expands_user(self):
        _write_file(
            os.path.join(self.home, "ads.json"), _authorized_user_info()
        )
        os.environ[ENV_VAR] = os.path.join("~", "ads.json")

        credentials = utils._create_credentials()

        self.assertEqual(credentials.refresh_token, FAKE_REFRESH_TOKEN)

    def test_env_var_to_missing_file_raises(self):
        # Falling back would silently switch to another Google identity.
        _write_file(self.default_path, _authorized_user_info())
        missing_path = os.path.join(self.home, "missing.json")
        os.environ[ENV_VAR] = missing_path

        with self.assertRaises(DefaultCredentialsError) as raised:
            utils._create_credentials()

        self.assertIn(ENV_VAR, str(raised.exception))
        self.assertIn(missing_path, str(raised.exception))
        self.auth_default.assert_not_called()

    def test_fastmcp_access_token_takes_precedence(self):
        _write_file(self.default_path, _authorized_user_info())
        token = mock.Mock(token="fake-client-access-token")

        with mock.patch(
            "fastmcp.server.dependencies.get_access_token",
            return_value=token,
        ):
            credentials = utils._create_credentials()

        self.assertEqual(credentials.token, "fake-client-access-token")
        self.assertIsNone(credentials.refresh_token)
        self.auth_default.assert_not_called()

    def test_logs_source_once_and_no_secrets(self):
        _write_file(self.default_path, _authorized_user_info())

        with self.assertLogs("ads_mcp.utils", level="INFO") as logs:
            utils._create_credentials()
            utils._create_credentials()

        self.assertEqual(len(logs.output), 1)
        self.assertIn("OAuth credentials file", logs.output[0])
        self.assertIn(self.default_path, logs.output[0])
        for secret in (FAKE_CLIENT_SECRET, FAKE_REFRESH_TOKEN):
            self.assertNotIn(secret, logs.output[0])

    def test_logs_application_default_credentials(self):
        with self.assertLogs("ads_mcp.utils", level="INFO") as logs:
            utils._create_credentials()

        self.assertEqual(len(logs.output), 1)
        self.assertIn("Application Default Credentials", logs.output[0])

    def test_invalid_file_raises_without_secrets(self):
        info = _authorized_user_info()
        del info["refresh_token"]
        _write_file(self.default_path, info)

        with self.assertRaises(DefaultCredentialsError) as raised:
            utils._create_credentials()

        message = str(raised.exception)
        self.assertIn(self.default_path, message)
        self.assertIn("refresh_token", message)
        self._assert_error_hides(raised.exception, FAKE_CLIENT_SECRET)
        self.auth_default.assert_not_called()

    def test_malformed_json_raises_without_secrets(self):
        _write_file(
            self.default_path,
            '{"client_secret": "%s", "refresh_token": "%s"'
            % (FAKE_CLIENT_SECRET, FAKE_REFRESH_TOKEN),
        )

        with self.assertRaises(DefaultCredentialsError) as raised:
            utils._create_credentials()

        self.assertIn(self.default_path, str(raised.exception))
        self.assertIn("not valid JSON", str(raised.exception))
        self._assert_error_hides(
            raised.exception, FAKE_CLIENT_SECRET, FAKE_REFRESH_TOKEN
        )

    def test_invalid_field_raises_without_its_value(self):
        # google-auth quotes an invalid expiry in its error message.
        info = _authorized_user_info()
        info["expiry"] = "fake-secret-in-expiry"
        _write_file(self.default_path, info)

        with self.assertRaises(DefaultCredentialsError) as raised:
            utils._create_credentials()

        self.assertIn(self.default_path, str(raised.exception))
        self._assert_error_hides(
            raised.exception,
            "fake-secret-in-expiry",
            FAKE_CLIENT_SECRET,
            FAKE_REFRESH_TOKEN,
        )

    def test_long_running_server_refreshes_expired_access_token(self):
        info = _authorized_user_info()
        del info["scopes"]
        _write_file(self.default_path, info)
        token_endpoint = mock.Mock(
            side_effect=[
                _token_response("fake-access-token-1"),
                _token_response("fake-access-token-2"),
            ]
        )

        # The first API request gets an access token with the refresh token.
        credentials = utils._create_credentials()
        headers = {}
        credentials.before_request(token_endpoint, "POST", ADS_API_URL, headers)

        self.assertEqual(headers["authorization"], "Bearer fake-access-token-1")
        token_request = token_endpoint.call_args.kwargs
        self.assertEqual(token_request["url"], TOKEN_URI)
        body = urllib.parse.parse_qs(token_request["body"].decode("utf-8"))
        self.assertEqual(body["grant_type"], ["refresh_token"])
        self.assertEqual(body["refresh_token"], [FAKE_REFRESH_TOKEN])
        self.assertEqual(body["scope"], [ADS_SCOPE])

        # Later tool calls share the credentials and reuse the access token.
        self.assertIs(utils._create_credentials(), credentials)
        credentials.before_request(token_endpoint, "POST", ADS_API_URL, {})
        self.assertEqual(token_endpoint.call_count, 1)

        # Once the access token expires, the next request refreshes it.
        credentials.expiry = datetime.datetime(2000, 1, 1)
        headers = {}
        credentials.before_request(token_endpoint, "POST", ADS_API_URL, headers)

        self.assertEqual(token_endpoint.call_count, 2)
        self.assertEqual(headers["authorization"], "Bearer fake-access-token-2")

    def test_reloads_replaced_file(self):
        _write_file(self.default_path, _authorized_user_info("first-token"))
        first = utils._create_credentials()

        _write_file(self.default_path, _authorized_user_info("second-token!"))
        os.utime(self.default_path, ns=(10**18, 10**18))
        second = utils._create_credentials()

        self.assertEqual(first.refresh_token, "first-token")
        self.assertEqual(second.refresh_token, "second-token!")

    def test_client_uses_file_credentials_and_keeps_other_settings(self):
        _write_file(self.default_path, _authorized_user_info())
        os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"] = "fake-developer-token"
        os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"] = "123-456-7890"

        client = utils._get_googleads_client()

        self.assertIsInstance(client.credentials, Credentials)
        self.assertEqual(client.credentials.refresh_token, FAKE_REFRESH_TOKEN)
        self.assertEqual(client.developer_token, "fake-developer-token")
        self.assertEqual(client.login_customer_id, "1234567890")


class AuthorizeTest(_IsolatedTestCase):
    """Tests google-ads-mcp-authorize with a mocked consent flow."""

    def setUp(self):
        super().setUp()
        # Never parsed: the flow is mocked.
        self.client_secret = os.path.join(self.home, "client_secret.json")
        _write_file(self.client_secret, "{}")
        self.flow = mock.Mock()
        self.flow.run_local_server.return_value = Credentials(
            token=FAKE_ACCESS_TOKEN,
            refresh_token=FAKE_REFRESH_TOKEN,
            token_uri=TOKEN_URI,
            client_id=FAKE_CLIENT_ID,
            client_secret=FAKE_CLIENT_SECRET,
            scopes=[ADS_SCOPE],
            granted_scopes=[ADS_SCOPE],
        )
        self.from_client_secrets_file = self._patch(
            mock.patch(
                "google_auth_oauthlib.flow.InstalledAppFlow"
                ".from_client_secrets_file",
                return_value=self.flow,
            )
        )

    def _main(self, *args):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            oauth_credentials.main(
                ["--client-secret", self.client_secret, *args]
            )
        return stdout.getvalue()

    def _assert_no_secrets(self, output):
        for secret in (
            FAKE_ACCESS_TOKEN,
            FAKE_REFRESH_TOKEN,
            FAKE_CLIENT_SECRET,
        ):
            self.assertNotIn(secret, output)

    def test_runs_installed_app_flow_for_ads_scope(self):
        self._main("--out", os.path.join(self.home, "out.json"))

        self.from_client_secrets_file.assert_called_once_with(
            self.client_secret,
            scopes=[ADS_SCOPE],
            autogenerate_code_verifier=True,
        )
        self.flow.run_local_server.assert_called_once_with(
            port=0, access_type="offline", prompt="consent"
        )

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    def test_writes_private_file_in_new_private_directory(self):
        out = os.path.join(self.home, "new", "dir", "oauth_credentials.json")
        # Even with a permissive umask.
        old_umask = os.umask(0)
        try:
            output = self._main("--out", out)
        finally:
            os.umask(old_umask)

        self.assertEqual(
            stat.S_IMODE(os.stat(os.path.dirname(out)).st_mode), 0o700
        )
        self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o600)
        with open(out, encoding="utf-8") as file:
            self.assertEqual(json.load(file), _authorized_user_info())
        # Only the credentials file: no temporary file is left behind.
        self.assertEqual(
            os.listdir(os.path.dirname(out)), ["oauth_credentials.json"]
        )
        self.assertIn(out, output)
        self.assertIn(ENV_VAR, output)
        self.assertIn("/mcp", output)
        self._assert_no_secrets(output)

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    def test_replaces_existing_file_with_private_file(self):
        out = os.path.join(self.home, "oauth_credentials.json")
        _write_file(out, "old")
        os.chmod(out, 0o644)
        os.chmod(self.home, 0o755)

        self._main("--out", out)

        self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o600)
        # A directory that already existed keeps its mode.
        self.assertEqual(stat.S_IMODE(os.stat(self.home).st_mode), 0o755)
        with open(out, encoding="utf-8") as file:
            self.assertEqual(json.load(file), _authorized_user_info())

    def _shared_directory(self):
        """Returns a directory whose new files everyone can read (macOS)."""
        shared = os.path.join(self.home, "shared")
        os.mkdir(shared)
        acl = subprocess.run(
            [
                "/bin/chmod",
                "+a",
                "everyone allow read,file_inherit,directory_inherit",
                shared,
            ],
            capture_output=True,
        )
        if acl.returncode != 0:
            self.skipTest("ACLs are not supported here")
        return shared

    def test_checks_acl_before_writing_secrets(self):
        out = os.path.join(self.home, "new", "oauth_credentials.json")
        checked = []

        def has_acl(fd):
            file_stat = os.fstat(fd)
            checked.append((file_stat.st_ino, file_stat.st_size))
            return False

        with (
            mock.patch.object(
                oauth_credentials, "_has_acl", side_effect=has_acl
            ),
            mock.patch.object(oauth_credentials, "_remove_acl") as remove_acl,
        ):
            self._main("--out", out)

        # The new directory loses inherited entries before the file exists,
        # and the file saved is the one checked, while it was still empty.
        remove_acl.assert_called_once_with(os.path.dirname(out))
        self.assertEqual(checked, [(os.stat(out).st_ino, 0)])

    def test_saves_nothing_where_the_file_would_inherit_an_acl(self):
        out = os.path.join(self.home, "oauth_credentials.json")

        with mock.patch.object(
            oauth_credentials, "_has_acl", return_value=True
        ):
            with self.assertRaises(SystemExit) as raised:
                self._main("--out", out)

        self.assertIn("chmod -N", str(raised.exception.code))
        self.assertFalse(os.path.exists(out))
        # No temporary file is left behind either.
        self.assertEqual(os.listdir(self.home), ["client_secret.json"])

    def test_has_acl_only_checks_on_macos(self):
        with mock.patch.object(oauth_credentials, "_IS_MACOS", False):
            self.assertFalse(oauth_credentials._has_acl(-1))

    @unittest.skipUnless(sys.platform == "darwin", "macOS ACLs")
    def test_has_acl_checks_the_open_file(self):
        shared = self._shared_directory()

        for directory, expected in ((self.home, False), (shared, True)):
            fd, path = tempfile.mkstemp(dir=directory)
            self.addCleanup(os.remove, path)
            with os.fdopen(fd) as file:
                self.assertIs(
                    oauth_credentials._has_acl(file.fileno()), expected
                )

    @unittest.skipUnless(sys.platform == "darwin", "macOS ACLs")
    def test_acl_check_is_not_fooled_by_a_replaced_path(self):
        # Another user who can rename files in the directory opens the empty
        # temporary file, then puts a file without ACL entries at its path.
        shared = self._shared_directory()
        decoy = os.path.join(self.home, "decoy")
        _write_file(decoy, "")
        opened_by_other_user = []
        fchmod = os.fchmod

        def replace_path_then_fchmod(fd, mode):
            (name,) = [n for n in os.listdir(shared) if n.endswith(".tmp")]
            tmp_path = os.path.join(shared, name)
            opened_by_other_user.append(open(tmp_path, "rb"))
            os.replace(decoy, tmp_path)
            fchmod(fd, mode)

        out = os.path.join(shared, "oauth_credentials.json")
        with mock.patch.object(
            oauth_credentials.os, "fchmod", side_effect=replace_path_then_fchmod
        ):
            with self.assertRaises(SystemExit):
                self._main("--out", out)

        with opened_by_other_user[0] as file:
            self.assertEqual(file.read(), b"")
        self.assertFalse(os.path.exists(out))

    def test_windows_saves_only_in_the_user_profile(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        outside_out = os.path.join(outside, "oauth_credentials.json")

        with mock.patch.object(oauth_credentials, "_IS_WINDOWS", True):
            with self.assertRaises(SystemExit):
                self._main("--out", outside_out)
            output = self._main()

        self.assertFalse(os.path.exists(outside_out))
        self.assertTrue(os.path.isfile(self.default_path))
        self.assertIn(self.default_path, output)

    @unittest.skipUnless(sys.platform == "darwin", "macOS ACLs")
    def test_never_writes_where_files_inherit_a_macos_acl(self):
        shared = self._shared_directory()
        # New files in the directory inherit the entry.
        control = os.path.join(shared, "control")
        _write_file(control, "")
        self.assertTrue(_acl_entries(control))
        in_shared = os.path.join(shared, "oauth_credentials.json")
        in_new_dir = os.path.join(shared, "new", "oauth_credentials.json")

        with self.assertRaises(SystemExit):
            self._main("--out", in_shared)
        self._main("--out", in_new_dir)

        self.assertEqual(sorted(os.listdir(shared)), ["control", "new"])
        self.assertEqual(_acl_entries(os.path.dirname(in_new_dir)), [])
        self.assertEqual(_acl_entries(in_new_dir), [])
        self.assertEqual(stat.S_IMODE(os.stat(in_new_dir).st_mode), 0o600)

    def test_saves_to_default_path_that_the_server_loads(self):
        output = self._main()

        self.assertIn(self.default_path, output)
        self.assertNotIn(ENV_VAR, output)
        self._assert_no_secrets(output)
        credentials = utils._create_credentials()
        self.assertEqual(credentials.refresh_token, FAKE_REFRESH_TOKEN)
        self.assertEqual(credentials.scopes, [ADS_SCOPE])
        self.auth_default.assert_not_called()

    def test_default_out_follows_env_var(self):
        custom_path = os.path.join(self.home, "custom", "ads.json")
        os.environ[ENV_VAR] = custom_path

        self._main()

        self.assertTrue(os.path.isfile(custom_path))
        self.assertFalse(os.path.exists(self.default_path))

    def test_saves_nothing_without_refresh_token(self):
        self.flow.run_local_server.return_value = Credentials(
            token=FAKE_ACCESS_TOKEN,
            token_uri=TOKEN_URI,
            client_id=FAKE_CLIENT_ID,
            client_secret=FAKE_CLIENT_SECRET,
            scopes=[ADS_SCOPE],
        )

        with self.assertRaises(SystemExit) as raised:
            self._main()

        self.assertNotIn(raised.exception.code, (None, 0))
        self.assertFalse(os.path.exists(self.default_path))

    def test_saves_nothing_without_ads_scope(self):
        self.flow.run_local_server.return_value = Credentials(
            token=FAKE_ACCESS_TOKEN,
            refresh_token=FAKE_REFRESH_TOKEN,
            token_uri=TOKEN_URI,
            client_id=FAKE_CLIENT_ID,
            client_secret=FAKE_CLIENT_SECRET,
            scopes=[ADS_SCOPE],
            granted_scopes=["openid"],
        )

        with self.assertRaises(SystemExit) as raised:
            self._main()

        self.assertNotIn(raised.exception.code, (None, 0))
        self.assertFalse(os.path.exists(self.default_path))

    def test_missing_client_secret_file(self):
        with self.assertRaises(SystemExit) as raised:
            oauth_credentials.main(
                ["--client-secret", os.path.join(self.home, "missing.json")]
            )

        self.assertNotIn(raised.exception.code, (None, 0))
        self.from_client_secrets_file.assert_not_called()


if __name__ == "__main__":
    unittest.main()

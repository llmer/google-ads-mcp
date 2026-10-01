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

"""Google Ads OAuth credentials kept in a local file.

Application Default Credentials from ``gcloud auth application-default login``
carry the Cloud Platform scope, so a Google Cloud session control policy can
require reauthentication every few hours. ``google-ads-mcp-authorize`` instead
runs the OAuth consent flow once, for the Google Ads scope only, and saves the
refresh token. When that file exists, the server uses it in place of
Application Default Credentials.
"""

import argparse
import contextlib
import ctypes
import errno
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
from typing import Sequence

from google.auth.exceptions import DefaultCredentialsError
from google.oauth2.credentials import Credentials

# OAuth scope for the Google Ads API. Google Ads does not publish a separate
# read-only scope; to run a read-only server, disable the namespaces of
# mutating tools in tools_config.yaml.
ADS_SCOPE = "https://www.googleapis.com/auth/adwords"

# Points the server, and google-ads-mcp-authorize, at a credentials file other
# than the default one.
CREDENTIALS_PATH_ENV_VAR = "GOOGLE_ADS_OAUTH_CREDENTIALS"

_IS_MACOS = sys.platform == "darwin"
_IS_WINDOWS = os.name == "nt"

# From macOS <sys/acl.h>.
_ACL_TYPE_EXTENDED = 0x00000100
_ACL_FIRST_ENTRY = 0

_cache_lock = threading.Lock()
# The last file loaded, as ((path, inode, size, mtime), credentials).
_cache: tuple[tuple[str, int, int, int], Credentials] | None = None


def default_credentials_path() -> str:
    """Returns ~/.config/google-ads-mcp/oauth_credentials.json."""
    return os.path.join(
        os.path.expanduser("~"),
        ".config",
        "google-ads-mcp",
        "oauth_credentials.json",
    )


def credentials_path() -> tuple[str, bool]:
    """Returns the credentials file path, and whether it was set explicitly.

    The path is ``$GOOGLE_ADS_OAUTH_CREDENTIALS`` when that is set, otherwise
    the default path. The file may not exist.
    """
    explicit = os.environ.get(CREDENTIALS_PATH_ENV_VAR)
    if explicit:
        return os.path.expanduser(explicit), True
    return default_credentials_path(), False


def load_credentials(path: str) -> Credentials | None:
    """Returns the credentials saved at ``path``, or None if there is no file.

    The credentials are cached and shared by later calls, so the access token
    is reused until it is about to expire; google-auth then refreshes it with
    the saved refresh token before the next request. That keeps a
    long-running server working. A replaced or modified file is loaded again.

    Raises:
        DefaultCredentialsError: The file exists but cannot be loaded. The
            error never includes values from the file.
    """
    global _cache
    try:
        file_stat = os.stat(path)
    except FileNotFoundError:
        return None
    key = (path, file_stat.st_ino, file_stat.st_size, file_stat.st_mtime_ns)

    with _cache_lock:
        if _cache is not None and _cache[0] == key:
            return _cache[1]
        try:
            credentials = Credentials.from_authorized_user_file(
                path, scopes=[ADS_SCOPE]
            )
        except json.JSONDecodeError as exc:
            reason = (
                f"it is not valid JSON (line {exc.lineno}, column {exc.colno})"
            )
        except OSError as exc:
            reason = exc.strerror or type(exc).__name__
        except Exception as exc:
            # Messages of other errors can quote values from the file.
            reason = (
                "it is not an authorized-user credentials file with"
                " client_id, client_secret and refresh_token"
                f" ({type(exc).__name__})"
            )
        else:
            _cache = (key, credentials)
            return credentials

    # Raised outside the except clauses so that the original error, which
    # can quote values from the file, is not chained to this one.
    raise DefaultCredentialsError(
        f"Could not load the Google Ads OAuth credentials file {path}:"
        f" {reason}. Run google-ads-mcp-authorize to create it again."
    )


def write_credentials(credentials: Credentials, path: str) -> None:
    """Saves ``credentials`` to ``path`` as an authorized-user JSON file.

    Only what is needed to refresh access tokens is saved, not the short-lived
    access token. A missing parent directory is created with mode 0700. The
    file is written with mode 0600 to a temporary file in the same directory,
    then moved into place, so it is never readable by other users or seen
    partially written.

    Raises:
        OSError: The file cannot be written, or not privately: on macOS, in a
            directory whose ACL entries new files inherit, and on Windows,
            outside the user profile.
    """
    info = {
        "type": "authorized_user",
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "scopes": [ADS_SCOPE],
    }
    path = os.path.abspath(path)
    directory = os.path.dirname(path)
    if _IS_WINDOWS and not _is_within(directory, os.path.expanduser("~")):
        # The mode cannot restrict access on Windows, so the file is only as
        # private as the permissions it inherits from its folder. The user
        # profile is private by default, as for gcloud's own credentials,
        # unless a folder in it was shared.
        raise OSError(
            f"{directory} is outside your user profile, where the credentials"
            " might be readable by other users. Use the default location."
        )
    try:
        os.makedirs(directory, mode=0o700)
    except FileExistsError:
        pass
    else:
        # makedirs applies the umask to the mode; set it exactly. Also drop
        # ACL entries inherited from the parent before any file is created
        # in the directory, so that the file cannot inherit them.
        os.chmod(directory, 0o700)
        _remove_acl(directory)

    fd, tmp_path = tempfile.mkstemp(
        dir=directory, prefix=".oauth_credentials-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            # mkstemp creates the file with mode 0600 minus the umask; set it
            # exactly. (On Windows, the mode cannot restrict access.)
            if not _IS_WINDOWS:
                os.fchmod(tmp_file.fileno(), 0o600)
            # On macOS, ACL entries inherited from the directory grant access
            # whatever the mode is, and removing them would not close a file
            # another user already opened. Refuse while the file is empty.
            if _has_acl(tmp_file.fileno()):
                raise OSError(
                    f"New files in {directory} inherit access control list"
                    " (ACL) entries that can let other users read them."
                    f" Remove them with 'chmod -N {directory}', or choose"
                    " another location."
                )
            json.dump(info, tmp_file, indent=2)
            tmp_file.write("\n")
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        raise


def _is_within(path: str, root: str) -> bool:
    """Returns whether ``path`` is ``root`` or inside it."""
    path, root = os.path.realpath(path), os.path.realpath(root)
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        # On different Windows drives.
        return False


def _has_acl(fd: int) -> bool:
    """Returns whether the open file ``fd`` has ACL entries, on macOS.

    The check goes through the descriptor, as another user who can rename
    files in the directory could put a different file at its path. (On Linux,
    the mode a file is created with also limits the ACL entries it inherits,
    so they cannot give other users access.)
    """
    if not _IS_MACOS:
        return False
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    libc.acl_get_fd_np.restype = ctypes.c_void_p
    libc.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
    libc.acl_get_entry.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    libc.acl_free.argtypes = [ctypes.c_void_p]

    ctypes.set_errno(0)
    acl = libc.acl_get_fd_np(fd, _ACL_TYPE_EXTENDED)
    if not acl:
        error = ctypes.get_errno()
        if error == errno.ENOENT:
            # The file has no ACL.
            return False
        raise OSError(error, "Could not read the access control list")
    try:
        entry = ctypes.c_void_p()
        return (
            libc.acl_get_entry(acl, _ACL_FIRST_ENTRY, ctypes.byref(entry)) == 0
        )
    finally:
        libc.acl_free(acl)


def _remove_acl(path: str) -> None:
    """Removes the ACL entries of ``path``, on macOS."""
    if not _IS_MACOS:
        return
    result = subprocess.run(
        ["/bin/chmod", "-N", path], capture_output=True, check=False
    )
    if result.returncode != 0:
        raise OSError(f"Could not remove the access control list of {path}.")


def authorize(client_secret_path: str) -> Credentials:
    """Runs the installed-app OAuth consent flow for the Google Ads scope."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(
        client_secret_path,
        scopes=[ADS_SCOPE],
        # Older google-auth-oauthlib releases do not use PKCE by default.
        autogenerate_code_verifier=True,
    )
    # access_type=offline returns a refresh token, and prompt=consent returns
    # one even when this client was authorized before.
    return flow.run_local_server(
        port=0, access_type="offline", prompt="consent"
    )


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point of google-ads-mcp-authorize."""
    parser = argparse.ArgumentParser(
        prog="google-ads-mcp-authorize",
        description=(
            "Authorize the Google Ads MCP server once with its own OAuth"
            " refresh token, for the Google Ads scope only. The server then"
            " uses it instead of gcloud Application Default Credentials."
        ),
    )
    parser.add_argument(
        "--client-secret",
        required=True,
        metavar="PATH",
        help=(
            "JSON file of a Desktop app OAuth client, downloaded from the"
            " Google Cloud console."
        ),
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        help=(
            "Where to save the credentials. Defaults to"
            f" ${CREDENTIALS_PATH_ENV_VAR} if set, otherwise"
            " ~/.config/google-ads-mcp/oauth_credentials.json."
        ),
    )
    args = parser.parse_args(argv)

    # The local redirect server logs each request URL, which carries the
    # authorization code, at INFO level. Keep the OAuth libraries quiet.
    for name in ("google_auth_oauthlib", "requests_oauthlib", "oauthlib"):
        logging.getLogger(name).setLevel(logging.WARNING)

    client_secret = os.path.expanduser(args.client_secret)
    if not os.path.isfile(client_secret):
        sys.exit(f"OAuth client file not found: {client_secret}")
    out = os.path.abspath(os.path.expanduser(args.out or credentials_path()[0]))

    credentials = authorize(client_secret)
    if not credentials.refresh_token:
        sys.exit(
            "Google returned no refresh token, so nothing was saved. Run the"
            " command again."
        )
    granted = credentials.granted_scopes
    if granted is not None and ADS_SCOPE not in granted:
        sys.exit(
            "Access to Google Ads was not granted, so nothing was saved. Run"
            " the command again and allow it."
        )

    try:
        write_credentials(credentials, out)
    except OSError as exc:
        sys.exit(f"Nothing was saved: {exc}")

    print(f"Saved Google Ads OAuth credentials to {out}")
    print()
    print("Next steps:")
    if out != default_credentials_path():
        print(
            f"  - Set {CREDENTIALS_PATH_ENV_VAR}={out} in the MCP server's"
            " environment."
        )
    print(
        "  - Restart the MCP server so it uses these credentials (in Claude"
        " Code, run /mcp and reconnect the server)."
    )


if __name__ == "__main__":
    main()

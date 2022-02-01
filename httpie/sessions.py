"""
Persistent, JSON-serialized sessions.

"""
import os
import re

from http.cookies import SimpleCookie
from http.cookiejar import Cookie
from pathlib import Path
from typing import Any, Dict, Optional, Union

from requests.auth import AuthBase
from requests.cookies import RequestsCookieJar, remove_cookie_by_name

from .context import Environment
from .cli.dicts import HTTPHeadersDict
from .config import BaseConfigDict, DEFAULT_CONFIG_DIR
from .utils import url_as_host
from .plugins.registry import plugin_manager


SESSIONS_DIR_NAME = 'sessions'
DEFAULT_SESSIONS_DIR = DEFAULT_CONFIG_DIR / SESSIONS_DIR_NAME
VALID_SESSION_NAME_PATTERN = re.compile('^[a-zA-Z0-9_.-]+$')
# Request headers starting with these prefixes won't be stored in sessions.
# They are specific to each request.
# <https://en.wikipedia.org/wiki/List_of_HTTP_header_fields#Requests>
SESSION_IGNORED_HEADER_PREFIXES = ['Content-', 'If-']

# Cookie related options
KEPT_COOKIE_OPTIONS = ['name', 'expires', 'path', 'value', 'domain', 'secure']
DEFAULT_COOKIE_PATH = '/'

INSECURE_COOKIE_JAR_WARNING = '''\
Outdated layout detected for the current session. Please consider updating it,
in order to not get affected by potential security problems.

For fixing the current session:

    With binding all cookies to the current host (secure):
        $ httpie cli sessions upgrade --bind-cookies {hostname} {session_id}

    Without binding cookies (leaving them as is) (insecure):
        $ httpie cli sessions upgrade {hostname} {session_id}
'''

INSECURE_COOKIE_JAR_WARNING_FOR_NAMED_SESSIONS = '''\

For fixing all named sessions:

    With binding all cookies to the current host (secure):
        $ httpie cli sessions upgrade-all --bind-cookies

    Without binding cookies (leaving them as is) (insecure):
        $ httpie cli sessions upgrade-all

See https://pie.co/docs/security for more information.
'''


def is_anonymous_session(session_name: str) -> bool:
    return os.path.sep in session_name


def materialize_cookie(cookie: Cookie) -> Dict[str, Any]:
    materialized_cookie = {
        option: getattr(cookie, option)
        for option in KEPT_COOKIE_OPTIONS
    }

    if (
        cookie._rest.get('is_explicit_none')
        and materialized_cookie['domain'] == ''
    ):
        materialized_cookie['domain'] = None

    return materialized_cookie


def get_httpie_session(
    env: Environment,
    config_dir: Path,
    session_name: str,
    host: Optional[str],
    url: str,
    *,
    refactor_mode: bool = False
) -> 'Session':
    bound_hostname = host or url_as_host(url)
    if not bound_hostname:
        # HACK/FIXME: httpie-unixsocket's URLs have no hostname.
        bound_hostname = 'localhost'

    # host:port => host_port
    hostname = bound_hostname.replace(':', '_')
    if is_anonymous_session(session_name):
        path = os.path.expanduser(session_name)
        session_id = path
    else:
        path = (
            config_dir / SESSIONS_DIR_NAME / hostname / f'{session_name}.json'
        )
        session_id = session_name

    session = Session(
        path,
        env=env,
        session_id=session_id,
        bound_host=bound_hostname.split(':')[0],
        refactor_mode=refactor_mode
    )
    session.load()
    return session


class Session(BaseConfigDict):
    helpurl = 'https://httpie.io/docs#sessions'
    about = 'HTTPie session file'

    def __init__(
        self,
        path: Union[str, Path],
        env: Environment,
        bound_host: str,
        session_id: str,
        refactor_mode: bool = False,
    ):
        super().__init__(path=Path(path))
        self['headers'] = {}
        self['cookies'] = []
        self['auth'] = {
            'type': None,
            'username': None,
            'password': None
        }
        self.env = env
        self.cookie_jar = RequestsCookieJar()
        self.session_id = session_id
        self.bound_host = bound_host
        self.refactor_mode = refactor_mode

    def pre_process_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        cookies = data.get('cookies')
        if isinstance(cookies, dict):
            normalized_cookies = [
                {
                    'name': key,
                    **value
                }
                for key, value in cookies.items()
            ]
        elif isinstance(cookies, list):
            normalized_cookies = cookies
        else:
            normalized_cookies = []

        should_issue_warning = False
        for cookie in normalized_cookies:
            domain = cookie.get('domain', '')
            if domain == '' and isinstance(cookies, dict):
                should_issue_warning = True
            elif domain is None:
                # domain = None means explicitly lack of cookie, though
                # requests requires domain to be string so we'll cast it
                # manually.
                cookie['domain'] = ''
                cookie['rest'] = {'is_explicit_none': True}

            self.cookie_jar.set(**cookie)

        if should_issue_warning and not self.refactor_mode:
            warning = INSECURE_COOKIE_JAR_WARNING.format(hostname=self.bound_host, session_id=self.session_id)
            if not is_anonymous_session(self.session_id):
                warning += INSECURE_COOKIE_JAR_WARNING_FOR_NAMED_SESSIONS

            self.env.log_error(
                warning,
                level='warning'
            )

        return data

    def post_process_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        cookies = data.get('cookies')
        # Save in the old-style fashion

        normalized_cookies = [
            materialize_cookie(cookie)
            for cookie in self.cookie_jar
        ]
        if isinstance(cookies, dict):
            data['cookies'] = {
                cookie.pop('name'): cookie
                for cookie in normalized_cookies
            }
        else:
            data['cookies'] = normalized_cookies

        return data

    def update_headers(self, request_headers: HTTPHeadersDict):
        """
        Update the session headers with the request ones while ignoring
        certain name prefixes.

        """
        headers = self.headers
        for name, value in request_headers.copy().items():
            if value is None:
                continue  # Ignore explicitly unset headers

            original_value = value
            if type(value) is not str:
                value = value.decode()

            if name.lower() == 'user-agent' and value.startswith('HTTPie/'):
                continue

            if name.lower() == 'cookie':
                for cookie_name, morsel in SimpleCookie(value).items():
                    if not morsel['path']:
                        morsel['path'] = DEFAULT_COOKIE_PATH
                    self.cookie_jar.set(cookie_name, morsel)

                all_cookie_headers = request_headers.getall(name)
                if len(all_cookie_headers) > 1:
                    all_cookie_headers.remove(original_value)
                else:
                    request_headers.popall(name)
                continue

            for prefix in SESSION_IGNORED_HEADER_PREFIXES:
                if name.lower().startswith(prefix.lower()):
                    break
            else:
                headers[name] = value

        self['headers'] = dict(headers)

    @property
    def headers(self) -> HTTPHeadersDict:
        return HTTPHeadersDict(self['headers'])

    @property
    def cookies(self) -> RequestsCookieJar:
        self.cookie_jar.clear_expired_cookies()
        return self.cookie_jar

    @cookies.setter
    def cookies(self, jar: RequestsCookieJar):
        self.cookie_jar = jar

    def remove_cookies(self, cookies: Dict[str, str]):
        for cookie in cookies:
            remove_cookie_by_name(
                self.cookie_jar,
                cookie['name'],
                domain=cookie.get('domain', None),
                path=cookie.get('path', None)
            )

    @property
    def auth(self) -> Optional[AuthBase]:
        auth = self.get('auth', None)
        if not auth or not auth['type']:
            return

        plugin = plugin_manager.get_auth_plugin(auth['type'])()

        credentials = {'username': None, 'password': None}
        try:
            # New style
            plugin.raw_auth = auth['raw_auth']
        except KeyError:
            # Old style
            credentials = {
                'username': auth['username'],
                'password': auth['password'],
            }
        else:
            if plugin.auth_parse:
                from .cli.argtypes import parse_auth
                parsed = parse_auth(plugin.raw_auth)
                credentials = {
                    'username': parsed.key,
                    'password': parsed.value,
                }

        return plugin.get_auth(**credentials)

    @auth.setter
    def auth(self, auth: dict):
        assert {'type', 'raw_auth'} == auth.keys()
        self['auth'] = auth

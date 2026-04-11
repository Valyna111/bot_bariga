 #!/usr/bin/env python3
"""
MangaBuff.ru Авторизация, регистрация, парсинг желаемых карт и владельцев карт
"""

import re
import time
import json
from urllib.parse import unquote, urlparse, parse_qs

try:
    from curl_cffi.requests import Session as CffiSession
    USE_CURL_CFFI = True
except ImportError:
    import requests
    USE_CURL_CFFI = False
    print("[WARN] curl_cffi не установлен, используется requests. Возможны проблемы с Cloudflare.")

class MangaBuffAuth:
    BASE_URL = "https://mangabuff.ru"

    def __init__(self, proxy: dict = None, impersonate: str = "chrome131"):
        self.impersonate = impersonate
        self._setup_session(proxy)

    def _setup_session(self, proxy):
        if USE_CURL_CFFI:
            self.session = CffiSession(impersonate=self.impersonate)
        else:
            self.session = requests.Session()
        if proxy:
            self.session.proxies.update(proxy)

        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.109 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
            'Sec-Ch-Ua': '"Google Chrome";v="131", "Not_A Brand";v="8"',
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': '"Windows"',
        })

    def _get_csrf_from_cookies(self) -> str:
        xsrf = self.session.cookies.get('XSRF-TOKEN')
        if xsrf:
            return unquote(xsrf)
        for cookie in self.session.cookies:
            name = cookie.name if hasattr(cookie, 'name') else cookie
            if name.upper() == 'XSRF-TOKEN':
                value = cookie.value if hasattr(cookie, 'value') else self.session.cookies[name]
                return unquote(value)
        return ''

    def login(self, email: str, password: str):
        resp = self.session.get(f'{self.BASE_URL}/login')
        if resp.status_code != 200:
            return False, f'GET login failed: HTTP {resp.status_code}'

        csrf = self._get_csrf_from_cookies()
        if not csrf:
            return False, 'CSRF token not found'

        time.sleep(1)

        login_data = {'email': email, 'password': password, 'remember': 'on'}
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'X-XSRF-TOKEN': csrf,
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': f'{self.BASE_URL}/login',
            'Origin': self.BASE_URL,
        }
        resp = self.session.post(f'{self.BASE_URL}/login', data=login_data, headers=headers, allow_redirects=False)

        check = self.session.get(f'{self.BASE_URL}/')
        if check.status_code != 200:
            return False, 'Auth check failed'

        html = check.text
        match = re.search(r'data-userid="(\d+)"', html)
        if not match:
            match = re.search(r'/users/(\d+)', html)
        if match:
            user_id = match.group(1)
            cookies = []
            for name, value in self.session.cookies.items():
                cookies.append({'name': name, 'value': value, 'domain': 'mangabuff.ru'})
            return True, {'user_id': user_id, 'cookies': cookies}
        else:
            return False, 'User ID not found after login'

    def register(self, username: str, email: str, password: str):
        resp = self.session.get(f'{self.BASE_URL}/register')
        if resp.status_code != 200:
            return False, f'GET register failed: HTTP {resp.status_code}'

        csrf = self._get_csrf_from_cookies()
        if not csrf:
            match = re.search(r'<input[^>]*name="_token"[^>]*value="([^"]+)"', resp.text)
            if match:
                csrf = match.group(1)
        if not csrf:
            return False, 'CSRF token not found for registration'

        register_data = {
            'name': username,
            'email': email,
            'password': password,
            'password_confirmation': password,
            '_token': csrf,
        }
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'Referer': f'{self.BASE_URL}/register',
            'Origin': self.BASE_URL,
        }
        resp = self.session.post(f'{self.BASE_URL}/register', data=register_data, headers=headers, allow_redirects=False)

        if resp.status_code == 302:
            return True, 'Registration successful, please confirm email if required'
        elif resp.status_code == 200:
            if 'email has already been taken' in resp.text.lower():
                return False, 'Email already registered'
            if 'username has already been taken' in resp.text.lower():
                return False, 'Username already taken'
            return False, 'Registration failed. Check required fields or captcha.'
        else:
            return False, f'HTTP {resp.status_code}'

    def load_cookies(self, cookies_list: list):
        for c in cookies_list:
            name = c.get('name')
            value = c.get('value')
            domain = c.get('domain', 'mangabuff.ru')
            if name and value:
                self.session.cookies.set(name, value, domain=domain)

    def is_authenticated(self) -> bool:
        try:
            resp = self.session.get(f'{self.BASE_URL}/')
            if resp.status_code != 200:
                return False
            html = resp.text
            if re.search(r'data-userid="\d+"', html):
                return True
            if 'header__user' in html or '/logout' in html:
                return True
            return False
        except:
            return False

    def get_user_id(self) -> str:
        resp = self.session.get(f'{self.BASE_URL}/')
        if resp.status_code != 200:
            return None
        match = re.search(r'data-userid="(\d+)"', resp.text)
        if not match:
            match = re.search(r'/users/(\d+)', resp.text)
        return match.group(1) if match else None

    def get_my_wanted_cards(self, user_id: str = None):
        if user_id is None:
            user_id = self.get_user_id()
            if not user_id:
                return []
        url = f"{self.BASE_URL}/cards/{user_id}/offers?type_w=0"
        response = self.session.get(url)
        if response.status_code != 200:
            return []
        html = response.text
        cards = []
        pattern = r'<div[^>]*class="[^"]*manga-cards__item[^"]*"[^>]*data-name="([^"]*)"[^>]*data-card-id="(\d+)"[^>]*data-manga-name="([^"]*)"'
        matches = re.findall(pattern, html)
        for name, card_id, manga_name in matches:
            cards.append({
                'card_id': card_id,
                'name': name,
                'manga': manga_name,
                'url': f"{self.BASE_URL}/cards/{card_id}/users"
            })
        return cards

    def get_first_owner(self, card_id: str):
        """
        Возвращает первого владельца карты (самый верхний в списке) или None.
        Возвращает словарь с ключами: user_id, username, card_user_id, is_online, trade_lock, handshake, profile_url.
        """
        url = f"{self.BASE_URL}/cards/{card_id}/users"
        response = self.session.get(url)
        if response.status_code != 200:
            return None
        html = response.text

        # Ищем первый блок card-show__owner
        match = re.search(r'<a\s+class="[^"]*card-show__owner([^"]*)"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.DOTALL)
        if not match:
            return None

        extra_classes, href, inner_html = match.groups()
        is_online = '--online' in extra_classes

        # Извлекаем user_id и card_user_id
        parsed = urlparse(href)
        path_parts = parsed.path.split('/')
        user_id = path_parts[2] if len(path_parts) > 2 else ''
        query_params = parse_qs(parsed.query)
        card_user_id = query_params.get('card_user_id', [''])[0]

        # Имя пользователя
        name_match = re.search(r'<span[^>]*class="[^"]*card-show__owner-name[^"]*"[^>]*>(.*?)</span>', inner_html)
        username = name_match.group(1).strip() if name_match else 'Неизвестный'

        # Иконки
        trade_lock = 'card-show__owner-icon--trade-lock' in inner_html
        handshake = 'card-show__owner-icon--block' in inner_html

        return {
            'user_id': user_id,
            'username': username,
            'card_user_id': card_user_id,
            'is_online': is_online,
            'trade_lock': trade_lock,
            'handshake': handshake,
            'profile_url': f"{self.BASE_URL}/users/{user_id}"
        }
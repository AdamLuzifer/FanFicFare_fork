# -*- coding: utf-8 -*-

# Standard library imports
from __future__ import absolute_import
import logging
import re
import json
import time
from datetime import datetime

import concurrent
import base64

# Third-party imports
global requests, BeautifulSoup, ThreadPoolExecutor
try:
    import requests
    from bs4 import BeautifulSoup
    from concurrent.futures import ThreadPoolExecutor
except ImportError as e:
    requests = None
    BeautifulSoup = None
    ThreadPoolExecutor = None
    import traceback
    logging.getLogger(__name__).error("Failed to import required modules: %s\n%s", 
                                     str(e), traceback.format_exc())

# Local imports
from ..htmlcleanup import stripHTML
from .. import exceptions as exceptions
from ..six.moves.urllib import parse as urlparse
from .base_adapter import BaseSiteAdapter, makeDate

logger = logging.getLogger(__name__)

def getClass():
    return AuthorTodayAdapter

class AuthorTodayAdapter(BaseSiteAdapter):

    def __init__(self, config, url):
        BaseSiteAdapter.__init__(self, config, url)
        
        # Check required dependencies
        global requests, BeautifulSoup
        if requests is None:
            raise exceptions.LibraryMissingException('requests')
        if BeautifulSoup is None:
            raise exceptions.LibraryMissingException('beautifulsoup4')
        
        self.username = self.getConfig("username")
        self.password = self.getConfig("password")
        self.is_adult = self.getConfig('is_adult', False)
        self.user_id = None  # Will be set during login if needed
        self.session = None  # Session for making requests
        self._logged_in = False  # Track login state
        self._login_attempts = 0  # Track number of login attempts
        self._browser_cache_checked = False
        
        # Initialize session first
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Cache-Control': 'max-age=0'
        })
        
        # Check if PIL/Pillow is available
        try:
            from PIL import Image
            self.has_pil = True
        except ImportError:
            self.has_pil = False
        
        # Log cache configuration
        logger.debug("Cache settings - Basic cache: %s, Browser cache: %s" % 
                    (self.getConfig('use_basic_cache'), 
                     self.getConfig('use_browser_cache')))
        
        # Extract story ID from URL
        m = re.match(self.getSiteURLPattern(), url)
        if m:
            self.story.setMetadata('storyId', m.group('id'))
            
            # normalized story URL
            self._setURL('https://' + self.getSiteDomain() + '/work/' + self.story.getMetadata('storyId'))
        else:
            raise exceptions.InvalidStoryURL(url,
                                          self.getSiteDomain(),
                                          self.getSiteExampleURLs())
        
        # Each adapter needs to have a unique site abbreviation
        self.story.setMetadata('siteabbrev', 'atd')
        
        # The date format will vary from site to site
        self.dateformat = "%Y-%m-%d"

    @staticmethod
    def getSiteDomain():
        return 'author.today'

    @classmethod
    def getSiteExampleURLs(cls):
        return "https://" + cls.getSiteDomain() + "/work/123456"

    def getSiteURLPattern(self):
        return r"https?://" + re.escape(self.getSiteDomain()) + r"/work/(?P<id>\d+)"

    def use_pagecache(self):
        return True

    def performLogin(self, url, data=None):
        """
        Perform login to author.today with session persistence.
        """
        if self._logged_in:
            return True

        if not self.username:
            raise exceptions.FailedToLogin(url, 'No username set. Please set username in personal.ini')
        if not self.password:
            raise exceptions.FailedToLogin(url, 'No password set. Please set password in personal.ini')

        self._login_attempts += 1
        if self._login_attempts > 3:
            self._login_attempts = 0  # Reset for next time
            raise exceptions.FailedToLogin(url, "Exceeded maximum login attempts (3)")
            
        logger.debug("Starting login process... (Attempt %d/3)", self._login_attempts)

        # Try to use browser cache if enabled and not checked yet
        if self.getConfig('use_browser_cache', False) and not self._browser_cache_checked:
            logger.debug("Attempting to use browser cache for login")
            self._browser_cache_checked = True
            cached_cookie = self.get_browser_cookie()
            if cached_cookie:
                self.session.cookies.update(cached_cookie)
                # Test if the cached cookie works
                test_response = self.session.get(f'https://{self.getSiteDomain()}/account')
                if test_response.status_code == 200 and 'account/logout' in test_response.text:
                    # Extract user ID from the response
                    user_id_match = re.search(r'window\.app\s*=\s*{\s*.*?userId\s*:\s*[\'"]?(\d+)[\'"]?', 
                                            test_response.text, re.DOTALL)
                    if user_id_match:
                        self.user_id = user_id_match.group(1)
                        logger.debug(f"Successfully logged in using browser cache. User ID: {self.user_id}")
                        self._logged_in = True
                        return True

        logger.debug('Will now login to URL (%s) as (%s)', 
                    f'https://{self.getSiteDomain()}/account/login',
                    self.username)

        try:
            # Get login page first
            login_url = f'https://{self.getSiteDomain()}/account/login'
            response = self.session.get(login_url)
            response.raise_for_status()

            # Extract CSRF token
            soup = BeautifulSoup(response.text, 'html.parser')
            csrf_input = soup.find('input', {'name': '__RequestVerificationToken'})
            if not csrf_input:
                raise exceptions.FailedToLogin(url, 'Could not find CSRF token')
            csrf_token = csrf_input.get('value')

            # Prepare login data
            login_data = {
                'Login': self.username,
                'Password': self.password,
                '__RequestVerificationToken': csrf_token,
                'RememberMe': 'true'
            }

            # Set headers for login request
            headers = {
                'Content-Type': 'application/x-www-form-urlencoded',
                'Origin': f'https://{self.getSiteDomain()}',
                'Referer': login_url,
                'X-Requested-With': 'XMLHttpRequest',
                'Accept': 'application/json, text/javascript, */*; q=0.01'
            }

            # Perform login
            response = self.session.post(login_url, data=login_data, headers=headers, allow_redirects=True)
            response.raise_for_status()

            # Check if response is JSON
            try:
                json_response = response.json()
                logger.debug("Login JSON response: %s" % str(json_response))
                if 'isSuccessful' in json_response and json_response['isSuccessful']:
                    logger.debug("Login successful via JSON response")
                    self._logged_in = True
                    
                    # Extract user ID from login response
                    user_id_match = re.search(r'window\.app\s*=\s*{\s*.*?userId\s*:\s*[\'"]?(\d+)[\'"]?', 
                                            response.text, re.DOTALL)
                    if user_id_match:
                        self.user_id = user_id_match.group(1)
                        logger.debug(f"Extracted user ID: {self.user_id}")
                    else:
                        # Try to get user ID from account page
                        account_response = self.session.get(f'https://{self.getSiteDomain()}/account')
                        user_id_match = re.search(r'window\.app\s*=\s*{\s*.*?userId\s*:\s*[\'"]?(\d+)[\'"]?',
                                                account_response.text, re.DOTALL)
                        if user_id_match:
                            self.user_id = user_id_match.group(1)
                            logger.debug(f"Extracted user ID from account page: {self.user_id}")
                        else:
                            logger.warning("Could not extract user ID after successful login")
                    
                    return True
                    
                error_msg = json_response.get('messages', ['Unknown error'])[0]
                logger.error(f"Login failed: {error_msg}")
                if "Страница устарела" in error_msg and self._login_attempts < 3:
                    logger.warning("Page expired, clearing session and retrying...")
                    self.session.cookies.clear()  # Clear cookies for fresh attempt
                    return self.performLogin(url, data)  # Try again with fresh session
                self._logged_in = False
                self._login_attempts = 0  # Reset for next time
                raise exceptions.FailedToLogin(url, error_msg)
            except ValueError:
                logger.debug("Response is not JSON, checking cookies...")
                # Not JSON response, check cookies
                if self.getAuthCookie():
                    self._logged_in = True
                    self._login_attempts = 0  # Reset counter on success
                    
                    # Try to get user ID from account page
                    account_response = self.session.get(f'https://{self.getSiteDomain()}/account')
                    user_id_match = re.search(r'window\.app\s*=\s*{\s*.*?userId\s*:\s*[\'"]?(\d+)[\'"]?',
                                            account_response.text, re.DOTALL)
                    if user_id_match:
                        self.user_id = user_id_match.group(1)
                        logger.debug(f"Extracted user ID from account page: {self.user_id}")
                    else:
                        logger.warning("Could not extract user ID after successful cookie login")
                    
                    logger.debug("Login successful via cookie check")
                    return True
                else:
                    logger.debug("Current cookies: %s" % str(dict(self.session.cookies)))
                    self._logged_in = False
                    self._login_attempts = 0  # Reset for next time
                    raise exceptions.FailedToLogin(url, "Login failed - authentication cookie not found")
                    
        except Exception as e:
            error_msg = str(e)
            if isinstance(e, requests.exceptions.RequestException):
                error_msg = f'Network error during login: {str(e)}'
            elif isinstance(e, exceptions.FailedToLogin):
                error_msg = str(e)
            else:
                error_msg = f'Unexpected error during login: {str(e)}'
            
            logger.error('Login failed: %s', error_msg)
            raise exceptions.FailedToLogin(url, error_msg)

    def checkLogin(self, url):
        """Check if current session is logged in."""
        try:
            response = self.session.get(url)
            return 'isLoggedIn = true' in response.text
        except:
            return False
            
    def get_browser_cookie(self):
        """Get cached browser cookies if available."""
        try:
            from ..browsercache import get_browser_cookies
            return get_browser_cookies(self.getSiteDomain())
        except:
            return None
            
    def getAuthCookie(self):
        """Check for presence of auth cookie."""
        try:
            cookies = self.session.cookies
            cookie_dict = {}
            # Handle potential duplicate cookies by taking the last value
            for cookie in cookies:
                cookie_dict[cookie.name] = cookie.value
            
            # Check for either LoginCookie or ngLoginCookie
            has_login = 'LoginCookie' in cookie_dict
            has_ng_login = 'ngLoginCookie' in cookie_dict
            
            return has_login or has_ng_login
        except:
            return False

    def decrypt_chapter_text(self, data, secret):
        """
        Decrypt the chapter text using the reader secret.
        Matches JavaScript implementation exactly:
        let ss = secret.split("").reverse().join("") + "@_@" + (app.userId || "");
        
        Args:
            data (dict): The encrypted data object from the server
            secret (str): The reader secret key
        
        Returns:
            str: The decrypted text, or empty string if decryption fails
        """
        try:
            if not data or not secret or 'text' not in data:
                logger.error("Missing required data for decryption")
                return ''
            
            encrypted_text = data['text']
            if not encrypted_text:
                logger.error("No encrypted text found")
                return ''
            
            # Create decryption key exactly like JavaScript
            # 1. Reverse the secret
            reversed_secret = secret[::-1]
            # 2. Add "@_@" and userId 
            user_id = self.user_id or self.getConfig('user_id', '')
            key = reversed_secret + '@_@' + str(user_id)
            
            # Get lengths for XOR operation
            key_len = len(key)
            text_len = len(encrypted_text)
            
            # Decrypt using XOR with the key
            result = []
            for pos in range(text_len):
                # Get key character (cycling through key)
                key_char = key[pos % key_len]
                # Get text character
                text_char = encrypted_text[pos]
                # XOR the character codes
                decrypted_char = chr(ord(text_char) ^ ord(key_char))
                result.append(decrypted_char)
                
            logger.debug("Decryption completed. Key length: %d, Text length: %d", 
                        key_len, text_len)
                    
            return ''.join(result)
            
        except Exception as e:
            logger.error("Error during decryption: %s", e)
            return ''

    def extract_tags(self, soup):
        """Extract and deduplicate tags from various sources on the page"""
        tags = set()  # Используем set для автоматического удаления дубликатов
        
        # Extract genres
        genres_div = soup.find('div', {'class': 'book-genres'})
        if genres_div:
            for genre in genres_div.find_all('a'):
                tag_text = genre.get_text().strip().lower()  # Приводим к нижнему регистру для лучшего сравнения
                if tag_text:
                    tags.add(tag_text)

        # Extract tags from spans with 'tags' class
        tags_spans = soup.find_all('span', {'class': 'tags'})
        for span in tags_spans:
            for tag in span.find_all('a'):
                tag_text = tag.get_text().strip().lower()
                if tag_text:
                    tags.add(tag_text)

        # Extract additional tags using various selectors
        additional_selectors = [
            'div.book-tags span',
            'div.book-tags a',
            'div.tags-container a',
            'div.book-meta-tags a'
        ]
        
        for selector in additional_selectors:
            for element in soup.select(selector):
                tag_text = element.get_text().strip().lower()
                if tag_text and not any(skip in tag_text for skip in ['глав', 'страниц', 'знак']):
                    tags.add(tag_text)

        # Преобразуем set обратно в список и сортируем для стабильного порядка
        return sorted(list(tags))

    def extractChapterUrlsAndMetadata(self):
        url = self.url
        logger.debug("URL: "+url)

        data = self.get_request(url)
        soup = self.make_soup(data)

        # Check if story exists
        if "Произведене не найдено" in data:
            raise exceptions.StoryDoesNotExist(self.url)

        # Check for adult content
        if "Произведение имеет метку 18+" in data and not self.is_adult:
            raise exceptions.AdultCheckRequired(self.url)

        # Extract metadata
        title = soup.find('h1', {'class': 'book-title'})
        self.story.setMetadata('title', title.get_text().strip() if title else '')

        # Extract cover image
        get_cover = True
        if get_cover:
            # Try to get cover image similar to FicBook's implementation
            cover_url = None
            
            # Try meta tag first (highest quality usually)
            meta_cover = soup.find('meta', {'property':'og:image'})
            if meta_cover:
                cover_url = meta_cover.get('content')
            
            # Fallback to direct image elements if meta tag not found
            if not cover_url:
                cover_selectors = [
                    ('img', {'class': 'cover-image'}),
                    ('img', {'class': 'book-cover-img'}),
                    ('img', {'class': 'book-cover'}),
                    ('div.book-cover img', {})
                ]
                
                for tag, attrs in cover_selectors:
                    if '.' in tag:
                        # Handle CSS selector style
                        img = soup.select_one(tag)
                    else:
                        img = soup.find(tag, attrs)
                    if img and img.get('src'):
                        cover_url = img['src']
                        break
            
            if cover_url:
                # Remove size parameters from URL
                cover_url = re.sub(r'\?(?:width|height|size)=\d+', '', cover_url)
                
                # Ensure absolute URL
                if not cover_url.startswith('http'):
                    cover_url = 'https://' + self.getSiteDomain() + cover_url
                
                # Run test before actual processing
                logger.debug("Testing cover image handling...")
                test_results = self.test_cover_handling(cover_url)
                
                try:
                    if self.has_pil:
                        # Download image and optimize with PIL
                        import io
                        import tempfile
                        import os
                        from PIL import Image
                        import requests
                        
                        response = requests.get(cover_url)
                        img = Image.open(io.BytesIO(response.content))
                        
                        # Optimize size if too large
                        max_size = (1200, 1800)  # Maximum dimensions
                        if img.size[0] > max_size[0] or img.size[1] > max_size[1]:
                            img.thumbnail(max_size, Image.Resampling.LANCZOS)
                        
                        # Save optimized image to temporary file
                        with tempfile.NamedTemporaryFile(delete=False, suffix='.'+img.format.lower() if img.format else '.jpg') as tmp_file:
                            img.save(tmp_file, format=img.format or 'JPEG', quality=85, optimize=True)
                            tmp_file_path = tmp_file.name
                        
                        # Create a local URL for the temporary file
                        tmp_url = 'file:///' + tmp_file_path.replace('\\', '/')
                        
                        # Set the optimized cover
                        self.setCoverImage(self.url, tmp_url)
                        logger.debug("Cover image optimized and set successfully: %s" % cover_url)
                        
                        # Clean up the temporary file
                        try:
                            os.unlink(tmp_file_path)
                        except:
                            pass
                    else:
                        # Set cover directly without optimization
                        self.setCoverImage(self.url, cover_url)
                        logger.debug("Cover image set without optimization: %s" % cover_url)
                except Exception as e:
                    logger.warning("Failed to set cover image: %s" % e)

        # Extract and set tags
        tags = self.extract_tags(soup)
        if tags:
            # Создаем словарь для отслеживания уже добавленных тегов
            added_tags = {}
            
            # Set tags as tags first
            for tag in tags:
                if tag not in added_tags.get('tags', set()):
                    self.story.addToList('tags', tag)
                    added_tags.setdefault('tags', set()).add(tag)
                    
            # Then set as genre
            for tag in tags:
                if tag not in added_tags.get('genre', set()):
                    self.story.addToList('genre', tag)
                    added_tags.setdefault('genre', set()).add(tag)
                    
            # Also set as subject tags
            for tag in tags:
                if tag not in added_tags.get('subject', set()):
                    self.story.addToList('subject', tag)
                    added_tags.setdefault('subject', set()).add(tag)

            logger.debug(f"Added unique tags: {', '.join(tags)}")
            
        # Log current metadata for debugging
        logger.debug(f"Current genres: {self.story.getList('genre')}")
        logger.debug(f"Current tags: {self.story.getList('tags')}")
        logger.debug(f"Current subjects: {self.story.getList('subject')}")

        author = soup.find('span', {'itemprop': 'author'})
        if author:
            author_name = author.find('meta', {'itemprop': 'name'})['content']
            author_link = author.find('a')
            self.story.setMetadata('author', author_name)
            if author_link:
                self.story.setMetadata('authorId', author_link['href'].split('/')[-1])
                self.story.setMetadata('authorUrl', 'https://' + self.getSiteDomain() + author_link['href'])

        # Get description
        description = soup.find('div', {'class': 'annotation'})
        if description:
            desc_text = description.find('div', {'class': 'rich-content'})
            self.story.setMetadata('description', desc_text.get_text().strip() if desc_text else '')

        # Get status
        status_label = soup.find('span', {'class': 'label-primary'})
        if status_label and 'в процессе' in status_label.get_text():
            self.story.setMetadata('status', 'In-Progress')
        else:
            self.story.setMetadata('status', 'Completed')

        # Get word count
        word_count = soup.find('span', {'class': 'hint-top'}, text=re.compile(r'.*зн\..*'))
        if word_count:
            count = word_count.get_text().strip().split()[0].replace(' ', '')
            self.story.setMetadata('numWords', count)

        # Get publish date
        pub_date = soup.find('span', {'data-format': 'calendar'})
        if pub_date:
            date_str = pub_date.get('data-time', '').split('T')[0]
            if date_str:
                self.story.setMetadata('datePublished', makeDate(date_str, self.dateformat))

        # Get update date
        update_date = soup.find('span', {'data-format': 'calendar-short'})
        if update_date:
            date_str = update_date.get('data-time', '').split('T')[0]
            if date_str:
                self.story.setMetadata('dateUpdated', makeDate(date_str, self.dateformat))

        # Get chapters
        chapter_list = soup.find('ul', {'class': 'table-of-content'})
        if not chapter_list:
            # No chapters found - might be a single chapter story
            self.add_chapter('Chapter 1', url)
            self.story.setMetadata('numChapters', 1)
            return
            
        chapters = []
        for chapter in chapter_list.find_all('li'):
            chapter_a = chapter.find('a')
            if not chapter_a:
                continue
                
            title = chapter_a.get_text().strip()
            chapter_url = 'https://' + self.getSiteDomain() + chapter_a['href']
            
            # Get chapter date
            date_span = chapter.find('span', {'data-format': 'calendar-xs'})
            date = None
            if date_span:
                date_str = date_span.get('data-time', '').split('T')[0]
                if date_str:
                    date = makeDate(date_str, self.dateformat)
            
            chapters.append((title, chapter_url, date))

        # Set chapter count
        self.story.setMetadata('numChapters', len(chapters))

        # Add chapters to story
        for title, chapter_url, date in chapters:
            self.add_chapter(title, chapter_url)

        return

    def get_request(self, url, **kwargs):
        """
        Overridden get_request to add cache monitoring
        """
        logger.debug("Fetching URL: %s (use_basic_cache: %s)" % 
                    (url, self.getConfig('use_basic_cache')))
        
        response = super().get_request(url, **kwargs)
        
        logger.debug("Response received for: %s (length: %s)" % 
                    (url, len(response) if response else 'None'))
        
        return response

    def getChapterText(self, url):
        logger.debug('Getting chapter text from: %s' % url)
        
        # Extract chapter ID from URL
        chapter_id = re.search(r'/reader/\d+/(\d+)', url)
        if not chapter_id:
            logger.error('Could not find chapter ID in URL: %s', url)
            return ""
        
        # Construct API URL for chapter content
        work_id = self.story.getMetadata('storyId')
        api_url = f'https://{self.getSiteDomain()}/reader/{work_id}/chapter?id={chapter_id.group(1)}&_={int(time.time()*1000)}'
        
        try:
            # Ensure we're logged in
            if not self._logged_in:
                self.performLogin(url, None)
            
            # Add additional headers
            headers = {
                'Accept': 'application/json',
                'X-Requested-With': 'XMLHttpRequest',
                'User-Agent': self.getConfig('user_agent'),
                'Referer': f'https://{self.getSiteDomain()}/reader/{work_id}'
            }
            
            # Now get the chapter content
            response = self.session.get(api_url, headers=headers)
            response.raise_for_status()
            
            # Get reader-secret from response headers (case-insensitive)
            reader_secret = None
            for header_name, header_value in response.headers.items():
                if header_name.lower() == 'reader-secret':
                    reader_secret = header_value
                    break
                
            if not reader_secret:
                logger.error('Could not find reader-secret in response headers')
                return ""
            
            try:
                json_data = response.json()
            except json.JSONDecodeError as e:
                logger.error("Failed to decode JSON response: %s" % e)
                return ""
            
            if not json_data.get('isSuccessful'):
                error_msg = json_data.get('message', 'Unknown error')
                logger.error('Server returned unsuccessful response: %s' % error_msg)
                return ""
            
            # Get encrypted text from data field
            if not json_data.get('data') or not isinstance(json_data['data'], dict):
                logger.error('Invalid data structure in response')
                return ""
            
            # Create data structure expected by decrypt_chapter_text
            chapter_data = {'text': json_data['data']['text']}
            
            # Decrypt chapter content
            decrypted_text = self.decrypt_chapter_text(chapter_data, reader_secret)
            if not decrypted_text:
                logger.error('Failed to decrypt chapter text')
                return ""
            
            # Parse the decrypted HTML
            chapter_soup = self.make_soup(decrypted_text)
            
            # Если включена загрузка изображений
            if self.getConfig('include_images', True):
                logger.debug("Starting image processing...")
                images = chapter_soup.find_all('img')
                if images:
                    logger.debug(f"Found {len(images)} images in chapter")
                    
                    # Обработаем каждое изображение
                    for img in images:
                        # Получаем URL изображения
                        img_url = img['src']
                        if not img_url.startswith('http'):
                            img_url = 'https://' + self.getSiteDomain() + img_url
                        
                        try:
                            # Создаем div для центрирования
                            div = chapter_soup.new_tag('div')
                            div['style'] = 'text-align: center; margin: 1em 0;'
                            
                            # Создаем новый тег img
                            new_img = chapter_soup.new_tag('img')
                            
                            # Генерируем имя файла для изображения
                            img_filename = re.sub(r'[^\w\-_.]', '_', img_url.split('/')[-1])
                            if not img_filename.lower().endswith(('.jpg', '.jpeg', '.png', '.gif')):
                                img_filename += '.jpg'
                            
                            # Устанавливаем атрибуты изображения
                            new_img['src'] = img_filename
                            new_img['style'] = 'max-width: 100%; height: auto;'
                            new_img['alt'] = img.get('alt', '')
                            
                            # Добавляем изображение в div
                            div.append(new_img)
                            
                            # Заменяем старый тег img на новый div
                            img.replace_with(div)
                            
                            # Добавляем изображение в очередь загрузки
                            self.story.addImgUrl(url, img_url, True, img_filename)
                            
                            logger.debug(f"Added image to download queue: {img_url} -> {img_filename}")
                        except Exception as e:
                            logger.error(f"Error processing image {img_url}: {e}")
                            img.decompose()
                    
                    logger.debug(f"Processed {len(images)} images")
                else:
                    logger.debug("No images found in chapter")
            
            # Remove any unwanted elements
            for div in chapter_soup.find_all('div', {'class': ['banner', 'adv', 'ads', 'advertisement']}):
                div.decompose()
            
            # Clean up any empty paragraphs
            for p in chapter_soup.find_all('p'):
                if not p.get_text(strip=True) and not p.find('img'):
                    p.decompose()
            
            # Преобразуем суп обратно в текст с сохранением HTML
            chapter_text = str(chapter_soup)
            
            return chapter_text
            
        except Exception as e:
            logger.error("Error getting chapter text: %s", e)
            return ""

    def download_and_decrypt_chapter(self, chapter_url):
        """
        Download and decrypt a chapter from the given URL.
        
        Args:
            chapter_url (str): The URL of the chapter to download.
            
        Returns:
            str: The decrypted chapter text.
        """
        logger.debug('Downloading chapter from: %s' % chapter_url)

        # Ensure we're logged in
        if not self._logged_in:
            self.performLogin(chapter_url)

        # Fetch chapter content
        try:
            chapter_content = self.getChapterText(chapter_url)
            if not chapter_content:
                logger.error('Failed to fetch chapter content from: %s' % chapter_url)
                return ''

            # Decrypt the chapter content
            decrypted_text = self.decrypt_chapter_text(chapter_content, self.user_id)
            if not decrypted_text:
                logger.error('Failed to decrypt chapter content from: %s' % chapter_url)
                return ''

            logger.debug('Successfully downloaded and decrypted chapter from: %s' % chapter_url)
            return decrypted_text

        except Exception as e:
            logger.error('Error downloading or decrypting chapter: %s', str(e))
            return ''

    def download_image(self, url):
        """Download and optimize a single image"""
        try:
            response = requests.get(url)
            response.raise_for_status()
            img_data = response.content

            # Use Pillow to process the image
            if self.has_pil:
                from PIL import Image
                import io
                import tempfile
                import os

                img = Image.open(io.BytesIO(img_data))

                # Optimize size if too large
                max_size = (1200, 1800)
                if img.size[0] > max_size[0] or img.size[1] > max_size[1]:
                    img.thumbnail(max_size, Image.Resampling.LANCZOS)

                # Save optimized image to temporary file
                with tempfile.NamedTemporaryFile(delete=False, suffix='.'+img.format.lower() if img.format else '.jpg') as tmp_file:
                    img.save(tmp_file, format=img.format or 'JPEG', quality=85, optimize=True)
                    tmp_file_path = tmp_file.name

                # Read optimized image data
                with open(tmp_file_path, 'rb') as f:
                    img_data = f.read()

                # Clean up temporary file
                os.unlink(tmp_file_path)

            return img_data
        except requests.RequestException as e:
            logger.error(f"Error downloading image from {url}: {e}")
            return None

    def download_images_concurrently(self, soup):
        """Download images concurrently and replace src attributes with downloaded content"""
        try:
            images = soup.find_all('img')
            if not images:
                return

            logger.debug(f"Found {len(images)} images to download")

            # Create thread pool
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                future_to_img = {}
                
                # Queue all image downloads
                for img in images:
                    if not img.get('src'):
                        continue
                        
                    img_url = img['src']
                    if not img_url.startswith('http'):
                        img_url = 'https://' + self.getSiteDomain() + img_url
                    
                    logger.debug(f"Queuing image download: {img_url}")
                    future = executor.submit(self.download_image, img_url)
                    future_to_img[future] = img

                # Process results as they complete
                for future in concurrent.futures.as_completed(future_to_img):
                    img_tag = future_to_img[future]
                    try:
                        img_data = future.result()
                        if img_data:
                            # Create base64 string from image data
                            b64_data = base64.b64encode(img_data).decode('utf-8')
                            img_tag['src'] = f'data:image/jpeg;base64,{b64_data}'
                            logger.debug(f"Successfully embedded image: {img_tag.get('src')[:50]}...")
                        else:
                            logger.warning(f"Failed to download image: {img_tag.get('src')}")
                            img_tag.decompose()
                    except Exception as e:
                        logger.error(f"Error downloading image {img_tag.get('src')}: {e}")
                        img_tag.decompose()

        except Exception as e:
            logger.error(f"Error in concurrent image download: {e}")

    def test_cover_handling(self, cover_url):
        """
        Test cover image handling with and without Pillow.
        Returns tuple: (with_pillow_size, without_pillow_size, with_pillow_format, without_pillow_format)
        """
        import requests
        import tempfile
        import os
        import io
        
        def get_image_info(image_data):
            """
            Helper function to get image info without Pillow.
            Tries to determine the image format based on magic numbers.
            Returns a tuple with the image size and format.
            """
            image_size = len(image_data)
            if image_data.startswith(b'\xff\xd8'):
                image_format = 'JPEG'
            elif image_data.startswith(b'\x89PNG\r\n\x1a\n'):
                image_format = 'PNG'
            else:
                image_format = 'Unknown'
            return image_size, image_format
        
        results = {'with_pillow': None, 'without_pillow': None}
        
        # Test without Pillow
        try:
            response = requests.get(cover_url)
            image_data = response.content
            image_size, image_format = get_image_info(image_data)
            results['without_pillow'] = (image_size, image_format)
        except Exception as e:
            logger.error(f"Error in without-Pillow test: {e}")
        
        # Test with Pillow
        try:
            from PIL import Image
            
            response = requests.get(cover_url)
            img = Image.open(io.BytesIO(response.content))
            
            max_size = (1200, 1800)
            if img.size[0] > max_size[0] or img.size[1] > max_size[1]:
                img.thumbnail(max_size, Image.Resampling.LANCZOS)

            # Save optimized image to temporary file
            with tempfile.NamedTemporaryFile(delete=False, suffix='.'+img.format.lower() if img.format else '.jpg') as tmp_file:
                img.save(tmp_file, format=img.format or 'JPEG', quality=85, optimize=True)
                tmp_file_path = tmp_file.name

            opt_size = os.path.getsize(tmp_file_path)
            opt_format = img.format
            results['with_pillow'] = (opt_size, opt_format)
            
            if results['without_pillow']:
                orig_size = results['without_pillow'][0]
                reduction = ((orig_size - opt_size) / orig_size) * 100
                logger.info(f"Size reduction: {reduction:.1f}%")
            
            os.unlink(tmp_file_path)
        except ImportError:
            logger.warning("Pillow not available for testing")
        except Exception as e:
            logger.error(f"Error in Pillow test: {e}")
        
        return results
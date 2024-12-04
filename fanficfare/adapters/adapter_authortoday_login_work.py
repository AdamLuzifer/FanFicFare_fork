# -*- coding: utf-8 -*-

# Standard library imports
from __future__ import absolute_import
import logging
import re
import json
import time
from datetime import datetime

import concurrent

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
            logger.debug('Already logged in')
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
                    logger.debug("Successfully logged in using browser cache")
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
                    
                    # Extract user ID for decryption
                    user_id_match = re.search(r'userId\s*=\s*(\d+)', response.text)
                    if user_id_match:
                        self.user_id = user_id_match.group(1)
                        logger.debug(f"Extracted user ID: {self.user_id}")
                    
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

    def decrypt_chapter_text(self, encrypted_text, reader_secret):
        """
        Decrypt the chapter text using the reader secret.
        
        Args:
            encrypted_text (str): The encrypted text from the server
            reader_secret (str): The reader secret key
            
        Returns:
            str: The decrypted text, or empty string if decryption fails
        """
        try:
            if not encrypted_text or not reader_secret:
                return ''
                
            # Create decryption key by reversing reader_secret and appending user_id
            key = reader_secret[::-1] + "@_@" + (self.user_id or "")
            key_len = len(key)
            text_len = len(encrypted_text)
            
            # Convert text to list of character codes
            text_codes = [ord(c) for c in encrypted_text]
            key_codes = [ord(c) for c in key]
            
            # Decrypt using XOR with cycling key
            result = []
            for pos in range(text_len):
                key_char = key_codes[pos % key_len]
                result.append(chr(text_codes[pos] ^ key_char))
                
            return ''.join(result)
            
        except Exception as e:
            logger.error("Error decrypting chapter text: %s", e)
            return ""

    def extract_tags(self, soup):
        """Extract and deduplicate tags from various sources on the page"""
        tags = []
        
        # Extract genres
        genres_div = soup.find('div', {'class': 'book-genres'})
        if genres_div:
            for genre in genres_div.find_all('a'):
                tag_text = genre.get_text().strip()
                if tag_text and tag_text not in tags:
                    tags.append(tag_text)

        # Extract tags from spans with 'tags' class
        tags_spans = soup.find_all('span', {'class': 'tags'})
        for span in tags_spans:
            for tag in span.find_all('a'):
                tag_text = tag.get_text().strip()
                if tag_text and tag_text not in tags:
                    tags.append(tag_text)

        # Extract additional tags using various selectors
        additional_selectors = [
            'div.book-tags span',
            'div.book-tags a',
            'div.tags-container a',
            'div.book-meta-tags a'
        ]
        
        for selector in additional_selectors:
            for element in soup.select(selector):
                tag_text = element.get_text().strip()
                if tag_text and not any(skip in tag_text.lower() for skip in ['глав', 'страниц', 'знак']):
                    if tag_text not in tags:
                        tags.append(tag_text)

        return tags

    def extractChapterUrlsAndMetadata(self):
        url = self.url
        logger.debug("URL: "+url)

        data = self.get_request(url)
        soup = self.make_soup(data)

        # Check if story exists
        if "Произведение не найдено" in data:
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
            # Set tags as genre
            for tag in tags:
                self.story.addToList('genre', tag)
                
            # Set tags as tags
            for tag in tags:
                self.story.addToList('tags', tag)
                
            # Also set as subject tags
            for tag in tags:
                self.story.addToList('subject', tag)

            logger.debug(f"Added tags: {', '.join(tags)}")
            
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
        
        # Extract work_id and chapter_id from URL
        match = re.match(r'.*?/reader/(\d+)/(\d+)', url)
        if not match:
            # Try alternate URL pattern
            match = re.match(r'.*?/work/(\d+)(?:/chapter/(\d+))?', url)
            if not match:
                logger.error('Failed to extract IDs from chapter URL')
                return ""
                
        work_id = match.group(1)
        chapter_id = match.group(2) if match.group(2) else "1"  # Default to first chapter
        
        # First, get the reader page to obtain necessary tokens
        reader_url = f'https://{self.getSiteDomain()}/reader/{work_id}'
        
        try:
            # Ensure we're logged in
            if not self._logged_in:
                self.performLogin(url, None)
            
            # Get reader page first
            response = self.session.get(reader_url)
            response.raise_for_status()
            
            # Extract reader-secret from page
            reader_secret = None
            
            # Try multiple patterns for reader secret
            secret_patterns = [
                r'readerSecret\s*=\s*[\'"]([^\'"]+)[\'"]',
                r'data-reader-secret=[\'"]([^\'"]+)[\'"]',
                r'"readerSecret"\s*:\s*[\'"]([^\'"]+)[\'"]'
            ]
            
            for pattern in secret_patterns:
                match = re.search(pattern, response.text)
                if match:
                    reader_secret = match.group(1)
                    logger.debug("Found reader secret using pattern: %s" % pattern)
                    break
            
            # Set required headers
            headers = {
                'Accept': 'application/json, text/javascript, */*; q=0.01',
                'X-Requested-With': 'XMLHttpRequest',
                'Referer': reader_url,
                'Origin': 'https://' + self.getSiteDomain(),
                'Sec-Fetch-Site': 'same-origin',
                'Sec-Fetch-Mode': 'cors',
                'Sec-Fetch-Dest': 'empty'
            }
            
            if reader_secret:
                headers['Reader-Secret'] = reader_secret
            
            # Add headers to session
            self.session.headers.update(headers)
            
            # API endpoint for chapter content with timestamp
            api_url = f'https://{self.getSiteDomain()}/reader/{work_id}/chapter?id={chapter_id}&_={int(time.time()*1000)}'
            
            # Get chapter content
            response = self.session.get(api_url)
            response.raise_for_status()

            try:
                json_data = response.json()
            except json.JSONDecodeError as e:
                logger.error("Failed to decode JSON response: %s" % e)
                return ""
            
            if not json_data.get('isSuccessful'):
                error_msg = json_data.get('message', 'Unknown error')
                logger.error('Server returned unsuccessful response for chapter: %s' % error_msg)
                return ""
            
            # Look for reader-secret in various places
            if not reader_secret:
                # Try headers first
                for header_name in ['Reader-Secret', 'reader-secret', 'X-Reader-Secret', 'x-reader-secret']:
                    if header_name.lower() in response.headers:
                        reader_secret = response.headers[header_name]
                        break
                
                # Then try response data
                if not reader_secret and 'readerSecret' in json_data:
                    reader_secret = json_data['readerSecret']
                
                # Finally try data.readerSecret
                if not reader_secret and 'data' in json_data and 'readerSecret' in json_data['data']:
                    reader_secret = json_data['data']['readerSecret']
            
            if not reader_secret:
                logger.error('Could not find reader-secret in any location')
                return ""
            
            # Get the actual text content
            if 'data' not in json_data or 'text' not in json_data['data']:
                logger.error('No text content found in response')
                return ""
            
            # Decrypt chapter content
            decrypted_text = self.decrypt_chapter_text(json_data['data']['text'], reader_secret)
            if not decrypted_text:
                return ""
            
            # Parse the decrypted HTML
            chapter_soup = self.make_soup(decrypted_text)
            
            # Remove any unwanted elements
            for div in chapter_soup.find_all('div', {'class': ['banner', 'adv', 'ads', 'advertisement']}):
                div.decompose()
            
            # Clean up any empty paragraphs
            for p in chapter_soup.find_all('p'):
                if not p.get_text(strip=True):
                    p.decompose()
            
            return self.utf8FromSoup(url, chapter_soup)
            
        except Exception as e:
            logger.error("Error getting chapter text: %s", e)
            return ""

    def download_image(self, url):
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

                # Clean up the temporary file
                os.unlink(tmp_file_path)

            return img_data
        except requests.RequestException as e:
            logger.error(f"Error downloading image from {url}: {e}")
            return None

    def download_images_concurrently(self, urls):
        images = {}
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future_to_url = {executor.submit(self.download_image, url): url for url in urls}
            for future in concurrent.futures.as_completed(future_to_url):
                url = future_to_url[future]
                try:
                    data = future.result()
                    if data:
                        images[url] = data
                except Exception as e:
                    logger.error(f"Error processing image from {url}: {e}")
        return images

    def test_cover_handling(self, cover_url):
        """
        Test cover image handling with and without Pillow.
        Returns tuple: (with_pillow_size, without_pillow_size, with_pillow_format, without_pillow_format)
        """
        import requests
        import tempfile
        import os
        import io
        
        def get_image_info(img_data):
            """Helper to get image info without Pillow"""
            size = len(img_data)
            # Basic format detection from magic numbers
            if img_data.startswith(b'\xff\xd8'):
                fmt = 'JPEG'
            elif img_data.startswith(b'\x89PNG\r\n\x1a\n'):
                fmt = 'PNG'
            else:
                fmt = 'Unknown'
            return size, fmt
        
        results = {'with_pillow': None, 'without_pillow': None}
        
        # Test without Pillow
        self.has_pil = False
        try:
            response = requests.get(cover_url)
            orig_data = response.content
            orig_size, orig_format = get_image_info(orig_data)
            results['without_pillow'] = (orig_size, orig_format)
            logger.debug(f"Without Pillow - Size: {orig_size} bytes, Format: {orig_format}")
        except Exception as e:
            logger.error(f"Error in without-Pillow test: {e}")
        
        # Test with Pillow
        try:
            from PIL import Image
            self.has_pil = True
            
            response = requests.get(cover_url)
            img = Image.open(io.BytesIO(response.content))
            
            # Optimize size if too large
            max_size = (1200, 1800)
            if img.size[0] > max_size[0] or img.size[1] > max_size[1]:
                img.thumbnail(max_size, Image.Resampling.LANCZOS)

            # Save optimized image to temporary file to measure size
            with tempfile.NamedTemporaryFile(delete=False, suffix='.'+img.format.lower() if img.format else '.jpg') as tmp_file:
                img.save(tmp_file, format=img.format or 'JPEG', quality=85, optimize=True)
                tmp_file_path = tmp_file.name
            
            # Get optimized file size
            opt_size = os.path.getsize(tmp_file_path)
            opt_format = img.format
            
            # Clean up
            os.unlink(tmp_file_path)
            
            results['with_pillow'] = (opt_size, opt_format)
            logger.debug(f"With Pillow - Size: {opt_size} bytes, Format: {opt_format}")
            
            # Print size reduction percentage if original size is known
            if results['without_pillow']:
                orig_size = results['without_pillow'][0]
                reduction = ((orig_size - opt_size) / orig_size) * 100
                logger.debug(f"Size reduction: {reduction:.1f}%")
                
        except ImportError:
            logger.warning("Pillow not available for testing")
        except Exception as e:
            logger.error(f"Error in Pillow test: {e}")
        
        return results
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
        self.user_id = None   # Will be set during login if needed
        self.session = None  # Session for making requests
        self._logged_in = False  # Track login state
        self._login_attempts = 0  # Track number of login attempts
        self._browser_cache_checked = False
        self.bearer_token = None
        self.token_expires = None
        
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
        
        # Добавляем счетчики для всей книги
        self.total_book_images = 0
        self.successful_book_downloads = 0
        self.failed_book_downloads = 0
        self.chapters_processed = 0
        # Добавляе�� счетчик типов изображений
        self.image_types = {
            'jpeg': 0,
            'png': 0,
            'gif': 0,
            'webp': 0,
            'other': 0
        }

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
        Perform login to author.today using bearer token API approach.
        First opens login page in WebView for user authentication,
        then retrieves bearer token using LoginCookie.
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
            self._login_attempts = 0
            raise exceptions.FailedToLogin(url, "Exceeded maximum login attempts (3)")
            
        logger.debug("Starting login process... (Attempt %d/3)", self._login_attempts)
    
        try:
            # Try to use browser cache if enabled and not checked yet
            if self.getConfig('use_browser_cache', False) and not self._browser_cache_checked:
                logger.debug("Attempting to use browser cache for login")
                self._browser_cache_checked = True
                cached_cookie = self.get_browser_cookie()
                if cached_cookie:
                    self.session.cookies.update(cached_cookie)
    
            # First step: Get login page and CSRF token
            login_url = f'https://{self.getSiteDomain()}/account/login'
            response = self.session.get(login_url)
            response.raise_for_status()
    
            # Extract CSRF token
            soup = BeautifulSoup(response.text, 'html.parser')
            csrf_input = soup.find('input', {'name': '__RequestVerificationToken'})
            if not csrf_input:
                raise exceptions.FailedToLogin(url, 'Could not find CSRF token')
            csrf_token = csrf_input.get('value')
    
            # Perform login to get LoginCookie
            login_data = {
                'Login': self.username,
                'Password': self.password,
                '__RequestVerificationToken': csrf_token,
                'RememberMe': 'true'
            }
    
            headers = {
                'Content-Type': 'application/x-www-form-urlencoded',
                'Origin': f'https://{self.getSiteDomain()}',
                'Referer': login_url,
                'X-Requested-With': 'XMLHttpRequest',
                'Accept': 'application/json, text/javascript, */*; q=0.01'
            }
    
            response = self.session.post(login_url, data=login_data, headers=headers)
            response.raise_for_status()
    
            # Check if we got LoginCookie
            if not self.getAuthCookie():
                raise exceptions.FailedToLogin(url, "Login failed - LoginCookie not found")
    
            # Now get the bearer token using LoginCookie
            token_url = f'https://{self.getSiteDomain()}/account/bearer-token'
            token_response = self.session.get(token_url)
            token_response.raise_for_status()
    
            try:
                token_data = token_response.json()
                if 'token' not in token_data:
                    raise exceptions.FailedToLogin(url, 'Failed to obtain bearer token')
    
                # Store the token and update session headers
                self.bearer_token = token_data['token']
                self.user_id = str(token_data['userId'])
                self.token_expires = datetime.strptime(token_data['expires'], "%Y-%m-%dT%H:%M:%SZ")
    
                # Update session headers with bearer token
                self.session.headers.update({
                    'Authorization': f'Bearer {self.bearer_token}'
                })
    
                self._logged_in = True
                self._login_attempts = 0
                logger.debug("Successfully obtained bearer token")
                return True
    
            except (ValueError, KeyError) as e:
                logger.error(f"Failed to parse token response: {str(e)}")
                raise exceptions.FailedToLogin(url, f'Failed to parse token response: {str(e)}')
    
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
        """Check if current session is logged in and token is valid."""
        if not self._logged_in or not self.bearer_token:
            return False
            
        # Check if token has expired
        if self.token_expires and datetime.utcnow() > self.token_expires:
            logger.debug("Bearer token has expired")
            return False
            
        try:
            # Try to make a test request to verify token
            test_url = f'https://{self.getSiteDomain()}/api/v1/profile'
            response = self.session.get(test_url)
            return response.status_code == 200
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
            return any(cookie.name in ['LoginCookie', 'ngLoginCookie'] for cookie in cookies)
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
                logger.error("Missing encrypted_text or reader_secret")
                return ''
                
            # Create decryption key by reversing reader_secret and appending user_id
            key = reader_secret[::-1] + "@_@" + (self.user_id or "")
            key_len = len(key)
            text_len = len(encrypted_text)
        
            logger.debug(f"Decrypting text (length: {text_len}) with key (length: {key_len})")
        
            # Convert text to list of character codes
            text_codes = [ord(c) for c in encrypted_text]
            key_codes = [ord(c) for c in key]
        
            # Decrypt using XOR with cycling key
            result = []
            for pos in range(text_len):
                key_char = key_codes[pos % key_len]
                result.append(chr(text_codes[pos] ^ key_char))
            
            decrypted = ''.join(result)
        
            # Проверяем, что расшифрованный текст содер��ит валидный HTML
            if not ('<' in decrypted and '>' in decrypted):
                logger.error("Decrypted text does not appear to be valid HTML")
                logger.debug(f"First 200 chars of decrypted text: {decrypted[:200]}")
                return ""
            
            logger.debug(f"Successfully decrypted {text_len} characters of text")
            return decrypted
        
        except Exception as e:
            logger.error(f"Error decrypting chapter text: {str(e)}")
            logger.debug(f"encrypted_text length: {len(encrypted_text) if encrypted_text else 0}")
            logger.debug(f"reader_secret length: {len(reader_secret) if reader_secret else 0}")
            return ""
        
    def extract_tags(self, soup):
        """
        Извлечь теги, используя API author.today.
        Возвращает список тегов из API, включая метку 18+ если AdultOnly=true.
        """
        try:
            # Attempt to log in if not already logged in
            if not self._logged_in:
                self.performLogin(self.url)
            
            # Получаем ID книги из URL
            story_id = self.story.getMetadata('storyId')
            logger.debug(f"Extracting tags for story ID: {story_id}")
            
            # Формируем URL для API запроса
            api_url = f'https://api.author.today/v1/work/{story_id}/details'
            
            # More robust token handling
            if not self.bearer_token:
                logger.warning("Bearer token not found. Attempting to retrieve a new token.")
                try:
                    # Retry login to get a fresh token
                    self.performLogin(self._getURL())
                except Exception as e:
                    logger.error(f"Failed to obtain bearer token: {e}")
                    return self._extract_tags_from_html(soup)

            # Делаем запрос к API
            headers = {
                'Authorization': f'Bearer {self.bearer_token}'
            }
            
            response = self.session.get(api_url, headers=headers)
            response.raise_for_status()
            
            data = response.json()
            logger.debug(f"API Response: {data}")  # Добавляем лог ответа API
            
            tags = []
            
            # Получаем теги из API
            if 'tags' in data:
                tags.extend(tag.strip() for tag in data['tags'] if tag.strip())
                logger.debug(f"Tags from API: {tags}")
            else:
                logger.warning("No 'tags' field found in API response")
                
            # Добавляем жанры, если они есть
            if 'genreId' in data and data['genreId']:
                genre = self._get_genre_name(data['genreId'])
                if genre:
                    tags.append(genre)
                    
            if 'firstSubGenreId' in data and data['firstSubGenreId']:
                subgenre = self._get_genre_name(data['firstSubGenreId'])
                if subgenre:
                    tags.append(subgenre)
                    
            if 'secondSubGenreId' in data and data['secondSubGenreId']:
                subgenre2 = self._get_genre_name(data['secondSubGenreId'])
                if subgenre2:
                    tags.append(subgenre2)
                    
            # Добавляем метку 18+ если контент для взрослых
            if data.get('adultOnly', False):
                tags.append("18+")
                
            # Добавляем дополнительные метки из поля marks, если они есть
            if 'marks' in data and data['marks']:
                for mark_id in data['marks']:
                    mark_name = self._get_mark_name(mark_id)
                    if mark_name:
                        tags.append(mark_name)
                        
            logger.debug(f"Final tags list: {tags}")
            
            if not tags:  # Если теги не найдены через API
                logger.warning("No tags found via API, falling back to HTML parsing")
                return self._extract_tags_from_html(soup)
                
            return list(dict.fromkeys(tags))  # Удаляем дубликаты, сохраняя порядок
            
        except Exception as e:
            logger.error(f"Error fetching tags from API: {str(e)}")
            return self._extract_tags_from_html(soup)

    def _extract_tags_from_html(self, soup):
        """Резервный метод извлечения тегов из HTML при ошибке API"""
        tags = []
        
        # Извлечение жанров
        genres_div = soup.find('div', {'class': 'book-genres'})
        if genres_div:
            for genre in genres_div.find_all('a'):
                tag_text = genre.get_text().strip()
                if tag_text and tag_text not in tags:
                    tags.append(tag_text)
    
        # Извлечение тегов из спанов с классом 'tags'
        tags_spans = soup.find_all('span', {'class': 'tags'})
        for span in tags_spans:
            for tag in span.find_all('a'):
                tag_text = tag.get_text().strip()
                if tag_text and tag_text not in tags:
                    tags.append(tag_text)
    
        # Проверка на наличие метки 18+
        adult_label = soup.select_one('div.book-stats span.label-adult-only')
        if adult_label:
            tags.append("18+")
    
        # Извлечение дополнительных тегов
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
        try:
            # Attempt to log in if not already logged in
            if not self._logged_in:
                self.performLogin(self.url)

            # Получаем ID книги из URL
            story_id = self.story.getMetadata('storyId')
            
            # Получаем изображения галереи через основной API запрос
            if story_id:
                api_url = f'https://api.author.today/v1/work/{story_id}/details'
                try:
                    headers = {
                        'Authorization': f'Bearer {self.bearer_token}',
                        'Accept': 'application/json'
                    }
                    
                    response = self.session.get(api_url, headers=headers)
                    response.raise_for_status()
                    data = response.json()
                    
                    if data.get('galleryImages'):
                        for img_data in data['galleryImages']:
                            if 'url' in img_data:
                                img_url = img_data['url']
                                caption = img_data.get('caption', '')
                                
                                # Добавляем изображение в историю
                                self.story.addImgUrl(
                                    parenturl=self.url,
                                    url=img_url,
                                    cover=False,
                                    gallery=True,
                                    caption=caption,
                                    fetch=self.get_request_raw
                                )
                                logger.debug(f"Added gallery image: {img_url} ({caption})")
                except Exception as e:
                    logger.warning(f"Failed to fetch gallery images: {e}")
                    logger.debug(f"API response: {response.text if 'response' in locals() else 'No response'}")

            # More robust token handling
            if not self.bearer_token:
                logger.warning("Bearer token not found. Attempting to retrieve a new token.")
                try:
                    # Retry login to get a fresh token
                    self.performLogin(self.url)
                except Exception as e:
                    logger.error(f"Failed to obtain bearer token: {e}")
                    raise exceptions.StoryDoesNotExist(self.url) from e

            # Формируем URL для API запроса
            api_url = f'https://api.author.today/v1/work/{story_id}/details'
            
            # Делаем запрос к API
            headers = {
                'Authorization': f'Bearer {self.bearer_token}'
            }
            
            response = self.session.get(api_url, headers=headers)
            response.raise_for_status()
            
            data = response.json()
            
            # Проверка на существование произведения
            if not data or 'id' not in data:
                raise exceptions.StoryDoesNotExist(self.url)
                
            # Проверка на 18+
            if data.get('adultOnly', False) and not self.is_adult:
                raise exceptions.AdultCheckRequired(self.url)

            # Установка метаданных из API
            self.story.setMetadata('title', data.get('title', ''))
            
            # Обложка
            if data.get('coverUrl'):
                cover_url = data['coverUrl']
                if not cover_url.startswith('http'):
                    cover_url = 'https://' + self.getSiteDomain() + cover_url
                
                try:
                    if self.has_pil:
                        # Оптимизация изображения с PIL
                        import io
                        import tempfile
                        import os
                        from PIL import Image
                        
                        response = self.session.get(cover_url)
                        img = Image.open(io.BytesIO(response.content))
                        
                        # Оптимизация размера
                        max_size = (1200, 1800)
                        if img.size[0] > max_size[0] or img.size[1] > max_size[1]:
                            img.thumbnail(max_size, Image.Resampling.LANCZOS)
                        
                        # Сохранение во временный файл
                        with tempfile.NamedTemporaryFile(delete=False, suffix='.'+img.format.lower() if img.format else '.jpg') as tmp_file:
                            img.save(tmp_file, format=img.format or 'JPEG', quality=85, optimize=True)
                            tmp_file_path = tmp_file.name
                        
                        # URL для временного файла
                        tmp_url = 'file:///' + tmp_file_path.replace('\\', '/')
                        self.setCoverImage(self.url, tmp_url)
                        
                        # Очистка временного файла
                        try:
                            os.unlink(tmp_file_path)
                        except:
                            pass
                    else:
                        self.setCoverImage(self.url, cover_url)
                except Exception as e:
                    logger.warning(f"Failed to set cover image: {e}")

            # Теги и жанры
            tags = data.get('tags', [])
            if data.get('adultOnly', False):
                tags.append("18+")
            
            for tag in tags:
                self.story.addToList('genre', tag)
                self.story.addToList('tags', tag)
                self.story.addToList('subject', tag)

            # Автор
            self.story.setMetadata('author', data.get('authorFIO', ''))
            self.story.setMetadata('authorId', str(data.get('authorId', '')))
            self.story.setMetadata('authorUrl', f'https://{self.getSiteDomain()}/u/{data.get("authorUserName", "")}')

            # Описание
            self.story.setMetadata('description', stripHTML(data.get('annotation', '')))

            # Статус
            self.story.setMetadata('status', 'Completed' if data.get('isFinished', False) else 'In-Progress')

            # Количество слов
            if 'textLength' in data:
                self.story.setMetadata('numWords', str(data['textLength']))

            # Даты
            if 'lastUpdateTime' in data:
                update_date = data['lastUpdateTime'].split('T')[0]
                self.story.setMetadata('dateUpdated', makeDate(update_date, self.dateformat))
                
            if 'lastModificationTime' in data:
                pub_date = data['lastModificationTime'].split('T')[0]
                self.story.setMetadata('datePublished', makeDate(pub_date, self.dateformat))

            # Дополнительные метаданные
            if data.get('seriesTitle'):
                self.story.setMetadata('series', data['seriesTitle'])
                if data.get('seriesOrder'):
                    self.story.setMetadata('seriesIndex', data['seriesOrder'])

            # Получение списка глав через отдельный API-запрос
            chapters_url = f'https://api.author.today/v1/work/{story_id}/content'
            chapters_response = self.session.get(chapters_url, headers=headers)
            chapters_response.raise_for_status()
            chapters_data = chapters_response.json()

            if not chapters_data:
                # Если глав нт - одноглавая история
                self.add_chapter('Chapter 1', self.url)
                self.story.setMetadata('numChapters', 1)
                return

            # Получаем список глав
            chapters = []
            for chapter in chapters_data:
                chapter_title = chapter.get('title', '')
                chapter_url = f'https://{self.getSiteDomain()}/reader/{story_id}/{chapter["id"]}'
                chapters.append((chapter_title, chapter_url))

            # Добавляем основные главы
            for title, url in chapters:
                self.add_chapter(title, url)

            # Проверяем наличие изображений в галерее
            if data.get('galleryImages'):
                gallery_images = data['galleryImages']
                if gallery_images:
                    # Создаем специальную главу для галереи
                    gallery_chapter_title = "Доп. материалы"
                    logger.debug(f"Creating gallery chapter with {len(gallery_images)} images")
                    
                    # Создаем HTML контент для галереи
                    gallery_html = self.make_gallery_chapter(gallery_images)
                    self.chapter_gallery_content = gallery_html
                    
                    # Добавляем главу галереи последней
                    self.add_chapter(gallery_chapter_title, None)
                    logger.debug("Added gallery chapter")

            # Обновляем количество глав
            total_chapters = len(chapters) + (1 if data.get('galleryImages') else 0)
            self.story.setMetadata('numChapters', total_chapters)
            logger.debug(f"Total chapters: {total_chapters} (including gallery)")

            return
            
        except Exception as e:
            logger.error("Error in extractChapterUrlsAndMetadata: %s", e)
            raise

    def _extractChapterUrlsAndMetadata_html(self):
        """Резервный метод извлечения метаданных из HTML при ошибке API"""
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
        """Get chapter text using Author.Today API"""
        logger.debug('Getting chapter text from: %s' % url)
        
        # Если это глава галереи (url=None)
        if url is None and hasattr(self, 'chapter_gallery_content'):
            logger.debug("Returning gallery chapter content")
            return self.chapter_gallery_content
        
        self.chapters_processed += 1
        
        # Ensure we're logged in before proceeding
        if not self._logged_in:
            self.performLogin(url)
        
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
        
        try:
            # Ensure we have bearer token
            if not self.bearer_token:
                logger.error("No bearer token available")
                return ""
    
            # Формируем URL для API запроса
            api_url = f'https://api.author.today/v1/work/{work_id}/chapter/{chapter_id}/text'
            
            # Set headers with bearer token
            headers = {
                'Authorization': f'Bearer {self.bearer_token}',
                'Accept': 'application/json,image/*',
                'Origin': 'https://' + self.getSiteDomain(),
                'Referer': f'https://{self.getSiteDomain()}/reader/{work_id}'
            }
            
            # Get chapter content
            response = self.session.get(api_url, headers=headers)
            response.raise_for_status()
    
            try:
                json_data = response.json()
            except json.JSONDecodeError as e:
                logger.error("Failed to decode JSON response: %s" % e)
                return ""
            
            if not json_data or 'text' not in json_data:
                logger.error('No text content found in response')
                return ""
            
            # Decrypt chapter content using provided key
            if 'key' not in json_data:
                logger.error('No decryption key found in response')
                return ""
                
            decrypted_text = self.decrypt_chapter_text(json_data['text'], json_data['key'])
            if not decrypted_text:
                return ""
            
            # Добавляем отладочный вывод HTML
            logger.debug("Decrypted HTML content:")
            logger.debug("=" * 80)
            logger.debug(decrypted_text[:2000])  # Первые 2000 символов
            logger.debug("=" * 80)
            
            # Parse the decrypted HTML
            chapter_soup = self.make_soup(decrypted_text)
            
            # Инициализация счетчиков для главы
            total_images = 0
            valid_urls = 0
            download_attempts = 0
            successful_downloads = 0
            failed_downloads = 0
            chapter_image_types = {
                'jpeg': 0,
                'png': 0,
                'gif': 0,
                'webp': 0,
                'other': 0
            }
            
            # Поиск изображений
            images = chapter_soup.find_all('img')
            total_images = len(images)
            self.total_book_images += total_images
            
            logger.info(f"\n=== Chapter {self.chapters_processed} Image Processing Statistics ===")
            logger.info(f"Images found in this chapter: {total_images}")
            
            # Обработка изображений
            if self.getConfig('extract_images', False):
                logger.debug("Image extraction is enabled")
                
                for idx, img in enumerate(images, 1):
                    if img.get('src'):
                        img_url = img['src']
                        valid_urls += 1
                        logger.debug(f"\nProcessing image {idx}/{total_images}: {img_url}")
                        
                        if img_url.startswith("https://cm.author.today/"):
                            download_attempts += 1
                            try:
                                logger.debug(f"Attempting to download image: {img_url}")
                                img_data = self.download_image(img_url)
                                
                                if img_data:
                                    successful_downloads += 1
                                    self.successful_book_downloads += 1
                                    
                                    # Определение типа изображения по content-type
                                    content_type = response.headers.get('content-type', '').lower()
                                    if 'jpeg' in content_type or 'jpg' in content_type:
                                        image_type = 'jpeg'
                                    elif 'png' in content_type:
                                        image_type = 'png'
                                    elif 'gif' in content_type:
                                        image_type = 'gif'
                                    elif 'webp' in content_type:
                                        image_type = 'webp'
                                    else:
                                        image_type = 'other'
                                    
                                    # Обновляем счетчики типов
                                    chapter_image_types[image_type] += 1
                                    self.image_types[image_type] += 1
                                    
                                    logger.debug(f"Successfully downloaded image {idx} (size: {len(img_data)} bytes, type: {image_type})")
                                else:
                                    failed_downloads += 1
                                    self.failed_book_downloads += 1
                                    logger.warning(f"Failed to download image {idx}")
                            except Exception as e:
                                failed_downloads += 1
                                self.failed_book_downloads += 1
                                logger.error(f"Error processing image {img_url}: {e}")
                        else:
                            logger.debug(f"Skipping non-author.today image URL: {img_url}")
                    else:
                        logger.warning(f"Image tag {idx} has no src attribute")
                
                # Статистика главы
                logger.info("\n=== Chapter Image Processing Statistics ===")
                logger.info(f"Chapter {self.chapters_processed}:")
                logger.info(f"Images found in chapter: {total_images}")
                logger.info(f"Valid URLs found: {valid_urls}")
                logger.info(f"Download attempts: {download_attempts}")
                logger.info(f"Successfully downloaded: {successful_downloads}")
                logger.info(f"Failed downloads: {failed_downloads}")
                if download_attempts > 0:
                    logger.info(f"Chapter success rate: {(successful_downloads/download_attempts*100):.1f}%")
                
                # Статистика типов изображений в главе
                logger.info("\n=== Chapter Image Types ===")
                for img_type, count in chapter_image_types.items():
                    if count > 0:
                        logger.info(f"{img_type.upper()}: {count}")
                
                # Общая статистика книги
                logger.info("\n=== Overall Book Statistics ===")
                logger.info(f"Chapters processed: {self.chapters_processed}")
                logger.info(f"Total images found in book: {self.total_book_images}")
                logger.info(f"Total successfully downloaded: {self.successful_book_downloads}")
                logger.info(f"Total failed downloads: {self.failed_book_downloads}")
                if self.total_book_images > 0:
                    logger.info(f"Overall success rate: {(self.successful_book_downloads/self.total_book_images*100):.1f}%")
                
                # Общая статистика типов изображений
                logger.info("\n=== Overall Image Types Statistics ===")
                for img_type, count in self.image_types.items():
                    if count > 0:
                        logger.info(f"{img_type.upper()}: {count}")
                        percentage = (count / self.successful_book_downloads * 100) if self.successful_book_downloads > 0 else 0
                        logger.info(f"{img_type.upper()} percentage: {percentage:.1f}%")
                
                logger.info("=====================================\n")
            else:
                logger.debug("Image extraction is disabled in configuration")
            
            return self.utf8FromSoup(url, chapter_soup)
            
        except Exception as e:
            logger.error("Error getting chapter text: %s", e)
            return ""
            
    def download_image(self, img_url):
        """Загрузка изображения с расширенным логированием"""
        logger.debug(f"\n=== Starting image download ===")
        logger.debug(f"URL: {img_url}")
        
        # Проверка URL
        if not img_url or not img_url.startswith("https://cm.author.today/"):
            logger.error(f"Invalid image URL: {img_url}")
            return None
        
        # Подготовка заголовков
        headers = {
            'Authorization': f'Bearer {self.bearer_token}' if self.bearer_token else '',
            'Accept': 'image/*',
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://' + self.getSiteDomain(),
            'Origin': 'https://' + self.getSiteDomain()
        }
        logger.debug(f"Request headers: {json.dumps(headers, indent=2)}")
        
        try:
            logger.debug("Sending HTTP request...")
            response = self.session.get(img_url, headers=headers)
            
            logger.debug(f"Response status code: {response.status_code}")
            logger.debug(f"Response headers: {json.dumps(dict(response.headers), indent=2)}")
            
            response.raise_for_status()
            
            content_length = len(response.content)
            logger.debug(f"Downloaded content length: {content_length} bytes")
            
            if content_length < 100:  # Минимальный размер изображения
                logger.warning(f"Suspicious image size: {content_length} bytes")
                return None
            
            # Проверка типа контента
            content_type = response.headers.get('content-type', '')
            logger.debug(f"Content type: {content_type}")
            
            if not any(img_type in content_type.lower() for img_type in ['image/jpeg', 'image/png', 'image/gif', 'image/webp']):
                logger.warning(f"Unexpected content type: {content_type}")
                return None
            
            logger.debug("Image download successful")
            return response.content
            
        except Exception as e:
            logger.error(f"Error downloading image: {e}")
            logger.exception("Full traceback:")
            return None

    def download_images_concurrently(self, urls):
        images = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:  # Ограничение на количество потоков
            future_to_url = {executor.submit(self.download_image, url): url for url in urls}
            for future in concurrent.futures.as_completed(future_to_url):
                url = future_to_url[future]
                try:
                    data = future.result()
                    if data:
                        images[url] = data
                    else:
                        logger.warning(f"Не удалось загрузить изображение с {url}")
                except Exception as e:
                    logger.error(f"Ошибка при обработке зображеня с {url}: {e}")
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
        self.has_pil = True
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

    def extract_images(self, soup):
        """
        Расширенный метод извлечения изображений с поддержкой галереи
        """
        images = []
        image_sources = []
        
        # Получаем ID книги
        story_id = self.story.getMetadata('storyId')
        
        # Получаем изображения галереи через основной API запрос
        if story_id:
            api_url = f'https://api.author.today/v1/work/{story_id}/details'
            try:
                headers = {
                    'Authorization': f'Bearer {self.bearer_token}',
                    'Accept': 'application/json'
                }
                
                response = self.session.get(api_url, headers=headers)
                response.raise_for_status()
                data = response.json()
                
                if data.get('galleryImages'):
                    for img_data in data['galleryImages']:
                        if 'url' in img_data:
                            img_url = img_data['url']
                            if img_url not in image_sources:
                                image_sources.append(img_url)
                                logger.debug(f"Found gallery image: {img_url} ({img_data.get('caption', '')})")
            except Exception as e:
                logger.warning(f"Failed to fetch gallery images: {e}")
                logger.debug(f"API response: {response.text if 'response' in locals() else 'No response'}")

        # Расширенная диагностика HTML
        html_content = str(soup)
        logger.debug(f"HTML длина: {len(html_content)} символов")
        logger.debug(f"Первые 1000 символов HTML:\n{html_content[:1000]}")
    
        # Поиск изображений с максимально широким охватом
        image_selectors = [
            # Стандартные теги
            'img', 
            # Медиа-контейнеры
            'picture', 'figure', 
            # Контейнер с фоновыми изображениями
            '[style*="background-image"]',
            # Специфические классы для Author.Today
            '.story-image', '.content-image', '.post-image'
        ]
    
        # Расширенный поиск изображений
        for selector in image_selectors:
            tags = soup.select(selector)
            logger.debug(f"Найдено тегов по селектору '{selector}': {len(tags)}")
            
            for tag in tags:
                # Извлечение URL из различных атрибутов
                src_attributes = [
                    'src', 'data-src', 'data-original', 
                    'data-image', 'data-url', 
                    # Для background-image
                    lambda t: re.search(r'url\([\'"]?([^\'"]+)[\'"]?\)', t.get('style', ''))
                ]
    
                for attr in src_attributes:
                    if callable(attr):
                        match = attr(tag)
                        if match:
                            src = match.group(1) if hasattr(match, 'group') else match
                    else:
                        src = tag.get(attr)
                    
                    if src and src not in image_sources:
                        # Нормализация URL
                        if not src.startswith('http'):
                            if src.startswith('//'):
                                src = 'https:' + src
                            elif src.startswith('/'):
                                src = f'https://{self.getSiteDomain()}{src}'
                            else:
                                src = f'https://{self.getSiteDomain()}/{src}'
                        
                        # Фильтрация и проверка URL
                        if src.startswith("https://cm.author.today/"):
                            image_sources.append(src)
                            logger.debug(f"Найден URL изображения: {src}")
    
        # Диагностика найденных источников
        logger.info(f"Всего найдено уникальных источников изображений: {len(image_sources)}")
    
        # Подготовка заголовков
        headers = {
            'Accept': 'image/jxl,image/jpeg,image/png,image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
            'Referer': f'https://{self.getSiteDomain()}',
            'User-Agent': self.getConfig('user_agent', 'FanFicFare/4.x'),
        }

        # Новый отладочный лог
        logger.debug(f"Заголовки для загрузки: {headers}")

        # Добавление Bearer токена
        if self.bearer_token:
            headers['Authorization'] = f'Bearer {self.bearer_token}'
            logger.debug("Добавлен Bearer токен для авторизации")
    
        # Загрузка изображений с расширенной диагностикой
        for img_url in image_sources:
            try:
                logger.info(f"Попытка загрузки изображения: {img_url}")
                
                # Проверка Bearer токена перед загрузкой
                if not self.bearer_token:
                    logger.warning("Отсутствует Bearer токен!")
    
                img_data = self.download_image(img_url, headers=headers)
                
                if img_data:
                    images.append(img_data)
                    logger.info(f"Успешно загружено изображение: {img_url}, размер: {len(img_data)} байт")
                    
                    # Добавление в историю с расширенной обработкой
                    try:
                        self.story.addImgUrl(
                            parenturl=self.url, 
                            url=img_url, 
                            cover=False,
                            fetch=self.get_request_raw
                        )
                        logger.debug(f"Изображение добавлено в историю: {img_url}")
                    except Exception as add_error:
                        logger.error(f"Ошибка при добавлении изображения {img_url}: {add_error}")
                else:
                    logger.warning(f"Не удалось загрузить изображение: {img_url}")
            
            except Exception as e:
                logger.error(f"Критическая ошибка при обработке изображения {img_url}: {e}")
                logger.exception("Полная трассировка ошибки")
    
        logger.info(f"Итого загружено изображений: {len(images)}")
        return images

    def make_gallery_chapter(self, gallery_images):
        """Создает HTML-контент для главы с галереей"""
        html = ['<div class="gallery-chapter">']
        
        def fetch_image(url, referer=None):
            """Функция для загрузки изображения с поддержкой referer"""
            return self.download_image(url)
        
        # Начальный номер для изображений
        image_counter = 0
        
        for idx, img in enumerate(gallery_images, 1):
            if 'url' in img:
                img_url = img['url']
                caption = img.get('caption', '')
                
                try:
                    # Генерируем имя файла с использованием счетчика
                    image_name = f'ffdl-{image_counter}.jpg'
                    image_counter += 1
                    
                    # Добавляем изображение через стандартный механизм
                    self.story.addImgUrl(
                        parenturl=self.url,
                        url=img_url,
                        cover=False,
                        fetch=fetch_image
                    )
                    
                    # Создаем HTML с правильным путем к файлу
                    html.append('<div class="gallery-item">')
                    html.append(f'<img src="images/{image_name}" alt="{caption}"/>')
                    if caption:
                        html.append(f'<div class="gallery-caption"><p>{caption}</p></div>')
                    html.append('</div>')
                    
                    self.successful_book_downloads += 1
                    logger.debug(f"Successfully added gallery image {idx}/{len(gallery_images)}: {img_url}")
                    
                except Exception as e:
                    self.failed_book_downloads += 1
                    logger.error(f"Error processing gallery image {img_url}: {e}")
                    continue
        
        html.append('</div>')
        
        css = """
        <style>
            .gallery-chapter {
                display: flex;
                flex-direction: column;
                align-items: center;
                gap: 2em;
                padding: 1em;
            }
            .gallery-item {
                max-width: 100%;
                text-align: center;
            }
            .gallery-item img {
                max-width: 100%;
                height: auto;
            }
            .gallery-caption {
                margin-top: 0.5em;
                font-style: italic;
                color: #666;
            }
        </style>
        """
        
        return css + '\n'.join(html)
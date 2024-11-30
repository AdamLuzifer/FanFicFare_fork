# -*- coding: utf-8 -*-

from __future__ import absolute_import
import logging
logger = logging.getLogger(__name__)
import re
import json
import base64
from ..htmlcleanup import stripHTML
from .. import exceptions as exceptions
import time
import requests
import concurrent.futures

from .base_adapter import BaseSiteAdapter, makeDate

def getClass():
    return AuthorTodayAdapter

class AuthorTodayAdapter(BaseSiteAdapter):

    def __init__(self, config, url):
        BaseSiteAdapter.__init__(self, config, url)
        
        self.username = None
        self.password = None
        self.is_adult = False
        self.user_id = None  # Will be set during login if needed
        
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

    def performLogin(self, url, data):
        params = {
            'username': self.username,
            'password': self.password
        }

        if not params['username']:
            params['username'] = self.getConfig("username")
            params['password'] = self.getConfig("password")

        if not params['username']:
            raise exceptions.FailedToLogin(url, params['username'])

        loginUrl = 'https://' + self.getSiteDomain() + '/login'
        logger.debug("Will now login to URL (%s) as (%s)" % (loginUrl, params['username']))

        soup = self.make_soup(self.get_request(loginUrl))
        
        # Get CSRF token
        csrf_token = soup.find('meta', {'name': 'csrf-token'})
        if csrf_token:
            params['csrf_token'] = csrf_token['content']

        data = self.post_request(loginUrl, params)
        
        if 'isLoggedIn = false' in data:
            logger.info('Failed to login to URL %s as %s' % (loginUrl, params['username']))
            raise exceptions.FailedToLogin(url, params['username'])
            
        # Extract user ID for decryption
        user_id_match = re.search(r'userId\s*=\s*(\d+)', data)
        if user_id_match:
            self.user_id = user_id_match.group(1)

    def decrypt_chapter_text(self, encrypted_text, reader_secret):
        if not encrypted_text or not reader_secret:
            return ''
            
        try:
            # Reverse reader_secret and append user ID
            secret = reader_secret[::-1] + '@_@' + (self.user_id or '')
            secret_len = len(secret)
            text_len = len(encrypted_text)
            
            # Decrypt using XOR with the key
            result = []
            for pos in range(text_len):
                char_code = ord(encrypted_text[pos]) ^ ord(secret[pos % secret_len])
                result.append(chr(char_code))
                
            return ''.join(result)
        except Exception as e:
            logger.error("Error decrypting chapter: %s", e)
            return ''

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
        
        # Extract chapter ID from URL
        chapter_id = re.search(r'/reader/\d+/(\d+)', url)
        if not chapter_id:
            logger.error('Could not find chapter ID in URL: %s', url)
            return ""
            
        # Construct API URL for chapter content
        work_id = self.story.getMetadata('storyId')
        api_url = f'https://{self.getSiteDomain()}/reader/{work_id}/chapter?id={chapter_id.group(1)}&_={int(time.time()*1000)}'
        
        try:
            # Use a session to manage cookies
            session = requests.Session()
            
            # Set the age restriction cookie manually
            session.cookies.set('ageRestrictionAccepted', 'true', domain=self.getSiteDomain())
            
            # Add additional headers
            headers = {
                'Accept': 'application/json',
                'X-Requested-With': 'XMLHttpRequest',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                'Referer': f'https://{self.getSiteDomain()}/reader/{work_id}'
            }
            
            # Now get the chapter content
            response = session.get(api_url, headers=headers)
            response.raise_for_status()
            json_data = response.json()
            
            if not json_data.get('isSuccessful'):
                logger.error('Server returned unsuccessful response for chapter')
                return ""
                
            # Get reader-secret from response headers
            reader_secret = None
            for header in response.headers:
                if header.lower() == 'reader-secret':
                    reader_secret = response.headers[header]
                    break
                    
            if not reader_secret:
                logger.error('Could not find reader-secret in response headers')
                return ""
                
            # Decrypt chapter content
            decrypted_text = self.decrypt_chapter_text(json_data['data']['text'], reader_secret)
            if not decrypted_text:
                return ""
                
            # Parse the decrypted HTML
            chapter_soup = self.make_soup(decrypted_text)
            
            # Remove any unwanted elements
            for div in chapter_soup.find_all('div', {'class': ['banner', 'adv']}):
                div.decompose()
                
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
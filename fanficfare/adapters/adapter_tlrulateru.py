#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright 2023 FanFicFare team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from __future__ import absolute_import
import logging
logger = logging.getLogger(__name__)
import re
from datetime import datetime
import urllib.parse
import urllib.request
import requests

from ..htmlcleanup import stripHTML
from .base_adapter import BaseSiteAdapter

def getClass():
    return TLRulateRuAdapter

class TLRulateRuAdapter(BaseSiteAdapter):

    def __init__(self, config, url):
        BaseSiteAdapter.__init__(self, config, url)
        
        self.story.setMetadata('siteabbrev','tlru')
        
        # get storyId from url
        # https://tl.rulate.ru/book/XXX
        self.story.setMetadata('storyId',self.parsedUrl.path.split('/')[2])
        
        # normalized story URL.
        self._setURL('https://' + self.getSiteDomain() + '/book/' + self.story.getMetadata('storyId'))

    @staticmethod
    def getSiteDomain():
        return 'tl.rulate.ru'

    @classmethod
    def getSiteExampleURLs(cls):
        return 'https://' + cls.getSiteDomain() + '/book/12345'

    def getSiteURLPattern(self):
        return r'https?://' + re.escape(self.getSiteDomain()) + r'/book/\d+/?$'

    def extractChapterUrlsAndMetadata(self):
        # Проверяем авторизацию перед извлечением данных
        if not self.is_logged_in():
            if not self.login():
                raise Exception("Failed to login to tl.rulate.ru")
                
        url = self.url
        logger.debug("URL: "+url)

        data = self.get_request(url)
        soup = self.make_soup(data)

        # Выводим HTML для отладки
        print("Initial HTML content:")
        print(soup.prettify())

        # Проверяем наличие формы подтверждения возраста
        age_form = soup.find('div', class_='errorpage')
        if age_form and "старше 18 лет" in age_form.get_text():
            print("Found age verification page, submitting confirmation...")
            
            # Находим форму и её элементы
            form = age_form.find('form')
            if form:
                # Получаем данные формы
                data = {
                    'path': form.find('input', {'name': 'path'})['value'],
                    'ok': 'Да'
                }
                
                # Отправляем POST-запрос на адрес формы
                response = self.post_request('https://tl.rulate.ru/mature', data)
                
                # После подтверждения возраста делаем новый запрос к странице книги
                response = self.get_request(self.url)
                soup = self.make_soup(response)
            
        # Теперь продолжаем поиск заголовка на странице
        title = soup.find('h1')
        if not title:
            print("Title not found!")
            raise Exception('Story title not found!')
            
        self.story.setMetadata('title', title.get_text().strip())

        # Extract cover
        cover_images = soup.select(".images img")
        if not cover_images:
            cover_images = soup.select(".book-thumbnail img")
            
        for i, cover_img in enumerate(cover_images):
            cover_url = cover_img.get('data-src') or cover_img.get('src')
            if not cover_url:
                continue
                
            if not cover_url.startswith('http'):
                if cover_url.startswith('//'):
                    cover_url = 'https:' + cover_url
                else:
                    cover_url = 'https://' + self.getSiteDomain() + ('/' if not cover_url.startswith('/') else '') + cover_url
            
            # Download and save cover
            try:
                if i == 0:
                    self.setCoverImage(url, cover_url)
                else:
                    self.story.addImgUrl(url, cover_url, self.get_request_raw)
            except Exception as e:
                logger.warning(f"Failed to get cover {i+1}: {str(e)}")

        # Extract author
        author = soup.find('strong', text=re.compile(r'Автор:')).find_next('em').get_text().strip() if soup.find('strong', text=re.compile(r'Автор:')) else None
        
        if author:
            self.story.addToList('author', author)
            self.story.addToList('authorId', author)  # Using author name as ID since we don't have specific IDs
            self.story.setMetadata('authorUrl', 'https://' + self.getSiteDomain() + '/search?t=' + author)
        else:
            # Try to find owner in translation panel
            owner = soup.select_one(".tools>dl.info>dd>a.user[href^='/users/']")
            if owner:
                prev_text = owner.previous_sibling.get_text().strip().lower()
                if prev_text.endswith("владелец:"):
                    author = owner.get_text().strip()
                    author_id = owner['href'].split('/')[-1]
                    self.story.addToList('author', author)
                    self.story.addToList('authorId', author_id)
                    self.story.setMetadata('authorUrl', 'https://' + self.getSiteDomain() + owner['href'])
            else:
                self.story.addToList('author', 'Unknown')
                self.story.addToList('authorId', 'unknown')
                self.story.setMetadata('authorUrl', '')

        # Extract description
        description = soup.find('div', class_='btn-toolbar').find_next_sibling('div')
        if description:
            self.setDescription(url, description)

        # Extract status
        status_text = soup.find('strong', text=re.compile(r'Выпуск:')).find_next('em').get_text().strip() if soup.find('strong', text=re.compile(r'Выпуск:')) else None
        if status_text:
            self.story.setMetadata('status', 'In-Progress' if 'продолжается' in status_text.lower() else 'Completed')

        # Extract rating
        rating_div = soup.find('div', text=re.compile(r'Произведение:'))
        if rating_div and rating_div.find_next_sibling('div'):
            rating = rating_div.find_next_sibling('div').get_text().strip().split('/')[0].strip()
            if rating:
                self.story.setMetadata('rating', rating)

        # Extract genres and tags
        print("Extracting genres...")
        genres = soup.select('#Info > div.row > div.span5 > p:nth-child(12) a.badge')
        if genres:
            for genre in genres:
                if 'genres' in genre['href']:
                    genre_text = genre.get_text().strip()
                    print(f"Found genre: {genre_text}")
                    self.story.addToList('genre', genre_text)

        print("Extracting tags...")
        tags = soup.select('#Info > div.row > div.span5 > p:nth-child(13) a.badge')
        if tags:
            for tag in tags:
                if 'tags' in tag['href']:
                    tag_text = tag.get_text().strip()
                    print(f"Found tag: {tag_text}")
                    self.story.addToList('category', tag_text)

        # Get chapter list
        chapters = []
        for row in soup.select("#Chapters .chapter_row"):
            # Проверяем наличие кнопки "читать"
            read_btn = row.select_one("td>a.btn")
            if not read_btn or read_btn.get_text().strip() != "читать":
                continue
                
            title_el = row.select_one("td.t a")
            if title_el:
                chapter_url = title_el['href']
                if not chapter_url.startswith('http'):
                    chapter_url = 'https://' + self.getSiteDomain() + chapter_url
                chapter_title = title_el.get_text().strip()
                chapters.append((chapter_title, chapter_url))

        # Sort chapters
        if soup.select_one("input[name=C_sortChapters][value='0']"):
            chapters.reverse()

        for title, url in chapters:
            self.add_chapter(title, url)

    def getChapterText(self, url):
        logger.debug('Getting chapter text from: %s' % url)
        
        data = self.get_request(url)
        soup = self.make_soup(data)
        
        # Проверяем различные селекторы для поиска контента
        chapter = soup.select_one("#text-container .content-text") or \
                 soup.select_one(".content-text") or \
                 soup.select_one("#text-container") or \
                 soup.select_one(".text-container") or \
                 soup.select_one(".text-content-group") or \
                 soup.select_one("#text") or \
                 soup.select_one(".chapter-text") or \
                 soup.select_one(".text")
                 
        if not chapter:
            # Проверяем различные случаи отсутствия контента
            if soup.find('p', text=re.compile("В этой главе нет ни одного переведённого фрагмента")):
                return "Глава не переведена"
            elif soup.find('div', text=re.compile("Глава не найдена")):
                return "Глава не найдена"
            elif soup.find(text=re.compile("Глава платная")):
                return "Глава платная"
            else:
                # Если не нашли контент по селекторам, попробуем найти любой текст
                text_content = soup.find('div', class_=lambda x: x and ('text' in x.lower() or 'content' in x.lower()))
                if text_content:
                    chapter = text_content
                else:
                    raise Exception('Chapter content not found!')
        
        # Remove link at the end of chapter if present
        last_p = chapter.find_all('p')[-1] if chapter.find_all('p') else None
        if last_p and self.getSiteDomain() in last_p.get_text():
            last_p.decompose()
            
        # Remove advertisement blocks
        for div in chapter.select("div.thumbnail"):
            div.decompose()
            
        # Process images
        for img in chapter.find_all('img'):
            # Get image URL from data-src or src
            img_url = img.get('data-src') or img.get('src', '')
            if not img_url:
                continue
                
            # Если это XPath, пропускаем
            if img_url.startswith('/html/'):
                continue
                
            # Если URL относительный и не начинается с http или //, добавляем домен
            if not (img_url.startswith('http') or img_url.startswith('//')):
                img_url = 'https://' + self.getSiteDomain() + ('/' if not img_url.startswith('/') else '') + img_url
            
            # Если URL начинается с //, добавляем https:
            if img_url.startswith('//'):
                img_url = 'https:' + img_url
            
            # Сохраняем alt текст из title если нет alt
            if not img.get('alt') and img.get('title'):
                img['alt'] = img['title']
                
            try:
                # Загружаем изображение
                if not img_url.startswith('/html/'):  # Пропускаем XPath ссылки
                    self.story.addImgUrl(url, img_url, self.get_request_raw)
            except Exception as e:
                logger.warning(f"Failed to process image {img_url}: {e}")
                img['src'] = img_url
            
        return self.utf8FromSoup(url, chapter) 

    def login(self):
        """Login to tl.rulate.ru"""
        print("Logging in to tl.rulate.ru...")
        
        # Получаем страницу с формой логина
        soup = self.make_soup(self.get_request(self.url))
        
        # Находим форму логина
        login_form = soup.select_one('#header-login form')
        if not login_form:
            print("Login form not found!")
            return False
            
        # Получаем action URL из формы
        form_action = login_form.get('action', '')
        if not form_action:
            print("Form action not found!")
            return False
            
        # Получаем поля формы
        username_field = login_form.select_one('input:nth-child(1)')
        password_field = login_form.select_one('input:nth-child(2)')
        submit_button = login_form.select_one('input.btn')
        
        if not all([username_field, password_field, submit_button]):
            print("Login form fields not found!")
            return False
            
        # Формируем данные для POST-запроса
        login_data = {
            username_field.get('name', 'username'): self.getConfig('username'),
            password_field.get('name', 'password'): self.getConfig('password'),
            submit_button.get('name', 'submit'): submit_button.get('value', 'Войти')
        }
        
        # Отправляем POST-запрос для логина с URL из формы
        response = self.post_request('https://tl.rulate.ru' + form_action, login_data)
        
        # Проверяем успешность логина
        soup = self.make_soup(response)
        if soup.select_one('#header-login'):
            print("Login failed!")
            return False
            
        print("Login successful!")
        return True
        
    def is_logged_in(self):
        """Check if we're logged in"""
        soup = self.make_soup(self.get_request(self.url))
        return not bool(soup.select_one('#header-login'))
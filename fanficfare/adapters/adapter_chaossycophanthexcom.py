# -*- coding: utf-8 -*-

# Copyright 2011 Fanficdownloader team, 2018 FanFicFare team
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

# Software: eFiction
from __future__ import absolute_import
import logging
logger = logging.getLogger(__name__)
import re
from ..htmlcleanup import stripHTML
from .. import exceptions as exceptions

# py2 vs py3 transition
from ..six import text_type as unicode

from .base_adapter import BaseSiteAdapter,  makeDate

def getClass():
    return ChaosSycophantHexComAdapter

# Class name has to be unique.  Our convention is camel case the
# sitename with Adapter at the end.  www is skipped.
class ChaosSycophantHexComAdapter(BaseSiteAdapter):

    def __init__(self, config, url):
        BaseSiteAdapter.__init__(self, config, url)

        self.username = "NoneGiven" # if left empty, site doesn't return any message at all.
        self.password = ""
        self.is_adult=False

        # get storyId from url--url validation guarantees query is only sid=1234
        self.story.setMetadata('storyId',self.parsedUrl.query.split('=',)[1])



        # normalized story URL.
        self._setURL('http://' + self.getSiteDomain() + '/viewstory.php?sid='+self.story.getMetadata('storyId'))

        # Each adapter needs to have a unique site abbreviation.
        self.story.setMetadata('siteabbrev','csph')

        # The date format will vary from site to site.
        # http://docs.python.org/library/datetime.html#strftime-strptime-behavior
        self.dateformat = "%m/%d/%Y"

    @staticmethod # must be @staticmethod, don't remove it.
    def getSiteDomain():
        # The site domain.  Does have www here, if it uses it.
        return 'chaos.sycophanthex.com'

    @classmethod
    def getSiteExampleURLs(cls):
        return "http://"+cls.getSiteDomain()+"/viewstory.php?sid=1234"

    def getSiteURLPattern(self):
        return re.escape("http://"+self.getSiteDomain()+"/viewstory.php?sid=")+r"\d+$"

    ## Getting the chapter list and the meta data, plus 'is adult' checking.
    def extractChapterUrlsAndMetadata(self):

        if self.is_adult or self.getConfig("is_adult"):
            # Weirdly, different sites use different warning numbers.
            # If the title search below fails, there's a good chance
            # you need a different number.  print data at that point
            # and see what the 'click here to continue' url says.
            addurl = "&ageconsent=ok&warning=19"
        else:
            addurl=""

        # index=1 makes sure we see the story chapter index.  Some
        # sites skip that for one-chapter stories.
        url = self.url+'&index=1'+addurl
        logger.debug("URL: "+url)

        data = self.get_request(url)

        # The actual text that is used to announce you need to be an
        # adult varies from site to site.  Again, print data before
        # the title search to troubleshoot.
        if "Age Consent Required" in data:
            raise exceptions.AdultCheckRequired(self.url)

        if "Access denied. This story has not been validated by the adminstrators of this site." in data:
            raise exceptions.AccessDenied(self.getSiteDomain() +" says: Access denied. This story has not been validated by the adminstrators of this site.")

        soup = self.make_soup(data)
        # print data


        ## Title
        pt = soup.find('div', {'id' : 'pagetitle'})
        a = pt.find('a', href=re.compile(r'viewstory.php\?sid='+self.story.getMetadata('storyId')+"$"))
        self.story.setMetadata('title',stripHTML(a))

        # Find authorid and URL from... author url.
        a = pt.find('a', href=re.compile(r"viewuser.php\?uid=\d+"))
        self.story.setMetadata('authorId',a['href'].split('=')[1])
        self.story.setMetadata('authorUrl','http://'+self.host+'/'+a['href'])
        self.story.setMetadata('author',a.string)

        rating=pt.text.split('(')[1].split(')')[0]
        self.story.setMetadata('rating', rating)

        # Find the chapters:
        for chapter in soup.find_all('a', href=re.compile(r'viewstory.php\?sid='+self.story.getMetadata('storyId')+r"&chapter=\d+$")):
            # just in case there's tags, like <i> in chapter titles.
            self.add_chapter(chapter,'http://'+self.host+'/'+chapter['href']+addurl)


        # eFiction sites don't help us out a lot with their meta data
        # formating, so it's a little ugly.

        # utility method
        def defaultGetattr(d,k):
            try:
                return d[k]
            except:
                return ""


        # <span class="label">Rated:</span> NC-17<br /> etc

        labels = soup.find_all('span',{'class':'label'})

        value = labels[0].previousSibling
        svalue = ""
        while value != None:
            val = value
            value = value.previousSibling
        while 'label' not in defaultGetattr(val,'class'):
            svalue += unicode(val)
            val = val.nextSibling
        self.setDescription(url,svalue)

        for labelspan in labels:
            value = labelspan.nextSibling
            label = labelspan.string

            if 'Word count' in label:
                self.story.setMetadata('numWords', value.split(' -')[0])

            if 'Categories' in label:
                cats = labelspan.parent.find_all('a',href=re.compile(r'browse.php\?type=categories'))
                for cat in cats:
                    self.story.addToList('category',cat.string)

            if 'Characters' in label:
                chars = labelspan.parent.find_all('a',href=re.compile(r'browse.php\?type=characters'))
                for char in chars:
                    self.story.addToList('characters',char.string)

            if 'Genre' in label:
                genres = labelspan.parent.find_all('a',href=re.compile(r'browse.php\?type=class&type_id=1'))
                for genre in genres:
                    self.story.addToList('genre',genre.string)

            if 'Warnings' in label:
                warnings = labelspan.parent.find_all('a',href=re.compile(r'browse.php\?type=class&type_id=2'))
                for warning in warnings:
                    self.story.addToList('warnings',warning.string)

            if 'Complete' in label:
                if 'Yes' in value:
                    self.story.setMetadata('status', 'Completed')
                else:
                    self.story.setMetadata('status', 'In-Progress')

            if 'Published' in label:
                self.story.setMetadata('datePublished', makeDate(stripHTML(value.split(' -')[0]), self.dateformat))

            if 'Updated' in label:
                # there's a stray [ at the end.
                #value = value[0:-1]
                self.story.setMetadata('dateUpdated', makeDate(stripHTML(value), self.dateformat))

        try:
            # Find Series name from series URL.
            a = soup.find('a', href=re.compile(r"viewseries.php\?seriesid=\d+"))
            series_name = a.string
            series_url = 'http://'+self.host+'/'+a['href']

            seriessoup = self.make_soup(self.get_request(series_url))
            storyas = seriessoup.find_all('a', href=re.compile(r'^viewstory.php\?sid=\d+$'))
            i=1
            for a in storyas:
                if a['href'] == ('viewstory.php?sid='+self.story.getMetadata('storyId')):
                    self.setSeries(series_name, i)
                    self.story.setMetadata('seriesUrl',series_url)
                    break
                i+=1

        except:
            # I find it hard to care if the series parsing fails
            pass

    # grab the text for an individual chapter.
    def getChapterText(self, url):

        logger.debug('Getting chapter text from: %s' % url)

        soup = self.make_soup(self.get_request(url))

        div = soup.find('div', {'id' : 'story'})

        if None == div:
            raise exceptions.FailedToDownload("Error downloading Chapter: %s!  Missing required element!" % url)

        return self.utf8FromSoup(url,div)

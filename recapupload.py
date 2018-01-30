# Upload a PDF to the RECAP Archive.
#
# John Hawkinson <jhawk@mit.edu>
# 29 December 2017
#
# This is kind of rough, because:
#
# * RECAP/CourtListener (CL) wants PACER metadata for each PDF BEFORE
#   it receives the PDF
# * BigCases doesn't preserve the exact RSS XML so we can't just feed
#   it to RECAP
# * RECAP doesn't have an RSS XML-input endpoint, although it could
#   easily gain one
# * So before we can upload a PDF, we have to fake up the metadata in
#   the format that CL understands, and transmit it. But only if that
#   metadata isn't already there.
# * Because BigCases doesn't preserve the parsed data from the RSS
#   feed, we need to use feedparser's date parser to get it.

from HTMLParser import HTMLParser

import calendar
import feedparser
import os
import re
import requests
import time
import urlparse

from bigcases_settings import settings


API_BASE = 'https://www.courtlistener.com/api/rest/v3/'
TIMEOUTS = (60, 300)  # 1 minute connect timeout, 5 min. read timeout

TESTING = False	      # Server throws away content in "debug" mode
VERBOSE = 1


class _UploadType():
    DOCKET = 1
    ATTACHMENT_PAGE = 2
    PDF = 3
    DOCKET_HISTORY_REPORT = 4
    APPELLATE_DOCKET = 5
    APPELLATE_ATTACHMENT_PAGE = 6


class RecapUpload(object):
    """Upload a document to the RECAP archive."""

    def PACER_Court_to_CL(self, PACER_court):
        # An unfortunate design decision was made in the past.
        PACER_TO_CL_IDS = {
            'azb': 'arb',         # Arizona Bankruptcy Court
            'cofc': 'uscfc',      # Court of Federal Claims
            'neb': 'nebraskab',   # Nebraska Bankruptcy
            'nysb-mega': 'nysb',  # Remove the mega thing
        }

        return PACER_TO_CL_IDS.get(PACER_court, PACER_court)

    def __init__(self, filename,
                 docket_number, docket_caption,
                 published_date,
                 item_description):
        """Upload a document to the RECAP archive.

        We do based on what little information we have. This involves
        multiple roundtrips to the RECAP server, unfortunately.

        The parameters we get are all from RSS, as available to the
        BigCases scraper. Unfortunately that's not the full RSS XML,
        because it takes a trip through a SQL database.

        PARAMETER: SOURCE
          docket_number and docket_caption:      item.title
          published_date:                        item.published
          item_description:                      item.description

        Outline:
        1. We first parse the item_description to get the docket text,
           the item URL (including DLS aka "doc1" number), and the
           entry number (link anchor).
        2. We lookup the case, or determine we need to fake it.
        3. We lookup the docket entry, or determine we need to fake it.
        4. If necessary, we fake the docket entry and case title.
        5. We upload the PDF.
        """

        if not len(settings.recap_token) >= 40:
            # Not a valid token
            if VERBOSE > 0:
                print "Not a valid authentication token."
            return None

        # #1. We first parse the item_description to get the docket text,

        h = HTMLParser()
        itemDecoded = h.unescape(item_description)
# BEFORE unescape() call:
# [Motion For Order] (<a href="https://ecf.dcd.uscourts.gov/
# doc1/04506366063?caseid=190182&amp;de_seq_num=369">94</a>)
# AFTER unescape() call:
# [Motion For Order] (<a href="https://ecf.dcd.uscourts.gov/
# doc1/04506366063?caseid=190182&de_seq_num=369">94</a>)

# NOTE that Appellate takes this form:
# [Order Filed (CLERK)] (<a href='https://ecf.cadc.uscourts.gov/
# docs1/01207988480'>Document</a>)

        match = re.search(r'''(?xs)
            \[(?P<text>.*)\]
            .*?
            <a\ href=(?P<q1>["'])(?P<url>.*?)(?P=q1)\s*>
            (?P<anchor>.*?)
            </a>
        ''', itemDecoded)
        if match is None:
            if VERBOSE > 0:
                print "Failed to parse item description."
            return None

        text = match.group('text')
        url = match.group('url')
        entry_number = match.group('anchor')

        match = re.search(r'http.*/docs?1/(?P<dls>\d+)\?', url)
        pacer_doc_id = match.group('dls')
        if pacer_doc_id[3:4] == '1':
            # PACER sites use the fourth digit of the pacer_doc_id to
            # flag whether the user has been shown a receipt page.  We
            # don't care about that, so we always set the fourth digit
            # to 0 when getting a doc ID.
            pacer_doc_id = pacer_doc_id[0:3] + '0' + pacer_doc_id[4:]

        parsed_url = urlparse.urlparse(url)
        court = self.PACER_Court_to_CL(parsed_url.hostname.split('.')[1])
        qparams = urlparse.parse_qs(parsed_url.query)

        pacer_case_id = qparams['caseid'][0]

        # #2. We lookup the case, or determine we need to fake it.

        need_fake_case = False
        need_fake_entry = False
        if VERBOSE > 0:
            print "Checking RECAP for case %s in %s" % (pacer_case_id, court)
        # /dockets/?pacer_case_id=189311&court=mad
        r = requests.get(
            url=API_BASE+'dockets/',
            headers={'Authorization': 'Token %s' % settings.recap_token},
            params={'court': court,
                    'pacer_case_id': pacer_case_id},
            timeout=TIMEOUTS,
        )
        if VERBOSE >= 2:
            print "Returns:", r, r.text
        rj = r.json()

        if (not r.ok) or rj['count'] < 1:
            if VERBOSE > 0:
                print "RECAP docket doesn't exist. We need to fake one up."
            need_fake_case = True
        else:
            # The CL docket ID, needed for the next query, is here:
            # "resource_uri":
            # "https://www.courtlistener.com/api/rest/v3/dockets/6125037/",
            cl_docket_id = re.search(r'/dockets/(\d+)/',
                                     rj['results'][0]['resource_uri']).group(1)

        # #3. We lookup the docket entry, or determine we need to fake it.
        if not need_fake_case:
            # If we don't have a case, we need not check for a docket entry
            if VERBOSE > 0:
                print "Checking RECAP for entry %s in CL case %s" \
                    % (entry_number, cl_docket_id)
            # Originally we had:
            #   /recap-documents/?pacer_case_id=189311&court=mad
            #     &pacer_doc_id=09508417779
            # Or simply
            #   /recap-documents/?pacer_doc_id=09508417779
            # But alternatively
            #   /docket-entries/?recap_documents__pacer_doc_id=09508346951
            # But now Mike's preference:
            #   /docket-entries/?docket_id=6249495&entry_number=13
            # Because in some corner cases there is a Docket Entry
            # object without a RECAP docket (...)
            r = requests.get(
                url=API_BASE+'docket-entries/',
                headers={'Authorization': 'Token %s' % settings.recap_token},
                params={
                    'docket': cl_docket_id,
                    'entry_number': entry_number,
                },
                timeout=TIMEOUTS,
            )
            if VERBOSE >= 2:
                print "Returns: ", r
            rj = r.json()
            # Control debugging spew.
            # plain_text is a very large amount of text.
            try:
                plainText = \
                    rj['results'][0]['recap_documents'][0]['plain_text']
                if len(plainText) > 80:
                    rj['results'][0]['recap_documents'][0]['plain_text'] = \
                        plainText.strip()[:40] + '...'
            except IndexError:
                pass
            if VERBOSE >= 2:
                print rj

            if (not r.ok) or rj['count'] < 1:
                if VERBOSE > 0:
                    print "The docket ENTRY doesn't exist. " + \
                        "We need to fake one up."
                need_fake_entry = True

        # #4. If necessary, we fake the docket entry and case title.
        if need_fake_case or need_fake_entry:
            # Either because of the case docket or the docket entry's absence
            # we must fake up a docket.

            # Unfortunately, feedparser returns a UTC tuple, and we
            # want to submit this in the Court's local time, which
            # guess at. So we convert back to epoch form and call
            # localtime. Worse, we can only control the local zone by
            # frobbing the environment.  Fortunately, we only care
            # about the date, not the time, so the window where this
            # matters is 3 hours for the continental US, although a
            # bit further from Guam to USVI.  We choose US/Pacific as
            # our zone because it is Less Wrong, and because filing at
            # 10pm Pacific is more likely than 1am Eastern.

            gmtime = feedparser._parse_date(published_date)
            oldtz = os.environ.get('TZ')
            os.environ['TZ'] = 'US/Pacific'
            time.tzset()
            localtime = time.localtime(calendar.timegm(gmtime))
            date = time.strftime('%m/%d/%Y', localtime)
            if oldtz:
                os.environ['TZ'] = oldtz
            else:
                del os.environ['TZ']
            time.tzset()

            html = ''
            html += '<h3>THIS IS A FAKED UP DOCKET VIA RSS THROUGH '
            html += 'recapupload.py<br>\n'
            html += '%s</h3>\n' % docket_number
            html += '<table>'
            if need_fake_case:
                html += '<td><br>%s' % docket_caption
            # Lack of whitespace after this colon matters!
            html += '<td>Date Filed:</table>\n'
            html += '<table>'
            html += '<tr>'
            html += '<td>Date Filed</td><th>#</th><td>Docket Text</td></tr>\n'
            html += '<tr>'
            html += '<td>%s</td>\n' % date
            html += '<td><a href="%s">%s</td>\n' % (url, entry_number)
            html += '<td>%s</td>' % text
            html += '</tr>\n</tbody></table>\n'

            if VERBOSE >= 2:
                print "We faked up this: \n", html

            files = {'filepath_local': ('filepath_local', html, 'text/html')}
            r = requests.post(
                url=API_BASE+'recap/',
                headers={'Authorization': 'Token %s' % settings.recap_token},
                data={
                    'upload_type': _UploadType.DOCKET,
                    'court': court,
                    'pacer_case_id': pacer_case_id,
                    'debug': TESTING,  # Server throws away in debug mode
                },
                files=files,
                timeout=TIMEOUTS,
            )
            if VERBOSE >= 2:
                print "Returns: ", r, r.text
            if (not r.ok):
                if VERBOSE > 0:
                    print "Failed to upload faked docket entry info to RECAP."
                return None

        # #5. We upload the PDF.
        files = {'filepath_local': open(filename, 'rb')}
        r = requests.post(
            url=API_BASE+'recap/',
            headers={'Authorization': 'Token %s' % settings.recap_token},
            data={
                'upload_type': _UploadType.PDF,
                'court': court,
                'pacer_case_id': pacer_case_id,
                'pacer_doc_id': pacer_doc_id,
                'document_number': entry_number,
                'debug': TESTING,  # Server throws away in debug mode
            },
            files=files,
            timeout=TIMEOUTS,
        )
        if VERBOSE > 0:
            print "RECAP upload returns: ", r, r.text

        return None

# -*- coding: utf-8 -*-
from BeautifulSoup import BeautifulSoup
from email.Header import Header
from email.MIMEImage import MIMEImage
from htmllib import HTMLParser
from plone import api
from plone.namedfile.scaling import ImageScale
from Products.Archetypes.public import ObjectField
from Products.CMFPlone.utils import safe_unicode
from Products.EasyNewsletter.config import PLACEHOLDERS
from Products.EasyNewsletter.interfaces import IIssueDataFetcher
from Products.EasyNewsletter.utils import safe_portal_encoding
from Products.EasyNewsletter.utils.ENLHTMLParser import ENLHTMLParser
from Products.PageTemplates.ZopePageTemplate import ZopePageTemplate
from stoneagehtml import compactify
from urlparse import urlparse
from zope.interface import implementer
import cStringIO
import formatter
import logging
import urllib

log = logging.getLogger("Products.EasyNewsletter")


@implementer(IIssueDataFetcher)
class DefaultIssueDataFetcher(object):

    def __init__(self, issue):
        self.issue = issue

    def __call__(self, receiver):
        """
        returns a dict of issue_data, like subject and several parts of
        the issue. This is done so, to split up the send method and
        make it more hookable.
        """
        data = {}

        request = self.issue.REQUEST
        subject = request.get("subject")
        if not subject:
            subject = self.issue.Title()

        data['subject_header'] = Header(safe_unicode(subject))

        output_html = self._render_output_html()
        # This will resolve 'resolveuid' links for us
        rendered_newsletter = self._exchange_relative_urls(output_html)

        data['body_html'] = rendered_newsletter['html']
        data['body_plain'] = rendered_newsletter['plain']

        image_urls = rendered_newsletter['images']
        data['images_to_attach'] = self._get_images_to_attach(image_urls)

        # personalize the old way
        # deprecated.
        data['body_html'], data['body_plain'] = self._personalize_texts(
            receiver,
            data['body_html'],
            data['body_plain']
        )
        return data

    def preview_html(self):
        html = self._render_output_html()
        for placeholder in PLACEHOLDERS:
            html = html.replace('[[' + placeholder + ']]', '')
        soup = BeautifulSoup(html)
        for node in soup.findAll('div', {'class': 'mailonly'}):
            node.extract()
        return soup.renderContents()

    @property
    def enl(self):
        if not hasattr(self, '_enl'):
            self._enl = self.issue.getNewsletter()
        return self._enl

    def _fullname(self, receiver):
        fullname = receiver.get('fullname')
        if not fullname:
            try:
                return self.enl.getFullname_fallback()
            except AttributeError:
                return "Sir or Madam"
        return fullname

    def _salutation(self, receiver):
        return receiver.get("salutation") or ''

    def _render_output_html(self):
        """ Return rendered newsletter
            with header+body+footer (raw html).
        """
        # get out_template from ENL object and render it in context of issue
        out_template_pt_field = self.enl.getField('out_template_pt')
        ObjectField.set(
            out_template_pt_field,
            self.issue,
            ZopePageTemplate(
                out_template_pt_field.getName(),
                self.enl.getRawOut_template_pt()
            )
        )
        output_html = safe_portal_encoding(
            self.issue.out_template_pt.pt_render()
        )
        output_html = compactify(output_html, filter_tags=False)
        return output_html

    def _personalize_texts(self, receiver, text, text_plain):
        subscriber_salutation = safe_portal_encoding(
            '{0} {1}'.format(
                self._salutation(receiver),
                safe_portal_encoding(self._fullname(receiver))
            )
        )
        text = text.replace(
            "[[SUBSCRIBER_SALUTATION]]", str(subscriber_salutation))
        text_plain = text_plain.replace(
            "[[SUBSCRIBER_SALUTATION]]", str(subscriber_salutation))

        # we can only build unsubscribe links with subscriber uid
        if 'uid' in receiver:
            try:
                unsubscribe_text = self.enl.getUnsubscribe_string()
            except AttributeError:
                unsubscribe_text = "Click here to unsubscribe"
            unsubscribe_link = self.enl.absolute_url() + \
                "/unsubscribe?subscriber=" + receiver['uid']

            text = text.replace(
                "[[UNSUBSCRIBE]]", """<a href="%s">%s.</a>""" % (
                    unsubscribe_link, unsubscribe_text))

            text_plain = text_plain.replace(
                "[[UNSUBSCRIBE]]", """\n%s: %s""" % (
                    unsubscribe_text, unsubscribe_link))
        else:
            text = text.replace("[[UNSUBSCRIBE]]", "")
            text_plain = text_plain.replace(
                "[[UNSUBSCRIBE]]", "")

        return text, text_plain

    def _exchange_relative_urls(self, output_html):
        """ exchange relative URLs and
            return dict with html, plain and images
        """
        parser_output_zpt = ENLHTMLParser(self.issue)
        parser_output_zpt.feed(output_html)
        text = parser_output_zpt.html
        text_plain = self._create_plaintext_message(text)
        image_urls = parser_output_zpt.image_urls
        return dict(html=text, plain=text_plain, images=image_urls)

    def _get_images_to_attach(self, image_urls):  # noqa
        # this should really be refactored!
        image_number = 0
        images_to_attach = []
        reference_tool = api.portal.get_tool('reference_catalog')

        for image_url in image_urls:
            try:
                image_url = urlparse(image_url)[2]
                o = None
                if 'resolveuid' in image_url:
                    urlparts = image_url.split('resolveuid/')[1:][0]
                    urlparts = urlparts.split('/')
                    uuid = urlparts.pop(0)
                    o = reference_tool.lookupObject(uuid)
                    if o and urlparts:
                        # get thumb
                        o = o.restrictedTraverse(urlparts[0])
                        image_url = '/'.join(urlparts)
                if "@@images" in image_url:
                    # HACK to get around restrictedTraverse not honoring
                    # ITraversable see
                    # http://developer.plone.org/serving/traversing.html\
                    # traversing-by-full-path
                    image_url_base, image_scale_params = image_url.split(
                        "@@images/")
                    if o is not None:
                        scales = o
                    else:
                        scales = self.issue.restrictedTraverse(
                            urllib.unquote(
                                image_url_base.strip('/') + '/@@images'
                            )
                        )
                    parts = list(reversed(image_scale_params.split("/")))
                    name = parts.pop()
                    dummy_request = dict(TraversalRequestNameStack=parts)
                    o = scales.publishTraverse(dummy_request, name)
                if o is None:
                    o = self.issue.restrictedTraverse(
                        urllib.unquote(image_url)
                    )
            except Exception, e:
                log.error("Could not resolve the image \"%s\": %s" % (
                    image_url, e))
            else:
                if hasattr(o, "_data"):  # file-based
                    image = MIMEImage(o._data)
                elif hasattr(o, "data"):
                    if isinstance(o, ImageScale):
                        image = MIMEImage(o.data.data)  # zodb-based dx image
                    else:
                        image = MIMEImage(o.data)  # zodb-based
                elif hasattr(o, "GET"):
                    image = MIMEImage(o.GET())  # z3 resource image
                else:
                    log.error(
                        "Could not get the image data from image object!")
                    image = None
                if image is not None:
                    image["Content-ID"] = "<image_%s>" % image_number
                    # attach images only to html parts
                images_to_attach.append(image)
            # Numbers have to match what we replaced in html
            image_number += 1
        return images_to_attach

    def _create_plaintext_message(self, text):
        """ Create a plain-text-message by parsing the html
            and attaching links as endnotes
        """
        plain_text_maxcols = 72
        textout = cStringIO.StringIO()
        formtext = formatter.AbstractFormatter(
            formatter.DumbWriter(textout, plain_text_maxcols)
        )
        parser = HTMLParser(formtext)
        parser.feed(text)
        parser.close()

        # append the anchorlist at the bottom of a message
        # to keep the message readable.
        anchorlist = "\n\n" + ("-" * plain_text_maxcols) + "\n\n"
        for counter, item in enumerate(parser.anchorlist):
            anchorlist += "[%d] %s\n" % (counter, item)

        text = textout.getvalue() + anchorlist
        del textout, formtext, parser, anchorlist
        return text

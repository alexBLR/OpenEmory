import hashlib
import json
import logging
import os

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.urlresolvers import reverse
from django.http import HttpResponse, HttpRequest
from django.test import TestCase
from django.contrib.auth.models import User, AnonymousUser

from eulfedora.server import Repository
from eulfedora.util import parse_rdf, RequestFailed
from eullocal.django.emory_ldap.backends import EmoryLDAPBackend

from mock import Mock, patch, MagicMock
from rdflib.graph import Graph as RdfGraph, Literal, RDF, URIRef
from sunburnt import sunburnt
from taggit.models import Tag

from openemory.accounts.auth import permission_required, login_required
from openemory.accounts.backends import FacultyOrLocalAdminBackend
from openemory.accounts.forms import ProfileForm
from openemory.accounts.models import researchers_by_interest, Bookmark, \
     pids_by_tag, articles_by_tag, UserProfile, EsdPerson, Degree, \
     Position, Grant
from openemory.accounts.templatetags.tags import tags_for_user
from openemory.publication.models import Article
from openemory.publication.views import ARTICLE_VIEW_FIELDS
from openemory.rdfns import DC, FRBR, FOAF

# re-use pdf fixture from publication app
pdf_filename = os.path.join(settings.BASE_DIR, 'publication', 'fixtures', 'test.pdf')
pdf_md5sum = '331e8397807e65be4f838ccd95787880'

logger = logging.getLogger(__name__)


# credentials for test accounts in json fixture
USER_CREDENTIALS = {
    'faculty': {'username': 'faculty', 'password': 'GPnFswH9X8'},
    'student': {'username': 'student', 'password': '2Zvi4dE3fJ'},
    'super': {'username': 'super', 'password': 'awXM6jnwJj'}, 
    'admin': {'username': 'siteadmin', 'password': '8SLEYvF4Tc'},
    'jolson': {'username': 'jolson', 'password': 'qw6gsrNBWX'},
}

def simple_view(request):
    "a simple view for testing custom auth decorators"
    return HttpResponse("Hello, World")

class BasePermissionTestCase(TestCase):
    '''Common setup/teardown functionality for permission_required and
    login_required tests.
    '''
    fixtures =  ['users']

    def setUp(self):
        self.request = HttpRequest()
        self.request.user = AnonymousUser()

        self.ajax_request = HttpRequest()
        self.ajax_request.META['HTTP_X_REQUESTED_WITH'] = 'XMLHttpRequest'
        self.ajax_request.user = AnonymousUser()
        
        self.faculty_user = User.objects.get(username='faculty')
        self.super_user = User.objects.get(username='super')


class PermissionRequiredTest(BasePermissionTestCase):

    def setUp(self):
        super(PermissionRequiredTest, self).setUp()
        decorator = permission_required('foo.can_edit')
        # decorate simple view above for testing
        self.decorated = decorator(simple_view)

    def test_wraps(self):
        self.assertEqual(simple_view.__doc__, self.decorated.__doc__)
        
    def test_anonymous(self):        
        response = self.decorated(self.request)
        expected, got = 401, response.status_code
        self.assertEqual(expected, got,
                "expected status code %s but got %s for decorated view with non-logged in user" \
                % (expected, got))

        # ajax request
        response = self.decorated(self.ajax_request)
        expected, got = 401, response.status_code
        self.assertEqual(expected, got,
                "expected status code %s but got %s for anonymous ajax request" \
                % (expected, got))
        expected, got = 'text/plain', response['Content-Type']
        self.assertEqual(expected, got,
                "expected content type %s but got %s for anonymous ajax request" \
                % (expected, got))
        expected, got = 'Not Authorized', response.content
        self.assertEqual(expected, got,
                "expected response content %s but got %s for anonymous ajax request" \
                % (expected, got))

    def test_logged_in_notallowed(self):
        # set request to use faculty user
        self.request.user = self.faculty_user
        response = self.decorated(self.request)
        
        expected, got = 403, response.status_code
        self.assertEqual(expected, got,
                "expected status code %s but got %s for decorated view with logged-in user without perms" \
                % (expected, got))
        self.assert_("Permission Denied" in response.content,
                "response should contain content from 403.html template fixture")

        # ajax request
        self.ajax_request.user = self.faculty_user
        response = self.decorated(self.ajax_request)
        expected, got = 403, response.status_code
        self.assertEqual(expected, got,
                "expected status code %s but got %s for ajax request by logged in user without perms" \
                % (expected, got))
        expected, got = 'text/plain', response['Content-Type']
        self.assertEqual(expected, got,
                "expected content type %s but got %s for ajax request by logged in user without perms" \
                % (expected, got))
        expected, got = 'Permission Denied', response.content
        self.assertEqual(expected, got,
                "expected response content %s but got %s for ajax request by logged in user without perms" \
                % (expected, got))


    def test_logged_in_allowed(self):
        # set request to use superuser account
        self.request.user = self.super_user
        response = self.decorated(self.request)
        expected, got = 200, response.status_code
        self.assertEqual(expected, got,
                "expected status code %s but got %s for decorated view with superuser" \
                % (expected, got))
        self.assert_("Hello, World" in response.content,
                     "response should contain actual view content")

class LoginRequiredTest(BasePermissionTestCase):

    def setUp(self):
        super(LoginRequiredTest, self).setUp()
        decorator = login_required()
        # decorate simple view above for testing
        self.decorated = decorator(simple_view)

    def test_wraps(self):
        self.assertEqual(simple_view.__doc__, self.decorated.__doc__)
        
    def test_anonymous(self):        
        response = self.decorated(self.request)
        expected, got = 401, response.status_code
        self.assertEqual(expected, got,
                "expected status code %s but got %s for decorated view with non-logged in user" \
                % (expected, got))
        
    def test_logged_in(self):
        # set request to use faculty user
        self.request.user = self.faculty_user
        response = self.decorated(self.request)
        
        expected, got = 200, response.status_code
        self.assertEqual(expected, got,
                "expected status code %s but got %s for decorated view with superuser" \
                % (expected, got))
        self.assert_("Hello, World" in response.content,
                     "response should contain actual view content")


class AccountViewsTest(TestCase):
    multi_db = True
    fixtures =  ['users', 'esdpeople']

    def setUp(self):
        self.faculty_username = 'jolson'
        self.faculty_user = User.objects.get(username=self.faculty_username)
        self.faculty_esd = EsdPerson.objects.get(netid='JOLSON')

        self.other_faculty_username = 'mmouse'
        self.other_faculty_user = User.objects.get(username=self.other_faculty_username)
        self.other_faculty_esd = EsdPerson.objects.get(netid='MMOUSE')

        self.student_user = User.objects.get(username='student')
        self.repo = Repository(username=settings.FEDORA_TEST_USER,
                                     password=settings.FEDORA_TEST_PASSWORD)
        # create a test article object to use in tests
        with open(pdf_filename) as pdf:
            self.article = self.repo.get_object(type=Article)
            self.article.label = 'A very scholarly article'
            self.article.pdf.content = pdf
            self.article.pdf.checksum = pdf_md5sum
            self.article.pdf.checksum_type = 'MD5'
            self.article.save()
        
        self.pids = [self.article.pid]

    def tearDown(self):
        for pid in self.pids:
            try:
                self.repo.purge_object(pid)
            except RequestFailed:
                logger.warn('Failed to purge test object %s' % pid)

        super(AccountViewsTest, self).tearDown()


    mocksolr = Mock(sunburnt.SolrInterface)
    mocksolr.return_value = mocksolr
    # solr interface has a fluent interface where queries and filters
    # return another solr query object; simulate that as simply as possible
    mocksolr.query.return_value = mocksolr.query
    mocksolr.query.query.return_value = mocksolr.query
    mocksolr.query.filter.return_value = mocksolr.query
    mocksolr.query.paginate.return_value = mocksolr.query
    mocksolr.query.exclude.return_value = mocksolr.query
    mocksolr.query.sort_by.return_value = mocksolr.query
    mocksolr.query.field_limit.return_value = mocksolr.query

    @patch('openemory.util.sunburnt.SolrInterface', mocksolr)
    def test_profile(self):
        profile_url = reverse('accounts:profile', kwargs={'username': 'nonuser'})
        response = self.client.get(profile_url)
        expected, got = 404, response.status_code
        self.assertEqual(expected, got, 'Expected %s but got %s for %s (non-existent user)' % \
                         (expected, got, profile_url))
        # mock result object
        result =  [
            {'title': 'article one', 'created': 'today', 'state': 'A',
             'last_modified': 'today', 'pid': 'a:1',
             'owner': self.faculty_username, 'dsids': ['content'],
             'parsed_author': ['nonuser:A. Non User', ':N. External User']},
            {'title': 'article two', 'created': 'yesterday', 'state': 'A',
             'last_modified': 'today','pid': 'a:2',
             'owner': self.faculty_username, 'dsids': ['contentMetadata'],
             'pmcid': '123456', 'parsed_author':
               ['nonuser:A. Non User', 'mmouse:Minnie Mouse']},
        ]
        unpub_result = [
            {'title': 'upload.pdf', 'created': 'today', 'state': 'I',
             'last_modified': 'today', 'pid': 'a:3',
             'owner': self.faculty_username}
            ]

        profile_url = reverse('accounts:profile', kwargs={'username': 'student'})
        with patch('openemory.accounts.views.get_object_or_404') as mockgetobj:
            mockgetobj.return_value = self.student_user
            with patch.object(self.faculty_user, 'get_profile') as mock_getprofile:
                response = self.client.get(profile_url)
                self.assertEquals(404, response.status_code,
                                  'students should not have profiles')


        profile_url = reverse('accounts:profile',
                kwargs={'username': self.faculty_username})
        with patch('openemory.accounts.views.get_object_or_404') as mockgetobj:
            mockgetobj.return_value = self.faculty_user

            response = self.client.get(profile_url)
            # ESD data should be displayed (not suppressed)
            self.assertContains(response, self.faculty_esd.directory_name,
                msg_prefix="profile page should display user's directory name")
            self.assertContains(response, self.faculty_esd.title,
                msg_prefix='title from ESD should be displayed')
            self.assertContains(response, self.faculty_esd.department_name,
                msg_prefix='department from ESD should be displayed')
            self.assertContains(response, self.faculty_esd.email,
                msg_prefix='email from ESD should be displayed')
            self.assertContains(response, self.faculty_esd.phone,
                msg_prefix='phone from ESD should be displayed')
            self.assertContains(response, self.faculty_esd.fax,
                msg_prefix='fax from ESD should be displayed')
            self.assertContains(response, self.faculty_esd.ppid,
                msg_prefix='PPID from ESD should be displayed')

            # optional user-entered information
            # degrees - nothing should be shown
            self.assertNotContains(response, 'Degrees',
                msg_prefix='profile should not display degrees if none are entered')
            # bio
            self.assertNotContains(response, 'Biography',
                msg_prefix='profile should not display bio if none has been added')
            # positions
            self.assertNotContains(response, 'Positions',
                msg_prefix='profile should not display positions if none has been added')
            # grants
            self.assertNotContains(response, 'Grants',
                msg_prefix='profile should not display grants if none has been added')
            
            # add degrees, bio, positions, grants; then check
            faculty_profile = self.faculty_user.get_profile()
            ba_degree = Degree(name='BA', institution='Somewhere U', year=1876,
                               holder=faculty_profile)
            ba_degree.save()
            ma_degree = Degree(name='MA', institution='Elsewhere Institute',
                               holder=faculty_profile)
            ma_degree.save()
            faculty_profile.biography = 'did some **stuff**'
            faculty_profile.save()
            position = Position(name='Director of Stuff, Association of Smart People',
                                holder=faculty_profile)
            position.save()
            gouda_grant = Grant(name='Gouda research', grantor='The Gouda Group',
                                project_title='Effects of low-gravity environments on gouda aging',
                                year=1616, grantee=faculty_profile)
            gouda_grant.save()
            queso_grant = Grant(grantor='Mexican-American food research council',
                                project_title='Cheese dip and subject happiness',
                                grantee=faculty_profile)
            queso_grant.save()

            response = self.client.get(profile_url)
            self.assertContains(response, 'Degrees',
                msg_prefix='profile should display degrees if user has entered them')
            self.assertContains(response, '%s, %s, %d.' % \
                                (ba_degree.name, ba_degree.institution, ba_degree.year))
            self.assertContains(response, '%s, %s.' % \
                                (ma_degree.name, ma_degree.institution))
            self.assertContains(response, 'Biography',
                msg_prefix='profile should display bio when one has been added')
            self.assertContains(response, 'did some <strong>stuff</strong>',
                msg_prefix='bio text should be displayed with markdown formatting')
            self.assertContains(response, 'Positions',
                msg_prefix='profile should display positions when one has been added')
            self.assertContains(response, 'Director of Stuff',
                msg_prefix='position title should be displayed')
            self.assertContains(response, 'Grants',
                msg_prefix='profile should display grants when one has been added')
            self.assertContains(response, gouda_grant.name)
            self.assertContains(response, gouda_grant.grantor)
            self.assertContains(response, gouda_grant.year)
            self.assertContains(response, gouda_grant.project_title)
            self.assertContains(response, queso_grant.grantor)
            self.assertContains(response, queso_grant.project_title)

            # internet suppressed
            self.faculty_esd.internet_suppressed = True
            self.faculty_esd.save()
            response = self.client.get(profile_url)
            # ESD data should not be displayed except for name
            self.assertContains(response, self.faculty_esd.directory_name,
                msg_prefix="profile page should display user's directory name")
            self.assertNotContains(response, self.faculty_esd.title,
                msg_prefix='title from ESD should not be displayed (internet suppressed)')
            self.assertNotContains(response, self.faculty_esd.department_name,
                msg_prefix='department from ESD should not be displayed (internet suppressed')
            self.assertNotContains(response, self.faculty_esd.email,
                msg_prefix='email from ESD should not be displayed (internet suppressed')
            self.assertNotContains(response, self.faculty_esd.phone,
                msg_prefix='phone from ESD should not be displayed (internet suppressed')
            self.assertNotContains(response, self.faculty_esd.fax,
                msg_prefix='fax from ESD should not be displayed (internet suppressed')
            self.assertNotContains(response, self.faculty_esd.ppid,
                msg_prefix='PPID from ESD should not be displayed (internet suppressed')
            # directory suppressed
            self.faculty_esd.internet_suppressed = False
            self.faculty_esd.directory_suppressed = True
            self.faculty_esd.save()
            response = self.client.get(profile_url)
            # ESD data should not be displayed except for name
            self.assertContains(response, self.faculty_esd.directory_name,
                msg_prefix="profile page should display user's directory name")
            self.assertNotContains(response, self.faculty_esd.title,
                msg_prefix='title from ESD should not be displayed (directory suppressed)')
            self.assertNotContains(response, self.faculty_esd.department_name,
                msg_prefix='department from ESD should not be displayed (directory suppressed')
            self.assertNotContains(response, self.faculty_esd.email,
                msg_prefix='email from ESD should not be displayed (directory suppressed')
            self.assertNotContains(response, self.faculty_esd.phone,
                msg_prefix='phone from ESD should not be displayed (directory suppressed')
            self.assertNotContains(response, self.faculty_esd.fax,
                msg_prefix='fax from ESD should not be displayed (directory suppressed')
            self.assertNotContains(response, self.faculty_esd.ppid,
                msg_prefix='PPID from ESD should not be displayed (directory suppressed')

            # suppressed, local override
            faculty_profile = self.faculty_user.get_profile()
            faculty_profile.show_suppressed = True
            faculty_profile.save()
            response = self.client.get(profile_url)
            # ESD data should be displayed except for name
            self.assertContains(response, self.faculty_esd.directory_name,
                msg_prefix="profile page should display user's directory name")
            self.assertContains(response, self.faculty_esd.title,
                msg_prefix='title from ESD should be displayed (directory suppressed, local override)')
            self.assertContains(response, self.faculty_esd.department_name,
                msg_prefix='department from ESD should be displayed (directory suppressed, local override')
            self.assertContains(response, self.faculty_esd.email,
                msg_prefix='email from ESD should be displayed (directory suppressed, local override')
            self.assertContains(response, self.faculty_esd.phone,
                msg_prefix='phone from ESD should be displayed (directory suppressed, local override')
            self.assertContains(response, self.faculty_esd.fax,
                msg_prefix='fax from ESD should be displayed (directory suppressed, local override')
            self.assertContains(response, self.faculty_esd.ppid,
                msg_prefix='PPID from ESD should be displayed (directory suppressed, local override')
            
            # patch profile to supply mocks for recent & unpublished articles
            with patch.object(self.faculty_user, 'get_profile') as mock_getprofile:
                mock_getprofile.return_value.recent_articles.return_value = result
                # not logged in as user yet - unpub should not be called
                mock_getprofile.return_value.unpublished_articles.return_value = unpub_result
                response = self.client.get(profile_url)
                mock_getprofile.return_value.recent_articles.assert_called_once()
                mock_getprofile.return_value.unpublished_articles.assert_not_called()
            
                self.assertContains(response, result[0]['title'],
                    msg_prefix='profile page should display article title')
                self.assertContains(response, result[0]['created'])
                self.assertContains(response, result[0]['last_modified'])
                self.assertContains(response, result[1]['title'])
                self.assertContains(response, result[1]['created'])
                self.assertContains(response, result[1]['last_modified'])
                # first result has content datastream, should have pdf link
                self.assertContains(response,
                                    reverse('publication:pdf', kwargs={'pid': result[0]['pid']}),
                                    msg_prefix='profile should link to pdf for article')
                # first result coauthored with a non-emory author
                coauthor_name = result[0]['parsed_author'][1].partition(':')[2]
                self.assertContains(response, coauthor_name,
                                    msg_prefix='profile should include non-emory coauthor')
                # second result does not have content datastream, should NOT have pdf link
                self.assertNotContains(response,
                                    reverse('publication:pdf', kwargs={'pid': result[1]['pid']}),
                                    msg_prefix='profile should link to pdf for article')

                # second result DOES have pmcid, should have pubmed central link
                self.assertNotContains(response,
                                    reverse('publication:pdf', kwargs={'pid': result[1]['pid']}),
                                    msg_prefix='profile should link to pdf for article')
                # second result coauthored with an emory author
                coauthor = result[1]['parsed_author'][1]
                coauthor_netid, colon, coauthor_name = coauthor.partition(':')
                self.assertContains(response, coauthor_name,
                                    msg_prefix='profile should include emory coauthor name')
                self.assertContains(response,
                                    reverse('accounts:profile', kwargs={'username': coauthor_netid}),
                                    msg_prefix='profile should link to emory coauthor')


                # normally, no upload link should be shown on profile page
                self.assertNotContains(response, reverse('publication:ingest'),
                    msg_prefix='profile page upload link should not display to anonymous user')

                # no research interests
                self.assertNotContains(response, 'Research interests',
                    msg_prefix='profile page should not display "Research interests" when none are set')


                
        # add research interests
        tags = ['myopia', 'arachnids', 'climatology']
        self.faculty_user.get_profile().research_interests.add(*tags)
        response = self.client.get(profile_url)
        self.assertContains(response, 'Research interests',
            msg_prefix='profile page should not display "Research interests" when tags are set')
        for tag in tags:
            self.assertContains(response, tag,
                msg_prefix='profile page should display research interest tags')

        # logged in, looking at own profile
        self.client.login(**USER_CREDENTIALS[self.faculty_username])
        with patch('openemory.accounts.views.get_object_or_404') as mockgetobj:
            mockgetobj.return_value = self.faculty_user
            with patch.object(self.faculty_user, 'get_profile') as mock_getprofile:
                mock_getprofile.return_value.recent_articles.return_value = result
                # not logged in as user yet - unpub should not be called
                mock_getprofile.return_value.unpublished_articles.return_value = unpub_result
                mock_getprofile.return_value.research_interests.all.return_value = []
                response = self.client.get(profile_url)
                mock_getprofile.return_value.recent_articles.assert_called_once()
                mock_getprofile.return_value.unpublished_articles.assert_called_once()

                self.assertContains(response, reverse('publication:ingest'),
                    msg_prefix='user looking at their own profile page should see upload link')
                # tag editing enabled
                self.assertTrue(response.context['editable_tags'])
                self.assert_('tagform' in response.context)
                # unpublished articles
                self.assertContains(response, 'You have unpublished articles',
                    msg_prefix='user with unpublished articles should see them on their own profile page')
                self.assertContains(response, unpub_result[0]['title'])
                self.assertContains(response, reverse('publication:edit',
                                                      kwargs={'pid': unpub_result[0]['pid']}),
                    msg_prefix='profile should include edit link for unpublished article')
                self.assertNotContains(response, reverse('publication:edit',
                                                      kwargs={'pid': result[0]['pid']}),
                    msg_prefix='profile should not include edit link for published article')
                
        # logged in, looking at someone else's profile
        profile_url = reverse('accounts:profile',
                kwargs={'username': self.other_faculty_username})
        response = self.client.get(profile_url)
        self.assertNotContains(response, reverse('publication:ingest'),
            msg_prefix='logged-in user looking at another profile page should not see upload link')
        # tag editing not enabled
        self.assert_('editable_tags' not in response.context)
        self.assert_('tagform' not in response.context)
                
        # personal bookmarks
        bk, created = Bookmark.objects.get_or_create(user=self.faculty_user, pid=result[0]['pid'])
        super_tags = ['new', 'to read']
        bk.tags.set(*super_tags)
        response = self.client.get(profile_url)
        for tag in super_tags:
            self.assertContains(response, tag,
                 msg_prefix='user sees their private article tags in any article list view')
        

    @patch('openemory.util.sunburnt.SolrInterface', mocksolr)
    def test_profile_rdf(self):
        # mock solr result 
        result =  [
            {'title': 'article one', 'created': 'today',
             'last_modified': 'today', 'pid': self.article.pid},
        ]
        self.mocksolr.query.execute.return_value = result

        profile_url = reverse('accounts:profile',
                kwargs={'username': self.faculty_username})
        profile_uri = URIRef('http://testserver' + profile_url)
        response = self.client.get(profile_url, HTTP_ACCEPT='application/rdf+xml')
        self.assertEqual('application/rdf+xml', response['Content-Type'])

        location_url = reverse('accounts:profile-data',
                kwargs={'username': self.faculty_username})
        location_uri = 'http://testserver' + location_url
        self.assertEqual(location_uri, response['Content-Location'])
        self.assertTrue('Accept' in response['Vary'])

        # check that content parses with rdflib, check a few triples
        rdf = parse_rdf(response.content, profile_url)
        self.assert_(isinstance(rdf, RdfGraph))
        topics = list(rdf.objects(profile_uri, FOAF.primaryTopic))
        self.assertTrue(topics, 'page should have a foaf:primaryTopic')
        author_node = topics[0]
        self.assert_( (author_node, RDF.type, FOAF.Person)
                      in rdf,
                      'author should be set as a foaf:Person in profile rdf')
        self.assertEqual(URIRef('http://testserver' + profile_url),
                         rdf.value(subject=author_node, predicate=FOAF.publications),
                      'author profile url should be set as a foaf:publications in profile rdf')
        # test article rdf included, related
        article_nodes = list(rdf.subjects(DC.identifier, Literal(self.article.pid)))
        self.assertEqual(len(article_nodes), 1, 'one article should have reposited pid')
        article_node = article_nodes[0]
        self.assert_((author_node, FRBR.creatorOf, article_node)
                     in rdf,
                     'author should be set as a frbr:creatorOf article in profile rdf')
        self.assert_((author_node, FOAF.made, article_node)
                     in rdf,
                     'author should be set as a foaf:made article in profile rdf')

        # article metadata
        for triple in self.article.as_rdf(node=article_node):
            self.assert_(triple in rdf,
                         'article rdf should be included in profile rdf graph')

        # directory data for non-suppressed user
        self.assert_((author_node, FOAF.name, Literal(self.faculty_esd.directory_name))
                     in rdf, 'author full name should be present')
        mbox_sha1sum = hashlib.sha1(self.faculty_esd.email).hexdigest()
        self.assert_((author_node, FOAF.mbox_sha1sum, Literal(mbox_sha1sum))
                     in rdf, 'author email hash should be present')
        self.assert_((author_node, FOAF.phone, URIRef('tel:' + self.faculty_esd.phone))
                     in rdf, 'author phone number should be present')

        # directory data internet-suppressed
        self.faculty_esd.internet_suppressed = True
        self.faculty_esd.save()
        response = self.client.get(profile_url, HTTP_ACCEPT='application/rdf+xml')
        rdf = parse_rdf(response.content, profile_url)
        author_node = list(rdf.objects(profile_uri, FOAF.primaryTopic))[0]

        self.assert_((author_node, FOAF.name, Literal(self.faculty_esd.directory_name))
                     in rdf, 'author full name should be present (internet suppressed)')
        mbox_sha1sum = hashlib.sha1(self.faculty_esd.email).hexdigest()
        self.assert_((author_node, FOAF.mbox_sha1sum, Literal(mbox_sha1sum))
                     not in rdf, 'author email hash should not be present (internet suppressed)')
        self.assert_((author_node, FOAF.phone, URIRef('tel:' + self.faculty_esd.phone))
                     not in rdf, 'author phone number should not be present (internet suppressed)')
        
        # directory data directory-suppressed
        self.faculty_esd.internet_suppressed = False
        self.faculty_esd.directory_suppressed = True
        self.faculty_esd.save()
        response = self.client.get(profile_url, HTTP_ACCEPT='application/rdf+xml')
        rdf = parse_rdf(response.content, profile_url)
        author_node = list(rdf.objects(profile_uri, FOAF.primaryTopic))[0]

        self.assert_((author_node, FOAF.name, Literal(self.faculty_esd.directory_name))
                     in rdf, 'author full name should be present (directory suppressed)')
        mbox_sha1sum = hashlib.sha1(self.faculty_esd.email).hexdigest()
        self.assert_((author_node, FOAF.mbox_sha1sum, Literal(mbox_sha1sum))
                     not in rdf, 'author email hash should not be present (directory suppressed)')
        self.assert_((author_node, FOAF.phone, URIRef('tel:' + self.faculty_esd.phone))
                     not in rdf, 'author phone number should not be present (directory suppressed)')

        # suppressed, local override
        faculty_profile = self.faculty_user.get_profile()
        faculty_profile.show_suppressed = True
        faculty_profile.save()
        response = self.client.get(profile_url, HTTP_ACCEPT='application/rdf+xml')
        rdf = parse_rdf(response.content, profile_url)
        author_node = list(rdf.objects(profile_uri, FOAF.primaryTopic))[0]

        self.assert_((author_node, FOAF.name, Literal(self.faculty_esd.directory_name))
                     in rdf, 'author full name should be present (directory suppressed, local override)')
        mbox_sha1sum = hashlib.sha1(self.faculty_esd.email).hexdigest()
        self.assert_((author_node, FOAF.mbox_sha1sum, Literal(mbox_sha1sum))
                     in rdf, 'author email hash should be present (directory suppressed, local override)')
        self.assert_((author_node, FOAF.phone, URIRef('tel:' + self.faculty_esd.phone))
                     in rdf, 'author phone number should be present (directory suppressed, local override)')


    # used for test_edit_profile and test_profile_photo
    profile_post_data = {
        'research_interests': 'esoteric stuff',
        # degrees, with formset management fields
        '_DEGREES-MAX_NUM_FORMS': '',
        '_DEGREES-INITIAL_FORMS': 0,
        '_DEGREES-TOTAL_FORMS': 3,
        '_DEGREES-0-name': 'BA',
        '_DEGREES-0-institution': 'Somewhere Univ',
        '_DEGREES-0-year': 1876,
        '_DEGREES-1-name': 'MA',
        '_DEGREES-1-institution': 'Elsewhere Institute',
        # (degree year is optional)
        # positions, with same
        '_POSITIONS-MAX_NUM_FORMS': '',
        '_POSITIONS-INITIAL_FORMS': 0,
        '_POSITIONS-TOTAL_FORMS': 3,
        '_POSITIONS-0-name': 'Big Cheese, Association of Curd Curators',
        '_POSITIONS-1-name': 'Hole Editor, Journal of Swiss Studies',
        # grants
        '_GRANTS-MAX_NUM_FORMS': '',
        '_GRANTS-INITIAL_FORMS': 0,
        '_GRANTS-TOTAL_FORMS': 3,
        '_GRANTS-0-name': 'Advanced sharpness research',
        '_GRANTS-0-grantor': 'Cheddar Institute',
        '_GRANTS-0-project_title': 'The effect of subject cheesiness on cheddar sharpness assessment',
        '_GRANTS-0-year': '1492',
        '_GRANTS-1-grantor': u'Soci\xb4t\xb4 Brie',
        '_GRANTS-1-project_title': 'A comprehensive analysis of yumminess',
        'biography': 'Went to school *somewhere*, studied something else **elsewhere**.',
    }

    @patch.object(EmoryLDAPBackend, 'authenticate')
    def test_edit_profile(self, mockauth):
        mockauth.return_value = None
        edit_profile_url = reverse('accounts:edit-profile',
                                   kwargs={'username': self.faculty_username})
        # not logged in should 401 
        response = self.client.get(edit_profile_url)
        expected, got = 401, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s but got %s for GET %s as AnonymousUser' % \
                         (expected, got, edit_profile_url))
        
        # logged in, looking at own profile
        self.client.login(**USER_CREDENTIALS[self.faculty_username])
        response = self.client.get(edit_profile_url)
        expected, got = 200, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s but got %s for GET %s as %s' % \
                         (expected, got, edit_profile_url, self.faculty_username))
        self.assert_(isinstance(response.context['form'], ProfileForm),
                     'profile edit form should be set in response context')
        # non-suppressed user should not see suppression-override option
        self.assertNotContains(response, 'show_suppressed',
            msg_prefix='user who is not ESD suppressed should not see override option')
        # modify ESD suppression options and check the form
        faculty_esd_data = self.faculty_user.get_profile().esd_data()
        faculty_esd_data.internet_suppressed = True
        faculty_esd_data.save()
        response = self.client.get(edit_profile_url)
        self.assertContains(response, 'show_suppressed',
            msg_prefix='user who is internet suppressed should see override option')
        faculty_esd_data.internet_suppressed = False
        faculty_esd_data.directory_suppressed = True
        faculty_esd_data.save()
        response = self.client.get(edit_profile_url)
        self.assertContains(response, 'show_suppressed',
            msg_prefix='user who is directory suppressed should see override option')

        response = self.client.post(edit_profile_url, self.profile_post_data)
        expected, got = 303, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s but got %s for POST %s as %s' % \
                         (expected, got, edit_profile_url, self.faculty_username))
        self.assertEqual('http://testserver' + reverse('accounts:profile',
                                                       kwargs={'username': self.faculty_username}),
                         response['Location'])
        # degrees added
        self.assertEqual(2, self.faculty_user.get_profile().degree_set.count())
        degree = self.faculty_user.get_profile().degree_set.all()[0]
        # check that the degree was correctly created
        self.assertEqual(degree.name, 'BA')
        self.assertEqual(degree.institution, 'Somewhere Univ')
        self.assertEqual(degree.year, 1876)

        # biography added
        faculty_profile = UserProfile.objects.get(user=self.faculty_user)
        self.assertEqual(self.profile_post_data['biography'],
                         faculty_profile.biography)

        # positions added
        self.assertEqual(2, self.faculty_user.get_profile().degree_set.count())
        position = self.faculty_user.get_profile().position_set.all()[0]
        self.assertTrue(position.name.startswith('Big Cheese'))

        # grants added
        self.assertEqual(2, self.faculty_user.get_profile().grant_set.count())
        grant = self.faculty_user.get_profile().grant_set.all()[0]
        self.assertEqual(grant.name, 'Advanced sharpness research')
        self.assertEqual(grant.grantor, 'Cheddar Institute')
        self.assertTrue(grant.project_title.startswith('The effect of subject cheesiness'))
        self.assertEqual(grant.year, 1492)

        # when editing again, existing degrees should be displayed
        response = self.client.get(edit_profile_url)
        self.assertContains(response, degree.name,
            msg_prefix='existing degree name should be displayed for editing')
        self.assertContains(response, degree.institution,
            msg_prefix='existing degree institution should be displayed for editing')
        # post without required degree field
        invalid_post_data = self.profile_post_data.copy()
        invalid_post_data['_DEGREES-0-name'] = ''
        response = self.client.post(edit_profile_url,invalid_post_data)
        self.assertContains(response, 'field is required',
            msg_prefix='error should display when a required degree field is empty')

        # existing positions displayed
        self.assertContains(response, position.name,
            msg_prefix='existing positions should be displayed for editing')

        # existing grants displayed
        self.assertContains(response, grant.name,
            msg_prefix='existing grant name should be displayed for editing')
        self.assertContains(response, grant.grantor,
            msg_prefix='existing grant grantor should be displayed for editing')
        self.assertContains(response, grant.project_title,
            msg_prefix='existing grant project title should be displayed for editing')
        self.assertContains(response, grant.year,
            msg_prefix='existing grant year should be displayed for editing')
        
        # attempt to edit another user's profile should fail
        other_profile_edit = reverse('accounts:edit-profile',
                                     kwargs={'username': 'mmouse'})
        response = self.client.get(other_profile_edit)
        expected, got = 403, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s but got %s for GET %s as %s' % \
                         (expected, got, other_profile_edit, self.faculty_username))

        # login as site admin
        self.client.login(**USER_CREDENTIALS['admin'])
        response = self.client.get(edit_profile_url)
        expected, got = 200, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s but got %s for GET %s as Site Admin' % \
                         (expected, got, edit_profile_url))

        # edit for an existing user with no profile should 404
        noprofile_edit_url = reverse('accounts:edit-profile',
                                     kwargs={'username': 'student'})
        response = self.client.get(noprofile_edit_url)
        expected, got = 404, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s but got %s for %s (user with no profile)' % \
                         (expected, got, noprofile_edit_url))
        
        # edit for an non-existent user should 404
        nouser_edit_url = reverse('accounts:edit-profile',
                                     kwargs={'username': 'nosuchuser'})
        response = self.client.get(nouser_edit_url)
        expected, got = 404, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s but got %s for %s (non-existent user)' % \
                         (expected, got, nouser_edit_url))

                        
    @patch.object(EmoryLDAPBackend, 'authenticate')
    def test_profile_photo(self, mockauth):
        # test display & edit profile photo
        mockauth.return_value = None
        profile_url = reverse('accounts:profile',
                                   kwargs={'username': self.faculty_username})
        edit_profile_url = reverse('accounts:edit-profile',
                                   kwargs={'username': self.faculty_username})
        self.client.login(**USER_CREDENTIALS[self.faculty_username])
        # no photo should display
        response = self.client.get(profile_url)
        self.assertNotContains(response, '<img class="profile"',
            msg_prefix='no photo should display on profile when user has not added one')

        # non-image file should error
        with open(pdf_filename) as pdf:
            post_data  = self.profile_post_data.copy()
            post_data['photo'] = pdf
            response = self.client.post(edit_profile_url, post_data)
            self.assertContains(response,
                                'either not an image or a corrupted image',
                 msg_prefix='error message is displayed when non-image is uploaded')
            
        # edit profile and add photo
        img_filename = os.path.join(settings.BASE_DIR, 'accounts',
                                    'fixtures', 'profile.gif')
        with open(img_filename) as img:
            post_data  = self.profile_post_data.copy()
            post_data['photo'] = img
            response = self.client.post(edit_profile_url, post_data)
            expected, got = 303, response.status_code
            self.assertEqual(expected, got,
                'edit with profile image; expected %s but returned %s for %s' \
                             % (expected, got, edit_profile_url))

            profile = self.faculty_user.get_profile()
            # photo should be non-empty
            self.assert_(profile.photo,
                         'profile photo should be set after successful upload')

        # photo should display
        response = self.client.get(profile_url)
        self.assertContains(response, '<img class="profile"',
            msg_prefix='photo should display on profile when user has added one')


        # user can remove photo via edit form
        post_data  = self.profile_post_data.copy()
        post_data['photo-clear'] = 'on' # remove uploaded photo
        response = self.client.post(edit_profile_url, post_data)
        expected, got = 303, response.status_code
        self.assertEqual(expected, got,
                'edit and remove profile image; expected %s but returned %s for %s' \
                             % (expected, got, edit_profile_url))
        # get a fresh copy of the profile to check
        profile = UserProfile.objects.get(user=self.faculty_user)
        # photo should be cleared
        self.assert_(not profile.photo,
                     'profile photo should be blank after cleared by user')
        
        # photo should not display
        response = self.client.get(profile_url)
        self.assertNotContains(response, '<img class="profile"',
            msg_prefix='photo should not display on profile when user has removet it')


                
    @patch.object(EmoryLDAPBackend, 'authenticate')
    def test_login(self, mockauth):
        mockauth.return_value = None

        login_url = reverse('accounts:login')
        # without next - wrong password should redirect to site index
        response = self.client.post(login_url,
                {'username': self.faculty_username, 'password': 'wrong'})
        expected, got = 303, response.status_code
        self.assertEqual(expected, got, 'Expected %s but got %s for failed login on %s' % \
                         (expected, got, login_url))
        self.assertEqual('http://testserver' + reverse('site-index'),
                         response['Location'],
                         'failed login with no next url should redirect to site index')
        # with next - wrong password should redirect to next
        response = self.client.post(login_url,
                {'username': self.faculty_username, 'password': 'wrong',
                 'next': reverse('publication:ingest')})
        expected, got = 303, response.status_code
        self.assertEqual(expected, got, 'Expected %s but got %s for failed login on %s' % \
                         (expected, got, login_url))
        self.assertEqual('http://testserver' + reverse('publication:ingest'),
                         response['Location'],
                         'failed login should redirect to next url when it is specified')

        # login with valid credentials but no next
        response = self.client.post(login_url, USER_CREDENTIALS[self.faculty_username])
        expected, got = 303, response.status_code
        self.assertEqual(expected, got, 'Expected %s but got %s for successful login on %s' % \
                         (expected, got, login_url))
        self.assertEqual('http://testserver' +
                         reverse('accounts:profile',
                                 kwargs={'username': self.faculty_username}),
                         response['Location'],
                         'successful login with no next url should redirect to user profile')


        # login with valid credentials and no next, user in Site Admin group
        response = self.client.post(login_url, USER_CREDENTIALS['admin'])
        expected, got = 303, response.status_code
        self.assertEqual(expected, got, 'Expected %s but got %s for successful login on %s' % \
                         (expected, got, login_url))
        self.assertEqual('http://testserver' + reverse('harvest:queue'),
                         response['Location'],
                         'successful admin login with no next url should redirect to harvest queue')

        # login with valid credentials and a next url specified
        opts = {'next': reverse('site-index')}
        opts.update(USER_CREDENTIALS[self.faculty_username])
        response = self.client.post(login_url, opts)
        expected, got = 302, response.status_code
        self.assertEqual(expected, got, 'Expected %s but got %s for successful login on %s' % \
                         (expected, got, login_url))
        self.assertEqual('http://testserver' + opts['next'],
                         response['Location'],
                         'successful login should redirect to next url when specified')


        # site-index login form should not specify 'next'
        self.client.logout()
        response = self.client.get(reverse('site-index'))
        self.assertNotContains(response, '<input type=hidden name=next',
            msg_prefix='login-form on site index page should not specify a next url')

    def test_tag_profile_GET(self):
        # add some tags to a user profile to fetch
        user = User.objects.get(username=self.faculty_username)
        tags = ['a', 'b', 'c', 'z']
        user.get_profile().research_interests.set(*tags)
        
        tag_profile_url = reverse('accounts:profile-tags',
                kwargs={'username': self.faculty_username})
        response = self.client.get(tag_profile_url)
        expected, got = 200, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s but got %s for GET on %s' % \
                         (expected, got, tag_profile_url))
        # inspect return response
        self.assertEqual('application/json', response['Content-Type'],
             'should return json on success')
        data = json.loads(response.content)
        self.assert_(data, "Response content successfully read as JSON")
        for tag in tags:
            self.assert_(tag in data)
            self.assertEqual(reverse('accounts:by-interest', kwargs={'tag': tag}),
                             data[tag])

        # check (currently) unsupported HTTP methods
        response = self.client.delete(tag_profile_url)
        expected, got = 405, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s (method not allowed) but got %s for DELETE on %s' % \
                         (expected, got, tag_profile_url))

        # bogus username - 404
        bogus_tag_profile_url = reverse('accounts:profile-tags',
                                  kwargs={'username': 'adumbledore'})
        response = self.client.get(bogus_tag_profile_url)
        expected, got = 404, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s but got %s for GET on %s (bogus username)' % \
                         (expected, got, bogus_tag_profile_url))


    def test_tag_profile_PUT(self):
        tag_profile_url = reverse('accounts:profile-tags',
                kwargs={'username': self.faculty_username})

        # attempt to set tags without being logged in
        response = self.client.put(tag_profile_url, data='one, two, three',
                                   content_type='text/plain')
        expected, got = 401, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s but got %s for PUT on %s as AnonymousUser' % \
                         (expected, got, tag_profile_url))

        # login as different user than the one being tagged
        self.client.login(**USER_CREDENTIALS['admin'])
        response = self.client.put(tag_profile_url, data='one, two, three',
                                   content_type='text/plain')
        expected, got = 403, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s but got %s for PUT on %s as different user' % \
                         (expected, got, tag_profile_url))
        
        # login as user being tagged
        self.client.login(**USER_CREDENTIALS[self.faculty_username])
        tags = ['one', '2', 'three four', 'five']
        response = self.client.put(tag_profile_url, data=', '.join(tags),
                                   content_type='text/plain')
        expected, got = 200, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s but got %s for PUT on %s as user' % \
                         (expected, got, tag_profile_url))
        # inspect return response
        self.assertEqual('application/json', response['Content-Type'],
             'should return json on success')
        data = json.loads(response.content)
        self.assert_(data, "Response content successfully read as JSON")
        for tag in tags:
            self.assert_(tag in data)

        # inspect user in db
        user = User.objects.get(username=self.faculty_username)
        for tag in tags:
            self.assertTrue(user.get_profile().research_interests.filter(name=tag).exists())

    def test_tag_profile_POST(self):
        tag_profile_url = reverse('accounts:profile-tags',
                kwargs={'username': self.faculty_username})

        # attempt to set tags without being logged in
        response = self.client.post(tag_profile_url, data='one, two, three',
                                   content_type='text/plain')
        expected, got = 401, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s but got %s for POST on %s as AnonymousUser' % \
                         (expected, got, tag_profile_url))

        # login as different user than the one being tagged
        self.client.login(**USER_CREDENTIALS['admin'])
        response = self.client.post(tag_profile_url, data='one, two, three',
                                   content_type='text/plain')
        expected, got = 403, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s but got %s for POST on %s as different user' % \
                         (expected, got, tag_profile_url))
        
        # login as user being tagged
        self.client.login(**USER_CREDENTIALS[self.faculty_username])
        # add initial tags to user
        initial_tags = ['one', '2']
        self.faculty_user.get_profile().research_interests.add(*initial_tags)
        new_tags = ['three four', 'five', '2']  # duplicate tag should be fine too
        response = self.client.post(tag_profile_url, data=', '.join(new_tags),
                                   content_type='text/plain')
        expected, got = 200, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s but got %s for POST on %s as user' % \
                         (expected, got, tag_profile_url))
        # inspect return response
        self.assertEqual('application/json', response['Content-Type'],
             'should return json on success')
        data = json.loads(response.content)
        self.assert_(data, "Response content successfully read as JSON")
        for tag in initial_tags:
            self.assert_(tag in data, 'initial tags should be set and returned on POST')
        for tag in new_tags:
            self.assert_(tag in data, 'new tags should be added and returned on POST')

        # inspect user in db
        user = User.objects.get(username=self.faculty_username)
        for tag in initial_tags:
            self.assertTrue(user.get_profile().research_interests.filter(name=tag).exists())
        for tag in new_tags:
            self.assertTrue(user.get_profile().research_interests.filter(name=tag).exists())

    @patch('openemory.accounts.models.solr_interface', mocksolr)
    def test_profiles_by_interest(self):
        mock_article = {'pid': 'article:1', 'title': 'mock article'}
        self.mocksolr.query.execute.return_value = [mock_article]
        
        # add tags
        oa = 'open-access'
        oa_scholar, created = User.objects.get_or_create(username='oascholar')
        self.faculty_user.get_profile().research_interests.add('open access', 'faculty habits')
        oa_scholar.get_profile().research_interests.add('open access', 'OA movement')

        prof_by_tag_url = reverse('accounts:by-interest', kwargs={'tag': oa})
        response = self.client.get(prof_by_tag_url)
        expected, got = 200, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s but got %s for %s' % \
                         (expected, got, prof_by_tag_url))
        # check response
        oa_tag = Tag.objects.get(slug=oa)
        self.assertEqual(oa_tag, response.context['interest'],
            'research interest tag should be passed to template context for display')
        self.assertContains(response, self.faculty_user.get_profile().get_full_name(),
            msg_prefix='response should display full name for users with specified interest')
        self.assertContains(response, oa_scholar.get_profile().get_full_name(),
            msg_prefix='response should display full name for users with specified interest')
        for tag in self.faculty_user.get_profile().research_interests.all():
            self.assertContains(response, tag.name,
                 msg_prefix='response should display other tags for users with specified interest')
            self.assertContains(response,
                 reverse('accounts:by-interest', kwargs={'tag': tag.slug}),
                 msg_prefix='response should link to other tags for users with specified interest')
        self.assertContains(response, mock_article['title'],
             msg_prefix='response should include recent article titles for matching users')

        # not logged in - no me too / you have this interest
        self.assertNotContains(response, 'one of your research interests',
            msg_prefix='anonymous user should not see indication they have this research interest')
        self.assertNotContains(response, 'add to my profile',
            msg_prefix='anonymous user should not see option to add this research interest to profile')

        # logged in, with this interest: should see indication 
        self.client.login(**USER_CREDENTIALS[self.faculty_username])
        response = self.client.get(prof_by_tag_url)
        self.assertContains(response, 'one of your research interests', 
            msg_prefix='logged in user with this interest should see indication')
        
        self.faculty_user.get_profile().research_interests.clear()
        response = self.client.get(prof_by_tag_url)
        self.assertContains(response, 'add to my profile', 
            msg_prefix='logged in user without this interest should have option to add to profile')

    def test_interests_autocomplete(self):
        # create some users with tags to search on
        testuser1, created = User.objects.get_or_create(username='testuser1')
        testuser1.get_profile().research_interests.add('Chemistry', 'Biology', 'Microbiology')
        testuser2, created = User.objects.get_or_create(username='testuser2')
        testuser2.get_profile().research_interests.add('Chemistry', 'Geology', 'Biology')
        testuser3, created = User.objects.get_or_create(username='testuser3')
        testuser3.get_profile().research_interests.add('Chemistry', 'Kinesiology')

        # bookmark tags should *not* count towards public tags
        bk1, new = Bookmark.objects.get_or_create(user=testuser1, pid='test:1')
        bk1.tags.set('Chemistry', 'to-read')

        interests_autocomplete_url = reverse('accounts:interests-autocomplete')
        response = self.client.get(interests_autocomplete_url, {'s': 'chem'})
        expected, got = 200, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s but got %s for %s' % \
                         (expected, got, interests_autocomplete_url))
        # inspect return response
        self.assertEqual('application/json', response['Content-Type'],
             'should return json on success')
        data = json.loads(response.content)
        self.assert_(data, "Response content successfully read as JSON")
        self.assertEqual('Chemistry', data[0]['value'],
            'response includes matching tag')
        self.assertEqual('Chemistry (3)', data[0]['label'],
            'display label includes correct term count')

        response = self.client.get(interests_autocomplete_url, {'s': 'BIO'})
        data = json.loads(response.content)
        self.assertEqual('Biology', data[0]['value'],
            'response includes matching tag (case-insensitive match)')
        self.assertEqual('Biology (2)', data[0]['label'],
            'response includes term count (most used first)')
        self.assertEqual('Microbiology', data[1]['value'],
            'response includes partially matching tag (internal match)')
        self.assertEqual('Microbiology (1)', data[1]['label'])

        # private bookmark tag should not be returned
        response = self.client.get(interests_autocomplete_url, {'s': 'read'})
        data = json.loads(response.content)
        self.assertEqual([], data)

    def test_degree_institutions_autocomplete(self):
        # create degrees to search on
        emory = 'Emory University'
        gatech = 'Georgia Tech'
        uga = 'University of Georgia'
        faculty_profile = self.faculty_user.get_profile()
        ba_degree, created = Degree.objects.get_or_create(name='BA',
                               institution=emory, holder=faculty_profile)
        ma_degree, created = Degree.objects.get_or_create(name='MA',
                               institution=emory, holder=faculty_profile)
        ms_degree, created = Degree.objects.get_or_create(name='MS',
                               institution=gatech, holder=faculty_profile)
        ba_degree, created = Degree.objects.get_or_create(name='BA',
                               institution=uga, holder=faculty_profile)

        degree_inst_autocomplete_url = reverse('accounts:degree-autocomplete',
                                               kwargs={'mode': 'institution'})
        response = self.client.get(degree_inst_autocomplete_url,
                                   {'term': 'emory'})
        expected, got = 200, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s but got %s for %s' % \
                         (expected, got, degree_inst_autocomplete_url))
        # inspect return response
        self.assertEqual('application/json', response['Content-Type'],
             'should return json on success')
        data = json.loads(response.content)
        self.assert_(data, "Response content successfully read as JSON")
        self.assertEqual(1, len(data),
            'response includes only one matching instutition')
        self.assertEqual(emory, data[0]['value'],
            'response includes expected instutition name')
        self.assertEqual(emory, data[0]['label'],
            'display label has  correct term count')
        # partial match
        response = self.client.get(degree_inst_autocomplete_url,
                                   {'term': 'univ'})
        data = json.loads(response.content)
        self.assertEqual(emory, data[0]['label'],
            'match with most matches is listed first (without count)')
        self.assertEqual(uga, data[1]['label'],
            'match with second most matches is listed second (without count)')

        # test degree name autocompletion
        degree_name_autocomplete_url = reverse('accounts:degree-autocomplete',
                                               kwargs={'mode': 'name'})
        response = self.client.get(degree_name_autocomplete_url,
                                   {'term': 'm'})
        expected, got = 200, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s but got %s for %s' % \
                         (expected, got, degree_name_autocomplete_url))
        # inspect return response
        self.assertEqual('application/json', response['Content-Type'],
             'should return json on success')
        data = json.loads(response.content)
        self.assert_(data, "Response content successfully read as JSON")
        self.assertEqual(2, len(data),
            'response includes two matching degree names')
        degree_names = [d['label'] for d in data]
        self.assert_('MA' in degree_names)
        self.assert_('MS' in degree_names)
        


    def test_tag_object_GET(self):
        # create a bookmark to get
        bk, created = Bookmark.objects.get_or_create(user=self.faculty_user, pid='pid:test1')
        mytags = ['nasty', 'brutish', 'short']
        bk.tags.set(*mytags)
        tags_url = reverse('accounts:tags', kwargs={'pid': bk.pid})

        # not logged in - forbidden
        response = self.client.get(tags_url)
        expected, got = 401, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s but got %s for GET on %s (not logged in)' % \
                         (expected, got, tags_url))

        # log in for subsequent tests
        self.client.login(**USER_CREDENTIALS[self.faculty_username])

        # untagged pid - 404
        untagged_url = reverse('accounts:tags', kwargs={'pid': 'pid:notags'})
        response = self.client.get(untagged_url)
        expected, got = 404, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s but got %s for GET on %s' % \
                         (expected, got, untagged_url))
        
        # logged in, get tagged pid
        response = self.client.get(tags_url)
        expected, got = 200, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s but got %s for GET on %s' % \
                         (expected, got, tags_url))
        # inspect return response
        self.assertEqual('application/json', response['Content-Type'],
                         'should return json on success')
        data = json.loads(response.content)
        self.assert_(isinstance(data, list), "Response content successfully read as JSON")
        for tag in mytags:
            self.assert_(tag in data)
        
        # check currently unsupported HTTP methods
        response = self.client.delete(tags_url)
        expected, got = 405, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s (method not allowed) but got %s for DELETE on %s' % \
                         (expected, got, tags_url))
        response = self.client.post(tags_url)
        expected, got = 405, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s (method not allowed) but got %s for POST on %s' % \
                         (expected, got, tags_url))


    @patch('openemory.accounts.views.Repository')
    def test_tag_object_PUT(self, mockrepo):
        # use mock repo to simulate an existing fedora object 
        mockrepo.return_value.get_object.return_value.exists = True
        
        testpid = 'pid:bk1'
        tags_url = reverse('accounts:tags', kwargs={'pid': testpid})
        mytags = ['pleasant', 'nice', 'long']
        
        # attempt to set tags without being logged in
        response = self.client.put(tags_url, data=', '.join(mytags),
                                   content_type='text/plain')
        expected, got = 401, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s but got %s for PUT on %s as AnonymousUser' % \
                         (expected, got, tags_url))
        
        # log in for subsequent tests
        self.client.login(**USER_CREDENTIALS[self.faculty_username])
        # create a new bookmark
        response = self.client.put(tags_url, data=', '.join(mytags),
                                   content_type='text/plain')
        expected, got = 201, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s but got %s for PUT on %s (logged in, new bookmark)' % \
                         (expected, got, tags_url))
        
        # inspect return response
        self.assertEqual('application/json', response['Content-Type'],
                         'should return json on success')
        data = json.loads(response.content)
        self.assert_(isinstance(data, list), "Response content successfully read as JSON")
        for tag in mytags:
            self.assert_(tag in data)

        # inspect bookmark in db
        self.assertTrue(Bookmark.objects.filter(user=self.faculty_user, pid=testpid).exists())
        bk = Bookmark.objects.get(user=self.faculty_user, pid=testpid)
        for tag in mytags:
            self.assertTrue(bk.tags.filter(name=tag).exists())

        # update same bookmark with a second put
        response = self.client.put(tags_url, data=', '.join(mytags[:2]),
                                   content_type='text/plain')
        expected, got = 200, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s but got %s for PUT on %s (logged in, existing bookmark)' % \
                         (expected, got, tags_url))
        data = json.loads(response.content)
        self.assert_(mytags[-1] not in data)
        # get fresh copy of the bookmark
        bk = Bookmark.objects.get(user=self.faculty_user, pid=testpid)
        self.assertFalse(bk.tags.filter(name=mytags[-1]).exists())
        
        # test bookmarking when the fedora object doesn't exist
        mockrepo.return_value.get_object.return_value.exists = False
        response = self.client.put(tags_url, data=', '.join(mytags),
                                   content_type='text/plain')
        expected, got = 404, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s but got %s for PUT on %s (non-existent fedora object)' % \
                         (expected, got, tags_url))

    def test_tag_autocomplete(self):
        # create some bookmarks with tags to search on
        bk1, new = Bookmark.objects.get_or_create(user=self.faculty_user, pid='test:1')
        bk1.tags.set('foo', 'bar', 'baz')
        bk2, new = Bookmark.objects.get_or_create(user=self.faculty_user, pid='test:2')
        bk2.tags.set('foo', 'bar')
        bk3, new = Bookmark.objects.get_or_create(user=self.faculty_user, pid='test:3')
        bk3.tags.set('foo')

        super_user = User.objects.get(username='super')
        bks1, new = Bookmark.objects.get_or_create(user=super_user, pid='test:1')
        bks1.tags.set('foo', 'bar')
        bks2, new = Bookmark.objects.get_or_create(user=super_user, pid='test:2')
        bks2.tags.set('foo')

        tag_autocomplete_url = reverse('accounts:tags-autocomplete')

        # not logged in - 401
        response = self.client.get(tag_autocomplete_url, {'s': 'foo'})
        expected, got = 401, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s but got %s for %s (not logged in)' % \
                         (expected, got, tag_autocomplete_url))

        self.client.login(**USER_CREDENTIALS[self.faculty_username])
        response = self.client.get(tag_autocomplete_url, {'s': 'foo'})
        expected, got = 200, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s but got %s for %s' % \
                         (expected, got, tag_autocomplete_url))
        data = json.loads(response.content)
        # check return response
        self.assertEqual('foo', data[0]['value'],
            'response includes matching tag')
        # faculty user has 3 foo tags
        self.assertEqual('foo (3)', data[0]['label'],
            'display label includes count for current user')
        
        # multiple tags - autocomplete the last one only
        response = self.client.get(tag_autocomplete_url, {'term': 'bar, baz, fo'})
        data = json.loads(response.content)
        # check return response
        self.assertEqual(1, len(data),
            'response should only include one matching tag')
        self.assertEqual('foo (3)', data[0]['label'],
            'response includes matching tag for last term')
        self.assertEqual('bar, baz, foo, ', data[0]['value'],
            'response value includes entire term list with completed tag')

        # login as different user - should get count for their own bookmarks only
        self.client.login(**USER_CREDENTIALS['super'])
        response = self.client.get(tag_autocomplete_url, {'s': 'foo'})
        data = json.loads(response.content)
        # super user has 2 foo tags
        self.assertEqual('foo (2)', data[0]['label'],
            'display label includes correct term count')



    def test_tags_in_sidebar(self):
        # create some bookmarks with tags to search on
        bk1, new = Bookmark.objects.get_or_create(user=self.faculty_user, pid='test:1')
        bk1.tags.set('full text', 'to read')
        bk2, new = Bookmark.objects.get_or_create(user=self.faculty_user, pid='test:2')
        bk2.tags.set('to read')

        profile = self.faculty_user.get_profile()
        profile.research_interests.set('ponies')
        profile.save()

        # can really test any page for this...
        profile_url = reverse('accounts:profile',
                kwargs={'username': self.faculty_username})

        # not logged in - no tags in sidebar
        response = self.client.get(profile_url)
        self.assertFalse(response.context['tags'],
            'no tags should be set in response context for unauthenticated user')
        self.assertNotContains(response, '<h2>Tags</h2>',
             msg_prefix='tags should not be displayed in sidebar for unauthenticated user')

        # log in to see tags
        self.client.login(**USER_CREDENTIALS[self.faculty_username])
        response = self.client.get(profile_url)
        # tags should be set in context, with count & sorted by count
        tags = response.context['tags']
        self.assertEqual(2, tags.count())
        self.assertEqual('to read', tags[0].name)
        self.assertEqual(2, tags[0].count)
        self.assertEqual('full text', tags[1].name)
        self.assertEqual(1, tags[1].count)
        self.assertContains(response, '<h2>Tags</h2>',
            msg_prefix='tags should not be displayed in sidebar for authenticated user')
        # test for tag-browse urls once they are added
        

    @patch('openemory.accounts.views.articles_by_tag')
    def test_tagged_items(self, mockart_by_tag):
        # create some bookmarks with tags to search on
        bk1, new = Bookmark.objects.get_or_create(user=self.faculty_user, pid='test:1')
        bk1.tags.set('full text', 'to read')
        bk2, new = Bookmark.objects.get_or_create(user=self.faculty_user, pid='test:2')
        bk2.tags.set('to read')

        tagged_item_url = reverse('accounts:tag', kwargs={'tag': 'to-read'})

        # not logged in - no access
        response = self.client.get(tagged_item_url)
        expected, got = 401, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s but got %s for %s (not logged in)' % \
                         (expected, got, tagged_item_url))
        
        # log in to see tagged items
        self.client.login(**USER_CREDENTIALS[self.faculty_username])
        mockart_by_tag.return_value = [
            {'title': 'test article 1', 'pid': 'test:1'},
            {'title': 'test article 2', 'pid': 'test:2'}
        ]
        response = self.client.get(tagged_item_url)
        # check mock solr response, response display
        mockart_by_tag.assert_called_with(self.faculty_user, bk2.tags.all()[0])
        self.assertContains(response, 'Tag: to read',
            msg_prefix='response is labeled by the requested tag')      
        self.assertContains(response, mockart_by_tag.return_value[0]['title'])
        self.assertContains(response, mockart_by_tag.return_value[1]['title'])
        self.assertContains(response, '2 articles',
            msg_prefix='response includes total number of articles')      
        
        # bogus tag - 404
        tagged_item_url = reverse('accounts:tag', kwargs={'tag': 'not-a-real-tag'})
        response = self.client.get(tagged_item_url)
        expected, got = 404, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s but got %s for %s (nonexistent tag)' % \
                         (expected, got, tagged_item_url))

    @patch('openemory.accounts.views.EmoryLDAPBackend')
    def test_user_names(self, mockldap):
        username_url = reverse('accounts:user-name',
                kwargs={'username': self.faculty_username})

        response = self.client.get(username_url,
                {'username': self.faculty_username})
        expected, got = 200, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s but got %s for %s' % \
                         (expected, got, username_url))
        expected, got = 'application/json', response['Content-Type']
        self.assertEqual(expected, got,
                         'Expected content-type %s but got %s for %s' % \
                         (expected, got, username_url))
        data = json.loads(response.content)
        self.assert_(data, "Response content successfully read as JSON")
        self.assertEqual(self.faculty_username, data['username'])
        self.assertEqual(self.faculty_user.last_name, data['last_name'])
        self.assertEqual(self.faculty_user.first_name, data['first_name'])
        # ldap should not be called when user is already in db
        mockldap.return_value.find_user.assert_not_called

        # unsupported http method
        response = self.client.delete(username_url)
        expected, got = 405, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s (method not allowed) but got %s for DELETE %s' % \
                         (expected, got, username_url))

        # post again with user not in db - should query ldap
        superuser = User.objects.get(username='super')
        mockldap.return_value.find_user.return_value = ('userdn', superuser)
        username_url = reverse('accounts:user-name', kwargs={'username': 'someotheruser'})
        response = self.client.get(username_url)
        mockldap.return_value.find_user.assert_called
        data = json.loads(response.content)
        self.assert_(data, "Response content successfully read as JSON")
        self.assertEqual(superuser.last_name, data['last_name'])
        self.assertEqual(superuser.first_name, data['first_name'])

        # not found in db or ldap - 404
        mockldap.return_value.find_user.return_value = ('userdn', None)
        response = self.client.get(username_url)
        expected, got = 404, response.status_code
        self.assertEqual(expected, got,
                         'Expected %s but got %s for %s (user not found in db or ldap)' % \
                         (expected, got, username_url))

    def test_list_departments(self):
        list_dept_url = reverse('accounts:list-departments')
        response = self.client.get(list_dept_url)
        # check listings based on esdpeople fixture
        self.assertContains(response, 'Candler School Of Theology', count=1,
            msg_prefix='division name with same department name should only appear once')
        self.assertContains(response, 'School Of Law', count=1,
            msg_prefix='division name with same department name should only appear once')
        self.assertContains(response, 'School Of Medicine', count=1,
            msg_prefix='division name should only appear once')
        self.assertNotContains(response, 'SOM:',
            msg_prefix='division prefix on department name should not be listed')
        self.assertContains(response, 'History',
            msg_prefix='department name should be listed')
        self.assertContains(response, 'Cardiology',
            msg_prefix='department name should be listed')
        
class ResarchersByInterestTestCase(TestCase):

    def test_researchers_by_interest(self):
        # no users, no tags
        self.assertEqual(0, researchers_by_interest('chemistry').count())

        # users, no tags
        u1 = User(username='foo')
        u1.save()
        u2 = User(username='bar')
        u2.save()
        u3 = User(username='baz')
        u3.save()
        
        self.assertEqual(0, researchers_by_interest('chemistry').count())

        # users with tags
        u1.get_profile().research_interests.add('chemistry', 'geology', 'biology')
        u2.get_profile().research_interests.add('chemistry', 'biology', 'microbiology')
        u3.get_profile().research_interests.add('chemistry', 'physiology')

        # check various combinations - all users, some, one, none
        chem = researchers_by_interest('chemistry')
        self.assertEqual(3, chem.count())
        for u in [u1, u2, u3]:
            self.assert_(u in chem)

        bio = researchers_by_interest('biology')
        self.assertEqual(2, bio.count())
        for u in [u1, u2]:
            self.assert_(u in bio)

        microbio = researchers_by_interest('microbiology')
        self.assertEqual(1, microbio.count())
        self.assert_(u2 in microbio)
        
        physio = researchers_by_interest('physiology')
        self.assertEqual(1, physio.count())
        self.assert_(u3 in physio)

        psych = researchers_by_interest('psychology')
        self.assertEqual(0, psych.count())

        # also allows searching by tag slug
        chem = researchers_by_interest(slug='chemistry')
        self.assertEqual(3, chem.count())
        
    
class UserProfileTest(TestCase):
    multi_db = True
    fixtures = ['users', 'esdpeople']

    mocksolr = Mock(sunburnt.SolrInterface)
    mocksolr.return_value = mocksolr
    # solr interface has a fluent interface where queries and filters
    # return another solr query object; simulate that as simply as possible
    mocksolr.query.return_value = mocksolr.query
    mocksolr.query.query.return_value = mocksolr.query
    mocksolr.query.filter.return_value = mocksolr.query
    mocksolr.query.paginate.return_value = mocksolr.query
    mocksolr.query.exclude.return_value = mocksolr.query
    mocksolr.query.sort_by.return_value = mocksolr.query
    mocksolr.query.field_limit.return_value = mocksolr.query

    def setUp(self):
        self.user = User.objects.get(username='student')
        self.mmouse = User.objects.get(username='mmouse')
        self.smcduck = User.objects.get(username='smcduck')
        
    @patch('openemory.accounts.models.solr_interface', mocksolr)
    def test_find_articles(self):
        # check important solr query args
        solrq = self.user.get_profile()._find_articles()
        self.mocksolr.query.assert_called_with(owner=self.user.username)
        qfilt = self.mocksolr.query.filter
        qfilt.assert_called_with(content_model=Article.ARTICLE_CONTENT_MODEL)

    @patch('openemory.accounts.models.solr_interface', mocksolr)
    def test_recent_articles(self):
        # check important solr query args
        testlimit = 4
        testresult = [{'pid': 'test:1234'},]
        self.mocksolr.query.execute.return_value = testresult
        recent = self.user.get_profile().recent_articles(limit=testlimit)
        self.assertEqual(recent, testresult)
        self.mocksolr.query.filter.assert_called_with(state='A')
        self.mocksolr.query.paginate.assert_called_with(rows=testlimit)
        self.mocksolr.query.execute.assert_called_once()

    @patch('openemory.accounts.models.solr_interface', mocksolr)
    def test_unpublished_articles(self):
        # check important solr query args
        unpub = self.user.get_profile().unpublished_articles()
        self.mocksolr.query.filter.assert_called_with(state='I')
        self.mocksolr.query.execute.assert_called_once()

    def test_esd_data(self):
        self.assertEqual(self.mmouse.get_profile().esd_data().ppid, 'P9418306')
        with self.assertRaises(EsdPerson.DoesNotExist):
            self.user.get_profile().esd_data()

    def test_has_profile_page(self):
        self.assertTrue(self.mmouse.get_profile().has_profile_page()) # esd data, is faculty
        self.assertFalse(self.smcduck.get_profile().has_profile_page()) # esd data, not faculty
        self.assertFalse(self.user.get_profile().has_profile_page()) # no esd data
        self.assertFalse(self.user.get_profile().nonfaculty_profile) # should be false by default

    def test_suppress_esd_data(self):
        # set both suppressed options to false - should be not suppressed
        mmouse_profile = self.mmouse.get_profile()
        esd_data = mmouse_profile.esd_data()
        esd_data.internet_suppressed = False
        esd_data.directory_suppressed = False
        esd_data.save()
        self.assertEqual(False, mmouse_profile.suppress_esd_data,
            'profile without ESD suppression should not be suppressed')
        # internet suppressed or directory suppressed
        esd_data = mmouse_profile.esd_data()
        esd_data.internet_suppressed = True
        esd_data.save()
        self.assertEqual(True, mmouse_profile.suppress_esd_data,
            'internet suppressed profile should be suppressed')
        esd_data.internet_suppressed = False
        esd_data.directory_suppressed = True
        self.assertEqual(True, mmouse_profile.suppress_esd_data,
            'directory suppressed profile should be suppressed')
        mmouse_profile.show_suppressed = True
        self.assertEqual(False, mmouse_profile.suppress_esd_data,
            'directory suppressed profile with local override should NOT be suppressed')


class TagsTemplateFilterTest(TestCase):
    fixtures =  ['users']

    def setUp(self):
        self.faculty_user = User.objects.get(username='faculty')
        testpid = 'foo:1'
        self.solr_return = {'pid': testpid}
        repo = Repository()
        self.obj = repo.get_object(pid=testpid)

    def test_anonymous(self):
        # anonymous - no error, no tags
        self.assertEqual([], tags_for_user(self.solr_return, AnonymousUser()))

    def test_no_bookmark(self):
        # should not error
        self.assertEqual([], tags_for_user(self.solr_return, self.faculty_user))

    def test_bookmark(self):
        # create a bookmark to query
        bk, created = Bookmark.objects.get_or_create(user=self.faculty_user,
                                                     pid=self.obj.pid)
        mytags = ['ay', 'bee', 'cee']
        bk.tags.set(*mytags)

        # query for tags by solr return
        tags = tags_for_user(self.solr_return, self.faculty_user)
        self.assertEqual(len(mytags), len(tags))
        self.assert_(isinstance(tags[0], Tag))
        tag_names = [t.name for t in tags]
        for tag in mytags:
            self.assert_(tag in tag_names)

        # query for tags by object - should be same
        obj_tags = tags_for_user(self.obj, self.faculty_user)
        self.assert_(all(t in obj_tags for t in tags))


    def test_no_pid(self):
        # passing in an object without a pid shouldn't error either
        self.assertEqual([], tags_for_user({}, self.faculty_user))
       

class ArticlesByTagTest(TestCase):

    # FIXME: mocksolr duplication ... how to make re-usable/sharable?
    mocksolr = MagicMock(sunburnt.SolrInterface)
    mocksolr.return_value = mocksolr
    # solr interface has a fluent interface where queries and filters
    # return another solr query object; simulate that as simply as possible
    mocksolr.query.return_value = mocksolr.query
    mocksolr.query.query.return_value = mocksolr.query
    mocksolr.query.filter.return_value = mocksolr.query
    mocksolr.query.paginate.return_value = mocksolr.query
    mocksolr.query.exclude.return_value = mocksolr.query
    mocksolr.query.sort_by.return_value = mocksolr.query
    mocksolr.query.field_limit.return_value = mocksolr.query

    def setUp(self):
        self.user, created = User.objects.get_or_create(username='testuser')
        self.testpids = ['test:1', 'test:2', 'test:3']
        tagval = 'test'
        for pid in self.testpids:
            bk, new = Bookmark.objects.get_or_create(user=self.user, pid=pid)
            bk.tags.set(tagval)
            
        self.tag = bk.tags.all()[0]

    def test_pids_by_tag(self):
        tagpids = pids_by_tag(self.user, self.tag)
        self.assertEqual(len(self.testpids), len(tagpids))
        for pid in self.testpids:
            self.assert_(pid in tagpids)

    @patch('openemory.accounts.models.solr_interface', mocksolr)
    def test_articles_by_tag(self):
        articles = articles_by_tag(self.user, self.tag)

        # inspect solr query options
        # Q should be called once for each pid
        q_call_args =self.mocksolr.Q.call_args_list  # list of arg, kwarg tuples
        for i in range(2):
            args, kwargs = q_call_args[i]
            self.assertEqual({'pid': self.testpids[i]}, kwargs)
        self.mocksolr.query.field_limit.assert_called_with(ARTICLE_VIEW_FIELDS)
        self.mocksolr.query.sort_by.assert_called_with('-last_modified')

        # no match should return empty list, not all articles
        t = Tag(name='not tagged')
        self.assertEqual([], articles_by_tag(self.user, t))


class FacultyOrLocalAdminBackendTest(TestCase):
    multi_db = True
    fixtures =  ['users', 'esdpeople']

    def setUp(self):
        self.backend = FacultyOrLocalAdminBackend()
        self.faculty_username = 'jolson'
        self.non_faculty_username = 'smcduck'

    @patch.object(EmoryLDAPBackend, 'authenticate')
    def test_authenticate_local(self, mockauth):
        mockauth.return_value = True
        # not in local db
        self.assertEqual(None, self.backend.authenticate('nobody', 'pwd'),
                         'authenticate should not be called for non-db user')
        self.assertEqual(0, mockauth.call_count)
        # student in local db
        self.assertEqual(None, self.backend.authenticate(USER_CREDENTIALS['student']['username'],
                                                         USER_CREDENTIALS['student']['password']),
                         'authenticate should not be called for student in local db')
        self.assertEqual(0, mockauth.call_count)

        # super-user in local db
        self.assertEqual(True, self.backend.authenticate(USER_CREDENTIALS['super']['username'],
                                                         USER_CREDENTIALS['super']['password']),
                         'authenticate should be called for superuser in local db')
        self.assertEqual(1, mockauth.call_count)
        
        # site-admin in local db
        self.assertEqual(True, self.backend.authenticate(USER_CREDENTIALS['admin']['username'],
                                                         USER_CREDENTIALS['admin']['password']),
                         'authenticate should be called for site admin in local db')
        self.assertEqual(2, mockauth.call_count)
                
    @patch.object(EmoryLDAPBackend, 'authenticate')        
    def test_authenticate_esd_faculty(self, mockauth):
        mockauth.return_value = True

        # non-faculty
        self.assertEqual(None, self.backend.authenticate(self.non_faculty_username, 'pwd'),
                         'authenticate should not be called for esd non-faculty person')
        self.assertEqual(0, mockauth.call_count)

        # faculty
        self.assertEqual(True, self.backend.authenticate(self.faculty_username, 'pwd'),
                         'authenticate should be called for esd faculty person')
        self.assertEqual(1, mockauth.call_count)

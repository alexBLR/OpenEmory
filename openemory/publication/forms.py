import logging
from django import forms
from django.core.exceptions import ValidationError
from django.contrib.auth.models import User
from django.utils.datastructures import SortedDict
# collections.OrderedDict not available until Python 2.7

from eulcommon.djangoextras.formfields import W3CDateWidget, DynamicChoiceField
from eulxml.forms import XmlObjectForm, SubformField
from eulxml.xmlmap.dc import DublinCore
from eulxml.xmlmap import mods
from eullocal.django.emory_ldap.backends import EmoryLDAPBackend

from openemory.publication.models import ArticleMods, \
     Keyword, AuthorName, AuthorNote, FundingGroup, JournalMods, \
     FinalVersion, ResearchField, marc_language_codelist, ResearchFields

logger = logging.getLogger(__name__)

class UploadForm(forms.Form):
    'Single-file upload form with assent to deposit checkbox.'
    assent = forms.BooleanField(label='Assent to deposit agreement',
        help_text='Check to indicate your assent to the repository policy.',
        error_messages={'required': 'You must indicate assent to upload an article'})
    pdf = forms.FileField(label='')

class BasicSearchForm(forms.Form):
    'single-input article text search form'
    keyword = forms.CharField()


class AffiliationTextInput(forms.TextInput):
    ''':class:`django.forms.TextInput` that renders read-only if its form's
    id field is set, editable otherwise.'''
    readonly_attrs = {
        'readonly': 'readonly',
        'class': 'readonly',
        'tabindex': '-1',
    }

    def render(self, name, value, attrs=None):
        super_render = super(AffiliationTextInput, self).render
        
        use_attrs = {} if self.editable() else self.readonly_attrs.copy()
        if attrs is not None:
            use_attrs.update(attrs)
        return super_render(name, value, use_attrs)

    def editable(self):
        '''Should this widget render as editable? Returns False if its
        form's id field (netid) is set, True otherwise.'''
        # relies on AuthorNameForm below setting this widget's form.
        return not self.form['id'].value()


## forms & subforms for editing article mods

class BaseXmlObjectForm(XmlObjectForm):
    # base xmlobjectform with CSS class declarations 
    error_css_class = 'error'
    required_css_class = 'required'

class ArticleModsTitleEditForm(BaseXmlObjectForm):
    form_label = 'Title Information'
    subtitle = forms.CharField(required=False)
    part_number = forms.CharField(required=False)
    part_name = forms.CharField(required=False)
    class Meta:
        model = mods.TitleInfo
        fields = ['title', 'subtitle', 'part_number', 'part_name']

class PartDetailNumberEditForm(BaseXmlObjectForm):
    # part-detail form for number only - no label, not required
    number = forms.CharField(label='', required=False)
    class Meta:
        model = mods.PartDetail
        fields = ['number']

class PartExtentEditForm(BaseXmlObjectForm):
    start = forms.CharField(required=False)
    end = forms.CharField(required=False)
    class Meta:
        model = mods.PartExtent
        fields = ['start', 'end']

class JournalEditForm(BaseXmlObjectForm):
    form_label = 'Journal Information'
    volume = SubformField(formclass=PartDetailNumberEditForm)
    number = SubformField(formclass=PartDetailNumberEditForm)
    pages = SubformField(formclass=PartExtentEditForm)
    class Meta:
        model = JournalMods
        fields = ['title', 'publisher', 'volume', 'number',
                  'pages']

class FundingGroupEditForm(BaseXmlObjectForm):
    form_label = 'Funding Group or Granting Agency'
    name = forms.CharField(label='', required=False) # suppress default label
    class Meta:
        model = FundingGroup
        fields = ['name']


class KeywordEditForm(BaseXmlObjectForm):
    topic = forms.CharField(label='', required=False) # suppress default label
    class Meta:
        model = Keyword
        fields = ['topic']

class AbstractEditForm(BaseXmlObjectForm):
    text = forms.CharField(label='',  # suppress default label
                           widget=forms.Textarea, required=False)
    class Meta:
        model = mods.Abstract
        fields = ['text']

class AuthorNotesEditForm(BaseXmlObjectForm):
    text = forms.CharField(label='',  # suppress default label
                           widget=forms.Textarea, required=False)
    class Meta:
        model = AuthorNote
        fields = ['text']

def validate_netid(value):
    '''Validate a netid field by checking if the specified netid is
    either a username in the local database or can be found in LDAP.'''
    if not User.objects.filter(username=value).exists():
        ldap = EmoryLDAPBackend()
        # log ldap requests; using repr so it is evident when ldap is a Mock
        logger.debug('Looking up user in LDAP by netid \'%s\' (using %r)' \
                     % (value, ldap))
        user_dn, user = ldap.find_user(value)
        if not user:
            raise ValidationError(u'%s is not a recognized Emory netid' % value)
    

class AuthorNameForm(BaseXmlObjectForm):
    id = forms.CharField(label='Emory netid', required=False,
                         help_text='Supply Emory netid for Emory co-authors',
                         validators=[validate_netid],
                         widget=forms.TextInput(attrs={'class':'netid-lookup'}))
    affiliation = forms.CharField(required=False, widget=AffiliationTextInput())
    class Meta:
        model = AuthorName
        fields = ['id', 'family_name', 'given_name', 'affiliation']
        widgets = {
            'affiliation': AffiliationTextInput,
        }

    def __init__(self, *args, **kwargs):
        super(AuthorNameForm, self).__init__(*args, **kwargs)
        # affiliation is optionally read-only depending on the value of the
        # id field. give that widget a reference to this form so that it can
        # make that determination.
        self.fields['affiliation'].widget.form = self
        
    def clean(self):
        # if id is set, affiliation should be Emory (no IDs for non-emory users)
        cleaned_data = self.cleaned_data
        id = cleaned_data.get('id')
        aff = cleaned_data.get('affiliation')
        if id and aff != 'Emory University':
            raise forms.ValidationError('ID is set but affiliation is not Emory University')
            
        return cleaned_data
    

class FinalVersionForm(BaseXmlObjectForm):
    form_label = 'Final Published Version'
    url = forms.URLField(label="URL", verify_exists=True, required=False)
    doi = forms.RegexField(label="DOI", regex='^doi:10\.\d+/.*', required=False,
                           help_text='Enter DOI (if any) in doi:10.##/## format')
    # NOTE: could potentially sanity-check DOIs by attempting to resolve them 
    # as URLs (e.g., http://dx.doi.org/<doi>) - leaving that out for now
    # for simplicity and because we don't know how reliable it would be
    
    class Meta:
        model = FinalVersion
        fields = ['url', 'doi']

class OtherURLSForm(BaseXmlObjectForm):
    form_label = 'URLs for other versions'
    url = forms.URLField(label="URL", verify_exists=True, required=False)
    class Meta:
        model = mods.Location
        fields = ['url']

_language_codes = None
def language_codes():
    '''Generate and return a dictionary of language names and codes
    from the MARC language Code List (as returned by
    :meth:`~openemory.publication.models.marc_language_codelist`).
    Key is language code, value is language name.
    '''
    global _language_codes
    if _language_codes is None:
        lang_codelist = marc_language_codelist()
        # preserve the order of the languages in the document
        _language_codes = SortedDict((lang.code, lang.name)
                                      for lang in lang_codelist.languages)
    return _language_codes

def language_choices():
    '''List of language code and name tuples, for use as a
    :class:`django.forms.ChoiceField` choice parameter'''
    codes = language_codes()
    # put english at the top of the list
    choices = [('eng', codes['eng'])]
    choices.extend((code, name) for code, name in codes.iteritems()
                   if code != 'eng')
    return choices
            


class ArticleModsEditForm(BaseXmlObjectForm):
    '''Form to edit the MODS descriptive metadata for an
    :class:`~openemory.publication.models.Article`.'''
    title_info = SubformField(formclass=ArticleModsTitleEditForm)
    authors = SubformField(formclass=AuthorNameForm)
    funders = SubformField(formclass=FundingGroupEditForm)
    journal = SubformField(formclass=JournalEditForm)
    final_version = SubformField(formclass=FinalVersionForm)
    abstract = SubformField(formclass=AbstractEditForm)
    keywords = SubformField(formclass=KeywordEditForm)
    author_notes = SubformField(formclass=AuthorNotesEditForm)
    locations = SubformField(formclass=OtherURLSForm,
                             label=OtherURLSForm.form_label)
    language_code = DynamicChoiceField(language_choices, label='Language',
                                      help_text='Language of the article')
    
    _research_fields = ResearchFields()
    subjects = forms.MultipleChoiceField(choices=_research_fields.as_field_choices(),
                                         widget=forms.SelectMultiple(attrs={'size': 20, 'width': 40}))

    # admin-only field
    reviewed = forms.BooleanField(help_text='Select to indicate this article has been ' +
                                  'reviewed; this will store a review event and remove ' +
                                  'the article from the review queue.',
                                  required=False) # does not have to be checked

    _embargo_choices = [('','no embargo'),
                        ('6 months','6 months'),
                        ('1 year', '1 year'),
                        ('18 months', '18 months'),
                        ('2 years', '2 years'),
                        ('3 years', '3 years')]
    embargo_duration = forms.ChoiceField(_embargo_choices,
        help_text='Restrict access to the PDF of your article for the selected time ' +
                  'after publication.', required=False)
    author_agreement = forms.FileField(required=False,
                                       help_text="Store a copy of the " +
                                       "article's author agreement here.")

    
    class Meta:
        model = ArticleMods
        fields = ['title_info','authors', 'version', 'publication_date',
                  'funders', 'journal', 'final_version', 'abstract', 'keywords',
                  'author_notes', 'locations', 'language_code']
        widgets = {
            'publication_date': W3CDateWidget,
        }

    def __init__(self, *args, **kwargs):
         make_optional = kwargs.pop('make_optional', False)
         super(ArticleModsEditForm, self).__init__(*args, **kwargs)
         # set default language to english
         lang_code = 'language_code'
         if lang_code not in self.initial or not self.initial[lang_code]:
             self.initial[lang_code] = 'eng'

         subj = 'subjects'
         if subj not in self.initial or not self.initial[subj]:
             # convert id stored in the MODS to the id format used in SKOS
             # - strip off leading 'id', add #
             self.initial[subj] = ['#%s' % s.id[2:] for s in self.instance.subjects]

         if  make_optional:
             for author_fs in self.formsets['authors']:
                 author_fs.fields['family_name'].required = False
                 author_fs.fields['given_name'].required = False

             self.fields['version'].required = False
             self.fields['publication_date'].required = False
             self.fields['language_code'].required = False
             self.fields['subjects'].required = False
             self.subforms['journal'].fields['title'].required = False
             self.subforms['journal'].fields['publisher'].required = False

         embargo = 'embargo_duration'
         if embargo not in self.initial or not self.initial[embargo]:
             # if embargo is set in metadata, use that as initial value
             if self.instance.embargo:
                 self.initial[embargo] = self.instance.embargo
             # otherwise, fall through to default choice (no embargo)
             

    def update_instance(self):
        # override default update to handle extra fields
        super(ArticleModsEditForm, self).update_instance()

        # NOTE: this is somewhat of a hack.  Because of the way we are
        # mapping the fields, XmlObjectform doesn't call the is_empty
        # check on origin_info so it gets left around when it
        # shouldn't, resulting in invalid MODS.
        # Eventually we should fix this in eulxml/xmlobjectform; may
        # require some revision to the current is_empty functionality,
        # and perhaps adding some kind of "prune" option to is_empty checks.
        if self.instance.origin_info and self.instance.origin_info.is_empty():
            del self.instance.origin_info
        
        # cleaned data only available when the form is actually valid
        if hasattr(self, 'cleaned_data'):
            # set or clear language text value based on language code
            lang_code = self.cleaned_data.get('language_code', None)
            if lang_code is None:
                # if language code is blank, clear out language text 
                self.instance.language = None
            else:
                # otherwise, set text value based on language code
                self.instance.language = language_codes()[lang_code]

            subjects = self.cleaned_data.get('subjects', [])
            # delete any current subject values
            del self.instance.subjects
            # add all the newly selected subjects, by both code and label
            for subj in subjects:
                # skos id is #nnnn; convert to valid xml id attribute: idnnnn
                subj_id = "id%s" % subj.strip('#')
                self.instance.subjects.append(ResearchField(id=subj_id,
                                                            topic=self._research_fields.get_label(subj)))

            embargo = self.cleaned_data.get('embargo_duration', None)
            # if not set or no embargo selected, clear out any previous value
            if embargo is None or not embargo:
                del self.instance.embargo
            else:
                self.instance.embargo = embargo




        # return object instance
        return self.instance
    

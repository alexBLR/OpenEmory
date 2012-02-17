from functools import wraps
from django.conf import settings
from eulfedora import testutil
from mock import _patch, _get_target, MagicMock, NonCallableMagicMock

TestRunner = testutil.FedoraTextTestSuiteRunner

try:
    import xmlrunner
    TestRunner = testutil.FedoraXmlTestSuiteRunner
    settings.TEST_OUTPUT_DIR='test-results'
except ImportError:
    pass

# http://www.caktusgroup.com/blog/2010/09/24/simplifying-the-testing-of-unmanaged-database-models-in-django/
class ManagedModelTestRunner(TestRunner):
    """
    Test runner that automatically makes all unmanaged models in your Django
    project managed for the duration of the test run, so that one doesn't need
    to execute the SQL manually to create them.
    """
    def setup_test_environment(self, *args, **kwargs):
        from django.db.models.loading import get_models
        self.unmanaged_models = [m for m in get_models()
                                 if not m._meta.managed]
        for m in self.unmanaged_models:
            m._meta.managed = True
        super(ManagedModelTestRunner, self).setup_test_environment(*args,
                                                                   **kwargs)

    def teardown_test_environment(self, *args, **kwargs):
        super(ManagedModelTestRunner, self).teardown_test_environment(*args,
                                                                      **kwargs)
        # reset unmanaged models
        for m in self.unmanaged_models:
            m._meta.managed = False


class _mock_solr_patch(_patch):
    MOCK_METHODS = [
        'exclude', 'facet_by', 'field_limit', 'filter', 'highlight',
        'sort_by', 'paginate', 'query',
    ]

    def __init__(self):
        mock_SolrInterface = MagicMock()

        super_init = super(_mock_solr_patch, self).__init__
        getter, attribute = _get_target('openemory.util.sunburnt.SolrInterface')
        super_init(getter, attribute, new=mock_SolrInterface,
                   spec=None, create=False, mocksignature=False,
                   spec_set=None, autospec=False, new_callable=None,
                   kwargs={})

        self.mock_solr = mock_SolrInterface.return_value
        self.configure_mock_solr(self.mock_solr)

    def configure_mock_solr(self, mock_solr):
        for methname in self.MOCK_METHODS:
            method = getattr(mock_solr, methname)
            method.return_value = mock_solr

    def decorate_callable(self, func):
        super_decorate = super(_mock_solr_patch, self).decorate_callable
        super_wrapped = super_decorate(func)

        @wraps(super_wrapped)
        def wrapped(test, *args, **kwargs):
            test.mock_solr = self.mock_solr
            return super_wrapped(test, *args, **kwargs)
        return wrapped
        
    def copy(self):
        return _mock_solr_patch()

    def __enter__(self):
        super_enter = super(_mock_solr_patch, self).__enter__
        mock_SolrInstance = super_enter()
        return mock_SolrInstance()


def mock_solr(test=None):
    patcher = _mock_solr_patch()
    if test:
        return patcher(test)
    else:
        return patcher


def mock_solr_results(result_items):
    mock_result = NonCallableMagicMock()
    mock_result.__iter__.return_value = result_items
    del mock_result.__getitem__
    mock_result.__len__.return_value = len(result_items)
    return mock_result
    

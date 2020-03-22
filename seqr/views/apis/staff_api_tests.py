import json

from django.test import TestCase
from django.urls.base import reverse

from seqr.views.apis.staff_api import elasticsearch_status, mme_details, seqr_stats, anvil_export, sample_metadata_export, get_projects_for_category, discovery_sheet, success_story, saved_variants_page
from seqr.views.utils.test_utils import _check_login

PROJECT_GUID = 'R0001_1kg'

PROJECT_CATEGRORY_NAME = 'Demo'

VARIANT_TAG = 'Review'

class VariantSearchAPITest(TestCase):
    fixtures = ['users', '1kg_project', 'reference_data', 'variant_searches']
    multi_db = True

    def test_elasticsearch_status(self):
        url = reverse(elasticsearch_status)
        _check_login(self, url)

        # response = self.client.get(url)
        # self.assertEqual(response.status_code, 200)
        # response_json = response.json()
        # self.assertListEqual(response_json.keys(), ['indices', 'diskStats', 'elasticsearchHost', 'errors'])

    def test_mme_details(self):
        url = reverse(mme_details)
        _check_login(self, url)

        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        response_json = response.json()
        self.assertListEqual(response_json.keys(), ['metrics', 'genesById', 'submissions'])

    def test_seqr_stats(self):
        url = reverse(seqr_stats)
        _check_login(self, url)

        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        response_json = response.json()
        self.assertListEqual(response_json.keys(), ['individualCount', 'familyCount', 'sampleCountByType'])

    def test_anvil_export(self):
        url = reverse(anvil_export, args=[PROJECT_GUID])
        _check_login(self, url)

        # response = self.client.get(url)
        # self.assertEqual(response.status_code, 200)
        # response_json = response.json()
        # self.assertListEqual(response_json.keys(), ['individualCount', 'familyCount', 'sampleCountByType'])

    def test_sample_metadata_export(self):
        url = reverse(sample_metadata_export, args=[PROJECT_GUID])
        _check_login(self, url)

        # response = self.client.get(url)
        # self.assertEqual(response.status_code, 200)
        # response_json = response.json()
        # self.assertListEqual(response_json.keys(), ['rows'])

    def test_get_projects_for_category(self):
        url = reverse(get_projects_for_category, args=[PROJECT_CATEGRORY_NAME])
        _check_login(self, url)

        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        response_json = response.json()
        self.assertListEqual(response_json.keys(), ['projectGuids'])

    def test_discovery_sheet(self):
        url = reverse(discovery_sheet, args=[PROJECT_GUID])
        _check_login(self, url)

        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        response_json = response.json()
        self.assertListEqual(response_json.keys(), ['rows', 'errors'])

    def test_success_story(self):
        url = reverse(success_story, args=['all'])
        _check_login(self, url)

        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        response_json = response.json()
        self.assertListEqual(response_json.keys(), ['rows'])

    def test_saved_variants_page(self):
        url = reverse(saved_variants_page, args=[PROJECT_CATEGRORY_NAME])
        _check_login(self, url)

        # response = self.client.get(url)
        # self.assertEqual(response.status_code, 200)
        # response_json = response.json()
        # self.assertListEqual(response_json.keys(), ['genesById', 'projectsByGuid', 'familiesByGuid', 'individualsByGuid', 'locusListsByGuid'])
